from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
import threading
import time

from freqinout.radio_interface.js8_api_client import (
    JS8ApiClient,
    JS8ApiConnectionError,
    JS8ApiEndpoint,
)


_JS8_SPEED_NAMES = {
    0: "Normal",
    1: "Fast",
    2: "Turbo",
    4: "Slow",
    8: "Ultra",
    16: "Subspace",
}


def js8_speed_name(value: object) -> str:
    """Return a stable display name for JS8/JS8Call-Improved speeds."""

    try:
        speed = int(value)
    except Exception:
        return str(value or "")
    return _JS8_SPEED_NAMES.get(speed, str(value))


@dataclass(frozen=True)
class JS8SendIssue:
    code: str
    detail: str
    blocking: bool = True


@dataclass(frozen=True)
class JS8SendPreflight:
    endpoint: JS8ApiEndpoint
    ok: bool
    issues: tuple[JS8SendIssue, ...] = ()
    tx_queue_depth: Optional[int] = None
    tx_text: str = ""
    selected_call: str = ""
    tx_enabled: Optional[bool] = None

    @property
    def needs_confirmation(self) -> bool:
        return any(not issue.blocking for issue in self.issues)

    @property
    def summary(self) -> str:
        if self.ok and not self.issues:
            return "JS8Call send preflight passed."
        if self.ok:
            return "JS8Call send preflight passed with operator confirmation required."
        first = next(
            (issue.detail for issue in self.issues if issue.blocking),
            self.issues[0].detail if self.issues else "JS8Call send preflight failed.",
        )
        return first


@dataclass(frozen=True)
class JS8SendResult:
    sent: bool
    preflight: JS8SendPreflight
    detail: str
    transmitted_text: str = ""


@dataclass(frozen=True)
class JS8SelectedTargetState:
    """One bounded observation of the target selected in a JS8Call UI."""

    endpoint: JS8ApiEndpoint
    target: str = ""
    available: bool = False
    detail: str = ""

    @property
    def has_target(self) -> bool:
        return bool(self.target)


_EXPECT_TX_LOCKS: dict[tuple[str, int], threading.RLock] = {}
_EXPECT_TX_LOCKS_GUARD = threading.Lock()


def _endpoint_tx_lock(client: JS8ApiClient) -> threading.RLock:
    key = client.endpoint.normalized().key
    with _EXPECT_TX_LOCKS_GUARD:
        lock = _EXPECT_TX_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _EXPECT_TX_LOCKS[key] = lock
        return lock


def js8_endpoint_from_radio_profile(profile: Mapping[str, Any], *, fallback_settings: Any = None) -> JS8ApiEndpoint:
    host = str((profile or {}).get("js8_host", "") or "").strip()
    port_raw = (profile or {}).get("js8_port", "")
    if not host and fallback_settings is not None:
        try:
            host = str(fallback_settings.get("js8_host", "") or "").strip()
        except Exception:
            host = ""
    if not port_raw and fallback_settings is not None:
        try:
            port_raw = fallback_settings.get("js8_port", 2442)
        except Exception:
            port_raw = 2442
    try:
        port = int(port_raw or 2442)
    except Exception:
        port = 2442
    return JS8ApiEndpoint(host or "127.0.0.1", port).normalized()


def js8_profile_allows_transmit(profile: Mapping[str, Any]) -> bool:
    """Return whether a radio profile may be offered as a JS8 transmit source.

    Observer/SDR profiles may own an isolated JS8Call instance so FIO can ingest
    receive traffic and report its health.  That ownership must never make the
    observer eligible for Compose, Expect auto-reply, QSY, or any other transmit
    path.  Endpoint resolution intentionally remains available for receive-only
    consumers; transmit callers must apply this capability predicate.
    """

    device_class = str((profile or {}).get("device_class", "tx_rx") or "tx_rx").strip().lower()
    return device_class != "observer"


def _param_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _param_int(params: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        if key not in params:
            continue
        try:
            return int(params.get(key) or 0)
        except Exception:
            continue
    return None


def preflight_js8_send(
    client: JS8ApiClient,
    message: str,
    *,
    timeout_s: float = 0.8,
    allow_dirty_tx_text: bool = False,
    allow_selected_target: bool = False,
    allow_uncertain_target_state: bool = False,
) -> JS8SendPreflight:
    endpoint = client.endpoint.normalized()
    issues: list[JS8SendIssue] = []
    message_text = str(message or "").strip()
    tx_queue_depth: Optional[int] = None
    tx_text = ""
    selected_call = ""
    tx_enabled: Optional[bool] = None

    if not message_text:
        issues.append(JS8SendIssue("empty_message", "No JS8 message text is ready to send."))

    if not client.is_running:
        client.start()
    if not client.is_connected:
        issues.append(
            JS8SendIssue(
                "not_connected",
                f"JS8Call TCP API is not connected at {endpoint.host}:{endpoint.port}.",
            )
        )
        return JS8SendPreflight(endpoint=endpoint, ok=False, issues=tuple(issues))

    try:
        response = client.request("STATION.GET_CONFIG", expect_types=("STATION.CONFIG",), timeout_s=timeout_s)
        tx_enabled = _param_bool(response.params.get("TX_ENABLED"))
        if tx_enabled is False:
            issues.append(JS8SendIssue("tx_disabled", "JS8Call transmit is disabled."))
    except Exception as exc:
        issues.append(JS8SendIssue("tx_config_unknown", f"Could not verify JS8Call TX enable state: {exc}", False))

    try:
        response = client.request("TX.GET_QUEUE_DEPTH", expect_types=("TX.QUEUE_DEPTH",), timeout_s=timeout_s)
        tx_queue_depth = _param_int(response.params, "DEPTH", "QUEUE_DEPTH")
        if tx_queue_depth is not None and tx_queue_depth > 0:
            issues.append(
                JS8SendIssue("tx_queue_not_empty", f"JS8Call has {tx_queue_depth} queued transmit frame(s).")
            )
    except Exception as exc:
        issues.append(JS8SendIssue("queue_unknown", f"Could not verify JS8Call transmit queue: {exc}", False))

    try:
        response = client.request("TX.GET_TEXT", expect_types=("TX.TEXT",), timeout_s=timeout_s)
        tx_text = str(response.value or response.params.get("TEXT") or "").strip()
        if tx_text and not allow_dirty_tx_text:
            issues.append(JS8SendIssue("tx_text_not_empty", "JS8Call transmit text is not empty."))
    except Exception as exc:
        issues.append(JS8SendIssue("tx_text_unknown", f"Could not verify JS8Call transmit text is empty: {exc}", False))

    try:
        response = client.request("RX.GET_CALL_SELECTED", expect_types=("RX.CALL_SELECTED",), timeout_s=timeout_s)
        selected_call = str(response.value or response.params.get("CALLSIGN") or response.params.get("CALL") or "").strip()
        if selected_call and not allow_selected_target:
            issues.append(
                JS8SendIssue(
                    "selected_target_present",
                    f"JS8Call has selected target '{selected_call}'. Clear it or confirm before CommsPlan sends.",
                )
            )
    except Exception as exc:
        if not allow_uncertain_target_state:
            issues.append(
                JS8SendIssue(
                    "target_state_unknown",
                    f"Could not verify JS8Call selected-target state: {exc}",
                )
            )
        else:
            issues.append(
                JS8SendIssue(
                    "target_state_unknown",
                    f"Could not verify JS8Call selected-target state: {exc}",
                    False,
                )
            )

    blocking = [issue for issue in issues if issue.blocking]
    return JS8SendPreflight(
        endpoint=endpoint,
        ok=not blocking,
        issues=tuple(issues),
        tx_queue_depth=tx_queue_depth,
        tx_text=tx_text,
        selected_call=selected_call,
        tx_enabled=tx_enabled,
    )


def normalize_js8_target(value: object) -> str:
    return str(value or "").strip().upper()


def query_js8_selected_target(
    client: JS8ApiClient,
    *,
    timeout_s: float = 0.6,
) -> JS8SelectedTargetState:
    """Read the current JS8Call callsign/group selection without changing it.

    Unsupported, unreachable, and timed-out endpoints are represented as an
    unavailable observation so Compose can remain quiet and usable.  The caller
    owns threading; this bounded socket request must not run on the GUI thread.
    """

    endpoint = client.endpoint.normalized()
    try:
        if not client.is_running:
            client.start()
        if not client.is_connected:
            return JS8SelectedTargetState(
                endpoint=endpoint,
                available=False,
                detail="JS8Call selected-target state is unavailable.",
            )
        response = client.request(
            "RX.GET_CALL_SELECTED",
            expect_types=("RX.CALL_SELECTED",),
            timeout_s=timeout_s,
        )
        target = normalize_js8_target(
            response.value
            or response.params.get("CALLSIGN")
            or response.params.get("CALL")
            or response.params.get("TARGET")
            or ""
        )
        return JS8SelectedTargetState(endpoint=endpoint, target=target, available=True)
    except Exception as exc:
        return JS8SelectedTargetState(
            endpoint=endpoint,
            available=False,
            detail=f"JS8Call selected-target state is unavailable: {exc}",
        )


def set_js8_selected_target(client: JS8ApiClient, target: object = "", *, settle_s: float = 0.12) -> None:
    """Best-effort set or clear JS8Call's selected call/group.

    Several JS8Call builds have used different API command names. Unknown
    commands are ignored, so sending the compatibility set is safer than
    assuming one API variant.
    """
    value = normalize_js8_target(target)
    params = {"CALL": value, "SELECTED_CALL": value, "TARGET": value}
    for command in (
        "RX.SET_CALL_SELECTED",
        "RX.SET_SELECTED_CALL",
        "TX.SET_SELECTED_CALL",
        "STATION.SET_SELECTED_CALL",
    ):
        try:
            client.send(command, value=value, params=params)
        except Exception:
            pass
    if settle_s > 0:
        time.sleep(settle_s)


def _send_js8_message_guarded_unlocked(
    client: JS8ApiClient,
    message: str,
    *,
    timeout_s: float = 0.8,
    allow_dirty_tx_text: bool = False,
    allow_selected_target: bool = False,
    allow_uncertain_target_state: bool = False,
    clear_selected_target: bool = False,
    set_selected_target: object = None,
) -> JS8SendResult:
    message_text = str(message or "").strip()
    try:
        if not client.is_running:
            client.start()
        if client.is_connected and (clear_selected_target or set_selected_target is not None):
            set_js8_selected_target(client, "" if clear_selected_target else set_selected_target)
    except Exception:
        pass
    preflight = preflight_js8_send(
        client,
        message_text,
        timeout_s=timeout_s,
        allow_dirty_tx_text=allow_dirty_tx_text,
        allow_selected_target=allow_selected_target,
        allow_uncertain_target_state=allow_uncertain_target_state,
    )
    if not preflight.ok:
        return JS8SendResult(False, preflight, preflight.summary)
    try:
        client.send("TX.SET_TEXT", value="")
        client.send("TX.SEND_MESSAGE", value=message_text)
    except JS8ApiConnectionError as exc:
        failed = JS8SendPreflight(
            endpoint=preflight.endpoint,
            ok=False,
            issues=preflight.issues + (JS8SendIssue("send_failed", f"JS8Call send failed: {exc}"),),
            tx_queue_depth=preflight.tx_queue_depth,
            tx_text=preflight.tx_text,
            selected_call=preflight.selected_call,
            tx_enabled=preflight.tx_enabled,
        )
        return JS8SendResult(False, failed, failed.summary)
    return JS8SendResult(True, preflight, "JS8Call message sent.", transmitted_text=message_text)


def send_js8_message_guarded(
    client: JS8ApiClient,
    message: str,
    *,
    timeout_s: float = 0.8,
    allow_dirty_tx_text: bool = False,
    allow_selected_target: bool = False,
    allow_uncertain_target_state: bool = False,
    clear_selected_target: bool = False,
    set_selected_target: object = None,
) -> JS8SendResult:
    """Serialize the complete preflight-to-send transaction per endpoint."""
    with _endpoint_tx_lock(client):
        return _send_js8_message_guarded_unlocked(
            client,
            message,
            timeout_s=timeout_s,
            allow_dirty_tx_text=allow_dirty_tx_text,
            allow_selected_target=allow_selected_target,
            allow_uncertain_target_state=allow_uncertain_target_state,
            clear_selected_target=clear_selected_target,
            set_selected_target=set_selected_target,
        )
