"""Fault-contained Linux MeshCore BLE process supervision.

Only the helper process imports and owns Bleak/BlueZ and the official
``meshcore`` client.  The desktop process retains bounded control of deadlines,
cancellation, shutdown, and health publication.  Serial/TCP MeshCore and every
Meshtastic transport continue to use their existing in-process adapters.
"""

from __future__ import annotations

import multiprocessing
import pickle
import sys
import threading
import time
from dataclasses import replace
from enum import Enum
from multiprocessing.connection import Connection
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4

from freqinout.core.logger import log
from freqinout.core.mesh.ble_deadlines import SUPERVISOR_CONNECT_SEC
from freqinout.core.mesh.lifecycle import MeshOperationCancelled
from freqinout.core.mesh.meshtastic_adapter import MeshConnectionError
from freqinout.core.mesh.models import (
    MeshAdapterEvent,
    MeshChannel,
    MeshChannelCapabilities,
    MeshHealthSnapshot,
    MeshMessage,
    MeshNode,
    MeshSendCapabilities,
    MeshSendRequest,
    MeshSendResult,
)
from freqinout.core.mesh.settings import MeshConnectionConfig, MeshConnectionType


IPC_VERSION = 1
MAX_IPC_BYTES = 1_048_576
HEARTBEAT_INTERVAL_SEC = 2.0
HEARTBEAT_STALE_SEC = 6.0
CONNECT_DEADLINE_SEC = SUPERVISOR_CONNECT_SEC
NORMAL_REQUEST_DEADLINE_SEC = 12.0
DISCONNECT_DEADLINE_SEC = 1.25
SHUTDOWN_TOTAL_BUDGET_SEC = 2.75


class LinuxBleState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    PROBING = "probing"
    DISCOVERING = "discovering"
    PAIRING_REQUIRED = "pairing_required"
    PAIRING = "pairing"
    PAIR_RELEASE = "pair_release"
    CONNECTING = "connecting"
    VERIFYING = "verifying"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    BACKOFF = "backoff"
    NEEDS_ATTENTION = "needs_attention"
    FAULTED = "faulted"
    STOPPING = "stopping"
    DISABLED = "disabled"


_ACTIVE_STATES = frozenset(
    {
        LinuxBleState.STARTING,
        LinuxBleState.PROBING,
        LinuxBleState.DISCOVERING,
        LinuxBleState.PAIRING_REQUIRED,
        LinuxBleState.PAIRING,
        LinuxBleState.PAIR_RELEASE,
        LinuxBleState.CONNECTING,
        LinuxBleState.VERIFYING,
        LinuxBleState.CONNECTED,
        LinuxBleState.DISCONNECTING,
        LinuxBleState.BACKOFF,
        LinuxBleState.NEEDS_ATTENTION,
        LinuxBleState.FAULTED,
        LinuxBleState.STOPPING,
    }
)


def validate_supervisor_transition(current: LinuxBleState, target: LinuxBleState) -> bool:
    """Return whether a lifecycle transition is valid.

    Terminal recovery is deliberately broad: every live state may stop or
    fault, while a new generation may start only from a terminal state.
    """

    if current == target:
        return True
    if target in {LinuxBleState.STOPPING, LinuxBleState.FAULTED} and current in _ACTIVE_STATES:
        return True
    if target == LinuxBleState.STOPPED:
        return current not in {LinuxBleState.DISABLED, LinuxBleState.STOPPED}
    if current in {LinuxBleState.STOPPED, LinuxBleState.FAULTED, LinuxBleState.NEEDS_ATTENTION}:
        return target in {LinuxBleState.STARTING, LinuxBleState.DISABLED, LinuxBleState.STOPPED}
    allowed = {
        LinuxBleState.STARTING: {LinuxBleState.PROBING, LinuxBleState.DISCOVERING, LinuxBleState.CONNECTING},
        LinuxBleState.PROBING: {LinuxBleState.DISCOVERING, LinuxBleState.CONNECTING, LinuxBleState.PAIRING_REQUIRED},
        LinuxBleState.DISCOVERING: {LinuxBleState.CONNECTING, LinuxBleState.PAIRING_REQUIRED},
        LinuxBleState.PAIRING_REQUIRED: {LinuxBleState.PAIRING, LinuxBleState.NEEDS_ATTENTION},
        LinuxBleState.PAIRING: {LinuxBleState.PAIR_RELEASE, LinuxBleState.NEEDS_ATTENTION},
        LinuxBleState.PAIR_RELEASE: {LinuxBleState.CONNECTING, LinuxBleState.NEEDS_ATTENTION},
        LinuxBleState.CONNECTING: {LinuxBleState.VERIFYING, LinuxBleState.PAIRING_REQUIRED, LinuxBleState.NEEDS_ATTENTION},
        LinuxBleState.VERIFYING: {LinuxBleState.CONNECTED, LinuxBleState.NEEDS_ATTENTION},
        LinuxBleState.CONNECTED: {LinuxBleState.DISCONNECTING, LinuxBleState.BACKOFF},
        LinuxBleState.DISCONNECTING: {LinuxBleState.STOPPED},
        LinuxBleState.BACKOFF: {LinuxBleState.CONNECTING, LinuxBleState.NEEDS_ATTENTION},
        LinuxBleState.STOPPING: {LinuxBleState.STOPPED},
    }
    return target in allowed.get(current, set())


def _message(kind: str, *, generation: int, request_id: str = "", **payload: Any) -> dict[str, Any]:
    value = {
        "version": IPC_VERSION,
        "kind": str(kind),
        "generation": int(generation),
        "request_id": str(request_id),
        **payload,
    }
    if len(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)) > MAX_IPC_BYTES:
        raise MeshConnectionError("MeshCore Bluetooth helper message exceeded the bounded IPC limit.")
    return value


def _validated_message(value: object, *, generation: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MeshConnectionError("MeshCore Bluetooth helper returned malformed IPC.")
    result = dict(value)
    if int(result.get("version", 0) or 0) != IPC_VERSION:
        raise MeshConnectionError("MeshCore Bluetooth helper protocol version is incompatible.")
    if int(result.get("generation", -1) or -1) != int(generation):
        raise MeshConnectionError("MeshCore Bluetooth helper returned stale generation data.")
    if len(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)) > MAX_IPC_BYTES:
        raise MeshConnectionError("MeshCore Bluetooth helper response exceeded the bounded IPC limit.")
    return result


class _StationBleAuthority:
    """One native Linux BLE owner across scan and runtime operations."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._owner: object | None = None
        self._retired_process: Any | None = None

    def acquire(self, owner: object, timeout_sec: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self._condition:
            while self._owner is not None and self._owner is not owner:
                retired = self._retired_process
                if retired is not None and not retired.is_alive():
                    retired.join(timeout=0.0)
                    close_process = getattr(retired, "close", None)
                    if callable(close_process):
                        close_process()
                    self._retired_process = None
                    self._owner = None
                    self._condition.notify_all()
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.1) if retired is not None else remaining)
            self._owner = owner
            return True

    def retain_unreaped(self, owner: object, process: Any) -> None:
        # Discovery has no persistent adapter object. Keep its lease and
        # process handle until a later explicit acquire can prove it exited.
        with self._condition:
            if self._owner is owner:
                self._retired_process = process

    def release(self, owner: object) -> None:
        with self._condition:
            if self._owner is owner:
                self._owner = None
                self._condition.notify_all()


_STATION_BLE_AUTHORITY = _StationBleAuthority()


def _broker_main(
    control: Connection,
    data: Connection,
    config: MeshConnectionConfig | None,
    generation: int,
) -> None:
    """Child entry point. No Qt object and no parent-owned state crosses here."""

    send_lock = threading.Lock()
    stopped = threading.Event()

    def send_control(kind: str, *, request_id: str = "", **payload: Any) -> None:
        message = _message(kind, generation=generation, request_id=request_id, **payload)
        with send_lock:
            control.send(message)

    def heartbeat() -> None:
        while not stopped.wait(HEARTBEAT_INTERVAL_SEC):
            try:
                send_control("heartbeat", monotonic=time.monotonic())
            except Exception:
                return

    threading.Thread(target=heartbeat, name="FIO MeshCore BLE heartbeat", daemon=True).start()
    adapter: object | None = None
    try:
        if config is not None:
            # Import only in the helper so a blocked BlueZ/Bleak stack cannot
            # retain a thread or event loop in the desktop process.
            from freqinout.core.mesh.meshcore_adapter import MeshCorePythonAdapter

            adapter = MeshCorePythonAdapter(config)
        send_control("ready")
        while True:
            raw = control.recv()
            command = _validated_message(raw, generation=generation)
            request_id = str(command.get("request_id", "") or "")
            operation = str(command.get("operation", "") or "")
            try:
                if operation == "shutdown":
                    if adapter is not None:
                        adapter.disconnect()
                    send_control("operation_complete", request_id=request_id, result=None)
                    return
                if operation == "discover":
                    import asyncio
                    from freqinout.core.mesh.meshcore_adapter import _discover_meshcore_ble_devices

                    result = tuple(
                        asyncio.run(
                            _discover_meshcore_ble_devices(
                                int(command.get("timeout_sec", 10) or 10),
                            )
                        )
                    )
                elif adapter is None:
                    raise MeshConnectionError("MeshCore Bluetooth helper has no runtime configuration.")
                elif operation == "connect":
                    adapter.connect()
                    result = adapter.health()
                elif operation == "disconnect":
                    adapter.disconnect()
                    result = adapter.health()
                elif operation == "health":
                    result = adapter.health()
                elif operation == "list_nodes":
                    result = tuple(adapter.list_nodes())[:512]
                elif operation == "list_channels":
                    result = tuple(adapter.list_channels())[:64]
                elif operation == "recent_messages":
                    result = tuple(adapter.get_recent_messages())[-256:]
                elif operation == "receive_events":
                    events = tuple(adapter.receive_events())[-256:]
                    data.send(_message("data", generation=generation, request_id=request_id, result=events))
                    result = len(events)
                elif operation == "channel_capabilities":
                    result = adapter.channel_capabilities()
                elif operation == "send_capabilities":
                    result = adapter.send_capabilities()
                elif operation == "send_message":
                    result = adapter.send_message(command.get("request"))
                else:
                    raise MeshConnectionError(f"Unsupported MeshCore Bluetooth helper operation: {operation}")
                send_control("operation_complete", request_id=request_id, result=result)
            except BaseException as exc:  # child boundary must classify, not escape
                send_control(
                    "operation_failed",
                    request_id=request_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        stopped.set()
        if adapter is not None:
            try:
                adapter.disconnect()
            except Exception:
                pass
        try:
            control.close()
            data.close()
        except Exception:
            pass


class LinuxMeshCoreBleSupervisorAdapter:
    """Synchronous MeshAdapter proxy backed by a killable helper process."""

    transport_name = "meshcore"

    def __init__(
        self,
        config: MeshConnectionConfig,
        *,
        process_context: Any | None = None,
    ) -> None:
        if config.connection_type is not MeshConnectionType.BLE:
            raise ValueError("Linux BLE supervision applies only to MeshCore BLE.")
        self.config = config
        self.adapter_id = config.adapter_id
        self._context = process_context or multiprocessing.get_context("spawn")
        self._process: Any | None = None
        self._control: Connection | None = None
        self._data: Connection | None = None
        self._generation = 0
        self._state = LinuxBleState.STOPPED if config.enabled else LinuxBleState.DISABLED
        self._last_error = ""
        self._last_health = MeshHealthSnapshot(
            adapter_id=config.adapter_id,
            transport="meshcore",
            enabled=config.enabled,
            connected=False,
            connection_type="ble",
            device_name=config.display_name,
            lifecycle_state=self._state.value,
        )
        self._request_lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._authority_token = object()
        self._authority_owned = False
        self._last_heartbeat = 0.0

    @property
    def lifecycle_state(self) -> LinuxBleState:
        return self._state

    def _set_state(self, target: LinuxBleState) -> None:
        if not validate_supervisor_transition(self._state, target):
            raise MeshConnectionError(
                f"Invalid MeshCore Bluetooth lifecycle transition: {self._state.value} -> {target.value}."
            )
        self._state = target

    def _start_helper(self) -> None:
        process = self._process
        if process is not None:
            if process.is_alive():
                raise MeshConnectionError("The previous MeshCore helper is still stopping. Reconnect paused; wait and choose Connect again.")
            # A failed reap may finish later. Join and close its IPC before
            # allocating another generation or releasing station authority.
            self._retire_helper(deadline=time.monotonic() + 1.0)
            if self._process is not None:
                raise MeshConnectionError("The previous MeshCore helper has not retired. Reconnect paused; choose Connect again later.")
        if self._cancel_event.is_set():
            raise MeshOperationCancelled("MeshCore Bluetooth operation cancelled.")
        if not self._authority_owned:
            if not _STATION_BLE_AUTHORITY.acquire(self._authority_token, 6.0):
                raise MeshConnectionError(
                    "Another MeshCore Bluetooth scan or connection is still finishing. Wait a moment and retry."
                )
            self._authority_owned = True
        if self._cancel_event.is_set():
            self._retire_helper(deadline=time.monotonic() + 1.0)
            raise MeshOperationCancelled("MeshCore Bluetooth operation cancelled.")
        self._generation += 1
        child_control = child_data = None
        try:
            parent_control, child_control = self._context.Pipe(duplex=True)
            self._control = parent_control
            parent_data, child_data = self._context.Pipe(duplex=False)
            self._data = parent_data
            process = self._context.Process(
                target=_broker_main,
                args=(child_control, child_data, self.config, self._generation),
                name=f"FIO MeshCore BLE broker {self.adapter_id}",
                daemon=True,
            )
            self._process = process
            self._set_state(LinuxBleState.STARTING)
            process.start()
        except BaseException:
            self._retire_helper(deadline=time.monotonic() + 1.0)
            raise
        finally:
            # Parent must close child pipe ends even when allocation/spawn
            # fails. connect's fault path retires parent ends and the lease.
            for connection in (child_control, child_data):
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
        ready = self._wait_control(request_id="", deadline=time.monotonic() + 4.0, expected="ready")
        if ready.get("kind") != "ready":
            self._fault_and_retire("MeshCore Bluetooth helper did not become ready.")

    def _wait_control(self, *, request_id: str, deadline: float, expected: str) -> dict[str, Any]:
        connection = self._control
        if connection is None:
            raise MeshConnectionError("MeshCore Bluetooth helper is not running.")
        while time.monotonic() < deadline:
            if self._cancel_event.is_set():
                raise MeshOperationCancelled("MeshCore Bluetooth operation cancelled.")
            process = self._process
            if process is None or not process.is_alive():
                raise MeshConnectionError("MeshCore Bluetooth helper exited unexpectedly.")
            if not connection.poll(min(0.1, max(0.0, deadline - time.monotonic()))):
                continue
            message = _validated_message(connection.recv(), generation=self._generation)
            kind = str(message.get("kind", "") or "")
            if kind == "heartbeat":
                self._last_heartbeat = time.monotonic()
                continue
            if request_id and str(message.get("request_id", "") or "") != request_id:
                continue
            if kind == "operation_failed":
                raise MeshConnectionError(str(message.get("detail", "") or "MeshCore Bluetooth operation failed."))
            if kind != expected:
                raise MeshConnectionError("MeshCore Bluetooth helper returned an unexpected control response.")
            return message
        raise MeshConnectionError("MeshCore Bluetooth helper operation exceeded its deadline.")

    def _request(self, operation: str, *, timeout_sec: float, **payload: Any) -> Any:
        with self._request_lock:
            # Only explicit connect owns helper creation. Routine requests must
            # neither replace a faulted generation nor restart a stopped one.
            if self._process is None or not self._process.is_alive() or self._control is None:
                raise MeshConnectionError("MeshCore Bluetooth helper is not running. Reconnect paused; choose Connect to try again.")
            request_id = uuid4().hex
            connection = self._control
            assert connection is not None
            try:
                connection.send(
                    _message(
                        "command",
                        generation=self._generation,
                        request_id=request_id,
                        operation=operation,
                        **payload,
                    )
                )
                response = self._wait_control(
                    request_id=request_id,
                    deadline=time.monotonic() + max(0.1, float(timeout_sec)),
                    expected="operation_complete",
                )
                if operation == "receive_events":
                    data = self._data
                    if data is None or not data.poll(0.25):
                        raise MeshConnectionError("MeshCore Bluetooth helper omitted its bounded data response.")
                    packet = _validated_message(data.recv(), generation=self._generation)
                    if str(packet.get("request_id", "") or "") != request_id:
                        raise MeshConnectionError("MeshCore Bluetooth helper returned stale data.")
                    return packet.get("result", ())
                return response.get("result")
            except (MeshOperationCancelled, MeshConnectionError):
                raise
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise MeshConnectionError(f"MeshCore Bluetooth helper communication failed: {exc}") from exc

    def connect(self) -> None:
        if not self.config.enabled:
            self._state = LinuxBleState.DISABLED
            raise MeshConnectionError("MeshCore adapter is disabled.")
        self._cancel_event.clear()
        self._set_state(LinuxBleState.STARTING if self._state != LinuxBleState.STARTING else self._state)
        try:
            self._start_helper()
            self._set_state(LinuxBleState.CONNECTING)
            result = self._request("connect", timeout_sec=CONNECT_DEADLINE_SEC)
            if not isinstance(result, MeshHealthSnapshot) or not result.connected:
                raise MeshConnectionError("MeshCore Bluetooth did not complete Companion verification.")
            self._set_state(LinuxBleState.VERIFYING)
            self._set_state(LinuxBleState.CONNECTED)
            self._last_error = ""
            self._last_health = replace(result, lifecycle_state=self._state.value)
        except Exception as exc:
            detail = str(exc)
            self._last_error = detail
            self._state = LinuxBleState.NEEDS_ATTENTION if _operator_action_error(detail) else LinuxBleState.FAULTED
            self._fault_and_retire(detail)
            if isinstance(exc, MeshOperationCancelled):
                raise
            raise MeshConnectionError(self._last_error) from exc

    def disconnect(self) -> None:
        if self._process is None:
            self._state = LinuxBleState.STOPPED
            return
        self._cancel_event.clear()
        if self._state not in {LinuxBleState.STOPPING, LinuxBleState.DISCONNECTING}:
            self._state = LinuxBleState.DISCONNECTING
        started = time.monotonic()
        try:
            self._request("disconnect", timeout_sec=DISCONNECT_DEADLINE_SEC)
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            self._retire_helper(deadline=started + SHUTDOWN_TOTAL_BUDGET_SEC)
            self._state = LinuxBleState.NEEDS_ATTENTION if self._process is not None else LinuxBleState.STOPPED
            self._last_health = replace(
                self._last_health,
                connected=False,
                lifecycle_state=self._state.value,
                last_error=self._last_error,
            )

    def cancel_pending_operation(self) -> None:
        self._cancel_event.set()
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()

    def _fault_and_retire(self, detail: str) -> None:
        self._last_error = str(detail or "MeshCore Bluetooth helper failed.") + " Reconnect paused; choose Connect to try again."
        self._state = LinuxBleState.NEEDS_ATTENTION
        self._retire_helper(deadline=time.monotonic() + 1.0)

    def _retire_helper(self, *, deadline: float) -> None:
        process = self._process
        control = self._control
        if process is not None and process.is_alive() and control is not None:
            request_id = uuid4().hex
            try:
                control.send(
                    _message(
                        "command",
                        generation=self._generation,
                        request_id=request_id,
                        operation="shutdown",
                    )
                )
            except Exception:
                pass
        if process is not None:
            # multiprocessing rejects join() before start(). A failed spawn
            # with no pid and no live child has nothing to join.
            if process.is_alive() or getattr(process, "pid", None) is not None:
                process.join(timeout=max(0.0, min(0.75, deadline - time.monotonic())))
            if process.is_alive():
                process.terminate()
                process.join(timeout=max(0.0, min(0.5, deadline - time.monotonic())))
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=max(0.0, min(0.5, deadline - time.monotonic())))
            if process.is_alive():
                log.error("MeshCore BLE broker did not reap within the shutdown budget adapter=%s", self.adapter_id)
                # Do not grant another helper ownership while native cleanup
                # may still be running. A later explicit stop can retry reaping.
                self._state = LinuxBleState.NEEDS_ATTENTION
                return
        if process is not None:
            close_process = getattr(process, "close", None)
            if callable(close_process):
                try:
                    close_process()
                except OSError:
                    pass
        for connection in (self._control, self._data):
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
        self._process = None
        self._control = None
        self._data = None
        if self._authority_owned:
            _STATION_BLE_AUTHORITY.release(self._authority_token)
            self._authority_owned = False

    def health(self) -> MeshHealthSnapshot:
        if self._state == LinuxBleState.CONNECTED:
            try:
                result = self._request("health", timeout_sec=2.0)
                if isinstance(result, MeshHealthSnapshot):
                    self._last_health = replace(result, lifecycle_state=self._state.value)
            except Exception as exc:
                self._last_error = str(exc)
                self._state = LinuxBleState.FAULTED
                self._fault_and_retire(self._last_error)
        return replace(
            self._last_health,
            connected=bool(self._state == LinuxBleState.CONNECTED and self._last_health.connected),
            lifecycle_state=self._state.value,
            last_error=self._last_error or self._last_health.last_error,
        )

    def list_nodes(self) -> list[MeshNode]:
        return list(self._request("list_nodes", timeout_sec=NORMAL_REQUEST_DEADLINE_SEC) or ())

    def list_channels(self) -> list[MeshChannel]:
        return list(self._request("list_channels", timeout_sec=NORMAL_REQUEST_DEADLINE_SEC) or ())

    def list_channels_incremental(self, on_channel: Callable[[MeshChannel], None], *, cancel_event: object | None = None) -> list[MeshChannel]:
        channels = self.list_channels()
        for channel in channels:
            if bool(getattr(cancel_event, "is_set", lambda: False)()):
                raise MeshOperationCancelled("MeshCore channel refresh cancelled.")
            on_channel(channel)
        return channels

    def get_recent_messages(self) -> list[MeshMessage]:
        return list(self._request("recent_messages", timeout_sec=NORMAL_REQUEST_DEADLINE_SEC) or ())

    def receive_events(self) -> Iterator[MeshAdapterEvent]:
        if self._state != LinuxBleState.CONNECTED:
            return iter(())
        try:
            return iter(tuple(self._request("receive_events", timeout_sec=3.0) or ()))
        except (MeshConnectionError, MeshOperationCancelled) as exc:
            self._fault_and_retire(str(exc))
            raise

    def channel_capabilities(self) -> MeshChannelCapabilities:
        result = self._request("channel_capabilities", timeout_sec=3.0)
        return result if isinstance(result, MeshChannelCapabilities) else MeshChannelCapabilities()

    def send_capabilities(self) -> MeshSendCapabilities:
        result = self._request("send_capabilities", timeout_sec=3.0)
        return result if isinstance(result, MeshSendCapabilities) else MeshSendCapabilities()

    def send_message(self, request: MeshSendRequest) -> MeshSendResult:
        result = self._request(
            "send_message",
            timeout_sec=max(2.0, min(32.0, float(request.timeout_sec) + 2.0)),
            request=request,
        )
        if not isinstance(result, MeshSendResult):
            raise MeshConnectionError("MeshCore Bluetooth helper returned an invalid send result.")
        return result


def _operator_action_error(detail: str) -> bool:
    text = str(detail or "").casefold()
    return any(
        marker in text
        for marker in (
            "pair",
            "pin",
            "passkey",
            "authentication",
            "agent",
            "not available",
            "powered off",
            "busy",
            "service",
        )
    )


def linux_ble_supervision_enabled(platform_name: str | None = None) -> bool:
    return str(platform_name or sys.platform).strip().lower().startswith("linux")


def discover_meshcore_ble_devices_supervised(
    timeout_sec: int = 10,
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[object, ...]:
    """Run Settings discovery through the same station-wide process authority."""

    owner = object()
    if not _STATION_BLE_AUTHORITY.acquire(owner, 6.0):
        raise MeshConnectionError("A MeshCore Bluetooth connection is active. Disconnect it before scanning.")
    parent_control = child_control = parent_data = child_data = process = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise MeshOperationCancelled("MeshCore BLE scan cancelled.")
        context = multiprocessing.get_context("spawn")
        parent_control, child_control = context.Pipe(duplex=True)
        parent_data, child_data = context.Pipe(duplex=False)
        generation = int(time.monotonic_ns())
        process = context.Process(
            target=_broker_main,
            args=(child_control, child_data, None, generation),
            name="FIO MeshCore BLE discovery broker",
            daemon=True,
        )
        process.start()
        child_control.close()
        child_data.close()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise MeshOperationCancelled("MeshCore BLE scan cancelled.")
            if not process.is_alive():
                raise MeshConnectionError("MeshCore Bluetooth scan helper exited unexpectedly.")
            if parent_control.poll(0.1):
                ready = _validated_message(parent_control.recv(), generation=generation)
                if ready.get("kind") == "ready":
                    break
        else:
            raise MeshConnectionError("MeshCore Bluetooth scan helper did not become ready.")
        request_id = uuid4().hex
        parent_control.send(
            _message(
                "command",
                generation=generation,
                request_id=request_id,
                operation="discover",
                timeout_sec=max(5, int(timeout_sec)),
            )
        )
        deadline = time.monotonic() + max(8.0, float(timeout_sec) + 5.0)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise MeshOperationCancelled("MeshCore BLE scan cancelled.")
            if not process.is_alive():
                raise MeshConnectionError("MeshCore Bluetooth scan helper exited unexpectedly.")
            if not parent_control.poll(0.1):
                continue
            message = _validated_message(parent_control.recv(), generation=generation)
            if message.get("kind") == "heartbeat":
                continue
            if str(message.get("request_id", "") or "") != request_id:
                continue
            if message.get("kind") == "operation_failed":
                raise MeshConnectionError(str(message.get("detail", "") or "MeshCore Bluetooth scan failed."))
            return tuple(message.get("result", ()) or ())
        raise MeshConnectionError("MeshCore Bluetooth scan exceeded its deadline.")
    finally:
        unwinding = sys.exc_info()[0] is not None
        reaped = process is None
        try:
            if process is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=0.75)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(timeout=0.5)
                reaped = not process.is_alive()
                if reaped:
                    if getattr(process, "pid", None) is not None:
                        process.join(timeout=0.0)
                    close_process = getattr(process, "close", None)
                    if callable(close_process):
                        close_process()
        finally:
            for connection in (parent_control, child_control, parent_data, child_data):
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if reaped:
                _STATION_BLE_AUTHORITY.release(owner)
            elif process is not None:
                _STATION_BLE_AUTHORITY.retain_unreaped(owner, process)
                log.error("MeshCore BLE scan helper has not retired; reconnect remains paused")
                if not unwinding:
                    raise MeshConnectionError("MeshCore Bluetooth scan helper has not retired. Reconnect paused; try again after it stops.")



__all__ = [
    "CONNECT_DEADLINE_SEC",
    "HEARTBEAT_INTERVAL_SEC",
    "HEARTBEAT_STALE_SEC",
    "IPC_VERSION",
    "LinuxBleState",
    "LinuxMeshCoreBleSupervisorAdapter",
    "SHUTDOWN_TOTAL_BUDGET_SEC",
    "discover_meshcore_ble_devices_supervised",
    "linux_ble_supervision_enabled",
    "validate_supervisor_transition",
]
