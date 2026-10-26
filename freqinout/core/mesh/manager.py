from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import math
import sys
import threading

from freqinout.core.mesh.adapter_base import MeshAdapter
from freqinout.core.mesh.meshcore_adapter import MeshCorePythonAdapter
from freqinout.core.mesh.meshtastic_adapter import MeshConnectionError, MeshtasticLocalAdapter
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
from freqinout.core.mesh.settings import MeshConnectionConfig, MeshConnectionType, mesh_transport_capability

MeshEventListener = Callable[[MeshAdapterEvent], None]
MeshAdapterFactory = Callable[[MeshConnectionConfig], MeshAdapter]


def default_mesh_adapter_factory(config: MeshConnectionConfig) -> MeshAdapter:
    protocol = config.protocol.strip().lower()
    capability = mesh_transport_capability(protocol, config.connection_type)
    if not capability.supported:
        raise MeshConnectionError(capability.reason)
    if protocol == "meshtastic":
        return MeshtasticLocalAdapter(config)
    if protocol == "meshcore":
        if sys.platform.startswith("linux") and config.connection_type is MeshConnectionType.BLE:
            from freqinout.core.mesh.linux_ble_supervisor import LinuxMeshCoreBleSupervisorAdapter

            return LinuxMeshCoreBleSupervisorAdapter(config)
        return MeshCorePythonAdapter(config)
    raise MeshConnectionError(f"{config.protocol or 'Mesh'} adapters are configured for a later implementation slice.")


class MeshConnectionManager:
    """Protocol-neutral mesh adapter lifecycle without Qt dependencies."""

    def __init__(
        self,
        configs: Iterable[MeshConnectionConfig] = (),
        *,
        adapter_factory: MeshAdapterFactory = default_mesh_adapter_factory,
    ) -> None:
        self._configs: dict[str, MeshConnectionConfig] = {}
        self._adapters: dict[str, MeshAdapter] = {}
        self._last_errors: dict[str, str] = {}
        self._listeners: list[MeshEventListener] = []
        self._adapter_factory = adapter_factory
        # Cancellation may be requested directly by the GUI thread while the
        # worker thread is inside a device call. Protect adapter publication and
        # snapshots without moving normal device work across threads.
        self._adapter_lock = threading.RLock()
        self._send_locks: dict[str, threading.Lock] = {}
        for config in configs:
            self.upsert_config(config)

    def upsert_config(self, config: MeshConnectionConfig) -> None:
        existing = self._configs.get(config.adapter_id)
        self._configs[config.adapter_id] = config
        if existing is not None and existing != config:
            self.stop_adapter(config.adapter_id)
            with self._adapter_lock:
                self._adapters.pop(config.adapter_id, None)

    def remove_config(self, adapter_id: str) -> None:
        self.stop_adapter(adapter_id)
        self._configs.pop(adapter_id, None)
        with self._adapter_lock:
            self._adapters.pop(adapter_id, None)
        self._last_errors.pop(adapter_id, None)
        self._send_locks.pop(adapter_id, None)

    def configured_ids(self) -> tuple[str, ...]:
        return tuple(self._configs)

    def active_adapter_ids(self) -> tuple[str, ...]:
        with self._adapter_lock:
            return tuple(self._adapters)

    def add_listener(self, listener: MeshEventListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: MeshEventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def start_all(self) -> dict[str, MeshHealthSnapshot]:
        return {
            adapter_id: self.start_adapter(adapter_id)
            for adapter_id, config in self._configs.items()
            if config.enabled
        }

    def start_adapter(self, adapter_id: str) -> MeshHealthSnapshot:
        config = self._require_config(adapter_id)
        if not config.enabled:
            snapshot = self._snapshot_for_config(config, connected=False, last_error="Mesh connection is disabled.")
            self._publish_health(snapshot)
            return snapshot

        with self._adapter_lock:
            adapter = self._adapters.get(adapter_id)
        if adapter is None:
            try:
                adapter = self._adapter_factory(config)
                with self._adapter_lock:
                    self._adapters[adapter_id] = adapter
            except Exception as exc:
                snapshot = self._snapshot_for_config(config, connected=False, last_error=str(exc))
                self._last_errors[adapter_id] = snapshot.last_error
                self._publish_health(snapshot)
                return snapshot

        try:
            adapter.connect()
            self._last_errors.pop(adapter_id, None)
            snapshot = adapter.health()
        except Exception as exc:
            snapshot = self._snapshot_for_config(config, connected=False, last_error=str(exc))
            self._last_errors[adapter_id] = snapshot.last_error
        self._publish_health(snapshot)
        return snapshot

    def stop_all(self) -> dict[str, MeshHealthSnapshot]:
        return {adapter_id: self.stop_adapter(adapter_id) for adapter_id in tuple(self._configs)}

    def stop_adapter(self, adapter_id: str) -> MeshHealthSnapshot:
        config = self._require_config(adapter_id)
        with self._adapter_lock:
            adapter = self._adapters.get(adapter_id)
        if adapter is not None:
            try:
                adapter.disconnect()
            except Exception as exc:
                self._last_errors[adapter_id] = str(exc)
        snapshot = self.health(adapter_id)
        self._publish_health(snapshot)
        return snapshot

    def health(self, adapter_id: str) -> MeshHealthSnapshot:
        config = self._require_config(adapter_id)
        with self._adapter_lock:
            adapter = self._adapters.get(adapter_id)
        if adapter is not None:
            try:
                snapshot = adapter.health()
                last_error = self._last_errors.get(adapter_id, "")
                if last_error and not snapshot.last_error:
                    return MeshHealthSnapshot(
                        adapter_id=snapshot.adapter_id,
                        transport=snapshot.transport,
                        enabled=snapshot.enabled,
                        connected=snapshot.connected,
                        connection_type=snapshot.connection_type,
                        device_name=snapshot.device_name,
                        firmware_version=snapshot.firmware_version,
                        battery_percent=snapshot.battery_percent,
                        battery_voltage=snapshot.battery_voltage,
                        last_rx=snapshot.last_rx,
                        last_tx=snapshot.last_tx,
                        last_error=last_error,
                        warnings=snapshot.warnings,
                    )
                return snapshot
            except Exception as exc:
                self._last_errors[adapter_id] = str(exc)
        return self._snapshot_for_config(config, connected=False, last_error=self._last_errors.get(adapter_id, ""))

    def health_snapshots(self) -> tuple[MeshHealthSnapshot, ...]:
        return tuple(self.health(adapter_id) for adapter_id in self._configs)

    def ingest_packet(self, adapter_id: str, packet: Mapping[str, object]) -> MeshMessage | None:
        adapter = self._require_adapter(adapter_id)
        ingest = getattr(adapter, "ingest_packet", None)
        if not callable(ingest):
            raise MeshConnectionError(f"{adapter_id} does not support packet ingestion.")
        message = ingest(packet)
        if message is not None:
            events = tuple(adapter.receive_events())
            if events:
                for event in events:
                    self._publish(event)
            else:
                self._publish(
                    MeshAdapterEvent(
                        event_type="message",
                        adapter_id=adapter_id,
                        transport=message.transport,
                        message=message,
                        raw=message.raw,
                    )
                )
        return message

    def poll_events(self, adapter_id: str) -> tuple[MeshAdapterEvent, ...]:
        adapter = self._require_adapter(adapter_id)
        events = tuple(adapter.receive_events())
        for event in events:
            self._publish(event)
        return events

    def poll_nodes(self, adapter_id: str) -> tuple[MeshNode, ...]:
        adapter = self._require_adapter(adapter_id)
        nodes = tuple(adapter.list_nodes())
        for node in nodes:
            self._publish(
                MeshAdapterEvent(
                    event_type="node",
                    adapter_id=adapter_id,
                    transport=node.transport,
                    node=node,
                    raw=node.raw,
                )
            )
        return nodes

    def poll_channels(self, adapter_id: str) -> tuple[MeshChannel, ...]:
        adapter = self._require_adapter(adapter_id)
        return tuple(adapter.list_channels())

    def poll_channels_incremental(
        self,
        adapter_id: str,
        on_channel: Callable[[MeshChannel], None],
        *,
        cancel_event: object | None = None,
    ) -> tuple[MeshChannel, ...]:
        adapter = self._require_adapter(adapter_id)
        incremental = getattr(adapter, "list_channels_incremental", None)
        if callable(incremental):
            return tuple(incremental(on_channel, cancel_event=cancel_event))
        channels = tuple(adapter.list_channels())
        for channel in channels:
            on_channel(channel)
        return channels

    def cancel_pending_operations(self) -> None:
        """Thread-safe best-effort cancellation used before worker shutdown."""

        with self._adapter_lock:
            adapters = tuple(self._adapters.values())
        for adapter in adapters:
            cancel = getattr(adapter, "cancel_pending_operation", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    continue

    def channel_capabilities(self, adapter_id: str) -> MeshChannelCapabilities:
        adapter = self._require_adapter(adapter_id)
        getter = getattr(adapter, "channel_capabilities", None)
        if not callable(getter):
            return MeshChannelCapabilities()
        capabilities = getter()
        return capabilities if isinstance(capabilities, MeshChannelCapabilities) else MeshChannelCapabilities()

    def configure_channel(
        self,
        adapter_id: str,
        channel_id: str,
        updates: Mapping[str, object],
    ) -> MeshChannel:
        adapter = self._require_adapter(adapter_id)
        configure = getattr(adapter, "configure_channel", None)
        capabilities = self.channel_capabilities(adapter_id)
        if not capabilities.can_configure or not callable(configure):
            raise MeshConnectionError(capabilities.guidance)
        safe_updates = {
            str(key): value
            for key, value in updates.items()
            if str(key) in {"name", "role", "uplink_enabled", "downlink_enabled"}
        }
        return configure(str(channel_id), safe_updates)

    def remove_channel_from_device(self, adapter_id: str, channel_id: str) -> None:
        adapter = self._require_adapter(adapter_id)
        remove = getattr(adapter, "remove_channel", None)
        capabilities = self.channel_capabilities(adapter_id)
        if not capabilities.can_remove_from_device or not callable(remove):
            raise MeshConnectionError(capabilities.guidance)
        remove(str(channel_id))

    def send_capabilities(self, adapter_id: str) -> MeshSendCapabilities:
        adapter = self._require_adapter(adapter_id)
        getter = getattr(adapter, "send_capabilities", None)
        if not callable(getter):
            return MeshSendCapabilities()
        capabilities = getter()
        return capabilities if isinstance(capabilities, MeshSendCapabilities) else MeshSendCapabilities()

    def send_message(self, request: MeshSendRequest) -> MeshSendResult:
        if not isinstance(request, MeshSendRequest):
            raise MeshConnectionError("Mesh send request is invalid.")
        adapter_id = str(request.adapter_id or "").strip()
        config = self._require_config(adapter_id)
        if request.expected_connection_fingerprint:
            from freqinout.core.mesh.settings import mesh_connection_fingerprint
            from datetime import datetime, timezone
            if mesh_connection_fingerprint(config) != request.expected_connection_fingerprint:
                raise MeshConnectionError("The reviewed Mesh connection changed before dispatch.")
            age = (datetime.now(timezone.utc) - request.requested_at).total_seconds()
            if age < 0 or age > request.timeout_sec:
                raise MeshConnectionError("The manual forwarding request expired before dispatch.")
            from freqinout.core.manual_forward_runtime import forward_authorization_reason
            issue = forward_authorization_reason(request.forwarding_db_path, request.forwarding_relay_id, request.forwarding_attempt)
            if issue:
                raise MeshConnectionError(issue)
        if not config.enabled:
            raise MeshConnectionError("This mesh connection is disabled.")
        if not config.send_enabled:
            raise MeshConnectionError("Sending is not enabled for this mesh connection.")
        text = str(request.text or "")
        if not text.strip():
            raise MeshConnectionError("Enter a mesh message before sending.")
        try:
            timeout_sec = float(request.timeout_sec)
        except (TypeError, ValueError) as exc:
            raise MeshConnectionError("Mesh send timeout is invalid.") from exc
        if not math.isfinite(timeout_sec) or timeout_sec < 0.1 or timeout_sec > 30.0:
            raise MeshConnectionError("Mesh send timeout must be between 0.1 and 30 seconds.")
        kind = str(request.destination_kind or "").strip().lower()
        if kind == "direct" and not str(request.destination_id or "").strip():
            raise MeshConnectionError("Choose a node for a direct mesh message.")
        if kind in {"channel", "broadcast"} and not str(request.channel_id or "").strip():
            raise MeshConnectionError("Choose a channel for this mesh message.")
        if kind not in {"direct", "channel", "broadcast"}:
            raise MeshConnectionError("Mesh destination kind must be direct, channel, or broadcast.")
        adapter = self._require_adapter(adapter_id)
        capabilities = self.send_capabilities(adapter_id)
        if request.expected_connection_fingerprint:
            limit = int(capabilities.max_text_bytes or 0)
            if limit <= 0 or len(text.encode("utf-8")) > limit:
                raise MeshConnectionError("The forwarded payload exceeds the selected adapter's verified byte budget, or its budget is unavailable.")
        if not capabilities.supported:
            raise MeshConnectionError(capabilities.guidance)
        if kind == "direct" and not capabilities.direct_send:
            raise MeshConnectionError("This mesh connection does not support direct-node sending.")
        if kind in {"channel", "broadcast"} and not capabilities.channel_send:
            raise MeshConnectionError("This mesh connection does not support channel sending.")
        snapshot = adapter.health()
        if not snapshot.connected:
            raise MeshConnectionError("Connect this mesh device before sending.")
        with self._adapter_lock:
            lock = self._send_locks.setdefault(adapter_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise MeshConnectionError("A message is already being sent through this mesh connection.")
        try:
            send = getattr(adapter, "send_message", None)
            if not callable(send):
                raise MeshConnectionError("This mesh adapter does not implement sending.")
            if request.expected_connection_fingerprint:
                # Health/capability discovery above may have taken time. Recheck
                # the durable lease at the actual adapter boundary as well.
                issue = forward_authorization_reason(request.forwarding_db_path, request.forwarding_relay_id, request.forwarding_attempt)
                age = (datetime.now(timezone.utc) - request.requested_at).total_seconds()
                if issue or age < 0 or age > request.timeout_sec:
                    raise MeshConnectionError(issue or "The forwarding request expired before adapter submission.")
            result = send(request)
            if not isinstance(result, MeshSendResult):
                raise MeshConnectionError("Mesh adapter returned an invalid send result.")
            return result
        finally:
            lock.release()

    def _require_config(self, adapter_id: str) -> MeshConnectionConfig:
        try:
            return self._configs[adapter_id]
        except KeyError as exc:
            raise KeyError(f"Unknown mesh adapter: {adapter_id}") from exc

    def _require_adapter(self, adapter_id: str) -> MeshAdapter:
        with self._adapter_lock:
            try:
                return self._adapters[adapter_id]
            except KeyError as exc:
                raise MeshConnectionError(f"Mesh adapter {adapter_id} is not started.") from exc

    def _snapshot_for_config(
        self,
        config: MeshConnectionConfig,
        *,
        connected: bool,
        last_error: str = "",
    ) -> MeshHealthSnapshot:
        return MeshHealthSnapshot(
            adapter_id=config.adapter_id,
            transport=config.protocol or "mesh",
            enabled=config.enabled,
            connected=connected,
            connection_type=config.connection_type.value,
            device_name=config.display_name,
            last_error=last_error,
        )

    def _publish_health(self, snapshot: MeshHealthSnapshot) -> None:
        self._publish(
            MeshAdapterEvent(
                event_type="health",
                adapter_id=snapshot.adapter_id,
                transport=snapshot.transport,
                health=snapshot,
            )
        )

    def _publish(self, event: MeshAdapterEvent) -> None:
        cache = None
        if event.node is not None:
            # Contacts are periodically resynchronized. Identical retained
            # state does not warrant another database write or map revision.
            cache = getattr(self, "_published_nodes", None)
            if cache is None:
                from collections import OrderedDict
                cache = self._published_nodes = OrderedDict()
            key = (event.node.adapter_id, event.node.transport, event.node.node_id)
            if cache.get(key) == event.node:
                return
        for listener in tuple(self._listeners):
            listener(event)
        # Cache only after sinks accepted the event; a failed write must be
        # retried by an identical subsequent contact synchronization.
        if cache is not None:
            cache[key] = event.node
            cache.move_to_end(key)
            while len(cache) > 4096:
                cache.popitem(last=False)
