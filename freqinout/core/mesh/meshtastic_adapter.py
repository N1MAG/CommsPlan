from __future__ import annotations

import sys
import math
import threading
from datetime import datetime, timezone
from importlib import import_module, util
from typing import Iterator, Mapping, Sequence

from freqinout.core.mesh.lifecycle import MeshOperationCancelled
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
    utc_now,
)
from freqinout.core.mesh.settings import MeshConnectionConfig, MeshConnectionType, validate_mesh_connection_config


class MeshConnectionError(RuntimeError):
    pass


def meshtastic_python_available() -> bool:
    if "meshtastic" in sys.modules:
        return True
    try:
        return util.find_spec("meshtastic") is not None
    except ValueError:
        return False


class MeshtasticLocalAdapter:
    transport_name = "meshtastic"

    def __init__(self, config: MeshConnectionConfig) -> None:
        self.config = config
        self.adapter_id = config.adapter_id
        self._interface: object | None = None
        self._last_error = ""
        self._messages: list[MeshMessage] = []
        self._events: list[MeshAdapterEvent] = []
        self._pub_module: object | None = None
        self._subscribed_topics: tuple[str, ...] = ()
        self._send_lock = threading.Lock()
        self._send_cancel_event = threading.Event()

    def connect(self) -> None:
        if not self.config.enabled:
            raise MeshConnectionError("Meshtastic adapter is disabled.")
        issues = tuple(issue for issue in validate_mesh_connection_config(self.config) if issue.severity == "error")
        if issues:
            raise MeshConnectionError("; ".join(issue.message for issue in issues))
        if not meshtastic_python_available():
            raise MeshConnectionError("Meshtastic Python package is not installed; configure the dependency before connecting.")

        module_path, class_name, kwargs, positional = self._interface_spec()
        module = import_module(module_path)
        factory = getattr(module, class_name)
        try:
            self._interface = factory(**kwargs)
        except TypeError:
            self._interface = factory(*positional)
        self._subscribe_receive_events()
        self._send_cancel_event.clear()
        self._last_error = ""

    def disconnect(self) -> None:
        self._send_cancel_event.set()
        interface = self._interface
        self._unsubscribe_receive_events()
        self._interface = None
        if interface is None:
            return
        close = getattr(interface, "close", None)
        if callable(close):
            close()

    def cancel_pending_operation(self) -> None:
        # Meshtastic constructors are synchronous. Closing a partially-created
        # interface is the only safe cross-version cancellation hook.
        interface = self._interface
        self._send_cancel_event.set()
        close = getattr(interface, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def send_capabilities(self) -> MeshSendCapabilities:
        return MeshSendCapabilities(
            supported=True,
            channel_send=True,
            direct_send=True,
            max_text_bytes=220,
            guidance=(
                "Meshtastic channel sends prove local API acceptance. Direct sends request a routing acknowledgement; "
                "an acknowledgement proves mesh receipt, not that a person read the message."
            ),
        )

    def send_message(self, request: MeshSendRequest) -> MeshSendResult:
        with self._send_lock:
            return self._send_message_locked(request)

    def _send_message_locked(self, request: MeshSendRequest) -> MeshSendResult:
        interface = self._interface
        if interface is None:
            raise MeshConnectionError("Meshtastic is not connected.")
        text = str(request.text or "")
        if not text.strip():
            raise MeshConnectionError("Enter a mesh message before sending.")
        if len(text.encode("utf-8")) > 220:
            raise MeshConnectionError("Meshtastic message is longer than CommsPlan's qualified 220-byte text limit.")
        try:
            channel_index = int(str(request.channel_id or "0").strip() or "0")
        except ValueError as exc:
            raise MeshConnectionError("Meshtastic channel must be a numeric channel index.") from exc
        if channel_index < 0 or channel_index > 7:
            raise MeshConnectionError("Meshtastic channel index must be between 0 and 7.")
        kind = str(request.destination_kind or "").strip().lower()
        if kind not in {"channel", "direct", "broadcast"}:
            raise MeshConnectionError("Choose a Meshtastic channel, broadcast, or direct-node destination.")
        destination = "^all" if kind in {"channel", "broadcast"} else str(request.destination_id or "").strip()
        if kind == "direct" and not destination:
            raise MeshConnectionError("Choose a Meshtastic node for a direct message.")

        self._send_cancel_event.clear()
        response_event = threading.Event()
        response_holder: dict[str, object] = {}

        def _on_response(packet: object) -> None:
            response_holder["packet"] = packet
            response_event.set()

        send_data = getattr(interface, "sendData", None)
        if not callable(send_data):
            raise MeshConnectionError("The installed Meshtastic client does not expose text-message sending.")
        want_ack = bool(kind == "direct" and request.require_ack)
        response_handlers = getattr(interface, "responseHandlers", None)
        handler_keys_before = set(response_handlers) if isinstance(response_handlers, dict) else set()
        try:
            packet = send_data(
                text.encode("utf-8"),
                destinationId=destination,
                portNum=1,
                wantAck=want_ack,
                wantResponse=False,
                onResponse=_on_response if want_ack else None,
                onResponseAckPermitted=want_ack,
                channelIndex=channel_index,
            )
        except Exception as exc:
            if isinstance(response_handlers, dict):
                for key in set(response_handlers) - handler_keys_before:
                    response_handlers.pop(key, None)
            raise MeshConnectionError(f"Meshtastic did not accept the send request: {exc}") from exc

        accepted_at = utc_now()
        native_id = str(getattr(packet, "id", "") or "")
        if not want_ack:
            return MeshSendResult(
                request_id=request.request_id,
                adapter_id=self.adapter_id,
                transport=self.transport_name,
                destination_kind=kind,
                destination_id=destination if kind == "direct" else "",
                channel_id=str(channel_index),
                state="accepted",
                native_message_id=native_id,
                requested_at=request.requested_at,
                accepted_at=accepted_at,
                detail="Meshtastic accepted the message for local radio transmission.",
                evidence="api_accepted",
            )

        try:
            requested_timeout = float(request.timeout_sec or 12.0)
        except (TypeError, ValueError) as exc:
            raise MeshConnectionError("Meshtastic acknowledgement timeout is invalid.") from exc
        if not math.isfinite(requested_timeout):
            raise MeshConnectionError("Meshtastic acknowledgement timeout must be finite.")
        deadline = max(0.1, min(30.0, requested_timeout))
        response_event.wait(timeout=deadline)
        handlers = getattr(interface, "responseHandlers", None)
        if isinstance(handlers, dict) and native_id:
            for key in (native_id, _optional_int(native_id)):
                if key is not None:
                    handlers.pop(key, None)
        if self._send_cancel_event.is_set() and not response_event.is_set():
            raise MeshOperationCancelled("Meshtastic send was cancelled before acknowledgement.")
        if not response_event.is_set():
            return MeshSendResult(
                request_id=request.request_id,
                adapter_id=self.adapter_id,
                transport=self.transport_name,
                destination_kind=kind,
                destination_id=destination,
                channel_id=str(channel_index),
                state="accepted",
                native_message_id=native_id,
                requested_at=request.requested_at,
                accepted_at=accepted_at,
                detail="Meshtastic accepted the direct message, but no routing acknowledgement arrived before timeout.",
                retryable=False,
                evidence="api_accepted_ack_timeout",
            )
        response = response_holder.get("packet")
        error_reason = _meshtastic_response_error(response)
        if error_reason:
            return MeshSendResult(
                request_id=request.request_id,
                adapter_id=self.adapter_id,
                transport=self.transport_name,
                destination_kind=kind,
                destination_id=destination,
                channel_id=str(channel_index),
                state="failed",
                native_message_id=native_id,
                requested_at=request.requested_at,
                accepted_at=accepted_at,
                completed_at=utc_now(),
                detail=f"Meshtastic routing failed: {error_reason}",
                retryable=True,
                evidence="routing_error",
            )
        acknowledged_at = utc_now()
        return MeshSendResult(
            request_id=request.request_id,
            adapter_id=self.adapter_id,
            transport=self.transport_name,
            destination_kind=kind,
            destination_id=destination,
            channel_id=str(channel_index),
            state="acked",
            native_message_id=native_id,
            requested_at=request.requested_at,
            accepted_at=accepted_at,
            completed_at=acknowledged_at,
            acknowledged_at=acknowledged_at,
            detail="Meshtastic returned a routing acknowledgement for the direct message.",
            evidence="routing_ack",
        )

    def channel_capabilities(self) -> MeshChannelCapabilities:
        return MeshChannelCapabilities(
            guidance="Use the Meshtastic companion application to configure or remove device channels.",
        )

    def health(self) -> MeshHealthSnapshot:
        device_name = self.config.display_name
        return MeshHealthSnapshot(
            adapter_id=self.adapter_id,
            transport=self.transport_name,
            enabled=self.config.enabled,
            connected=self._interface is not None,
            connection_type=self.config.connection_type.value,
            device_name=device_name,
            last_error=self._last_error,
            warnings=tuple(issue.message for issue in validate_mesh_connection_config(self.config) if issue.severity != "error"),
        )

    def list_nodes(self) -> list[MeshNode]:
        interface = self._interface
        nodes = getattr(interface, "nodes", {}) if interface is not None else {}
        if not isinstance(nodes, Mapping):
            return []
        return [self._node_from_mapping(node_id, details) for node_id, details in nodes.items()]

    def list_channels(self) -> list[MeshChannel]:
        local_node = getattr(self._interface, "localNode", None)
        channels = getattr(local_node, "channels", None)
        if not channels:
            return []
        result: list[MeshChannel] = []
        for index, channel in enumerate(channels):
            settings = getattr(channel, "settings", None)
            configured_name = str(getattr(settings, "name", "") or "").strip()
            name = configured_name or ("Public" if index == 0 else f"Channel {index}")
            role = _enum_name(getattr(channel, "role", "") or "")
            psk = getattr(settings, "psk", None)
            has_key = _has_private_key(psk)
            privacy = "public" if index == 0 and not has_key else ("private" if has_key or index > 0 else "unknown")
            result.append(
                MeshChannel(
                    self.adapter_id,
                    self.transport_name,
                    index,
                    name,
                    role=role,
                    channel_id=str(index),
                    privacy=privacy,
                    psk_hint="on device" if has_key else ("not needed" if privacy == "public" else ""),
                )
            )
        return sorted(result, key=lambda item: (_generated_channel_sort(item.name), item.index))

    def ingest_packet(self, packet: Mapping[str, object] | object) -> MeshMessage | None:
        event = self.normalize_packet(packet)
        if event is None:
            return None
        if event.message is not None:
            message = event.message
            self._messages.append(message)
        self._events.append(event)
        return event.message

    def get_recent_messages(self) -> list[MeshMessage]:
        return list(self._messages)

    def receive_events(self) -> Iterator[MeshAdapterEvent]:
        events = tuple(self._events)
        self._events.clear()
        yield from events

    def _on_receive(self, packet: Mapping[str, object] | object, interface: object | None = None) -> None:
        self.ingest_packet(packet)

    def _subscribe_receive_events(self) -> None:
        try:
            pub = import_module("pubsub").pub
        except Exception:
            return
        subscribed: list[str] = []
        for topic in ("meshtastic.receive.text", "meshtastic.receive"):
            try:
                pub.subscribe(self._on_receive, topic)
                subscribed.append(topic)
            except Exception:
                continue
        self._pub_module = pub
        self._subscribed_topics = tuple(subscribed)

    def _unsubscribe_receive_events(self) -> None:
        pub = self._pub_module
        if pub is None:
            self._subscribed_topics = ()
            return
        for topic in self._subscribed_topics:
            try:
                pub.unsubscribe(self._on_receive, topic)
            except Exception:
                continue
        self._pub_module = None
        self._subscribed_topics = ()

    def normalize_text_packet(self, packet: Mapping[str, object] | object) -> MeshMessage | None:
        packet = _packet_mapping(packet)
        decoded_map = _decoded_mapping(packet)
        portnum = _packet_portnum(packet)
        text = _packet_text(decoded_map, packet)
        if not text:
            return None
        rx_time = _datetime_from_packet_time(packet.get("rxTime") or packet.get("rx_time"))
        from_node = _packet_node_id(packet, "from")
        to_node = _packet_node_id(packet, "to")
        packet_id = str(packet.get("id") or packet.get("packetId") or f"{from_node}:{rx_time.timestamp() if rx_time else ''}")
        is_direct = bool(to_node and not _is_broadcast_target(to_node))
        return MeshMessage(
            adapter_id=self.adapter_id,
            transport=self.transport_name,
            message_id=packet_id,
            text=text,
            from_node=from_node,
            to_node=to_node,
            channel="Direct" if is_direct else _packet_channel_name(packet),
            portnum=portnum,
            rx_time=rx_time,
            hop_count=_packet_hop_count(packet),
            route_type=str(packet.get("route_type") or packet.get("rxRoute") or ""),
            direct_receive=True if is_direct else _optional_bool(packet.get("direct_receive") or packet.get("directReceive")),
            via_node=str(packet.get("via_node") or packet.get("viaNode") or ""),
            path_hops=_string_tuple(packet.get("path_hops") or packet.get("pathHops")),
            snr=_optional_float(packet.get("rxSnr") or packet.get("snr")),
            rssi=_optional_float(packet.get("rxRssi") or packet.get("rssi")),
            lat=_optional_float(packet.get("lat")),
            lon=_optional_float(packet.get("lon")),
            grid=str(packet.get("grid") or ""),
            raw=packet,
        )

    def normalize_packet(self, packet: Mapping[str, object] | object) -> MeshAdapterEvent | None:
        packet = _packet_mapping(packet)
        if not packet:
            return None
        portnum = _packet_portnum(packet)
        if portnum == "POSITION_APP":
            node = self.normalize_position_packet(packet)
            if node is None:
                return None
            return MeshAdapterEvent(
                event_type="node",
                adapter_id=self.adapter_id,
                transport=self.transport_name,
                node=node,
                raw=packet,
            )
        if portnum == "NODEINFO_APP":
            node = self.normalize_nodeinfo_packet(packet)
            if node is None:
                return None
            return MeshAdapterEvent(
                event_type="node",
                adapter_id=self.adapter_id,
                transport=self.transport_name,
                node=node,
                raw=packet,
            )
        if portnum == "TEXT_MESSAGE_APP" or (not portnum and _packet_text(_decoded_mapping(packet), packet)):
            message = self.normalize_text_packet(packet)
            if message is None:
                return None
            return MeshAdapterEvent(
                event_type="message",
                adapter_id=self.adapter_id,
                transport=self.transport_name,
                message=message,
                raw=packet,
            )
        return None

    def normalize_position_packet(self, packet: Mapping[str, object] | object) -> MeshNode | None:
        packet = _packet_mapping(packet)
        decoded_map = _decoded_mapping(packet)
        position = _object_mapping(decoded_map.get("position") or packet.get("position"))
        if not position:
            return None
        lat, lon = _position_lat_lon(position)
        node_id = _packet_node_id(packet, "from")
        if not node_id and lat is None and lon is None:
            return None
        return MeshNode(
            adapter_id=self.adapter_id,
            transport=self.transport_name,
            node_id=node_id,
            last_heard=_datetime_from_packet_time(packet.get("rxTime") or packet.get("rx_time")),
            hop_count=_packet_hop_count(packet),
            route_type=str(packet.get("route_type") or packet.get("rxRoute") or ""),
            direct_receive=_optional_bool(packet.get("direct_receive") or packet.get("directReceive")),
            via_node=str(packet.get("via_node") or packet.get("viaNode") or ""),
            path_hops=_string_tuple(packet.get("path_hops") or packet.get("pathHops")),
            snr=_optional_float(packet.get("rxSnr") or packet.get("snr")),
            rssi=_optional_float(packet.get("rxRssi") or packet.get("rssi")),
            lat=lat,
            lon=lon,
            raw=packet,
        )

    def normalize_nodeinfo_packet(self, packet: Mapping[str, object] | object) -> MeshNode | None:
        packet = _packet_mapping(packet)
        decoded_map = _decoded_mapping(packet)
        user = _object_mapping(decoded_map.get("user") or packet.get("user"))
        if not user:
            return None
        node_id = str(user.get("id") or _packet_node_id(packet, "from") or "").strip()
        if not node_id:
            return None
        return MeshNode(
            adapter_id=self.adapter_id,
            transport=self.transport_name,
            node_id=node_id,
            long_name=str(user.get("longName") or user.get("long_name") or ""),
            short_name=str(user.get("shortName") or user.get("short_name") or ""),
            public_key_or_hash=str(user.get("publicKey") or user.get("public_key") or ""),
            role=str(user.get("role") or ""),
            last_heard=_datetime_from_packet_time(packet.get("rxTime") or packet.get("rx_time")),
            hop_count=_packet_hop_count(packet),
            route_type=str(packet.get("route_type") or packet.get("rxRoute") or ""),
            direct_receive=_optional_bool(packet.get("direct_receive") or packet.get("directReceive")),
            via_node=str(packet.get("via_node") or packet.get("viaNode") or ""),
            path_hops=_string_tuple(packet.get("path_hops") or packet.get("pathHops")),
            snr=_optional_float(packet.get("rxSnr") or packet.get("snr")),
            rssi=_optional_float(packet.get("rxRssi") or packet.get("rssi")),
            raw=packet,
        )

    def _interface_spec(self) -> tuple[str, str, dict[str, object], tuple[object, ...]]:
        connection_type = self.config.connection_type
        if connection_type is MeshConnectionType.SERIAL:
            return (
                "meshtastic.serial_interface",
                "SerialInterface",
                {"devPath": self.config.serial_port},
                (self.config.serial_port,),
            )
        if connection_type is MeshConnectionType.BLE:
            device = self.config.ble_device_id or self.config.ble_device_name
            return (
                "meshtastic.ble_interface",
                "BLEInterface",
                {"address": device},
                (device,),
            )
        if connection_type is MeshConnectionType.TCP:
            return (
                "meshtastic.tcp_interface",
                "TCPInterface",
                {"hostname": self.config.tcp_host, "portNumber": self.config.tcp_port},
                (self.config.tcp_host, None, False, True, self.config.tcp_port),
            )
        raise MeshConnectionError(f"Meshtastic {connection_type.value} connections are configured for a later adapter slice.")

    def _node_from_mapping(self, node_id: object, details: object) -> MeshNode:
        data = _object_mapping(details)
        user = _object_mapping(data.get("user"))
        position = _object_mapping(data.get("position"))
        metrics = _object_mapping(data.get("deviceMetrics"))
        return MeshNode(
            adapter_id=self.adapter_id,
            transport=self.transport_name,
            node_id=str(node_id),
            long_name=str(user.get("longName") or ""),
            short_name=str(user.get("shortName") or ""),
            public_key_or_hash=str(user.get("publicKey") or ""),
            role=str(user.get("role") or ""),
            last_heard=_datetime_from_packet_time(data.get("lastHeard")),
            hop_count=_optional_int(data.get("hopsAway")),
            route_type=str(data.get("route_type") or data.get("routeType") or ""),
            direct_receive=_optional_bool(data.get("direct_receive") or data.get("directReceive")),
            via_node=str(data.get("via_node") or data.get("viaNode") or ""),
            path_hops=_string_tuple(data.get("path_hops") or data.get("pathHops")),
            snr=_optional_float(data.get("snr")),
            battery_percent=_optional_float(metrics.get("batteryLevel")),
            lat=_position_lat_lon(position)[0],
            lon=_position_lat_lon(position)[1],
            raw=data,
        )


def _datetime_from_packet_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        normalized = str(value).strip().lower()
        if normalized in {"true", "yes", "on"}:
            return True
        if normalized in {"false", "no", "off"}:
            return False
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (tuple, list)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _packet_mapping(packet: Mapping[str, object] | object) -> Mapping[str, object]:
    if isinstance(packet, Mapping):
        return packet
    if hasattr(packet, "toDict") and callable(getattr(packet, "toDict")):
        try:
            value = packet.toDict()
        except Exception:
            value = None
        if isinstance(value, Mapping):
            return value
    if hasattr(packet, "to_dict") and callable(getattr(packet, "to_dict")):
        try:
            value = packet.to_dict()
        except Exception:
            value = None
        if isinstance(value, Mapping):
            return value
    data: dict[str, object] = {}
    for name in (
        "id",
        "packetId",
        "fromId",
        "from",
        "toId",
        "to",
        "channel",
        "rxTime",
        "rx_time",
        "rxSnr",
        "rxRssi",
        "hopLimit",
        "hop_count",
        "hopCount",
        "hopsAway",
        "rxRoute",
        "route_type",
        "routeType",
        "direct_receive",
        "directReceive",
        "via_node",
        "viaNode",
        "path_hops",
        "pathHops",
        "snr",
        "rssi",
        "lat",
        "lon",
        "grid",
        "decoded",
        "position",
        "user",
        "text",
        "payload",
        "portnum",
        "portNum",
    ):
        if hasattr(packet, name):
            data[name] = getattr(packet, name)
    return data


def _object_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "toDict") and callable(getattr(value, "toDict")):
        try:
            mapped = value.toDict()
        except Exception:
            mapped = None
        if isinstance(mapped, Mapping):
            return mapped
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            mapped = value.to_dict()
        except Exception:
            mapped = None
        if isinstance(mapped, Mapping):
            return mapped
    data: dict[str, object] = {}
    for name in (
        "portnum",
        "portNum",
        "text",
        "payload",
        "position",
        "user",
        "latitude",
        "longitude",
        "latitudeI",
        "longitudeI",
        "lat",
        "lon",
        "id",
        "longName",
        "long_name",
        "shortName",
        "short_name",
        "publicKey",
        "public_key",
        "role",
    ):
        if hasattr(value, name):
            data[name] = getattr(value, name)
    return data


def _decoded_mapping(packet: Mapping[str, object]) -> Mapping[str, object]:
    decoded = packet.get("decoded")
    return _object_mapping(decoded)


def _packet_portnum(packet: Mapping[str, object]) -> str:
    decoded = _decoded_mapping(packet)
    return _enum_name(decoded.get("portnum") or decoded.get("portNum") or packet.get("portnum") or packet.get("portNum"))


def _enum_name(value: object) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name)
    text = str(value or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _packet_text(decoded_map: Mapping[str, object], packet: Mapping[str, object]) -> str:
    text = str(decoded_map.get("text") or packet.get("text") or "").strip()
    if text:
        return text
    payload = decoded_map.get("payload") or packet.get("payload")
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="ignore").strip()
    if isinstance(payload, bytearray):
        return bytes(payload).decode("utf-8", errors="ignore").strip()
    return ""


def _packet_node_id(packet: Mapping[str, object], direction: str) -> str:
    if direction == "from":
        value = packet.get("fromId") or packet.get("from")
    else:
        value = packet.get("toId") or packet.get("to")
    return str(value or "").strip()


def _packet_channel_name(packet: Mapping[str, object]) -> str:
    value = packet.get("channel")
    if value is None or value == "":
        return ""
    text = str(value).strip()
    return "Public" if text == "0" else f"Channel {text}"


def _is_broadcast_target(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"", "^all", "all", "broadcast", "!ffffffff", "ffffffff", "0xffffffff", "4294967295"}


def _packet_hop_count(packet: Mapping[str, object]) -> int | None:
    for key in ("hop_count", "hopCount", "hopsAway"):
        value = _optional_int(packet.get(key))
        if value is not None:
            return value
    hop_limit = _optional_int(packet.get("hopLimit"))
    return hop_limit


def _position_lat_lon(position: Mapping[str, object]) -> tuple[float | None, float | None]:
    position = _object_mapping(position)
    lat = _optional_float(position.get("latitude") or position.get("lat"))
    lon = _optional_float(position.get("longitude") or position.get("lon"))
    if lat is None:
        lat_i = _optional_float(position.get("latitudeI") or position.get("latI"))
        if lat_i is not None:
            lat = lat_i / 10_000_000.0
    if lon is None:
        lon_i = _optional_float(position.get("longitudeI") or position.get("lonI"))
        if lon_i is not None:
            lon = lon_i / 10_000_000.0
    return lat, lon


def _has_private_key(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (bytes, bytearray)):
        return any(value)
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(bool(item) for item in value)
    return bool(str(value).strip())


def _generated_channel_sort(name: str) -> int:
    normalized = str(name or "").strip().lower()
    if normalized == "public":
        return 0
    if normalized.startswith("channel "):
        return 2
    return 1


def _meshtastic_response_error(packet: object) -> str:
    data = _packet_mapping(packet)
    decoded = _object_mapping(data.get("decoded"))
    routing = _object_mapping(decoded.get("routing") or data.get("routing"))
    value = routing.get("errorReason") or routing.get("error_reason")
    if value in (None, "", 0, "0", "NONE", "ROUTING_ERROR_NONE"):
        return ""
    return _enum_name(value) or "unknown routing error"
