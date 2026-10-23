from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MeshEndpoint:
    adapter_id: str
    transport: str
    connection_type: str
    address: str
    display_name: str = ""


@dataclass(frozen=True)
class MeshHealthSnapshot:
    adapter_id: str
    transport: str
    enabled: bool
    connected: bool
    connection_type: str
    device_name: str = ""
    firmware_version: str = ""
    battery_percent: float | None = None
    battery_voltage: float | None = None
    last_rx: datetime | None = None
    last_tx: datetime | None = None
    last_error: str = ""
    warnings: tuple[str, ...] = ()
    lifecycle_state: str = ""
    required: bool = False
    guidance: str = ""


@dataclass(frozen=True)
class MeshNode:
    adapter_id: str
    transport: str
    node_id: str
    long_name: str = ""
    short_name: str = ""
    public_key_or_hash: str = ""
    callsign: str = ""
    role: str = ""
    last_heard: datetime | None = None
    hop_count: int | None = None
    route_type: str = ""
    direct_receive: bool | None = None
    via_node: str = ""
    path_hops: tuple[str, ...] = ()
    snr: float | None = None
    rssi: float | None = None
    battery_percent: float | None = None
    lat: float | None = None
    lon: float | None = None
    grid: str = ""
    raw: Mapping[str, object] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.callsign or self.short_name or self.long_name or self.node_id

    def as_map_context(self) -> dict[str, object]:
        return {
            "source_family": self.transport,
            "source_ref": self.node_id,
            "label": self.display_name,
            "lat": self.lat,
            "lon": self.lon,
            "grid": self.grid,
            "last_heard": self.last_heard.isoformat() if self.last_heard else "",
            "routing": self.routing_context(),
            "confidence": "declared" if self.lat is not None and self.lon is not None else "unknown",
        }

    def routing_context(self) -> dict[str, object]:
        return {
            "route_type": self.route_type,
            "direct_receive": self.direct_receive,
            "via_node": self.via_node,
            "hop_count": self.hop_count,
            "path_hops": self.path_hops,
            "snr": self.snr,
            "rssi": self.rssi,
        }


@dataclass(frozen=True)
class MeshChannel:
    adapter_id: str
    transport: str
    index: int
    name: str
    role: str = ""
    channel_id: str = ""
    privacy: str = "unknown"
    psk_hint: str = ""


@dataclass(frozen=True)
class MeshChannelCapabilities:
    can_configure: bool = False
    can_remove_from_device: bool = False
    guidance: str = "Use the device companion application to change device channels."


@dataclass(frozen=True)
class MeshMessage:
    adapter_id: str
    transport: str
    message_id: str
    text: str
    from_node: str = ""
    to_node: str = ""
    channel: str = ""
    portnum: str = ""
    rx_time: datetime | None = None
    hop_count: int | None = None
    route_type: str = ""
    direct_receive: bool | None = None
    via_node: str = ""
    path_hops: tuple[str, ...] = ()
    snr: float | None = None
    rssi: float | None = None
    lat: float | None = None
    lon: float | None = None
    grid: str = ""
    topics: tuple[str, ...] = ()
    severity: str = "info"
    raw: Mapping[str, object] = field(default_factory=dict)
    public_key_or_hash: str = ""
    contact_node_id: str = ""

    def as_traffic_context(self) -> dict[str, object]:
        received = self.rx_time or utc_now()
        subject = self.text.replace("\n", " ").strip()
        return {
            "source_family": self.transport,
            "source_ref": self.message_id,
            "received_time": received.isoformat(),
            "event_time": received.isoformat(),
            "from_actor": self.from_node,
            "to_target": self.to_node,
            "group": self.channel,
            "subject": subject[:80],
            "summary": subject[:240],
            "topics": self.topics,
            "severity": self.severity,
            "lat": self.lat,
            "lon": self.lon,
            "grid": self.grid,
            "transport_metadata": {
                "adapter_id": self.adapter_id,
                "portnum": self.portnum,
                "routing": self.routing_context(),
            },
        }

    def routing_context(self) -> dict[str, object]:
        return {
            "route_type": self.route_type,
            "direct_receive": self.direct_receive,
            "via_node": self.via_node,
            "hop_count": self.hop_count,
            "path_hops": self.path_hops,
            "snr": self.snr,
            "rssi": self.rssi,
        }


@dataclass(frozen=True)
class MeshSendRequest:
    """One immutable operator-authorized outbound mesh operation."""

    adapter_id: str
    text: str
    destination_kind: str
    destination_id: str = ""
    channel_id: str = ""
    request_id: str = field(default_factory=lambda: uuid4().hex)
    requested_at: datetime = field(default_factory=utc_now)
    timeout_sec: float = 12.0
    require_ack: bool = True


@dataclass(frozen=True)
class MeshSendResult:
    """Evidence returned by a mesh adapter without overstating delivery."""

    request_id: str
    adapter_id: str
    transport: str
    destination_kind: str
    destination_id: str = ""
    channel_id: str = ""
    state: str = "failed"
    native_message_id: str = ""
    requested_at: datetime | None = None
    accepted_at: datetime | None = None
    completed_at: datetime | None = None
    acknowledged_at: datetime | None = None
    detail: str = ""
    retryable: bool = False
    evidence: str = ""
    raw: Mapping[str, object] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.state in {"accepted", "sent", "acked"}


@dataclass(frozen=True)
class MeshSendCapabilities:
    supported: bool = False
    channel_send: bool = False
    direct_send: bool = False
    max_text_bytes: int = 0
    guidance: str = "Mesh sending is not available for this connection."


@dataclass(frozen=True)
class MeshAdapterEvent:
    event_type: str
    adapter_id: str
    transport: str
    timestamp: datetime = field(default_factory=utc_now)
    message: MeshMessage | None = None
    node: MeshNode | None = None
    health: MeshHealthSnapshot | None = None
    raw: Mapping[str, object] = field(default_factory=dict)
