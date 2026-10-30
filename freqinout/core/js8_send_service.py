from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
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


@dataclass(frozen=True)
class JS8SelectedTargetClearResult:
    state: JS8SelectedTargetState
    cleared: bool = False
    status: str = "unavailable"
    detail: str = ""


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
    require_target_id: bool = False,
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
        response = client.request("RX.GET_CALL_SELECTED", expect_types=("RX.CALL_SELECTED",), timeout_s=timeout_s, **({"require_id": True} if require_target_id else {}))
        selected_call = _selected_target_value(response)
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


def _selected_target_value(response: object) -> str:
    """Read value/legacy parameter spellings without mistaking a target for empty."""
    params = getattr(response, "params", {}) or {}
    return normalize_js8_target(
        getattr(response, "value", "") or params.get("CALLSIGN")
        or params.get("CALL") or params.get("TARGET") or ""
    )


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
        target = _selected_target_value(response)
        return JS8SelectedTargetState(endpoint=endpoint, target=target, available=True)
    except Exception as exc:
        return JS8SelectedTargetState(
            endpoint=endpoint,
            available=False,
            detail=f"JS8Call selected-target state is unavailable: {exc}",
        )


def clear_js8_selected_target(
    client: JS8ApiClient,
    expected_target: object,
    *,
    timeout_s: float = 0.8,
    cancelled: Callable[[], bool] | None = None,
) -> JS8SelectedTargetClearResult:
    """Explicit clear-only transaction; correlated evidence, never a send.

    The endpoint lock serializes CP commands, not external JS8Call UI clicks.
    Unsupported variants keep the existing manual-deselection fallback.
    """
    endpoint = client.endpoint.normalized()
    state = JS8SelectedTargetState(endpoint)
    expected = normalize_js8_target(expected_target)
    timeout = max(0.05, min(2.0, float(timeout_s)))
    lock = _endpoint_tx_lock(client)

    def result(status: str, detail: str, cleared: bool = False) -> JS8SelectedTargetClearResult:
        return JS8SelectedTargetClearResult(state, cleared, status, detail)

    def stopped() -> bool:
        return bool(cancelled and cancelled())

    def read() -> JS8SelectedTargetState:
        response = client.request(
            "RX.GET_CALL_SELECTED", expect_types=("RX.CALL_SELECTED",),
            timeout_s=timeout, require_id=True,
        )
        return JS8SelectedTargetState(endpoint, _selected_target_value(response), True)

    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while not stopped() and time.monotonic() < deadline:
            acquired = lock.acquire(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            if acquired:
                break
        if stopped():
            return result("cancelled", "Selection clear cancelled.")
        if not acquired:
            return result("unavailable", "JS8Call is busy with another CP action. Try again.")
        if not client.is_running:
            client.start()
        if not client.is_connected:
            return result("unavailable", "JS8Call is not connected; selection was not cleared.")
        # Reconnection during this transaction invalidates its identity evidence.
        connection = getattr(client, "last_connected_ts", None)
        state = read()
        if stopped():
            return result("cancelled", "Selection clear cancelled.")
        if not state.target:
            return result("already_empty", "No selection is currently set in JS8Call.", True)
        if state.target != expected:
            return result("changed", "JS8Call selection changed. Review it before clearing.")
        if getattr(client, "last_connected_ts", None) != connection:
            state = JS8SelectedTargetState(endpoint)
            return result("unavailable", "JS8Call reconnected; refresh its selection before clearing.")
        if stopped():
            return result("cancelled", "Selection clear cancelled.")
        response = client.request(
            "RX.SET_CALL_SELECTED", value="", expect_types=("RX.CALL_SELECTED",),
            timeout_s=timeout, require_id=True,
        )
        setter_target = _selected_target_value(response)
        state = read()
        if getattr(client, "last_connected_ts", None) != connection:
            state = JS8SelectedTargetState(endpoint)
            return result("unavailable", "JS8Call reconnected; could not verify the clear result.")
        if stopped():
            return result("cancelled", "Selection clear completed after cancellation; refresh to verify current state.")
        if setter_target or state.target:
            return result("failed", "JS8Call selection could not be verified empty. Deselect in JS8Call or refresh.")
        return result("cleared", "Selection cleared in JS8Call.", True)
    except Exception:
        # A setter may have reached the application even if its reply was lost.
        # Do not publish a previous selection as current or a timeout as empty.
        state = JS8SelectedTargetState(endpoint)
        return result("unavailable", "Could not verify selection. Refresh or deselect in JS8Call.")
    finally:
        if acquired:
            lock.release()


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
    require_target_id: bool = False,
    before_send: Callable[[], tuple[bool, str]] | None = None,
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
    connection = getattr(client, "last_connected_ts", None)
    preflight = preflight_js8_send(
        client,
        message_text,
        timeout_s=timeout_s,
        allow_dirty_tx_text=allow_dirty_tx_text,
        allow_selected_target=allow_selected_target,
        allow_uncertain_target_state=allow_uncertain_target_state,
        require_target_id=require_target_id,
    )
    if not preflight.ok:
        return JS8SendResult(False, preflight, preflight.summary)
    if require_target_id and (not client.is_connected or getattr(client, 'last_connected_ts', None) != connection):
        changed = JS8SendPreflight(endpoint=preflight.endpoint, ok=False,
            issues=(JS8SendIssue('connection_changed', 'JS8Call reconnected during preflight; nothing sent.'),))
        return JS8SendResult(False, changed, changed.summary)
    if before_send is not None:
        try:
            allowed, detail = before_send()
        except Exception as exc:
            allowed, detail = False, f'Source/guard unavailable: {exc}'
        if not allowed:
            blocked = JS8SendPreflight(endpoint=preflight.endpoint, ok=False,
                issues=(JS8SendIssue('source_changed', detail),))
            return JS8SendResult(False, blocked, detail)
    if require_target_id:
        try:
            response = client.request('RX.GET_CALL_SELECTED', expect_types=('RX.CALL_SELECTED',),
                                      timeout_s=timeout_s, require_id=True)
            selected = _selected_target_value(response)
            if selected or not client.is_connected or getattr(client, 'last_connected_ts', None) != connection:
                raise ValueError('JS8Call selection or connection changed; review before sending.')
        except Exception as exc:
            blocked = JS8SendPreflight(endpoint=preflight.endpoint, ok=False,
                issues=(JS8SendIssue('target_state_changed', str(exc)),))
            return JS8SendResult(False, blocked, blocked.summary)
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
    require_target_id: bool = False,
    before_send: Callable[[], tuple[bool, str]] | None = None,
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
            require_target_id=require_target_id,
            before_send=before_send,
            clear_selected_target=clear_selected_target,
            set_selected_target=set_selected_target,
        )


def send_js8_addressed_message_guarded(
    client: JS8ApiClient, message: str, *, timeout_s: float = 0.8,
    source_preflight: Callable[[], tuple[bool, str]],
) -> JS8SendResult:
    """NCS addressed text: verified UI deselection, then the normal send guard.

    No permissive target override reaches TX. The shared RLock spans every step;
    an external GUI change is caught by the final selected-target preflight.
    A failed clear is blocked rather than retried or speculatively transmitted.
    """
    def blocked(code, detail):
        state = JS8SendPreflight(endpoint=client.endpoint.normalized(), ok=False,
            issues=(JS8SendIssue(code, detail),))
        return JS8SendResult(False, state, detail)

    with _endpoint_tx_lock(client):
        allowed, reason = source_preflight()
        if not allowed:
            return blocked('source_changed', reason)
        # Inspect busy/text state before changing the user's JS8 selection.
        initial = preflight_js8_send(client, message, timeout_s=timeout_s,
            allow_selected_target=True, allow_uncertain_target_state=False, require_target_id=True)
        if not initial.ok:
            return JS8SendResult(False, initial, initial.summary)
        connected_ts = getattr(client, 'last_connected_ts', None)
        if initial.selected_call:
            cleared = clear_js8_selected_target(client, expected_target=initial.selected_call,
                                                 timeout_s=timeout_s)
            if not cleared.cleared:
                return blocked('target_clear_failed', cleared.detail)
        allowed, reason = source_preflight()
        if not allowed:
            return blocked('source_changed', reason)
        if not client.is_connected or getattr(client, 'last_connected_ts', None) != connected_ts:
            return blocked('connection_changed', 'JS8Call reconnected; review and send again explicitly.')
        return send_js8_message_guarded(client, message, timeout_s=timeout_s,
            allow_uncertain_target_state=False, require_target_id=True, before_send=source_preflight)
