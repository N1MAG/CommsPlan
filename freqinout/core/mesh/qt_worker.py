from __future__ import annotations

from dataclasses import replace
from time import monotonic_ns
from pathlib import Path
import threading
from typing import Iterable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from freqinout.core.mesh.manager import MeshAdapterFactory, MeshConnectionManager, default_mesh_adapter_factory
from freqinout.core.mesh.lifecycle import (
    MeshOperationCancelled,
    MeshOperationSnapshot,
    MeshRetryPolicy,
    MeshRetryState,
    mesh_error_requires_operator_action,
    release_mesh_connect_attempt,
    save_mesh_retry_handoff,
    take_mesh_retry_handoff,
    try_acquire_mesh_connect_attempt,
)
from freqinout.core.mesh.models import MeshAdapterEvent, MeshHealthSnapshot, MeshSendRequest, MeshSendResult, utc_now
from freqinout.core.mesh.settings import MeshConnectionConfig
from freqinout.core.mesh.store import MeshEventStoreSink, append_mesh_send_audit, mesh_send_policy_issue


class MeshConnectionWorker(QObject):
    event_ready = Signal(object)
    health_ready = Signal(object)
    error_ready = Signal(str)
    started = Signal()
    stopped = Signal()
    operation_state = Signal(str, str, int)
    channels_ready = Signal(str, tuple)
    channel_capabilities_ready = Signal(str, object)
    operation_ready = Signal(object)
    send_ready = Signal(object)

    RETRY_EXHAUSTED_GUIDANCE = (
        "Reconnect paused after 3 failed attempts - select Connect to try again."
    )

    def __init__(
        self,
        configs: Iterable[MeshConnectionConfig],
        *,
        db_path: str | Path | None = None,
        poll_interval_ms: int = 1000,
        node_poll_interval_ms: int = 300000,
        channel_poll_interval_ms: int | None = None,
        reconnect_interval_ms: int = 15000,
        reconnect_max_interval_ms: int = 300000,
        adapter_factory: MeshAdapterFactory = default_mesh_adapter_factory,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        configs = tuple(configs)
        self._adapter_factory = adapter_factory
        self._manager = MeshConnectionManager(configs, adapter_factory=adapter_factory)
        self._poll_interval_ms = max(250, int(poll_interval_ms or 1000))
        self._node_poll_interval_ms = max(self._poll_interval_ms, int(node_poll_interval_ms or 300000))
        if channel_poll_interval_ms is None or int(channel_poll_interval_ms) <= 0:
            self._channel_poll_interval_ms: int | None = None
        else:
            self._channel_poll_interval_ms = max(self._poll_interval_ms, int(channel_poll_interval_ms))
        self._retry_policy = MeshRetryPolicy(
            initial_delay_ms=max(self._poll_interval_ms, int(reconnect_interval_ms or 15000)),
            maximum_delay_ms=max(self._poll_interval_ms, int(reconnect_max_interval_ms or 300000)),
        )
        self._retry_context_keys = {
            config.adapter_id: self._retry_context_key(config)
            for config in configs
        }
        self._retry_states = {}
        for config in configs:
            restored = take_mesh_retry_handoff(self._retry_context_keys[config.adapter_id])
            self._retry_states[config.adapter_id] = restored or MeshRetryState()
        self._last_node_poll_ms = 0
        self._last_channel_poll_ms = 0
        self._last_reconnect_ms = 0
        self._timer: QTimer | None = None
        self._running = False
        self._stop_event = threading.Event()
        self._operation_sequence = 0
        self._active_operations: dict[tuple[str, str], MeshOperationSnapshot] = {}
        self._operation_cancel_events: dict[tuple[str, str], tuple[str, threading.Event]] = {}
        self._operation_lock = threading.RLock()
        self._stopped_emitted = False
        self._db_path = Path(db_path) if db_path is not None else None
        self._store_sink = MeshEventStoreSink(self._db_path) if self._db_path is not None else None
        self._manager.add_listener(self._handle_manager_event)

    @Slot()
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._stopped_emitted = False
        try:
            now_ms = self._elapsed_ms()
            for adapter_id in self._manager.configured_ids():
                self._attempt_connection(adapter_id, now_ms, force=False, emit_connecting=False)
        except Exception as exc:
            self.error_ready.emit(str(exc))
        # Do not issue a contact/channel request on the first timer tick after
        # startup.  Connection bring-up already performs device setup, and a
        # quiet card should not immediately receive a second burst of reads.
        poll_started_ms = self._elapsed_ms()
        self._last_node_poll_ms = poll_started_ms
        self._last_channel_poll_ms = poll_started_ms
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(self._poll_interval_ms)
            self._timer.timeout.connect(self.poll_once)
        self._timer.start()
        self.started.emit()

    @Slot()
    def stop(self) -> None:
        self.request_stop()
        was_running = self._running
        self._running = False
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        self._last_node_poll_ms = 0
        self._last_channel_poll_ms = 0
        self._last_reconnect_ms = 0
        if was_running:
            try:
                self._manager.stop_all()
            except Exception as exc:
                self.error_ready.emit(str(exc))
        for operation in tuple(self._active_operations.values()):
            if operation.state not in {"complete", "cancelled", "error"}:
                self._publish_operation(operation, "cancelled", detail="Worker stopped")
        if not self._stopped_emitted:
            self._stopped_emitted = True
            self.stopped.emit()

    def request_stop(self) -> None:
        """Thread-safe cancellation hook; safe to call before the worker event loop is free."""

        self._stop_event.set()
        with self._operation_lock:
            cancel_events = tuple(self._operation_cancel_events.values())
        for _operation_id, cancel_event in cancel_events:
            cancel_event.set()
        self._manager.cancel_pending_operations()

    def request_cancel(self, source_id: str, request_class: str) -> None:
        """Thread-safe cancellation for a live request without stopping ingest."""

        key = (str(source_id or "").strip(), str(request_class or "").strip())
        with self._operation_lock:
            entry = self._operation_cancel_events.get(key)
        if entry is None:
            return
        entry[1].set()
        self._manager.cancel_pending_operations()

    @Slot(str)
    def retry_now(self, adapter_id: str) -> None:
        if not self._running or self._stop_event.is_set():
            return
        self.reset_retry_budget(adapter_id)
        self._attempt_connection(str(adapter_id), self._elapsed_ms(), force=True, emit_connecting=True)

    def reset_retry_budget(self, adapter_id: str) -> None:
        """Clear one adapter's inherited pause before an explicit Connect."""

        state = self._retry_states.setdefault(str(adapter_id), MeshRetryState())
        state.retry_now()
        self._persist_retry_state(str(adapter_id))

    @Slot(str)
    def refresh_channels(self, adapter_id: str) -> None:
        adapter_id = str(adapter_id or "").strip()
        if not self._running or self._stop_event.is_set() or not adapter_id:
            return
        operation = self._begin_operation(adapter_id, "channel-sync")
        cancel_event = self._operation_cancel_events[(adapter_id, "channel-sync")][1]
        self.operation_state.emit(adapter_id, "syncing-channels", 0)
        try:
            staged_count = 0

            def _stage(channel) -> None:
                nonlocal staged_count
                if self._stop_event.is_set() or cancel_event.is_set():
                    return
                staged_count += 1
                if self._store_sink is not None:
                    self._store_sink.stage_channels((channel,))
                self.channels_ready.emit(adapter_id, (channel,))
                self.operation_state.emit(adapter_id, "syncing-channels", staged_count)
                self._publish_operation(operation, "running", current=staged_count)

            channels = self._manager.poll_channels_incremental(
                adapter_id,
                _stage,
                cancel_event=cancel_event,
            )
            if self._stop_event.is_set() or cancel_event.is_set():
                self.operation_state.emit(adapter_id, "channel-sync-cancelled", staged_count)
                self._publish_operation(operation, "cancelled", current=staged_count, detail="Channel refresh cancelled.")
                return
            self.channel_capabilities_ready.emit(adapter_id, self._manager.channel_capabilities(adapter_id))
            self.operation_state.emit(adapter_id, "channels-ready", len(channels))
            self._publish_operation(operation, "complete", current=len(channels), total=len(channels))
        except MeshOperationCancelled as exc:
            self.operation_state.emit(adapter_id, "channel-sync-cancelled", staged_count)
            self._publish_operation(operation, "cancelled", current=staged_count, detail=str(exc))
        except Exception as exc:
            state = "cancelled" if self._stop_event.is_set() or cancel_event.is_set() else "error"
            if state == "cancelled":
                self.operation_state.emit(adapter_id, "channel-sync-cancelled", staged_count)
            else:
                self.error_ready.emit(str(exc))
                self.operation_state.emit(adapter_id, "channel-sync-error", 0)
            self._publish_operation(operation, state, current=staged_count, detail=str(exc))

    @Slot(str, str, object)
    def configure_channel(self, adapter_id: str, channel_id: str, updates: object) -> None:
        if not isinstance(updates, dict) or self._stop_event.is_set():
            return
        try:
            channel = self._manager.configure_channel(adapter_id, channel_id, updates)
            self.channels_ready.emit(adapter_id, (channel,))
        except Exception as exc:
            self.error_ready.emit(str(exc))

    @Slot(str, str)
    def remove_channel_from_device(self, adapter_id: str, channel_id: str) -> None:
        if self._stop_event.is_set():
            return
        try:
            self._manager.remove_channel_from_device(adapter_id, channel_id)
            self.refresh_channels(adapter_id)
        except Exception as exc:
            self.error_ready.emit(str(exc))

    @Slot(object)
    def send_message(self, request: object) -> None:
        if not isinstance(request, MeshSendRequest):
            return
        if self._stop_event.is_set() or not self._running:
            self.send_ready.emit(
                MeshSendResult(
                    request_id=request.request_id,
                    adapter_id=request.adapter_id,
                    transport="",
                    destination_kind=request.destination_kind,
                    destination_id=request.destination_id,
                    channel_id=request.channel_id,
                    state="failed",
                    requested_at=request.requested_at,
                    completed_at=utc_now(),
                    detail="Local Mesh runtime stopped before the send could begin.",
                    retryable=True,
                    evidence="runtime_stopped",
                )
            )
            return
        operation = self._begin_operation(request.adapter_id, "mesh-send")
        transport = ""
        try:
            transport = str(self._manager.health(request.adapter_id).transport or "")
        except Exception:
            pass
        try:
            if self._db_path is None:
                raise RuntimeError("Mesh send audit storage is unavailable.")
            append_mesh_send_audit(
                self._db_path,
                request,
                transport=transport,
                state="requested",
                detail="Operator requested an outbound mesh message.",
            )
        except Exception as exc:
            result = MeshSendResult(
                request_id=request.request_id,
                adapter_id=request.adapter_id,
                transport=transport,
                destination_kind=request.destination_kind,
                destination_id=request.destination_id,
                channel_id=request.channel_id,
                state="failed",
                requested_at=request.requested_at,
                completed_at=utc_now(),
                detail=f"Mesh send was blocked because its audit record could not be created: {exc}",
                retryable=True,
                evidence="audit_unavailable",
            )
            self._publish_operation(operation, "error", detail=result.detail)
            self.send_ready.emit(result)
            return
        try:
            policy_issue = mesh_send_policy_issue(self._db_path, request, transport=transport)
            if policy_issue:
                result = MeshSendResult(
                    request_id=request.request_id,
                    adapter_id=request.adapter_id,
                    transport=transport,
                    destination_kind=request.destination_kind,
                    destination_id=request.destination_id,
                    channel_id=request.channel_id,
                    state="failed",
                    requested_at=request.requested_at,
                    completed_at=utc_now(),
                    detail=policy_issue,
                    retryable=False,
                    evidence="policy_denied",
                )
            else:
                result = self._manager.send_message(request)
        except MeshOperationCancelled as exc:
            result = MeshSendResult(
                request_id=request.request_id,
                adapter_id=request.adapter_id,
                transport=transport,
                destination_kind=request.destination_kind,
                destination_id=request.destination_id,
                channel_id=request.channel_id,
                state="cancelled",
                requested_at=request.requested_at,
                completed_at=utc_now(),
                detail=str(exc) or "Mesh send was cancelled.",
                retryable=True,
                evidence="cancelled",
            )
        except Exception as exc:
            result = MeshSendResult(
                request_id=request.request_id,
                adapter_id=request.adapter_id,
                transport=transport,
                destination_kind=request.destination_kind,
                destination_id=request.destination_id,
                channel_id=request.channel_id,
                state="failed",
                requested_at=request.requested_at,
                completed_at=utc_now(),
                detail=str(exc),
                retryable=True,
                evidence="adapter_error",
            )
        try:
            append_mesh_send_audit(self._db_path, request, transport=transport, result=result)
        except Exception as exc:
            self.error_ready.emit(f"Mesh send result audit failed: {exc}")
            result = replace(
                result,
                detail=f"{result.detail} Final audit persistence failed: {exc}".strip(),
            )
        self._publish_operation(
            operation,
            "complete" if result.accepted else "cancelled" if result.state == "cancelled" else "error",
            current=1,
            total=1,
            detail=result.detail,
        )
        self.send_ready.emit(result)

    @Slot()
    def poll_once(self) -> None:
        if not self._running:
            return
        now_ms = self._elapsed_ms()
        should_reconnect = (now_ms - self._last_reconnect_ms) >= self._poll_interval_ms
        should_poll_nodes = (now_ms - self._last_node_poll_ms) >= self._node_poll_interval_ms
        should_poll_channels = (
            self._channel_poll_interval_ms is not None
            and (now_ms - self._last_channel_poll_ms) >= self._channel_poll_interval_ms
        )
        if should_reconnect:
            self._retry_disconnected_adapters()
            self._last_reconnect_ms = now_ms
        for adapter_id in self._manager.active_adapter_ids():
            try:
                self._manager.poll_events(adapter_id)
                if should_poll_nodes:
                    self._manager.poll_nodes(adapter_id)
                if should_poll_channels:
                    channels = self._manager.poll_channels(adapter_id)
                    if self._store_sink is not None:
                        if channels:
                            self._store_sink.stage_channels(channels)
                        self._store_sink.prune_retained_messages()
                snapshot = self._manager.health(adapter_id)
                self._handle_health(snapshot)
            except Exception as exc:
                self.error_ready.emit(str(exc))
        if should_poll_nodes:
            self._last_node_poll_ms = now_ms
        if should_poll_channels:
            self._last_channel_poll_ms = now_ms

    def manager(self) -> MeshConnectionManager:
        return self._manager

    def _handle_manager_event(self, event: MeshAdapterEvent) -> None:
        if self._store_sink is not None:
            try:
                self._store_sink(event)
            except Exception as exc:
                self.error_ready.emit(str(exc))
        if event.health is not None:
            self._handle_health(event.health)
        self.event_ready.emit(event)

    def _handle_health(self, snapshot: MeshHealthSnapshot) -> None:
        self.health_ready.emit(snapshot)

    def _retry_disconnected_adapters(self) -> None:
        now_ms = self._elapsed_ms()
        for adapter_id in self._manager.configured_ids():
            try:
                snapshot = self._manager.health(adapter_id)
                state = self._retry_states.setdefault(adapter_id, MeshRetryState())
                if (not snapshot.connected and not state.operator_action_required
                        and mesh_error_requires_operator_action(snapshot.last_error)):
                    state.record_operator_action_required()
                    self._persist_retry_state(adapter_id)
                if snapshot.enabled and not snapshot.connected and state.operator_action_required:
                    self.operation_state.emit(adapter_id, "needs-attention", 0)
                elif snapshot.enabled and not snapshot.connected and state.due(now_ms):
                    self._retry_adapter(adapter_id, now_ms)
                elif snapshot.enabled and not snapshot.connected:
                    self.operation_state.emit(adapter_id, "retrying", state.remaining_ms(now_ms))
            except Exception as exc:
                self.error_ready.emit(str(exc))

    def _retry_adapter(self, adapter_id: str, now_ms: int) -> None:
        self._attempt_connection(adapter_id, now_ms, force=False, emit_connecting=True)

    def _attempt_connection(
        self,
        adapter_id: str,
        now_ms: int,
        *,
        force: bool,
        emit_connecting: bool,
    ) -> None:
        if self._stop_event.is_set():
            return
        state = self._retry_states.setdefault(adapter_id, MeshRetryState())
        if not force and not state.due(now_ms):
            self.operation_state.emit(adapter_id, "retrying", state.remaining_ms(now_ms))
            return
        context_key = self._retry_context_keys.get(adapter_id, adapter_id)
        if not try_acquire_mesh_connect_attempt(context_key):
            delay = state.defer(now_ms, self._poll_interval_ms)
            self._persist_retry_state(adapter_id)
            self.operation_state.emit(adapter_id, "retrying", delay)
            return
        operation = self._begin_operation(adapter_id, "connect")
        if emit_connecting:
            self.operation_state.emit(adapter_id, "connecting", 0)
        try:
            snapshot = self._manager.start_adapter(adapter_id)
            connection_detail = self._record_connection_result(
                adapter_id,
                snapshot.connected,
                now_ms,
                snapshot.last_error,
            )
            if connection_detail == self.RETRY_EXHAUSTED_GUIDANCE:
                paused_snapshot = replace(
                    snapshot,
                    lifecycle_state="needs_attention",
                    guidance=self.RETRY_EXHAUSTED_GUIDANCE,
                )
                if self._store_sink is not None:
                    self._store_sink(
                        MeshAdapterEvent(
                            event_type="health",
                            adapter_id=paused_snapshot.adapter_id,
                            transport=paused_snapshot.transport,
                            health=paused_snapshot,
                        )
                    )
                self._handle_health(paused_snapshot)
            self._publish_operation(
                operation,
                "complete" if snapshot.connected else "error",
                detail=connection_detail or snapshot.last_error,
            )
        finally:
            release_mesh_connect_attempt(context_key)

    def _record_connection_result(
        self,
        adapter_id: str,
        connected: bool,
        now_ms: int,
        error: str = "",
    ) -> str:
        state = self._retry_states.setdefault(adapter_id, MeshRetryState())
        if connected:
            state.record_success()
            self._persist_retry_state(adapter_id, clear=True)
            self.operation_state.emit(adapter_id, "connected", 0)
            return ""
        if mesh_error_requires_operator_action(error):
            state.record_operator_action_required()
            self._persist_retry_state(adapter_id)
            self.operation_state.emit(adapter_id, "needs-attention", 0)
            if error:
                self.error_ready.emit(str(error))
            return str(error or "Mesh pairing requires operator attention.")
        delay = state.record_failure(now_ms, self._retry_policy)
        self._persist_retry_state(adapter_id)
        if state.operator_action_required:
            self.operation_state.emit(adapter_id, "needs-attention", 0)
        else:
            self.operation_state.emit(adapter_id, "retrying", delay)
        if error:
            self.error_ready.emit(str(error))
        if state.operator_action_required:
            return self.RETRY_EXHAUSTED_GUIDANCE
        return str(error or "")

    def _begin_operation(self, source_id: str, request_class: str) -> MeshOperationSnapshot:
        key = (str(source_id), str(request_class))
        with self._operation_lock:
            previous = self._active_operations.get(key)
            previous_cancel = self._operation_cancel_events.get(key)
            if previous_cancel is not None:
                previous_cancel[1].set()
            self._operation_sequence += 1
            operation = MeshOperationSnapshot(
                operation_id=f"mesh-{self._operation_sequence}",
                source_id=key[0],
                request_class=key[1],
                state="running",
                started_monotonic_ms=self._elapsed_ms(),
                supersedes=previous.operation_id if previous is not None else "",
            )
            self._active_operations[key] = operation
            self._operation_cancel_events[key] = (operation.operation_id, threading.Event())
        self.operation_ready.emit(operation)
        return operation

    def _publish_operation(
        self,
        operation: MeshOperationSnapshot,
        state: str,
        *,
        current: int = 0,
        total: int = 0,
        detail: str = "",
    ) -> None:
        now_ms = self._elapsed_ms()
        terminal = state in {"complete", "cancelled", "error"}
        snapshot = MeshOperationSnapshot(
            operation_id=operation.operation_id,
            source_id=operation.source_id,
            request_class=operation.request_class,
            state=state,
            progress_current=max(0, int(current)),
            progress_total=max(0, int(total)),
            started_monotonic_ms=operation.started_monotonic_ms,
            completed_monotonic_ms=now_ms if terminal else 0,
            elapsed_ms=max(0, now_ms - operation.started_monotonic_ms),
            supersedes=operation.supersedes,
            detail=str(detail or ""),
        )
        with self._operation_lock:
            self._active_operations[(snapshot.source_id, snapshot.request_class)] = snapshot
            if terminal:
                key = (snapshot.source_id, snapshot.request_class)
                cancel_entry = self._operation_cancel_events.get(key)
                if cancel_entry is not None and cancel_entry[0] == snapshot.operation_id:
                    self._operation_cancel_events.pop(key, None)
        self.operation_ready.emit(snapshot)

    @staticmethod
    def _elapsed_ms() -> int:
        return monotonic_ns() // 1_000_000

    def _retry_context_key(self, config: MeshConnectionConfig) -> tuple[object, ...]:
        # Include the factory identity so isolated tests/custom adapters and
        # unrelated runtime profiles cannot inherit one another's backoff.
        return (
            id(self._adapter_factory),
            str(config.adapter_id),
            str(config.protocol),
            str(config.connection_type.value),
            str(config.endpoint_address),
            int(config.tcp_port),
            int(config.serial_baud),
            int(config.ble_scan_timeout_sec),
            str(config.http_base_url),
            str(config.mqtt_broker),
            str(config.mqtt_topic_root),
        )

    def _persist_retry_state(self, adapter_id: str, *, clear: bool = False) -> None:
        key = self._retry_context_keys.get(adapter_id, adapter_id)
        if clear:
            # A successful connection consumes/overwrites any stale handoff.
            take_mesh_retry_handoff(key)
            return
        save_mesh_retry_handoff(key, self._retry_states.setdefault(adapter_id, MeshRetryState()))
