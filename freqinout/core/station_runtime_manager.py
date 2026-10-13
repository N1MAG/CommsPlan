from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from freqinout.core.logger import log
from freqinout.core.multi_radio_store import (
    MultiRadioStore,
    normalize_ptt_group,
    normalize_resource_group,
    normalize_rf_guard_mode,
    stricter_rf_guard_mode,
)
from freqinout.core.multi_rig_runtime_status import (
    STARTUP_FRESH_DEFAULT_READY,
    STARTUP_MIGRATED,
    MultiRigRuntimeStatus,
    build_multi_rig_runtime_status,
)
from freqinout.core.radio_status_poll_coordinator import RadioStatusPollCoordinator
from freqinout.core.receiver_control import (
    ReceiverControlClient,
    ReceiverIdentity,
    receiver_control_verification_matches,
    receiver_identity_from_profile,
)
from freqinout.core.software_status_service import SoftwareStatusService
from freqinout.core.varac_ingest import load_latest_varac_sync_status
from freqinout.radio_interface.js8_status import JS8ControlClient, VarACStatusClient
from freqinout.radio_interface.rigctl_client import RigControlClient, rig_control_client_from_settings


CONTROL_STATUS_KEYS: Dict[str, Optional[str]] = {
    "flrig": "FLRig",
    "rigctld": "RigCtlD",
    "js8call": "JS8Call_API",
    "manual": None,
}


def _legacy_control_via(control_backend: str) -> str:
    backend = str(control_backend or "manual").strip().lower()
    if backend == "flrig":
        return "FLRig"
    if backend == "js8call":
        return "JS8Call"
    if backend == "rigctld":
        return "RIGCTLD"
    return "Manual"


def _format_frequency_label(freq_hz: Optional[int]) -> str:
    if not isinstance(freq_hz, (int, float)) or int(freq_hz) <= 0:
        return ""
    return f"{float(freq_hz) / 1_000_000.0:.3f} MHz"


def _hz_to_band(freq_hz: Optional[float]) -> str:
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


def _normalize_group_list(value: object) -> List[str]:
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raw_items = []
    normalized = [normalize_resource_group(item) for item in raw_items if normalize_resource_group(item)]
    return sorted(set(normalized))


def _normalize_band_token(value: object) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def _parse_string_list(value: object) -> List[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    out = [_normalize_band_token(item) for item in parsed if _normalize_band_token(item)]
    return list(dict.fromkeys(out))


def _row_bool(value: object, default: bool) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default)


def _normalize_device_class(value: object) -> str:
    raw = str(value or "").strip().lower() or "tx_rx"
    return raw if raw in {"tx_rx", "observer", "gateway"} else "tx_rx"


def _device_endpoint_summary(profile: Mapping[str, Any]) -> str:
    if _normalize_device_class(profile.get("device_class", "tx_rx")) == "observer":
        application = str(profile.get("sdr_application", "") or "").strip()
        adapter = str(profile.get("sdr_adapter", "manual") or "manual").strip().lower()
        target = str(profile.get("sdr_target", "") or "").strip()
        host = str(profile.get("sdr_host", "") or "").strip()
        port = profile.get("sdr_port")
        label = application or "Observer SDR"
        if (
            adapter == "manual"
            or not _row_bool(profile.get("sdr_control_enabled", False), False)
            or not receiver_control_verification_matches(profile)
        ):
            return f"{label} / manual tuning"
        if host and port not in (None, ""):
            suffix = f" / {target}" if target else ""
            return f"{label} {host}:{int(port)}{suffix}"
        if host:
            return f"{label} {host}"
        return f"{label} / receiver unavailable"
    backend = str(profile.get("control_backend", "") or "").strip().lower()
    if backend == "rigctld":
        host = str(profile.get("rig_host", "") or "").strip() or "127.0.0.1"
        port = int(profile.get("rig_port", 4532) or 4532)
        return f"rigctld {host}:{port}"
    if backend == "js8call":
        host = str(profile.get("js8_host", "") or "").strip() or "127.0.0.1"
        port = int(profile.get("js8_port", 2442) or 2442)
        return f"JS8Call {host}:{port}"
    if backend == "manual":
        return "Manual / no control endpoint"
    flrig_host = str(profile.get("flrig_host", "") or "").strip() or "127.0.0.1"
    flrig_port = int(profile.get("flrig_port", 12345) or 12345)
    fldigi_host = str(profile.get("fldigi_host", "") or "").strip() or flrig_host
    fldigi_port = int(profile.get("fldigi_port", 7362) or 7362)
    return f"FLRig {flrig_host}:{flrig_port}; FLDigi {fldigi_host}:{fldigi_port}"


class DeviceSettingsProxy:
    def __init__(self, profile: Mapping[str, Any], fallback_settings: Optional[object] = None) -> None:
        self.profile = dict(profile)
        self._fallback = fallback_settings
        self._data = self._build_data(self.profile)

    @staticmethod
    def _build_data(profile: Mapping[str, Any]) -> Dict[str, Any]:
        backend = str(profile.get("control_backend", "manual") or "manual").strip().lower() or "manual"
        data: Dict[str, Any] = {
            "control_via": _legacy_control_via(backend),
            "device_class": _normalize_device_class(profile.get("device_class", "tx_rx")),
            "rig_host": str(profile.get("rig_host", "") or "").strip(),
            "rig_port": profile.get("rig_port", 4532) or 4532,
            "flrig_host": str(profile.get("flrig_host", "") or "127.0.0.1").strip() or "127.0.0.1",
            "flrig_port": profile.get("flrig_port", 12345) or 12345,
            "fldigi_host": str(profile.get("fldigi_host", "") or profile.get("flrig_host", "") or "127.0.0.1").strip()
            or "127.0.0.1",
            "fldigi_port": profile.get("fldigi_port", 7362) or 7362,
            "fldigi_log_path": str(profile.get("fldigi_log_path", "") or "").strip(),
            "fldigi_checkin_dir": str(profile.get("fldigi_checkin_dir", "") or "").strip(),
            "js8_host": str(profile.get("js8_host", "") or "127.0.0.1").strip() or "127.0.0.1",
            "js8_port": profile.get("js8_port", 2442) or 2442,
            "js8_offset_hz": int(profile.get("js8_offset_hz", 0) or 0),
            "js8_profile_path": str(profile.get("js8_profile_path", "") or "").strip(),
            "js8_directed_path": str(profile.get("js8_directed_path", "") or "").strip(),
            "js8_forms_path": str(profile.get("js8_forms_path", "") or "").strip(),
            "varac_path": str(profile.get("varac_install_path", "") or "").strip(),
            "varac_db_path": str(profile.get("varac_db_path", "") or "").strip(),
            "varac_ini_path": str(profile.get("varac_ini_path", "") or "").strip(),
            "varac_launch_cmd": str(profile.get("launch_cmd", "") or "").strip(),
            "sdr_host": str(profile.get("sdr_host", "") or "").strip(),
            "sdr_port": profile.get("sdr_port"),
            "sdr_application": str(profile.get("sdr_application", "") or "").strip(),
            "sdr_adapter": str(profile.get("sdr_adapter", "manual") or "manual").strip().lower(),
            "sdr_target": str(profile.get("sdr_target", "") or "").strip(),
            "sdr_control_enabled": _row_bool(profile.get("sdr_control_enabled", 0), False),
            "sdr_verification_state": str(
                profile.get("sdr_verification_state", "manual") or "manual"
            ).strip().lower(),
            "sdr_verification_json": str(profile.get("sdr_verification_json", "{}") or "{}"),
            "message_paths": {},
            "launch_control_enabled": bool(int(profile.get("launch_enabled", 0) or 0)),
        }
        flrig_path = str(profile.get("flrig_path", "") or "").strip()
        if flrig_path:
            data["path_flrig"] = flrig_path
        elif backend == "flrig":
            data["path_flrig"] = str(profile.get("launch_path", "") or "").strip()
        fldigi_path = str(profile.get("fldigi_path", "") or "").strip()
        if fldigi_path:
            data["path_fldigi"] = fldigi_path
        js8_install_path = str(profile.get("js8_install_path", "") or "").strip()
        if js8_install_path:
            data["path_js8call"] = js8_install_path
        elif backend == "js8call":
            data["path_js8call"] = str(profile.get("launch_path", "") or "").strip()
        spotter_path = str(profile.get("spotter_launch_path", "") or "").strip()
        if spotter_path:
            data["path_js8spotter"] = spotter_path
        commstat_path = str(profile.get("commstat_launch_path", "") or "").strip()
        if commstat_path:
            data["path_commstat"] = commstat_path
        incoming_path = str(profile.get("varac_incoming_path", "") or "").strip()
        if incoming_path:
            data["message_paths"] = {"varac": incoming_path}
        return data

    def get(self, key: str, default: Any = None) -> Any:
        if key == "message_paths":
            merged_paths: Dict[str, Any] = {}
            fallback_get = getattr(self._fallback, "get", None)
            if callable(fallback_get):
                try:
                    fallback_value = fallback_get(key, default)
                    if isinstance(fallback_value, dict):
                        merged_paths.update(fallback_value)
                except Exception:
                    pass
            local_value = self._data.get("message_paths", {})
            if isinstance(local_value, dict):
                merged_paths.update(local_value)
            return merged_paths or default
        if key in self._data:
            return self._data.get(key, default)
        fallback_get = getattr(self._fallback, "get", None)
        if callable(fallback_get):
            try:
                return fallback_get(key, default)
            except Exception:
                return default
        return default

    def all(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        fallback_all = getattr(self._fallback, "all", None)
        if callable(fallback_all):
            try:
                raw = fallback_all()
                if isinstance(raw, dict):
                    merged.update(raw)
            except Exception:
                pass
        merged.update(self._data)
        fallback_message_paths = merged.get("message_paths", {})
        local_message_paths = self._data.get("message_paths", {})
        if isinstance(fallback_message_paths, dict) and isinstance(local_message_paths, dict):
            combined_message_paths = dict(fallback_message_paths)
            combined_message_paths.update(local_message_paths)
            merged["message_paths"] = combined_message_paths
        return merged

    def update_js8_offset(self, offset_hz: int) -> None:
        """Refresh one operational default without rebuilding endpoint clients."""

        value = int(offset_hz)
        self.profile["js8_offset_hz"] = value
        self._data["js8_offset_hz"] = value


@dataclass
class DeviceRuntimeSnapshot:
    device_profile_id: int
    name: str
    device_class: str
    control_backend: str
    deployment_mode: str
    runtime_active: bool
    runtime_primary: bool
    scheduler_owner: bool
    endpoint_summary: str
    ptt_group: str
    assigned_operating_profile_id: Optional[int]
    assigned_operating_profile_name: str
    assignment_state: str
    scheduler_enabled: bool
    use_messages: bool
    use_map: bool
    use_background_ingest: bool
    use_launch_control: bool
    use_net_control_tabs: bool
    # None means the cache has no current operational evidence.  Cache-only
    # consumers must not turn absence of evidence into an endpoint failure.
    control_ready: Optional[bool]
    overall_state: str
    status_summary: str
    warning_text: str
    ptt_active: bool
    shared_ptt_blocked: bool
    shared_ptt_owner_device_id: Optional[int]
    shared_ptt_owner_name: str
    shared_ptt_status_text: str
    observer_follow_source_device_id: Optional[int]
    observer_follow_source_name: str
    observer_follow_summary: str
    varac_cluster_name: str
    varac_cluster_id: str
    varac_instance_number: Optional[int]
    varac_gateway_handler: bool
    varac_gateway_handler_name: str
    varac_cluster_summary: str
    current_frequency_hz: Optional[int]
    current_frequency_label: str
    current_band: str
    antenna_group: str
    frontend_group: str
    amplifier_group: str
    swap_role: str
    swap_summary: str
    service_states: Dict[str, Dict[str, object]]
    operational_state: str = ""
    operational_detail: str = ""
    operational_endpoint_label: str = ""


@dataclass(frozen=True)
class EndpointOperationalHealth:
    """UI projection of one scheduler-owned, cache-only endpoint summary."""

    state_code: str
    indicator_state: str
    control_ready: Optional[bool]
    label: str
    detail: str
    endpoint_label: str


def endpoint_operational_health(
    summary: Mapping[str, object] | None,
) -> EndpointOperationalHealth:
    """Classify cached scheduler evidence by operational impact.

    This function deliberately performs no endpoint, process, database, or
    settings I/O.  It is shared by the command bar and Station Overview so a
    radio cannot be green in one surface and yellow in another.
    """

    row = summary if isinstance(summary, Mapping) else {}
    state_code = str(row.get("state") or "verification_unavailable").strip().lower()
    label = str(row.get("label") or "Checking control status").strip()
    detail = str(row.get("detail") or "No current endpoint readback is available.").strip()
    endpoint_label = str(row.get("endpoint_label") or "").strip()

    if state_code == "on_schedule_verified":
        indicator_state, control_ready = "ok", True
    elif state_code == "js8_verification_unavailable":
        # RF control is verified; the missing JS8 offset is an advisory.
        indicator_state, control_ready = "ok", True
    elif state_code in {"control_stalled", "endpoint_unavailable", "receiver_unavailable"}:
        indicator_state, control_ready = "warn", False
    elif state_code == "readback_mismatch":
        # The endpoint is reachable, but operating state is wrong.
        indicator_state, control_ready = "warn", True
    else:
        # Applying, waiting, manual, and absent/stale evidence are neutral.
        indicator_state, control_ready = "idle", None

    return EndpointOperationalHealth(
        state_code=state_code,
        indicator_state=indicator_state,
        control_ready=control_ready,
        label=label,
        detail=detail,
        endpoint_label=endpoint_label,
    )


def apply_endpoint_operational_summary(
    snapshot: DeviceRuntimeSnapshot,
    summary: Mapping[str, object] | None,
) -> DeviceRuntimeSnapshot:
    """Overlay detached cache evidence onto a detached runtime snapshot."""

    if isinstance(summary, Mapping) and "observed_frequency_hz" in summary:
        # Detached scheduler readback, never the desired schedule frequency.
        value = summary.get("observed_frequency_hz")
        snapshot.current_frequency_hz = int(value) if value is not None else None
        snapshot.current_frequency_label = _format_frequency_label(snapshot.current_frequency_hz)
        snapshot.current_band = _hz_to_band(snapshot.current_frequency_hz)
        if value is not None and summary.get("observed_frequency_current") is False:
            snapshot.current_frequency_label += " · last confirmed"
    projection = endpoint_operational_health(summary)
    snapshot.operational_state = projection.state_code
    snapshot.operational_detail = projection.detail
    snapshot.operational_endpoint_label = projection.endpoint_label
    snapshot.control_ready = projection.control_ready
    snapshot.overall_state = projection.indicator_state
    snapshot.status_summary = projection.label
    snapshot.service_states["Scheduler"] = {
        "state": projection.indicator_state,
        "tooltip": projection.detail or projection.label,
        "control_state": projection.state_code,
        "label": projection.label,
        "endpoint": projection.endpoint_label,
    }
    return snapshot


@dataclass
class SharedPttLockSnapshot:
    device_profile_id: int
    ptt_group: str
    blocked: bool
    owner_device_profile_id: Optional[int]
    owner_name: str
    owner_backend: str
    owner_ptt_active: bool
    target_ptt_active: bool
    reason: str
    # These defaulted fields keep the legacy snapshot constructor compatible
    # while letting scheduler lanes report whether their cached interlock
    # evidence was safe to use.  A missing or stale cache is deliberately not
    # interpreted as an idle transmitter.
    evidence_known: bool = True
    evidence_stale: bool = False
    evidence_detail: str = ""


@dataclass
class RfConflictSnapshot:
    target_device_profile_id: int
    target_device_name: str
    target_band: str
    target_frequency_hz: Optional[int]
    peer_device_profile_id: Optional[int]
    peer_name: str
    peer_band: str
    peer_frequency_hz: Optional[int]
    peer_device_ids: List[int]
    peer_names: List[str]
    shared_antenna_groups: List[str]
    shared_amplifier_groups: List[str]
    shared_frontend_groups: List[str]
    shared_band_overlap_groups: List[str]
    shared_advanced_frequency_groups: List[str]
    advanced_frequency_window_hz: int
    frequency_delta_hz: Optional[int]
    guard_mode: str
    blocked: bool
    same_band: bool
    same_frequency: bool
    summary: str
    detail: str
    signature: str
    peer_status_unknown: bool = False
    peer_status_stale: bool = False
    peer_status_detail: str = ""


class DeviceRuntime:
    def __init__(
        self,
        profile: Mapping[str, Any],
        *,
        is_primary: bool,
        fallback_settings: Optional[object] = None,
        assignment: Optional[Mapping[str, Any]] = None,
        operating_profile: Optional[Mapping[str, Any]] = None,
        status_poll_coordinator: Optional[RadioStatusPollCoordinator] = None,
        receiver_client_factory: Optional[
            Callable[[Mapping[str, Any]], Optional[ReceiverControlClient]]
        ] = None,
    ) -> None:
        self.fallback_settings = fallback_settings
        self.profile: Dict[str, Any] = {}
        self.is_primary = False
        self.assignment: Dict[str, Any] = {}
        self.operating_profile: Dict[str, Any] = {}
        self.settings_proxy: DeviceSettingsProxy | None = None
        self.status_service: SoftwareStatusService | None = None
        self.rig_client: Optional[RigControlClient] = None
        self.js8_control_client: Optional[JS8ControlClient] = None
        self.varac_status_client: Optional[VarACStatusClient] = None
        self.receiver_client: Optional[ReceiverControlClient] = None
        self.receiver_identity: Optional[ReceiverIdentity] = None
        self._receiver_client_factory = receiver_client_factory
        self.status_poll_coordinator = status_poll_coordinator or RadioStatusPollCoordinator(
            ttl_seconds=0.8,
            retry_seconds=4.0,
            time_fn=time.monotonic,
        )
        self._config_signature: tuple[object, ...] = tuple()
        self._ptt_state_cache: bool = False
        self._ptt_state_ts: float = 0.0
        self._ptt_retry_ts: float = 0.0
        self._ptt_state_ttl_sec: float = 0.8
        self._ptt_retry_sec: float = 4.0
        self._freq_state_cache: Optional[int] = None
        self._freq_state_ts: float = 0.0
        self._freq_retry_ts: float = 0.0
        self._freq_state_ttl_sec: float = 0.8
        self._freq_retry_sec: float = 4.0
        self.update(
            profile,
            is_primary=is_primary,
            assignment=assignment,
            operating_profile=operating_profile,
        )

    @staticmethod
    def _signature_for(profile: Mapping[str, Any], *, is_primary: bool) -> tuple[object, ...]:
        profile_items = tuple(sorted((str(key), profile.get(key)) for key in profile.keys()))
        return (bool(is_primary), profile_items)

    def update(
        self,
        profile: Mapping[str, Any],
        *,
        is_primary: bool,
        assignment: Optional[Mapping[str, Any]] = None,
        operating_profile: Optional[Mapping[str, Any]] = None,
    ) -> None:
        signature = self._signature_for(profile, is_primary=is_primary)
        if signature == self._config_signature:
            self.profile = dict(profile)
            self.is_primary = bool(is_primary)
            self.assignment = dict(assignment or {})
            self.operating_profile = dict(operating_profile or {})
            return
        self.stop()
        self.profile = dict(profile)
        self.is_primary = bool(is_primary)
        self.assignment = dict(assignment or {})
        self.operating_profile = dict(operating_profile or {})
        self.settings_proxy = DeviceSettingsProxy(self.profile, self.fallback_settings)
        self.status_service = SoftwareStatusService(self.settings_proxy)
        backend = str(self.profile.get("control_backend", "") or "").strip().lower()
        device_class = _normalize_device_class(self.profile.get("device_class", "tx_rx"))
        if device_class == "observer":
            try:
                self.receiver_identity = receiver_identity_from_profile(self.profile)
            except Exception as exc:
                log.debug("DeviceRuntime: invalid persisted receiver identity for %s: %s", self.profile.get("name", ""), exc)
                self.receiver_identity = None
            if (
                self.receiver_identity is not None
                and _row_bool(self.profile.get("sdr_control_enabled", 0), False)
                and receiver_control_verification_matches(self.profile)
                and callable(self._receiver_client_factory)
            ):
                try:
                    self.receiver_client = self._receiver_client_factory(self.profile)
                except Exception as exc:
                    log.debug("DeviceRuntime: failed building receive-only client for %s: %s", self.profile.get("name", ""), exc)
                    self.receiver_client = None
        if device_class != "observer" and backend in {"flrig", "rigctld"}:
            try:
                self.rig_client = rig_control_client_from_settings(self.settings_proxy)
            except Exception as exc:
                log.debug("DeviceRuntime: failed building rig client for %s: %s", self.profile.get("name", ""), exc)
                self.rig_client = None
        js8_companion_configured = bool(
            self.profile.get("js8_instance_id") or self.profile.get("js8_install_path")
            or self.profile.get("js8_profile_path")
        )
        if device_class != "observer" and (self.is_primary or backend == "js8call" or js8_companion_configured):
            try:
                host = str(self.settings_proxy.get("js8_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
                port = int(self.settings_proxy.get("js8_port", 2442) or 2442)
                self.js8_control_client = JS8ControlClient(host=host, port=port, settings=self.settings_proxy)
            except Exception as exc:
                log.debug("DeviceRuntime: failed building JS8 client for %s: %s", self.profile.get("name", ""), exc)
                self.js8_control_client = None
        if device_class != "observer" and (self.is_primary or any(
            self.settings_proxy.get(key, "") for key in ("varac_path", "varac_db_path", "varac_ini_path", "varac_launch_cmd")
        )):
            try:
                self.varac_status_client = VarACStatusClient(settings=self.settings_proxy)
            except Exception as exc:
                log.debug("DeviceRuntime: failed building VarAC client for %s: %s", self.profile.get("name", ""), exc)
                self.varac_status_client = None
        self._config_signature = signature

    def stop(self) -> None:
        if self.receiver_client is not None:
            try:
                self.receiver_client.close()
            except Exception:
                pass

        if self.js8_control_client is not None:
            try:
                self.js8_control_client.stop()
            except Exception:
                pass
        self.rig_client = None
        self.js8_control_client = None
        self.varac_status_client = None
        self.receiver_client = None
        self.receiver_identity = None
        self._ptt_state_cache = False
        self._ptt_state_ts = 0.0
        self._ptt_retry_ts = 0.0
        self._freq_state_cache = None
        self._freq_state_ts = 0.0
        self._freq_retry_ts = 0.0
        try:
            self.status_poll_coordinator.invalidate(self._status_poll_key("ptt"))
            self.status_poll_coordinator.invalidate(self._status_poll_key("frequency"))
        except Exception:
            pass

    def update_js8_offset(self, offset_hz: int) -> None:
        """Refresh the cached per-radio default without reconnecting CAT/audio."""

        value = int(offset_hz)
        self.profile["js8_offset_hz"] = value
        if self.settings_proxy is not None:
            self.settings_proxy.update_js8_offset(value)
        # Keep the runtime's comparison baseline aligned with this deliberately
        # in-place update.  Otherwise the next store sync would see a changed
        # profile signature and unnecessarily rebuild the radio-control clients.
        self._config_signature = self._signature_for(
            self.profile,
            is_primary=bool(getattr(self, "is_primary", False)),
        )

    def _status_poll_key(self, kind: str) -> str:
        device_id = int(self.profile.get("id", 0) or 0)
        if device_id > 0:
            return f"device:{device_id}:{kind}"
        return f"device:{id(self)}:{kind}"

    def ptt_active(self, *, force: bool = False) -> bool:
        if _normalize_device_class(self.profile.get("device_class", "tx_rx")) == "observer":
            return False
        backend = str(self.profile.get("control_backend", "manual") or "manual").strip().lower() or "manual"
        if backend not in {"flrig", "rigctld"}:
            return False
        if self.rig_client is None or not hasattr(self.rig_client, "get_ptt"):
            return False
        snapshot = self.status_poll_coordinator.get_snapshot(
            self._status_poll_key("ptt"),
            lambda: {
                "ptt_active": bool(self.rig_client.get_ptt()),
                "ptt_known": True,
                "source": "runtime_ptt",
            },
            force=force,
        )
        self._ptt_state_cache = bool(snapshot.ptt_active) if snapshot.ptt_known else False
        self._ptt_state_ts = float(snapshot.generated_at or 0.0)
        self._ptt_retry_ts = float(snapshot.backoff_until or 0.0)
        self._ptt_retry_sec = float(self.status_poll_coordinator.retry_seconds)
        if snapshot.errors:
            log.debug("DeviceRuntime: get_ptt failed for %s: %s", self.profile.get("name", ""), snapshot.errors)
        return bool(self._ptt_state_cache)

    def current_frequency_hz(self, *, force: bool = False) -> Optional[int]:
        if _normalize_device_class(self.profile.get("device_class", "tx_rx")) == "observer":
            return None
        backend = str(self.profile.get("control_backend", "manual") or "manual").strip().lower() or "manual"
        if backend not in {"flrig", "rigctld"}:
            return None
        if self.rig_client is None or not hasattr(self.rig_client, "get_vfo_frequency"):
            return None
        snapshot = self.status_poll_coordinator.get_snapshot(
            self._status_poll_key("frequency"),
            lambda: {
                "frequency_hz": self.rig_client.get_vfo_frequency(),
                "source": "runtime_frequency",
            },
            force=force,
        )
        self._freq_state_cache = snapshot.frequency_hz
        self._freq_state_ts = float(snapshot.generated_at or 0.0)
        self._freq_retry_ts = float(snapshot.backoff_until or 0.0)
        self._freq_retry_sec = float(self.status_poll_coordinator.retry_seconds)
        if snapshot.errors:
            log.debug("DeviceRuntime: get_vfo_frequency failed for %s: %s", self.profile.get("name", ""), snapshot.errors)
        return self._freq_state_cache

    def operating_policy(self) -> Dict[str, object]:
        operating = self.operating_profile if isinstance(self.operating_profile, dict) else {}
        assignment = self.assignment if isinstance(self.assignment, dict) else {}
        scheduler_mode = str(operating.get("scheduler_mode", "full") or "full").strip().lower() or "full"
        if scheduler_mode not in {"full", "simple"}:
            scheduler_mode = "full"
        return {
            "operating_profile_name": str(operating.get("name", "") or "").strip(),
            "assignment_state": str(assignment.get("assignment_state", "") or "").strip().lower() or "unassigned",
            "scheduler_enabled": _row_bool(operating.get("scheduler_enabled", 1), True),
            "scheduler_mode": scheduler_mode,
            "use_messages": _row_bool(operating.get("use_messages", 1), True),
            "use_map": _row_bool(operating.get("use_map", 1), True),
            "use_background_ingest": _row_bool(operating.get("use_background_ingest", 1), True),
            "use_launch_control": _row_bool(operating.get("use_launch_control", 0), False),
            "use_net_control_tabs": _row_bool(operating.get("use_net_control_tabs", 1), True),
        }

    def snapshot(self, *, force: bool = False, cache_only: bool = False) -> DeviceRuntimeSnapshot:
        backend = str(self.profile.get("control_backend", "manual") or "manual").strip().lower() or "manual"
        device_class = _normalize_device_class(self.profile.get("device_class", "tx_rx"))
        service_states: Dict[str, Dict[str, object]] = {}
        varac_cluster_name = str(self.profile.get("varac_cluster_name", "") or "").strip()
        varac_cluster_id = str(self.profile.get("varac_cluster_public_id", "") or "").strip()
        varac_cluster_member_enabled = bool(int(self.profile.get("varac_cluster_member_enabled", 0) or 0)) and bool(varac_cluster_name)
        varac_instance_number = (
            int(self.profile.get("varac_instance_number", 0) or 0)
            if self.profile.get("varac_instance_number") not in (None, "")
            else None
        )
        varac_gateway_handler = bool(int(self.profile.get("varac_gateway_handler", 0) or 0))
        varac_gateway_handler_name = str(self.profile.get("varac_gateway_handler_name", "") or "").strip()
        varac_cluster_member_count = int(self.profile.get("varac_cluster_enabled_member_count", 0) or 0)
        varac_shared_db_path = str(self.profile.get("varac_shared_db_path", "") or "").strip()
        if device_class != "observer" and self.status_service is not None and not cache_only:
            kwargs: Dict[str, object] = {
                "force": force,
                "host_override": str(self.profile.get("js8_host", "") or "").strip() or None,
                "port_override": int(self.profile.get("js8_port", 2442) or 2442),
                "flrig_host_override": str(self.profile.get("flrig_host", "") or "").strip() or None,
                "flrig_port_override": int(self.profile.get("flrig_port", 12345) or 12345),
                "fldigi_host_override": str(self.profile.get("fldigi_host", "") or "").strip() or None,
                "fldigi_port_override": int(self.profile.get("fldigi_port", 7362) or 7362),
            }
            if backend == "rigctld":
                kwargs["rigctld_host_override"] = str(self.profile.get("rig_host", "") or "").strip() or None
                kwargs["rigctld_port_override"] = int(self.profile.get("rig_port", 4532) or 4532)
            service_states = self.status_service.status_snapshot(**kwargs)

        control_key = CONTROL_STATUS_KEYS.get(backend)
        control_info = dict(service_states.get(control_key, {})) if control_key else {}
        if device_class == "observer":
            control_enabled = _row_bool(self.profile.get("sdr_control_enabled", 0), False)
            if control_enabled and receiver_control_verification_matches(self.profile) and self.receiver_client is not None:
                observer_info = {
                    "state": "attention",
                    "tooltip": "Receiver control is configured but has no cached live verification; use manual tuning until tune/readback succeeds.",
                    "running": False,
                    "reachable": False,
                    "endpoint": _device_endpoint_summary(self.profile),
                    "control_state": "receiver_unavailable",
                }
            elif control_enabled:
                observer_info = {
                    "state": "attention",
                    "tooltip": "Receiver control is unavailable; tune this receiver manually.",
                    "running": False,
                    "reachable": False,
                    "endpoint": _device_endpoint_summary(self.profile),
                    "control_state": "receiver_unavailable",
                }
            else:
                observer_info = {
                    "state": "idle",
                    "tooltip": "Manual tuning. CommsPlan will show the scheduled receive frequency without controlling the receiver.",
                    "running": False,
                    "reachable": False,
                    "endpoint": _device_endpoint_summary(self.profile),
                    "control_state": "manual_tuning",
                }
            service_states["Observer"] = dict(observer_info)
            # Construction from persisted configuration is not a live probe.
            # Cached adapter readback must establish readiness elsewhere.
            control_ready = False
            overall_state = str(observer_info.get("state", "idle") or "idle").strip().lower() or "idle"
            status_summary = str(observer_info.get("tooltip", "") or "").strip() or "Observer SDR status unavailable"
        else:
            varac_install_path = str(self.profile.get("varac_install_path", "") or "").strip()
            varac_db_path = str(self.profile.get("varac_db_path", "") or "").strip()
            varac_ini_path = str(self.profile.get("varac_ini_path", "") or "").strip()
            varac_launch_cmd = str(self.profile.get("launch_cmd", "") or "").strip()
            varac_configured = any((varac_install_path, varac_db_path, varac_ini_path, varac_launch_cmd))
            running_varac = False
            if self.status_service is not None and varac_configured:
                try:
                    running_varac = bool(
                        self.status_service.cached_program_is_running("VarAC")
                        if cache_only and hasattr(self.status_service, "cached_program_is_running")
                        else self.status_service.program_is_running("VarAC")
                    )
                except Exception:
                    running_varac = False
            raw_varac_status = (
                self.varac_status_client.get_status()
                if self.varac_status_client is not None and varac_configured and not cache_only
                else {}
            )
            varac_reason = str(raw_varac_status.get("reason", "") or "").strip().lower()
            reason_map = {
                "waiting_for_frequency": "waiting for frequency to clear",
                "connecting": "connecting",
                "connected": "connected",
                "incoming": "incoming session",
                "file_wait": "waiting for file confirmation",
                "transfer": "transferring data",
                "broadcast_incomplete": "broadcast in progress",
            }
            if varac_cluster_member_enabled and not varac_configured:
                varac_info = {
                    "state": "warn",
                    "tooltip": "VarAC cluster member enabled, but device-local VarAC settings are incomplete.",
                    "running": False,
                    "busy": False,
                    "waiting_for_frequency": False,
                }
            elif varac_configured:
                if bool(raw_varac_status.get("busy")) or bool(raw_varac_status.get("waiting_for_frequency")):
                    reason_txt = reason_map.get(varac_reason, varac_reason.replace("_", " ").strip())
                    tooltip = "VarAC busy"
                    if reason_txt:
                        tooltip = f"VarAC busy: {reason_txt}"
                    varac_info = {
                        "state": "ok",
                        "tooltip": tooltip,
                        "running": True,
                        "busy": True,
                        "waiting_for_frequency": bool(raw_varac_status.get("waiting_for_frequency")),
                    }
                elif running_varac:
                    varac_info = {
                        "state": "ok",
                        "tooltip": "VarAC node running for this device.",
                        "running": True,
                        "busy": False,
                        "waiting_for_frequency": False,
                    }
                else:
                    varac_info = {
                        "state": "idle",
                        "tooltip": "VarAC configured for this device, but not running.",
                        "running": False,
                        "busy": False,
                        "waiting_for_frequency": False,
                    }
            else:
                varac_info = {
                    "state": "idle",
                    "tooltip": "VarAC not configured for this device.",
                    "running": False,
                    "busy": False,
                    "waiting_for_frequency": False,
                }
            service_states["VarAC"] = dict(varac_info)

            varac_cluster_summary = ""
            if varac_cluster_member_enabled:
                raw_cluster_sync = self.profile.get("varac_cluster_ingest_status", {})
                cluster_sync = dict(raw_cluster_sync) if isinstance(raw_cluster_sync, dict) else {}
                shared_db_exists = False
                if varac_shared_db_path:
                    try:
                        shared_db_exists = Path(varac_shared_db_path).expanduser().exists()
                    except Exception:
                        shared_db_exists = False
                cluster_role_text = ""
                if varac_gateway_handler:
                    cluster_role_text = "gateway handler"
                elif varac_gateway_handler_name:
                    cluster_role_text = f"gateway handled by {varac_gateway_handler_name}"
                elif varac_cluster_member_count > 1:
                    cluster_role_text = "gateway handler not selected"
                elif varac_cluster_member_count == 1:
                    cluster_role_text = "single-member cluster"
                cluster_base = f"VarAC cluster {varac_cluster_name}"
                if varac_cluster_id:
                    cluster_base += f" [{varac_cluster_id}]"
                if varac_instance_number is not None:
                    cluster_base += f" node #{varac_instance_number}"
                if cluster_role_text:
                    cluster_base += f"; {cluster_role_text}"
                varac_cluster_summary = cluster_base + "."

                if varac_shared_db_path:
                    if shared_db_exists:
                        cluster_state = "ok"
                        cluster_tooltip = f"{cluster_base}. Shared DB available at {varac_shared_db_path}."
                    else:
                        cluster_state = "warn"
                        cluster_tooltip = f"{cluster_base}. Shared DB missing at {varac_shared_db_path}."
                    cluster_endpoint = varac_shared_db_path
                else:
                    cluster_state = "warn"
                    cluster_tooltip = (
                        f"{cluster_base}. Shared DB path not configured."
                        if cluster_base
                        else "VarAC shared cluster DB path not configured."
                    )
                    cluster_endpoint = ""
                ingest_summary = ""
                finished_ts = float(cluster_sync.get("run_finished_ts", 0.0) or 0.0)
                rows_written = int(cluster_sync.get("rows_written", 0) or 0)
                success_flag = bool(int(cluster_sync.get("success", 0) or 0))
                if finished_ts > 0:
                    age_seconds = max(0.0, time.time() - finished_ts)
                    if age_seconds >= 3600:
                        age_label = f"{int(age_seconds // 3600)}h ago"
                    elif age_seconds >= 60:
                        age_label = f"{int(age_seconds // 60)}m ago"
                    else:
                        age_label = f"{int(age_seconds)}s ago"
                    refresh_sec = max(5, int(self.profile.get("varac_counters_refresh_sec", 30) or 30))
                    stale = age_seconds > max(120.0, float(refresh_sec * 4))
                    if success_flag:
                        ingest_summary = f" Last ingest OK {age_label} ({rows_written} rows written)."
                        if stale and cluster_state == "ok":
                            cluster_state = "warn"
                            cluster_tooltip += f" Last ingest is stale ({age_label})."
                        else:
                            cluster_tooltip += ingest_summary
                    else:
                        ingest_error = str(cluster_sync.get("error_text", "") or "").strip()
                        ingest_summary = f" Last ingest failed {age_label}."
                        if ingest_error:
                            ingest_summary += f" {ingest_error}"
                        cluster_state = "warn"
                        cluster_tooltip += ingest_summary
                elif cluster_state == "ok":
                    ingest_summary = " Awaiting first shared-cluster ingest."
                    cluster_tooltip += ingest_summary
                varac_cluster_summary = (cluster_base + "." + ingest_summary).strip()
                service_states["VarAC Cluster"] = {
                    "state": cluster_state,
                    "tooltip": cluster_tooltip,
                    "running": cluster_state == "ok",
                    "reachable": cluster_state == "ok",
                    "endpoint": cluster_endpoint,
                }

            control_state = str(control_info.get("state", "ok" if backend == "manual" else "idle") or "idle").strip().lower()
            if backend == "manual":
                control_ready = True
                overall_state = "ok"
                status_summary = "Manual control"
            elif cache_only and not control_info:
                control_ready = None
                overall_state = "idle"
                status_summary = "Checking control status"
            else:
                control_ready = control_state == "ok"
                overall_state = control_state if control_state in {"ok", "warn", "error"} else "idle"
                if self.is_primary and not control_ready and overall_state == "idle":
                    overall_state = "warn"
                status_summary = str(control_info.get("tooltip", "") or "").strip()
                if not status_summary:
                    status_summary = f"{backend.upper()} control reachable" if control_ready else f"{backend.upper()} control unavailable"
            cluster_state = str(service_states.get("VarAC Cluster", {}).get("state", "idle") or "idle").strip().lower()
            varac_state = str(service_states.get("VarAC", {}).get("state", "idle") or "idle").strip().lower()
            if varac_cluster_member_enabled and overall_state not in {"warn", "error"} and (
                cluster_state == "warn" or varac_state == "warn"
            ):
                overall_state = "warn"

        warnings: List[str] = []
        if device_class == "observer":
            warnings.append(
                "Observer / SDR devices are monitored and receive follow guidance only; "
                "they never own the primary compatibility shell in this slice."
            )
        if str(service_states.get("VarAC", {}).get("state", "idle") or "idle").strip().lower() == "warn":
            tooltip = str(service_states.get("VarAC", {}).get("tooltip", "") or "").strip()
            if tooltip:
                warnings.append(tooltip)
        if str(service_states.get("VarAC Cluster", {}).get("state", "idle") or "idle").strip().lower() == "warn":
            tooltip = str(service_states.get("VarAC Cluster", {}).get("tooltip", "") or "").strip()
            if tooltip:
                warnings.append(tooltip)
        warning_text = " ".join(dict.fromkeys([warning for warning in warnings if warning]))
        policy = self.operating_policy()
        ptt_group = "" if device_class == "observer" else normalize_ptt_group(self.profile.get("ptt_group", ""))
        ptt_active = bool(self._ptt_state_cache) if cache_only else self.ptt_active(force=force)
        current_frequency_hz = self._freq_state_cache if cache_only else self.current_frequency_hz(force=force)
        return DeviceRuntimeSnapshot(
            device_profile_id=int(self.profile.get("id", 0) or 0),
            name=str(self.profile.get("name", "") or "").strip(),
            device_class=device_class,
            control_backend=backend,
            deployment_mode=str(self.profile.get("deployment_mode", "full") or "full").strip().lower() or "full",
            runtime_active=bool(int(self.profile.get("runtime_active", 0) or 0)),
            runtime_primary=bool(self.is_primary),
            scheduler_owner=bool(self.is_primary and device_class != "observer"),
            endpoint_summary=_device_endpoint_summary(self.profile),
            ptt_group=ptt_group,
            assigned_operating_profile_id=(
                int(self.assignment.get("operating_profile_id", 0) or 0)
                if self.assignment.get("operating_profile_id") not in (None, "")
                else None
            ),
            assigned_operating_profile_name=str(self.operating_profile.get("name", "") or "").strip(),
            assignment_state=str(policy.get("assignment_state", "") or "").strip().lower(),
            scheduler_enabled=bool(policy.get("scheduler_enabled", True)),
            use_messages=bool(policy.get("use_messages", True)),
            use_map=bool(policy.get("use_map", True)),
            use_background_ingest=bool(policy.get("use_background_ingest", True)),
            use_launch_control=bool(policy.get("use_launch_control", False)),
            use_net_control_tabs=bool(policy.get("use_net_control_tabs", True)),
            control_ready=control_ready,
            overall_state=overall_state,
            status_summary=status_summary,
            warning_text=warning_text,
            ptt_active=ptt_active,
            shared_ptt_blocked=False,
            shared_ptt_owner_device_id=None,
            shared_ptt_owner_name="",
            shared_ptt_status_text="",
            observer_follow_source_device_id=None,
            observer_follow_source_name="",
            observer_follow_summary="",
            varac_cluster_name=varac_cluster_name,
            varac_cluster_id=varac_cluster_id,
            varac_instance_number=varac_instance_number,
            varac_gateway_handler=varac_gateway_handler,
            varac_gateway_handler_name=varac_gateway_handler_name,
            varac_cluster_summary=varac_cluster_summary if device_class != "observer" else "",
            current_frequency_hz=current_frequency_hz,
            current_frequency_label=_format_frequency_label(current_frequency_hz),
            current_band=_hz_to_band(current_frequency_hz),
            antenna_group=normalize_resource_group(self.profile.get("antenna_group", "")),
            frontend_group=normalize_resource_group(self.profile.get("frontend_group", "")),
            amplifier_group=normalize_resource_group(self.profile.get("amplifier_group", "")),
            swap_role="",
            swap_summary="",
            service_states=service_states,
        )


class StationRuntimeManager:
    def __init__(
        self,
        store: Optional[MultiRadioStore] = None,
        settings: Optional[object] = None,
        *,
        receiver_client_factory: Optional[
            Callable[[Mapping[str, Any]], Optional[ReceiverControlClient]]
        ] = None,
    ) -> None:
        self.store = store or MultiRadioStore()
        self.settings = settings
        self._runtimes: Dict[int, DeviceRuntime] = {}
        self._active_profile_ids: List[int] = []
        self._primary_device_id: Optional[int] = None
        self._rf_conflict_policies: List[Dict[str, Any]] = []
        self._sdr_follow_policies: List[Dict[str, Any]] = []
        self._varac_clusters: Dict[int, Dict[str, Any]] = {}
        self._varac_cluster_members_by_device: Dict[int, Dict[str, Any]] = {}
        self._varac_sync_by_source: Dict[str, Dict[str, Any]] = {}
        self._active_profile_swap: Optional[Dict[str, Any]] = None
        self._runtime_status: Optional[MultiRigRuntimeStatus] = None
        self._status_poll_coordinator = RadioStatusPollCoordinator(
            ttl_seconds=0.8,
            retry_seconds=4.0,
            time_fn=time.monotonic,
        )
        self._receiver_client_factory = receiver_client_factory
        self._hardware_lifecycle_snapshot: object | None = None

    def set_hardware_lifecycle_snapshot(self, snapshot: object) -> None:
        """Publish one immutable background hardware snapshot to runtime readers."""

        generation = int(getattr(snapshot, "generation", 0) or 0)
        current_generation = int(
            getattr(getattr(self, "_hardware_lifecycle_snapshot", None), "generation", 0) or 0
        )
        if generation >= current_generation:
            self._hardware_lifecycle_snapshot = snapshot

    def hardware_health_for_device(self, device_profile_id: int) -> object | None:
        snapshot = self._hardware_lifecycle_snapshot
        radios = getattr(snapshot, "radios", {}) if snapshot is not None else {}
        return radios.get(int(device_profile_id)) if isinstance(radios, Mapping) else None

    def invalidate_runtime_status(self) -> None:
        self._runtime_status = None

    def sync_with_store(
        self,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
        *,
        refresh_runtime_status: bool = False,
        include_varac_sync_status: bool = True,
    ) -> None:
        settings_reload = getattr(self.settings, "reload", None)
        if callable(settings_reload):
            try:
                settings_reload()
            except Exception:
                pass

        if runtime_status is None:
            if refresh_runtime_status or self._runtime_status is None:
                runtime_status = build_multi_rig_runtime_status(self.store)
            else:
                runtime_status = self._runtime_status
        self._runtime_status = runtime_status
        if runtime_status.startup_mode not in {STARTUP_FRESH_DEFAULT_READY, STARTUP_MIGRATED}:
            if self._runtimes:
                for runtime in list(self._runtimes.values()):
                    runtime.stop()
                self._runtimes.clear()
            self._active_profile_ids = []
            self._primary_device_id = None
            self._rf_conflict_policies = []
            self._sdr_follow_policies = []
            self._varac_clusters = {}
            self._varac_cluster_members_by_device = {}
            self._varac_sync_by_source = {}
            self._active_profile_swap = None
            return

        active_profiles = list(self.store.list_runtime_active_device_profiles())
        primary_profile = self.store.get_runtime_primary_device_profile()
        primary_id = int(primary_profile.get("id", 0) or 0) if isinstance(primary_profile, dict) else None
        assignments = {
            int(row.get("device_profile_id", 0) or 0): dict(row)
            for row in self.store.list_effective_assignments()
            if isinstance(row, dict)
        }
        operating_profiles = {
            int(row.get("id", 0) or 0): dict(row)
            for row in self.store.list_operating_profiles()
            if isinstance(row, dict)
        }
        try:
            self._varac_clusters = {
                int(row.get("id", 0) or 0): dict(row)
                for row in self.store.list_varac_clusters()
                if isinstance(row, dict)
            }
        except Exception as exc:
            log.debug("StationRuntimeManager: failed loading VarAC clusters: %s", exc)
            self._varac_clusters = {}
        try:
            self._varac_cluster_members_by_device = {
                int(row.get("device_profile_id", 0) or 0): dict(row)
                for row in self.store.list_varac_cluster_members()
                if isinstance(row, dict) and int(row.get("enabled", 1) or 0) == 1
            }
        except Exception as exc:
            log.debug("StationRuntimeManager: failed loading VarAC cluster members: %s", exc)
            self._varac_cluster_members_by_device = {}
        if include_varac_sync_status:
            try:
                self._varac_sync_by_source = {
                    str(key): dict(value)
                    for key, value in load_latest_varac_sync_status().items()
                    if isinstance(value, dict)
                }
            except Exception as exc:
                log.debug("StationRuntimeManager: failed loading VarAC ingest status: %s", exc)
                self._varac_sync_by_source = {}
        try:
            self._rf_conflict_policies = [
                dict(row)
                for row in self.store.list_station_coordination_policies("rf_conflict")
                if isinstance(row, dict)
            ]
        except Exception as exc:
            log.debug("StationRuntimeManager: failed loading RF conflict policies: %s", exc)
            self._rf_conflict_policies = []
        try:
            self._sdr_follow_policies = [
                dict(row)
                for row in self.store.list_station_coordination_policies("sdr_follow")
                if isinstance(row, dict)
            ]
        except Exception as exc:
            log.debug("StationRuntimeManager: failed loading SDR follow policies: %s", exc)
            self._sdr_follow_policies = []
        try:
            active_swap = self.store.get_active_profile_swap()
            self._active_profile_swap = dict(active_swap) if isinstance(active_swap, dict) else None
        except Exception as exc:
            log.debug("StationRuntimeManager: failed loading active profile swap: %s", exc)
            self._active_profile_swap = None

        active_ids = {int(profile.get("id", 0) or 0) for profile in active_profiles}
        for device_id in list(self._runtimes.keys()):
            if device_id not in active_ids:
                self._runtimes[device_id].stop()
                del self._runtimes[device_id]

        self._active_profile_ids = []
        self._primary_device_id = primary_id
        for profile in active_profiles:
            device_id = int(profile.get("id", 0) or 0)
            if device_id <= 0:
                continue
            self._active_profile_ids.append(device_id)
            assignment = assignments.get(device_id, {})
            operating = operating_profiles.get(int(assignment.get("operating_profile_id", 0) or 0), {})
            membership = dict(self._varac_cluster_members_by_device.get(device_id, {}))
            cluster = (
                dict(self._varac_clusters.get(int(membership.get("cluster_db_id", 0) or 0), {}))
                if membership
                else {}
            )
            profile_data = dict(profile)
            if membership:
                cluster_source_key = f"cluster:{int(membership.get('cluster_db_id', 0) or 0)}"
                profile_data.update(
                    {
                        "varac_cluster_member_enabled": int(membership.get("enabled", 1) or 0),
                        "varac_cluster_db_id": int(membership.get("cluster_db_id", 0) or 0),
                        "varac_cluster_name": str(membership.get("cluster_name", "") or "").strip(),
                        "varac_cluster_public_id": str(membership.get("cluster_public_id", "") or "").strip(),
                        "varac_shared_db_path": str(membership.get("shared_db_path", "") or "").strip(),
                        "varac_counters_refresh_sec": int(membership.get("counters_refresh_sec", 30) or 30),
                        "varac_instance_number": int(membership.get("instance_number", 0) or 0),
                        "varac_gateway_handler": 1 if bool(membership.get("is_gateway_handler")) else 0,
                        "varac_gateway_handler_name": str(cluster.get("gateway_handler_name", "") or "").strip(),
                        "varac_cluster_enabled_member_count": int(cluster.get("enabled_member_count", 0) or 0),
                        "varac_cluster_ingest_status": dict(self._varac_sync_by_source.get(cluster_source_key, {})),
                    }
                )
            runtime = self._runtimes.get(device_id)
            if runtime is None:
                runtime = DeviceRuntime(
                    profile_data,
                    is_primary=device_id == primary_id,
                    fallback_settings=self.settings,
                    assignment=assignment,
                    operating_profile=operating,
                    status_poll_coordinator=self._status_poll_coordinator,
                    receiver_client_factory=self._receiver_client_factory,
                )
                self._runtimes[device_id] = runtime
            else:
                runtime.update(
                    profile_data,
                    is_primary=device_id == primary_id,
                    assignment=assignment,
                    operating_profile=operating,
                )

    def stop(self) -> None:
        for runtime in list(self._runtimes.values()):
            try:
                runtime.stop()
            except Exception:
                continue
        self._runtimes.clear()
        self._active_profile_ids = []
        self._primary_device_id = None
        self._rf_conflict_policies = []
        self._sdr_follow_policies = []
        self._varac_clusters = {}
        self._varac_cluster_members_by_device = {}
        self._varac_sync_by_source = {}
        self._active_profile_swap = None

    def shared_ptt_lock_snapshot(
        self,
        *,
        for_device_id: Optional[int] = None,
        force: bool = False,
        status_by_device: Optional[Mapping[object, object]] = None,
    ) -> SharedPttLockSnapshot:
        target_id = int(for_device_id or 0) if for_device_id not in (None, "") else int(self._primary_device_id or 0)
        if target_id <= 0:
            return SharedPttLockSnapshot(
                device_profile_id=0,
                ptt_group="",
                blocked=False,
                owner_device_profile_id=None,
                owner_name="",
                owner_backend="",
                owner_ptt_active=False,
                target_ptt_active=False,
                reason="",
            )
        target_runtime = self._runtimes.get(int(target_id))
        if target_runtime is None:
            return SharedPttLockSnapshot(
                device_profile_id=int(target_id),
                ptt_group="",
                blocked=False,
                owner_device_profile_id=None,
                owner_name="",
                owner_backend="",
                owner_ptt_active=False,
                target_ptt_active=False,
                reason="",
            )
        ptt_group = normalize_ptt_group(target_runtime.profile.get("ptt_group", ""))
        target_evidence_known = True
        target_evidence_stale = False
        target_evidence_detail = ""
        if status_by_device is None:
            target_ptt_active = bool(target_runtime.ptt_active(force=force))
        else:
            target_ptt_active, target_evidence_known, target_evidence_stale, target_evidence_detail = (
                self._cached_ptt_evidence(status_by_device, int(target_id))
            )
        if not ptt_group:
            return SharedPttLockSnapshot(
                device_profile_id=int(target_id),
                ptt_group="",
                blocked=False,
                owner_device_profile_id=None,
                owner_name="",
                owner_backend="",
                owner_ptt_active=False,
                target_ptt_active=target_ptt_active,
                reason="",
                evidence_known=target_evidence_known,
                evidence_stale=target_evidence_stale,
                evidence_detail=target_evidence_detail,
            )

        owners: List[DeviceRuntime] = []
        unknown_members: List[Dict[str, object]] = []
        for runtime_id, runtime in self._runtimes.items():
            if int(runtime_id) == int(target_id):
                continue
            if normalize_ptt_group(runtime.profile.get("ptt_group", "")) != ptt_group:
                continue
            if status_by_device is None:
                peer_ptt_active = bool(runtime.ptt_active(force=force))
                peer_evidence_known = True
                peer_evidence_stale = False
                peer_evidence_detail = ""
            else:
                peer_ptt_active, peer_evidence_known, peer_evidence_stale, peer_evidence_detail = (
                    self._cached_ptt_evidence(status_by_device, int(runtime_id))
                )
            if not peer_evidence_known:
                unknown_members.append(
                    {
                        "id": int(runtime_id),
                        "name": str(runtime.profile.get("name", "") or "").strip() or f"Device {runtime_id}",
                        "stale": bool(peer_evidence_stale),
                        "detail": peer_evidence_detail,
                    }
                )
            elif peer_ptt_active:
                owners.append(runtime)
        owners.sort(
            key=lambda runtime: (
                0 if runtime.is_primary else 1,
                str(runtime.profile.get("name", "") or "").lower(),
                int(runtime.profile.get("id", 0) or 0),
            )
        )
        owner = owners[0] if owners else None
        owner_profile = owner.profile if owner is not None else {}
        owner_name = str(owner_profile.get("name", "") or "").strip()
        owner_backend = str(owner_profile.get("control_backend", "") or "").strip().lower()
        evidence_known = bool(target_evidence_known and not unknown_members)
        evidence_stale = bool(target_evidence_stale or any(bool(item.get("stale")) for item in unknown_members))
        evidence_details: List[str] = []
        if not target_evidence_known:
            target_name = str(target_runtime.profile.get("name", "") or "").strip() or f"Device {target_id}"
            detail = target_evidence_detail or "cached PTT evidence is unavailable"
            evidence_details.append(f"{target_name}: {detail}")
        for member in unknown_members:
            detail = str(member.get("detail", "") or "").strip() or "cached PTT evidence is unavailable"
            evidence_details.append(f"{member['name']}: {detail}")
        evidence_detail = "; ".join(evidence_details)
        blocked = owner is not None or not evidence_known
        if not evidence_known:
            reason = f"Shared PTT group {ptt_group} is blocked because cached PTT evidence is incomplete."
            if evidence_detail:
                reason += f" {evidence_detail}."
        elif owner is not None:
            owner_label = owner_name or f"Device {int(owner_profile.get('id', 0) or 0)}"
            reason = f"Shared PTT group {ptt_group} is in use by {owner_label}."
        elif target_ptt_active:
            target_name = str(target_runtime.profile.get("name", "") or "").strip() or f"Device {target_id}"
            reason = f"Shared PTT group {ptt_group} is keyed by {target_name}."
        else:
            reason = f"Shared PTT group {ptt_group} is clear."
        return SharedPttLockSnapshot(
            device_profile_id=int(target_id),
            ptt_group=ptt_group,
            blocked=blocked,
            owner_device_profile_id=(
                int(owner_profile.get("id", 0) or 0) if owner is not None else None
            ),
            owner_name=owner_name,
            owner_backend=owner_backend,
            owner_ptt_active=owner is not None,
            target_ptt_active=target_ptt_active,
            reason=reason,
            evidence_known=evidence_known,
            evidence_stale=evidence_stale,
            evidence_detail=evidence_detail,
        )

    @staticmethod
    def _cached_status_row(
        status_by_device: Mapping[object, object],
        device_profile_id: int,
    ) -> Optional[object]:
        """Return one lane's status without invoking a device endpoint."""
        for key in (int(device_profile_id), str(int(device_profile_id))):
            try:
                if key in status_by_device:
                    return status_by_device[key]
            except Exception:
                continue
        return None

    @staticmethod
    def _status_field(row: object, *names: str) -> object:
        for name in names:
            if isinstance(row, Mapping) and name in row:
                return row.get(name)
            try:
                value = getattr(row, name)
            except Exception:
                continue
            if value is not None:
                return value
        return None

    @classmethod
    def _cached_ptt_evidence(
        cls,
        status_by_device: Mapping[object, object],
        device_profile_id: int,
    ) -> tuple[bool, bool, bool, str]:
        """Return ``active, known, stale, detail`` from a supplied cache only."""
        row = cls._cached_status_row(status_by_device, device_profile_id)
        if row is None:
            return False, False, False, "cached PTT evidence is missing"
        nested = cls._status_field(row, "ptt")
        source = nested if isinstance(nested, Mapping) else row
        active_marker = cls._status_field(source, "ptt_active", "active")
        known_marker = cls._status_field(source, "ptt_known", "known")
        stale = bool(cls._status_field(source, "ptt_stale", "stale"))
        errors = cls._status_field(source, "ptt_error", "error", "errors")
        if errors:
            return False, False, stale, f"cached PTT evidence error: {errors}"
        if stale:
            return False, False, True, "cached PTT evidence is stale"
        if known_marker is False:
            return False, False, False, "cached PTT evidence is unavailable"
        if active_marker is None:
            return False, False, False, "cached PTT evidence is missing"
        return bool(active_marker), True, False, ""

    @classmethod
    def _cached_frequency_evidence(
        cls,
        status_by_device: Mapping[object, object],
        device_profile_id: int,
    ) -> tuple[Optional[int], bool, bool, str]:
        """Return ``frequency_hz, known, stale, detail`` from a supplied cache only."""
        row = cls._cached_status_row(status_by_device, device_profile_id)
        if row is None:
            return None, False, False, "cached peer frequency evidence is missing"
        nested = cls._status_field(row, "frequency")
        source = nested if isinstance(nested, Mapping) else row
        raw_frequency = cls._status_field(source, "frequency_hz", "current_frequency_hz", "value")
        known_marker = cls._status_field(source, "frequency_known", "known")
        stale = bool(cls._status_field(source, "frequency_stale", "stale"))
        errors = cls._status_field(source, "frequency_error", "error", "errors")
        if errors:
            return None, False, stale, f"cached peer frequency evidence error: {errors}"
        if stale:
            return None, False, True, "last peer frequency check is stale"
        if known_marker is False:
            return None, False, False, "cached peer frequency evidence is unavailable"
        try:
            frequency_hz = int(raw_frequency)
        except (TypeError, ValueError):
            frequency_hz = 0
        if frequency_hz <= 0:
            return None, False, False, "cached peer frequency evidence is missing"
        return frequency_hz, True, False, ""

    @staticmethod
    def _apply_shared_ptt_annotations(snapshots: List[DeviceRuntimeSnapshot]) -> None:
        by_group: Dict[str, List[DeviceRuntimeSnapshot]] = {}
        for snapshot in snapshots:
            snapshot.shared_ptt_blocked = False
            snapshot.shared_ptt_owner_device_id = None
            snapshot.shared_ptt_owner_name = ""
            snapshot.shared_ptt_status_text = ""
            if snapshot.ptt_group:
                by_group.setdefault(snapshot.ptt_group, []).append(snapshot)
        for group, members in by_group.items():
            owners = [snap for snap in members if snap.ptt_active]
            owners.sort(key=lambda snap: (0 if snap.runtime_primary else 1, snap.name.lower(), snap.device_profile_id))
            for snapshot in members:
                other_owners = [owner for owner in owners if owner.device_profile_id != snapshot.device_profile_id]
                if other_owners:
                    owner = other_owners[0]
                    snapshot.shared_ptt_blocked = True
                    snapshot.shared_ptt_owner_device_id = owner.device_profile_id
                    snapshot.shared_ptt_owner_name = owner.name
                    if snapshot.ptt_active:
                        snapshot.shared_ptt_status_text = (
                            f"Shared PTT {group}: contention with {owner.name or f'Device {owner.device_profile_id}'}."
                        )
                    else:
                        snapshot.shared_ptt_status_text = (
                            f"Shared PTT {group}: blocked by {owner.name or f'Device {owner.device_profile_id}'}."
                        )
                    continue
                if snapshot.ptt_active:
                    snapshot.shared_ptt_status_text = f"Shared PTT {group}: keyed here."
                else:
                    snapshot.shared_ptt_status_text = f"Shared PTT {group}: clear."

    def _apply_observer_follow_annotations(self, snapshots: List[DeviceRuntimeSnapshot]) -> None:
        for snapshot in snapshots:
            snapshot.observer_follow_source_device_id = None
            snapshot.observer_follow_source_name = ""
            snapshot.observer_follow_summary = ""

        primary_snapshot = next(
            (snap for snap in snapshots if snap.runtime_primary and snap.device_class != "observer"),
            None,
        )
        if primary_snapshot is None:
            for snapshot in snapshots:
                if snapshot.device_class == "observer":
                    snapshot.observer_follow_summary = "Observer standby: awaiting a primary transceiver."
            return

        primary_id = int(primary_snapshot.device_profile_id)
        primary_name = primary_snapshot.name or f"Device {primary_id}"
        primary_band = str(primary_snapshot.current_band or "").strip().upper()
        policy_by_target = {
            int(policy.get("target_device_id", 0) or 0): dict(policy)
            for policy in self._sdr_follow_policies
            if bool(int(policy.get("enabled", 1) or 0)) and int(policy.get("source_device_id", 0) or 0) == primary_id
        }

        for snapshot in snapshots:
            if snapshot.device_class != "observer":
                continue
            snapshot.observer_follow_source_device_id = primary_id
            snapshot.observer_follow_source_name = primary_name
            if snapshot.device_profile_id not in policy_by_target:
                snapshot.observer_follow_summary = "Observer standby: no follow rule for the current primary device."
                continue
            runtime = self._runtimes.get(int(snapshot.device_profile_id))
            preferred_bands = _parse_string_list(
                (runtime.operating_profile if runtime is not None else {}).get("preferred_band_set_json", "[]")
            )
            alternate_band = next((band for band in preferred_bands if band and band != primary_band), "")
            if primary_band and alternate_band:
                snapshot.observer_follow_summary = (
                    f"Observer follow: park on {alternate_band} while {primary_name} is on {primary_band}."
                )
            elif primary_band:
                snapshot.observer_follow_summary = f"Observer follow: monitor {primary_name} on {primary_band}."
            elif preferred_bands:
                snapshot.observer_follow_summary = (
                    f"Observer standby: preferred bands {', '.join(preferred_bands)}; primary band unavailable."
                )
            else:
                snapshot.observer_follow_summary = (
                    f"Observer standby: follow {primary_name} when the primary tuning becomes available."
                )

    def _apply_profile_swap_annotations(self, snapshots: List[DeviceRuntimeSnapshot]) -> None:
        active_swap = dict(self._active_profile_swap or {})
        for snapshot in snapshots:
            snapshot.swap_role = ""
            snapshot.swap_summary = ""
        if not active_swap:
            return
        source_id = int(active_swap.get("source_device_id", 0) or 0)
        target_id = int(active_swap.get("target_device_id", 0) or 0)
        mode = str(active_swap.get("mode", "") or "").strip().lower()
        source_name = str(active_swap.get("source_device_name", "") or "").strip() or f"Device {source_id}"
        carried_name = str(active_swap.get("applied_operating_profile_name", "") or "").strip()
        for snapshot in snapshots:
            if snapshot.device_profile_id == target_id:
                snapshot.swap_role = "target"
                if mode == "carry_primary_profile" and carried_name:
                    snapshot.swap_summary = (
                        f"Temporary swap target: primary shell moved from {source_name}; carrying {carried_name}."
                    )
                else:
                    snapshot.swap_summary = f"Temporary swap target: acting as primary for {source_name}."
            elif snapshot.device_profile_id == source_id:
                snapshot.swap_role = "source"
                snapshot.swap_summary = f"Temporary swap source: restore returns the primary shell to {source_name}."

    def get_runtime_snapshots(
        self,
        *,
        force: bool = False,
        cache_only: bool = False,
    ) -> List[DeviceRuntimeSnapshot]:
        snapshots: List[DeviceRuntimeSnapshot] = []
        for device_id in self._active_profile_ids:
            runtime = self._runtimes.get(int(device_id))
            if runtime is None:
                continue
            snapshots.append(runtime.snapshot(force=force, cache_only=cache_only))
        self._apply_shared_ptt_annotations(snapshots)
        self._apply_observer_follow_annotations(snapshots)
        self._apply_profile_swap_annotations(snapshots)
        return snapshots

    def get_status_poll_metrics(self) -> Dict[str, int]:
        return self._status_poll_coordinator.metrics_snapshot().as_dict()

    def get_primary_runtime(self) -> Optional[DeviceRuntime]:
        if self._primary_device_id is None:
            return None
        return self._runtimes.get(int(self._primary_device_id))

    def get_runtime_for_device(self, device_profile_id: int) -> Optional[DeviceRuntime]:
        try:
            ident = int(device_profile_id or 0)
        except Exception:
            ident = 0
        if ident <= 0:
            return None
        return self._runtimes.get(ident)

    def update_runtime_js8_offset(self, device_profile_id: int, offset_hz: int) -> bool:
        """Refresh one runtime default without reconnecting any control client."""

        runtime = self.get_runtime_for_device(device_profile_id)
        if runtime is None:
            return False
        runtime.update_js8_offset(int(offset_hz))
        return True

    def get_active_profile_swap(self) -> Optional[Dict[str, Any]]:
        return dict(self._active_profile_swap) if isinstance(self._active_profile_swap, dict) else None

    def evaluate_primary_rf_conflict(
        self,
        *,
        target_band: str = "",
        target_frequency_hz: Optional[int] = None,
        source: str = "",
        force: bool = False,
    ) -> Optional[RfConflictSnapshot]:
        """Legacy primary-device facade for target-aware RF conflict checks."""
        primary_runtime = self.get_primary_runtime()
        if primary_runtime is None:
            return None
        primary_id = int(primary_runtime.profile.get("id", 0) or 0)
        if primary_id <= 0:
            return None
        return self.evaluate_rf_conflict_for_device(
            primary_id,
            target_band=target_band,
            target_frequency_hz=target_frequency_hz,
            source=source,
            force=force,
        )

    def evaluate_rf_conflict_for_device(
        self,
        target_device_profile_id: int,
        *,
        target_band: str = "",
        target_frequency_hz: Optional[int] = None,
        source: str = "",
        force: bool = False,
        status_by_device: Optional[Mapping[object, object]] = None,
    ) -> Optional[RfConflictSnapshot]:
        """Evaluate RF guard policies for one requested device.

        When ``status_by_device`` is supplied, this method uses only that
        caller-owned cache for peer frequency evidence.  It must therefore be
        safe to call from a scheduler lane without synchronously polling any
        other radio endpoint.
        """
        try:
            target_id = int(target_device_profile_id or 0)
        except (TypeError, ValueError):
            target_id = 0
        if target_id <= 0:
            return None
        target_runtime = self._runtimes.get(target_id)
        if target_runtime is None:
            return None
        normalized_band = str(target_band or "").strip().upper() or _hz_to_band(target_frequency_hz)
        if not normalized_band and not isinstance(target_frequency_hz, (int, float)):
            return None

        candidates: List[Dict[str, Any]] = []
        for policy in self._rf_conflict_policies:
            if not bool(int(policy.get("enabled", 1) or 0)):
                continue
            source_id = int(policy.get("source_device_id", 0) or 0)
            policy_target_id = int(policy.get("target_device_id", 0) or 0)
            if target_id not in {source_id, policy_target_id}:
                continue
            peer_id = policy_target_id if target_id == source_id else source_id
            peer_runtime = self._runtimes.get(peer_id)
            if peer_runtime is None:
                continue
            trigger = dict(policy.get("trigger") or {})
            antenna_groups = _normalize_group_list(trigger.get("antenna_groups"))
            amplifier_groups = _normalize_group_list(trigger.get("amplifier_groups"))
            frontend_groups = _normalize_group_list(trigger.get("frontend_groups"))
            band_overlap_groups = _normalize_group_list(trigger.get("band_overlap_groups"))
            advanced_frequency_groups = _normalize_group_list(trigger.get("advanced_frequency_groups"))
            if not (
                antenna_groups
                or amplifier_groups
                or frontend_groups
                or band_overlap_groups
                or advanced_frequency_groups
            ):
                continue
            if status_by_device is None:
                peer_frequency_hz = peer_runtime.current_frequency_hz(force=force)
                peer_status_unknown = not isinstance(peer_frequency_hz, (int, float)) or int(peer_frequency_hz) <= 0
                peer_status_stale = False
                peer_status_detail = ""
                try:
                    peer_frequency_snapshot = peer_runtime.status_poll_coordinator.latest_snapshot(
                        peer_runtime._status_poll_key("frequency")
                    )
                except Exception:
                    peer_frequency_snapshot = None
                if peer_frequency_snapshot is not None:
                    peer_status_stale = bool(getattr(peer_frequency_snapshot, "stale", False))
                    errors = getattr(peer_frequency_snapshot, "errors", {}) or {}
                    if errors:
                        peer_status_unknown = True
                        peer_status_detail = "; ".join(str(v) for v in errors.values() if str(v or "").strip())
                    elif peer_status_stale:
                        peer_status_unknown = True
                        peer_status_detail = "last peer frequency check is stale"
            else:
                peer_frequency_hz, peer_known, peer_status_stale, peer_status_detail = self._cached_frequency_evidence(
                    status_by_device,
                    peer_id,
                )
                peer_status_unknown = not peer_known
            if peer_status_unknown:
                peer_frequency_hz = None
                peer_band = ""
                same_frequency = False
                same_band = False
            else:
                peer_frequency_hz = int(peer_frequency_hz)
                peer_band = _hz_to_band(peer_frequency_hz)
                same_frequency = (
                    isinstance(target_frequency_hz, (int, float))
                    and abs(int(target_frequency_hz) - peer_frequency_hz) <= 5
                )
                same_band = bool(normalized_band and peer_band and normalized_band == peer_band)
            advanced_windows = dict(trigger.get("advanced_frequency_windows_hz") or {})
            advanced_window_hz = 0
            if advanced_frequency_groups:
                try:
                    target_window = int(advanced_windows.get(str(target_id), 0) or 0)
                except Exception:
                    target_window = 0
                try:
                    peer_window = int(advanced_windows.get(str(peer_id), 0) or 0)
                except Exception:
                    peer_window = 0
                advanced_window_hz = max(0, target_window, peer_window)
            frequency_delta_hz = (
                abs(int(target_frequency_hz) - int(peer_frequency_hz))
                if isinstance(target_frequency_hz, (int, float)) and isinstance(peer_frequency_hz, (int, float))
                else None
            )
            advanced_frequency_close = (
                bool(advanced_frequency_groups)
                and advanced_window_hz > 0
                and isinstance(frequency_delta_hz, int)
                and frequency_delta_hz <= advanced_window_hz
            )
            shared_resource_count = (
                len(antenna_groups)
                + len(amplifier_groups)
                + len(frontend_groups)
                + len(band_overlap_groups)
                + len(advanced_frequency_groups)
            )
            unknown_shared_resource_risk = bool(peer_status_unknown and shared_resource_count > 0)
            if band_overlap_groups and not (same_frequency or same_band):
                if not unknown_shared_resource_risk:
                    continue
            if advanced_frequency_groups and not advanced_frequency_close and not (
                antenna_groups or amplifier_groups or frontend_groups or band_overlap_groups
            ):
                if not unknown_shared_resource_risk:
                    continue
            if not advanced_frequency_close and not same_frequency and not same_band:
                if not unknown_shared_resource_risk:
                    continue
            guard_mode = str(policy.get("safety_mode") or "").strip().lower()
            if guard_mode not in {"block", "prompt", "warn"}:
                action = dict(policy.get("action") or {})
                guard_mode = str(action.get("guard_mode") or "prompt").strip().lower()
            if guard_mode not in {"block", "prompt", "warn"}:
                guard_mode = "prompt"
            peer_name = str(peer_runtime.profile.get("name", "") or "").strip() or f"Device {peer_id}"
            candidates.append(
                {
                    "peer_id": peer_id,
                    "peer_name": peer_name,
                    "peer_band": peer_band,
                    "peer_frequency_hz": peer_frequency_hz,
                    "peer_status_unknown": bool(peer_status_unknown),
                    "peer_status_stale": bool(peer_status_stale),
                    "peer_status_detail": peer_status_detail,
                    "same_frequency": bool(same_frequency),
                    "same_band": bool(same_band),
                    "shared_antenna_groups": antenna_groups,
                    "shared_amplifier_groups": amplifier_groups,
                    "shared_frontend_groups": frontend_groups,
                    "shared_band_overlap_groups": band_overlap_groups,
                    "shared_advanced_frequency_groups": advanced_frequency_groups
                    if (advanced_frequency_close or peer_status_unknown)
                    else [],
                    "advanced_frequency_window_hz": int(advanced_window_hz)
                    if (advanced_frequency_close or peer_status_unknown)
                    else 0,
                    "frequency_delta_hz": frequency_delta_hz if advanced_frequency_close else None,
                    "guard_mode": guard_mode,
                    "blocked": guard_mode == "block",
                    "shared_count": (
                        len(antenna_groups)
                        + len(amplifier_groups)
                        + len(frontend_groups)
                        + len(band_overlap_groups)
                        + len(advanced_frequency_groups if advanced_frequency_close else [])
                    ),
                }
            )
        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                0 if bool(item.get("same_frequency")) else 1,
                1 if bool(item.get("peer_status_unknown")) else 0,
                -int(item.get("shared_count", 0) or 0),
                str(item.get("peer_name", "")).lower(),
                int(item.get("peer_id", 0) or 0),
            )
        )
        primary_candidate = candidates[0]
        peer_ids = [int(item.get("peer_id", 0) or 0) for item in candidates]
        peer_names = [str(item.get("peer_name", "") or "").strip() for item in candidates if str(item.get("peer_name", "") or "").strip()]
        antenna_groups = sorted({group for item in candidates for group in item.get("shared_antenna_groups", [])})
        amplifier_groups = sorted({group for item in candidates for group in item.get("shared_amplifier_groups", [])})
        frontend_groups = sorted({group for item in candidates for group in item.get("shared_frontend_groups", [])})
        band_overlap_groups = sorted({group for item in candidates for group in item.get("shared_band_overlap_groups", [])})
        advanced_frequency_groups = sorted(
            {group for item in candidates for group in item.get("shared_advanced_frequency_groups", [])}
        )
        advanced_window_hz = max(int(item.get("advanced_frequency_window_hz", 0) or 0) for item in candidates)
        frequency_deltas = [
            int(item["frequency_delta_hz"])
            for item in candidates
            if isinstance(item.get("frequency_delta_hz"), int)
        ]
        frequency_delta_hz = min(frequency_deltas) if frequency_deltas else None
        peer_status_unknown = any(bool(item.get("peer_status_unknown")) for item in candidates)
        peer_status_stale = any(bool(item.get("peer_status_stale")) for item in candidates)
        peer_status_details = [
            str(item.get("peer_status_detail", "") or "").strip()
            for item in candidates
            if str(item.get("peer_status_detail", "") or "").strip()
        ]
        guard_mode = "warn"
        for item in candidates:
            guard_mode = stricter_rf_guard_mode(guard_mode, normalize_rf_guard_mode(item.get("guard_mode"), "confirm"))
        blocked = guard_mode == "block"

        resource_parts: List[str] = []
        if antenna_groups:
            resource_parts.append("antenna " + ", ".join(antenna_groups))
        if amplifier_groups:
            resource_parts.append("amplifier " + ", ".join(amplifier_groups))
        if frontend_groups:
            resource_parts.append("front-end " + ", ".join(frontend_groups))
        if band_overlap_groups:
            resource_parts.append("band-overlap guard " + ", ".join(band_overlap_groups))
        if advanced_frequency_groups:
            resource_parts.append(
                f"advanced guard {', '.join(advanced_frequency_groups)} within {advanced_window_hz} Hz"
            )
        resource_text = "; ".join(resource_parts) if resource_parts else "shared RF resources"

        normalized_target_hz = int(target_frequency_hz) if isinstance(target_frequency_hz, (int, float)) else None
        target_frequency_label = _format_frequency_label(normalized_target_hz)
        peer_frequency_label = (
            _format_frequency_label(int(primary_candidate["peer_frequency_hz"]))
            if isinstance(primary_candidate.get("peer_frequency_hz"), int)
            else ""
        )
        peer_frequency_hz = (
            int(primary_candidate["peer_frequency_hz"])
            if isinstance(primary_candidate.get("peer_frequency_hz"), int)
            else None
        )
        if peer_status_unknown:
            overlap_text = f"unverified peer tuning for target {normalized_band or target_frequency_label or 'frequency change'}"
        elif advanced_frequency_groups and isinstance(frequency_delta_hz, int):
            overlap_text = f"within {frequency_delta_hz} Hz of {peer_frequency_label or 'the peer frequency'}"
        else:
            overlap_text = (
                f"same frequency {target_frequency_label}"
                if primary_candidate["same_frequency"] and target_frequency_label
                else f"same band {normalized_band or str(primary_candidate.get('peer_band', '') or '').strip()}"
            )
        extra_count = max(0, len(peer_names) - 1)
        extra_text = f" (+{extra_count} more)" if extra_count else ""
        summary = f"RF conflict: {primary_candidate['peer_name']}{extra_text} on {overlap_text} via {resource_text}."
        target_tuning = " ".join(part for part in (target_frequency_label, normalized_band) if part).strip()
        if peer_status_unknown:
            detail = (
                f"Target {target_tuning or normalized_band or 'frequency change'} cannot be compared with "
                f"{primary_candidate['peer_name']} because CommsPlan cannot verify that radio's current frequency."
            )
        else:
            peer_tuning = " ".join(
                part
                for part in (peer_frequency_label, str(primary_candidate.get("peer_band", "") or "").strip())
                if part
            ).strip()
            detail = (
                f"Target {target_tuning or normalized_band or 'frequency change'} overlaps "
                f"{primary_candidate['peer_name']} at {peer_tuning or 'active tuning'}."
            )
        if extra_count:
            detail += f" Other active peers: {', '.join(peer_names[1:])}."
        if peer_status_unknown:
            status_detail = "; ".join(dict.fromkeys(peer_status_details)) or "peer status is unknown"
            detail += f" Peer status: {status_detail}."
        elif peer_status_stale:
            status_detail = "; ".join(dict.fromkeys(peer_status_details)) or "peer status is stale"
            detail += f" Peer status: {status_detail}."
        detail += f" Shared resources: {resource_text}."
        signature = "|".join(
            [
                str(target_id),
                str(source or "").strip().upper(),
                normalized_band,
                str(normalized_target_hz or 0),
                ",".join(str(peer_id) for peer_id in peer_ids),
                # Approval belongs to the tuning evidence that was reviewed,
                # including every conflicting peer, even within one band.
                ",".join(
                    f"{item['peer_id']}:{item.get('peer_frequency_hz') or 0}:{item.get('peer_band') or ''}"
                    for item in sorted(candidates, key=lambda item: int(item['peer_id']))
                ),
                ",".join(antenna_groups),
                ",".join(amplifier_groups),
                ",".join(frontend_groups),
                ",".join(band_overlap_groups),
                ",".join(advanced_frequency_groups),
                str(advanced_window_hz),
                str(frequency_delta_hz if isinstance(frequency_delta_hz, int) else ""),
                "peer_unknown" if peer_status_unknown else ("peer_stale" if peer_status_stale else ""),
                guard_mode,
            ]
        )
        return RfConflictSnapshot(
            target_device_profile_id=target_id,
            target_device_name=str(target_runtime.profile.get("name", "") or "").strip() or f"Device {target_id}",
            target_band=normalized_band,
            target_frequency_hz=normalized_target_hz,
            peer_device_profile_id=int(primary_candidate["peer_id"]),
            peer_name=str(primary_candidate["peer_name"]),
            peer_band=str(primary_candidate.get("peer_band", "") or "").strip(),
            peer_frequency_hz=peer_frequency_hz,
            peer_device_ids=peer_ids,
            peer_names=peer_names,
            shared_antenna_groups=antenna_groups,
            shared_amplifier_groups=amplifier_groups,
            shared_frontend_groups=frontend_groups,
            shared_band_overlap_groups=band_overlap_groups,
            shared_advanced_frequency_groups=advanced_frequency_groups,
            advanced_frequency_window_hz=advanced_window_hz,
            frequency_delta_hz=frequency_delta_hz,
            guard_mode=guard_mode,
            blocked=blocked,
            same_band=bool(primary_candidate["same_band"]),
            same_frequency=bool(primary_candidate["same_frequency"]),
            summary=summary,
            detail=detail,
            signature=signature,
            peer_status_unknown=bool(peer_status_unknown),
            peer_status_stale=bool(peer_status_stale),
            peer_status_detail="; ".join(dict.fromkeys(peer_status_details)),
        )

    def get_runtime_primary_device_profile(self) -> Optional[Dict[str, Any]]:
        runtime = self.get_primary_runtime()
        if runtime is None:
            return None
        return dict(runtime.profile)

    def runtime_status(self) -> Optional[MultiRigRuntimeStatus]:
        return self._runtime_status

    def primary_runtime_signature(self) -> tuple[object, ...]:
        runtime = self.get_primary_runtime()
        if runtime is None:
            return tuple()
        profile = runtime.profile if isinstance(runtime.profile, dict) else {}
        return (
            int(profile.get("id", 0) or 0),
            str(profile.get("name", "") or "").strip(),
            str(profile.get("control_backend", "") or "").strip().lower(),
            str(profile.get("deployment_mode", "full") or "full").strip().lower(),
            str(profile.get("rig_host", "") or "").strip(),
            int(profile.get("rig_port", 4532) or 4532),
            str(profile.get("flrig_host", "") or "").strip(),
            int(profile.get("flrig_port", 12345) or 12345),
            str(profile.get("fldigi_host", "") or "").strip(),
            int(profile.get("fldigi_port", 7362) or 7362),
            str(profile.get("js8_host", "") or "").strip(),
            int(profile.get("js8_port", 2442) or 2442),
            bool(runtime.is_primary),
        )

    def primary_runtime_policy(self) -> Dict[str, object]:
        runtime = self.get_primary_runtime()
        if runtime is None:
            return {
                "operating_profile_name": "",
                "assignment_state": "unassigned",
                "scheduler_enabled": True,
                "scheduler_mode": "full",
                "use_messages": True,
                "use_map": True,
                "use_background_ingest": True,
                "use_launch_control": False,
                "use_net_control_tabs": True,
                "swap_active": False,
                "swap_mode": "",
                "swap_summary": "",
                "swap_source_name": "",
                "swap_target_name": "",
            }
        policy = dict(runtime.operating_policy())
        active_swap = dict(self._active_profile_swap or {})
        if active_swap:
            mode = str(active_swap.get("mode", "") or "").strip().lower()
            source_name = str(active_swap.get("source_device_name", "") or "").strip()
            target_name = str(active_swap.get("target_device_name", "") or "").strip()
            carried_name = str(active_swap.get("applied_operating_profile_name", "") or "").strip()
            if mode == "carry_primary_profile" and carried_name:
                summary = f"Temporary swap active: {target_name or 'target device'} is primary for {source_name or 'previous primary'} while carrying {carried_name}."
            else:
                summary = f"Temporary swap active: {target_name or 'target device'} is primary for {source_name or 'previous primary'}."
            policy.update(
                {
                    "swap_active": True,
                    "swap_mode": mode,
                    "swap_summary": summary,
                    "swap_source_name": source_name,
                    "swap_target_name": target_name,
                }
            )
        else:
            policy.update(
                {
                    "swap_active": False,
                    "swap_mode": "",
                    "swap_summary": "",
                    "swap_source_name": "",
                    "swap_target_name": "",
                }
            )
        return policy
