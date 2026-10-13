from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from importlib import import_module
import json
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence

from freqinout.core.config_paths import get_config_dir


class MeshConnectionType(str, Enum):
    TCP = "tcp"
    SERIAL = "serial"
    BLE = "ble"
    HTTP = "http"
    MQTT = "mqtt"

    @classmethod
    def from_value(cls, value: object) -> "MeshConnectionType":
        normalized = str(value or "").strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.TCP


@dataclass(frozen=True)
class MeshTransportCapability:
    protocol: str
    connection_type: MeshConnectionType
    supported: bool
    reason: str = ""


@dataclass(frozen=True)
class MeshOutboundCapability:
    protocol: str
    connection_type: MeshConnectionType
    supported: bool
    reason: str = ""


def mesh_outbound_capability(
    protocol: object,
    connection_type: MeshConnectionType | object,
) -> MeshOutboundCapability:
    """Return the qualified outbound contract for one local connection."""

    normalized_protocol = str(protocol or "").strip().lower()
    kind = (
        connection_type
        if isinstance(connection_type, MeshConnectionType)
        else MeshConnectionType.from_value(connection_type)
    )
    if normalized_protocol == "meshtastic" and kind in {
        MeshConnectionType.TCP,
        MeshConnectionType.SERIAL,
        MeshConnectionType.BLE,
    }:
        return MeshOutboundCapability(normalized_protocol, kind, True)
    if normalized_protocol == "meshcore" and kind in {
        MeshConnectionType.TCP,
        MeshConnectionType.SERIAL,
        MeshConnectionType.BLE,
    }:
        return MeshOutboundCapability(normalized_protocol, kind, True)
    return MeshOutboundCapability(
        normalized_protocol or "mesh",
        kind,
        False,
        f"{normalized_protocol.title() or 'Mesh'} {kind.value.upper()} sending is not implemented.",
    )


def mesh_transport_capability(
    protocol: object,
    connection_type: MeshConnectionType | object,
    *,
    python_version: tuple[int, int] | None = None,
) -> MeshTransportCapability:
    """Return the truthful runtime contract for one protocol/transport pair.

    Saved configuration may contain planned transports, so this function does
    not mutate or coerce a selection.  UI and runtime validation share this
    matrix to avoid advertising a connection that the adapter cannot open.
    """

    normalized_protocol = str(protocol or "").strip().lower()
    kind = (
        connection_type
        if isinstance(connection_type, MeshConnectionType)
        else MeshConnectionType.from_value(connection_type)
    )
    if normalized_protocol == "meshtastic":
        if kind in {MeshConnectionType.TCP, MeshConnectionType.SERIAL, MeshConnectionType.BLE}:
            return MeshTransportCapability(normalized_protocol, kind, True)
        return MeshTransportCapability(
            normalized_protocol,
            kind,
            False,
            "Meshtastic HTTP and MQTT connections are planned but are not implemented in this CommsPlan release.",
        )
    if normalized_protocol == "meshcore":
        if kind is MeshConnectionType.BLE:
            return MeshTransportCapability(normalized_protocol, kind, True)
        if kind in {MeshConnectionType.TCP, MeshConnectionType.SERIAL}:
            version = python_version or (sys.version_info.major, sys.version_info.minor)
            if version >= (3, 10):
                return MeshTransportCapability(normalized_protocol, kind, True)
            return MeshTransportCapability(
                normalized_protocol,
                kind,
                False,
                "MeshCore USB serial and TCP require Python 3.10 or newer; MeshCore Bluetooth remains available.",
            )
        return MeshTransportCapability(
            normalized_protocol,
            kind,
            False,
            "MeshCore HTTP is not a Companion transport and MeshCore MQTT is not implemented in this CommsPlan release.",
        )
    return MeshTransportCapability(
        normalized_protocol or "mesh",
        kind,
        False,
        f"{normalized_protocol.title() or 'Mesh'} connections are not implemented in this CommsPlan release.",
    )


def supported_mesh_connection_types(
    protocol: object,
    *,
    python_version: tuple[int, int] | None = None,
) -> tuple[MeshConnectionType, ...]:
    return tuple(
        kind
        for kind in MeshConnectionType
        if mesh_transport_capability(protocol, kind, python_version=python_version).supported
    )


@dataclass(frozen=True)
class MeshConnectionConfig:
    adapter_id: str = "meshtastic-main"
    protocol: str = "meshtastic"
    connection_name: str = ""
    connection_name_auto: bool = True
    source_radio_id: str = ""
    source_role: str = ""
    enabled: bool = False
    connection_type: MeshConnectionType = MeshConnectionType.TCP
    tcp_host: str = ""
    tcp_port: int = 4403
    serial_port: str = ""
    serial_baud: int = 115200
    ble_device_id: str = ""
    ble_device_name: str = ""
    ble_scan_timeout_sec: int = 20
    http_base_url: str = ""
    mqtt_enabled: bool = False
    mqtt_broker: str = ""
    mqtt_topic_root: str = ""
    send_enabled: bool = False
    store_messages_enabled: bool = True
    map_positions_enabled: bool = True
    bridge_to_reticulum_enabled: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, object], prefix: str = "meshtastic") -> "MeshConnectionConfig":
        def get(name: str, default: object = "") -> object:
            return values.get(f"{prefix}_{name}", values.get(name, default))

        def has(name: str) -> bool:
            return f"{prefix}_{name}" in values or name in values

        def as_bool(value: object, default: bool = False) -> bool:
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}

        def as_int(value: object, default: int) -> int:
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                return default

        protocol_default = _mesh_protocol_settings_prefix(prefix, fallback=cls.protocol)
        protocol = _mesh_protocol_settings_prefix(get("protocol", protocol_default), fallback=protocol_default)
        adapter_default = "meshcore-main" if protocol == "meshcore" else cls.adapter_id
        adapter_id = str(get("adapter_id", adapter_default) or adapter_default)
        connection_name = str(get("connection_name", "") or "").strip()
        if not connection_name and has("adapter_id"):
            connection_name = adapter_id
        connection_name_auto = as_bool(
            get("connection_name_auto", connection_name_is_automatic(connection_name)),
            connection_name_is_automatic(connection_name),
        )

        return cls(
            adapter_id=adapter_id,
            protocol=protocol,
            connection_name=connection_name,
            connection_name_auto=connection_name_auto,
            source_radio_id=str(get("source_radio_id", "") or "").strip(),
            source_role=str(get("source_role", "") or "").strip(),
            enabled=as_bool(get("enabled", cls.enabled), cls.enabled),
            connection_type=MeshConnectionType.from_value(get("connection_type", cls.connection_type.value)),
            tcp_host=str(get("tcp_host", cls.tcp_host) or "").strip(),
            tcp_port=as_int(get("tcp_port", cls.tcp_port), cls.tcp_port),
            serial_port=str(get("serial_port", cls.serial_port) or "").strip(),
            serial_baud=as_int(get("serial_baud", cls.serial_baud), cls.serial_baud),
            ble_device_id=str(get("ble_device_id", cls.ble_device_id) or "").strip(),
            ble_device_name=str(get("ble_device_name", cls.ble_device_name) or "").strip(),
            ble_scan_timeout_sec=as_int(get("ble_scan_timeout_sec", cls.ble_scan_timeout_sec), cls.ble_scan_timeout_sec),
            http_base_url=str(get("http_base_url", cls.http_base_url) or "").strip(),
            mqtt_enabled=as_bool(get("mqtt_enabled", cls.mqtt_enabled), cls.mqtt_enabled),
            mqtt_broker=str(get("mqtt_broker", cls.mqtt_broker) or "").strip(),
            mqtt_topic_root=str(get("mqtt_topic_root", cls.mqtt_topic_root) or "").strip(),
            send_enabled=as_bool(get("send_enabled", cls.send_enabled), cls.send_enabled),
            store_messages_enabled=as_bool(
                get("store_messages_enabled", cls.store_messages_enabled), cls.store_messages_enabled
            ),
            map_positions_enabled=as_bool(
                get("map_positions_enabled", cls.map_positions_enabled), cls.map_positions_enabled
            ),
            bridge_to_reticulum_enabled=as_bool(
                get("bridge_to_reticulum_enabled", cls.bridge_to_reticulum_enabled),
                cls.bridge_to_reticulum_enabled,
            ),
        )

    @property
    def endpoint_address(self) -> str:
        if self.connection_type is MeshConnectionType.SERIAL:
            return self.serial_port
        if self.connection_type is MeshConnectionType.BLE:
            return self.ble_device_id or self.ble_device_name
        if self.connection_type is MeshConnectionType.HTTP:
            return self.http_base_url
        if self.connection_type is MeshConnectionType.MQTT:
            return self.mqtt_broker
        return self.tcp_host

    @property
    def display_name(self) -> str:
        """Human-facing saved-device name for chips, settings, and health."""

        connection_name = str(self.connection_name or "").strip()
        adapter_id = str(self.adapter_id or "").strip()
        if connection_name and connection_name.casefold() != adapter_id.casefold():
            return connection_name
        for value in (
            self.ble_device_name,
            connection_name,
            adapter_id,
            self.serial_port,
            self.tcp_host,
            self.http_base_url,
            self.mqtt_broker,
            self.endpoint_address,
        ):
            text = str(value or "").strip()
            if text:
                return text
        return "Local Mesh"

    def to_mapping(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "protocol": self.protocol,
            "connection_name": self.connection_name,
            "connection_name_auto": self.connection_name_auto,
            "source_radio_id": self.source_radio_id,
            "source_role": self.source_role,
            "enabled": self.enabled,
            "connection_type": self.connection_type.value,
            "tcp_host": self.tcp_host,
            "tcp_port": self.tcp_port,
            "serial_port": self.serial_port,
            "serial_baud": self.serial_baud,
            "ble_device_id": self.ble_device_id,
            "ble_device_name": self.ble_device_name,
            "ble_scan_timeout_sec": self.ble_scan_timeout_sec,
            "http_base_url": self.http_base_url,
            "mqtt_enabled": self.mqtt_enabled,
            "mqtt_broker": self.mqtt_broker,
            "mqtt_topic_root": self.mqtt_topic_root,
            "send_enabled": self.send_enabled,
            "store_messages_enabled": self.store_messages_enabled,
            "map_positions_enabled": self.map_positions_enabled,
            "bridge_to_reticulum_enabled": self.bridge_to_reticulum_enabled,
        }


@dataclass(frozen=True)
class MeshConfigIssue:
    field: str
    message: str
    severity: str = "error"


def validate_mesh_connection_config(config: MeshConnectionConfig) -> tuple[MeshConfigIssue, ...]:
    issues: list[MeshConfigIssue] = []
    if not config.enabled:
        return ()
    capability = mesh_transport_capability(config.protocol, config.connection_type)
    if not capability.supported:
        issues.append(MeshConfigIssue("connection_type", capability.reason))
    if config.connection_type is MeshConnectionType.TCP and not config.tcp_host:
        issues.append(MeshConfigIssue("tcp_host", "TCP mesh connections need a host name or IP address."))
    if config.connection_type is MeshConnectionType.SERIAL and not config.serial_port:
        issues.append(MeshConfigIssue("serial_port", "USB serial mesh connections need a serial device path."))
    if config.connection_type is MeshConnectionType.BLE and not (config.ble_device_id or config.ble_device_name):
        issues.append(MeshConfigIssue("ble_device", "BLE mesh connections need a saved device id or name."))
    if config.connection_type is MeshConnectionType.HTTP and not config.http_base_url:
        issues.append(MeshConfigIssue("http_base_url", "HTTP mesh connections need the device base URL."))
    if config.connection_type is MeshConnectionType.MQTT and not config.mqtt_broker:
        issues.append(MeshConfigIssue("mqtt_broker", "MQTT mesh connections need a broker."))
    if config.connection_type is MeshConnectionType.MQTT and not config.mqtt_enabled:
        issues.append(MeshConfigIssue("mqtt_enabled", "MQTT must be explicitly enabled before use.", severity="warning"))
    if config.tcp_port <= 0 or config.tcp_port > 65535:
        issues.append(MeshConfigIssue("tcp_port", "TCP port must be between 1 and 65535."))
    if config.serial_baud <= 0:
        issues.append(MeshConfigIssue("serial_baud", "Serial baud rate must be greater than zero."))
    if config.ble_scan_timeout_sec < 5:
        issues.append(MeshConfigIssue("ble_scan_timeout_sec", "BLE scan timeout should be at least 5 seconds."))
    return tuple(issues)


def discover_serial_ports() -> tuple[str, ...]:
    try:
        list_ports = import_module("serial.tools.list_ports")
    except Exception:
        return ()
    try:
        ports = list_ports.comports()
    except Exception:
        return ()
    names: list[str] = []
    for port in ports:
        device = str(getattr(port, "device", "") or "").strip()
        if device:
            names.append(device)
    return tuple(dict.fromkeys(names))


def default_mesh_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout_nets.db"


def load_mesh_connection_configs(settings_or_values: object, prefix: str = "all") -> tuple[MeshConnectionConfig, ...]:
    values = _settings_values(settings_or_values)
    configs: list[MeshConnectionConfig] = []
    # Identity repair must see disabled saved siblings too. Otherwise an active
    # endpoint can retain a legacy adapter id already owned by a disconnected
    # device and publish health under the wrong device. Filtering to runnable
    # records happens only after the complete saved set has been normalized.
    configs.extend(_load_mesh_connection_library(values, include_disabled=True))
    active_configs: list[MeshConnectionConfig] = []
    for active_prefix in _mesh_active_prefixes(prefix):
        config = normalize_mesh_connection_config(MeshConnectionConfig.from_mapping(values, prefix=active_prefix))
        if config.enabled and (_mesh_prefix_has_explicit_config(values, active_prefix) or _mesh_connection_has_endpoint(config)):
            active_configs.append(config)
            configs.append(config)
    deduped = _dedupe_mesh_connection_configs(configs)
    if not active_configs:
        return tuple(config for config in deduped if config.enabled)
    # The protocol-prefixed record is the active runtime selection.  Saved
    # library siblings remain available to the UI, but stale enabled flags in
    # an older library must not start multiple adapters in the same runtime
    # family after a scan/use handoff or interrupted save.
    active_by_family = {
        (str(config.protocol or "").strip().lower(), config.connection_type): mesh_connection_config_key(config)
        for config in active_configs
    }
    return tuple(
        config
        for config in deduped
        if config.enabled
        and (
            (str(config.protocol or "").strip().lower(), config.connection_type) not in active_by_family
            or mesh_connection_config_key(config)
            == active_by_family[(str(config.protocol or "").strip().lower(), config.connection_type)]
        )
    )


def load_saved_mesh_connection_configs(
    settings_or_values: object,
    prefix: str = "all",
) -> tuple[MeshConnectionConfig, ...]:
    """Load configured mesh devices for UI selection, including disconnected saved devices."""

    values = _settings_values(settings_or_values)
    configs = list(_load_mesh_connection_library(values, include_disabled=True))
    for active_prefix in _mesh_active_prefixes(prefix):
        config = normalize_mesh_connection_config(MeshConnectionConfig.from_mapping(values, prefix=active_prefix))
        if _mesh_prefix_has_explicit_config(values, active_prefix) and _mesh_connection_has_endpoint(config):
            configs.append(config)
    return tuple(_dedupe_mesh_connection_configs(configs))


def serialize_mesh_connection_library(configs: Sequence[MeshConnectionConfig]) -> str:
    return json.dumps([config.to_mapping() for config in _dedupe_mesh_connection_configs(configs)], sort_keys=True)


_AUTO_CONNECTION_NAME_RE = re.compile(r"^(meshcore|meshtastic)-(\d+)$", re.IGNORECASE)


def mesh_protocol_slug(protocol: object) -> str:
    """Return the stable user-facing prefix for a supported mesh protocol."""

    normalized = str(protocol or "").strip().lower()
    return normalized if normalized in {"meshcore", "meshtastic"} else "mesh"


def next_mesh_connection_name(
    protocol: object,
    configs: Sequence[MeshConnectionConfig] = (),
) -> str:
    """Choose the first unused protocol-number name without touching saved identities."""

    prefix = mesh_protocol_slug(protocol)
    used = {
        str(config.connection_name or config.adapter_id or "").strip().casefold()
        for config in configs
        if str(config.connection_name or config.adapter_id or "").strip()
    }
    index = 1
    while f"{prefix}-{index}".casefold() in used:
        index += 1
    return f"{prefix}-{index}"


def next_mesh_adapter_id(
    protocol: object,
    configs: Sequence[MeshConnectionConfig] = (),
    *,
    preferred_name: object = "",
) -> str:
    """Return a unique internal adapter id without exposing a BLE address."""

    prefix = mesh_protocol_slug(protocol)
    used = {
        _normalize_mesh_config_key(config.adapter_id)
        for config in configs
        if _normalize_mesh_config_key(config.adapter_id)
    }
    preferred = re.sub(
        r"[^a-z0-9]+",
        "-",
        re.sub(r"^(meshcore|meshtastic|mesh)[\s:_-]+", "", str(preferred_name or "").strip(), flags=re.IGNORECASE).lower(),
    ).strip("-")
    base = f"{prefix}-{preferred}" if preferred else f"{prefix}-1"
    if _normalize_mesh_config_key(base) not in used:
        return base
    index = 2
    while _normalize_mesh_config_key(f"{base}-{index}") in used:
        index += 1
    return f"{base}-{index}"


def connection_name_is_automatic(name: object) -> bool:
    return bool(_AUTO_CONNECTION_NAME_RE.fullmatch(str(name or "").strip()))


def update_automatic_connection_name(
    current_name: object,
    protocol: object,
    *,
    is_auto: bool,
    configs: Sequence[MeshConnectionConfig] = (),
) -> str:
    """Update an untouched generated name while preserving any operator edit."""

    text = str(current_name or "").strip()
    if not is_auto:
        return text
    prefix = mesh_protocol_slug(protocol)
    match = _AUTO_CONNECTION_NAME_RE.fullmatch(text)
    if match:
        candidate = f"{prefix}-{match.group(2)}"
        used = {
            str(config.connection_name or config.adapter_id or "").strip().casefold()
            for config in configs
            if str(config.connection_name or config.adapter_id or "").strip().casefold() != text.casefold()
        }
        if candidate.casefold() not in used:
            return candidate
    return next_mesh_connection_name(prefix, configs)


def merge_mesh_connection_library(
    existing_values: Mapping[str, object],
    selected_config: MeshConnectionConfig,
    *,
    previous_key: str = "",
) -> tuple[MeshConnectionConfig, ...]:
    selected = normalize_mesh_connection_config(selected_config)
    normalized_previous = _normalize_mesh_config_key(previous_key)
    configs = [
        config
        for config in _load_mesh_connection_library(existing_values, include_disabled=True)
        if (
            not normalized_previous
            or _normalize_mesh_config_key(mesh_connection_config_key(config)) != normalized_previous
        )
        and not _mesh_config_is_obvious_stale_sibling(config, selected)
    ]
    if selected_config.enabled or _mesh_connection_has_endpoint(selected_config):
        configs.append(selected)
    return tuple(_dedupe_mesh_connection_configs(configs))


def activate_mesh_connection_config(
    existing_values: Mapping[str, object],
    selected_key: str,
    prefix: str = "meshtastic",
) -> dict[str, object] | None:
    """Return settings updates that make one saved mesh device the active runtime endpoint."""

    normalized_selected = _normalize_mesh_config_key(selected_key)
    if not normalized_selected:
        return None
    configs = list(load_saved_mesh_connection_configs(existing_values, prefix=prefix))
    selected: MeshConnectionConfig | None = None
    for config in configs:
        if _normalize_mesh_config_key(mesh_connection_config_key(config)) == normalized_selected:
            selected = config
            break
    if selected is None:
        return None
    selected = replace(selected, enabled=True)
    selected_key_normalized = _normalize_mesh_config_key(mesh_connection_config_key(selected))
    updated: list[MeshConnectionConfig] = []
    for config in configs:
        config_key = _normalize_mesh_config_key(mesh_connection_config_key(config))
        same_runtime_family = (
            str(config.protocol or "").strip().lower() == str(selected.protocol or "").strip().lower()
            and config.connection_type is selected.connection_type
        )
        same_endpoint_wrong_protocol = (
            str(config.protocol or "").strip().lower() != str(selected.protocol or "").strip().lower()
            and _mesh_configs_refer_to_same_endpoint(config, selected)
        )
        if config_key == selected_key_normalized:
            updated.append(selected)
        elif same_runtime_family or same_endpoint_wrong_protocol:
            updated.append(replace(config, enabled=False))
        else:
            updated.append(config)
    active_prefix = _mesh_protocol_settings_prefix(selected.protocol, fallback=prefix)
    payload = mesh_connection_active_settings_payload(selected, prefix=active_prefix)
    for sibling_prefix in _mesh_active_prefixes("all"):
        if sibling_prefix == active_prefix:
            continue
        sibling = normalize_mesh_connection_config(MeshConnectionConfig.from_mapping(existing_values, prefix=sibling_prefix))
        if sibling.enabled and _mesh_configs_refer_to_same_endpoint(sibling, selected):
            payload[f"{sibling_prefix}_enabled"] = False
    payload["mesh_connection_library"] = serialize_mesh_connection_library(_dedupe_mesh_connection_configs(updated))
    return payload


def _mesh_active_prefixes(prefix: object) -> tuple[str, ...]:
    normalized = str(prefix or "").strip().lower()
    if normalized in {"", "all", "mesh", "local_mesh"}:
        return ("meshcore", "meshtastic")
    return (_mesh_protocol_settings_prefix(normalized, fallback=normalized),)


def _mesh_protocol_settings_prefix(protocol: object, *, fallback: str = "meshtastic") -> str:
    normalized = str(protocol or "").strip().lower()
    if normalized in {"meshcore", "meshtastic"}:
        return normalized
    return str(fallback or "meshtastic").strip().lower() or "meshtastic"


def mesh_connection_active_settings_payload(
    config: MeshConnectionConfig,
    prefix: str = "meshtastic",
) -> dict[str, object]:
    return {
        f"{prefix}_adapter_id": config.adapter_id,
        f"{prefix}_protocol": config.protocol,
        f"{prefix}_connection_name": config.connection_name,
        f"{prefix}_connection_name_auto": config.connection_name_auto,
        f"{prefix}_source_radio_id": config.source_radio_id,
        f"{prefix}_source_role": config.source_role,
        f"{prefix}_enabled": config.enabled,
        f"{prefix}_connection_type": config.connection_type.value,
        f"{prefix}_tcp_host": config.tcp_host,
        f"{prefix}_tcp_port": config.tcp_port,
        f"{prefix}_serial_port": config.serial_port,
        f"{prefix}_serial_baud": config.serial_baud,
        f"{prefix}_ble_device_id": config.ble_device_id,
        f"{prefix}_ble_device_name": config.ble_device_name,
        f"{prefix}_ble_scan_timeout_sec": config.ble_scan_timeout_sec,
        f"{prefix}_http_base_url": config.http_base_url,
        f"{prefix}_mqtt_enabled": config.mqtt_enabled,
        f"{prefix}_mqtt_broker": config.mqtt_broker,
        f"{prefix}_mqtt_topic_root": config.mqtt_topic_root,
        f"{prefix}_send_enabled": config.send_enabled,
        f"{prefix}_store_messages_enabled": config.store_messages_enabled,
        f"{prefix}_map_positions_enabled": config.map_positions_enabled,
        f"{prefix}_bridge_to_reticulum_enabled": config.bridge_to_reticulum_enabled,
    }


def mesh_connection_config_key(config: MeshConnectionConfig) -> str:
    protocol = str(config.protocol or "mesh").strip().lower() or "mesh"
    connection_type = str(config.connection_type.value or "").strip().lower()
    endpoint = _mesh_connection_identity_endpoint(config)
    return f"{protocol}:{connection_type}:{endpoint}"


def mesh_health_matches_config(
    config: MeshConnectionConfig,
    row: Mapping[str, object],
) -> bool:
    """Match health to one saved endpoint without crossing named devices."""

    config_protocol = str(config.protocol or "").strip().lower()
    row_protocol = str(row.get("transport") or "").strip().lower()
    if config_protocol and row_protocol and config_protocol != row_protocol:
        return False
    config_name = _normalize_mesh_identity(config.ble_device_name)
    row_name = _normalize_mesh_identity(row.get("device_name"))
    config_device_id = _normalize_mesh_identity(config.ble_device_id)
    row_adapter = _normalize_mesh_identity(row.get("adapter_id"))
    if config_device_id and config_device_id in {row_adapter, row_name}:
        return True
    if config_name and row_name:
        # Advertised names are the strongest readable identity currently
        # persisted in a health row. A contradictory named device must never
        # match merely because legacy data reused an adapter id. A raw BLE id
        # in device_name was handled above so pre-GATT failures still surface.
        return config_name == row_name
    config_adapter = _normalize_mesh_identity(config.adapter_id)
    return bool(config_adapter and config_adapter == row_adapter)


def normalize_mesh_connection_config(config: MeshConnectionConfig) -> MeshConnectionConfig:
    """Repair legacy/stale protocol identity for saved local mesh endpoints.

    Early Meshtastic work introduced a protocol default that could be applied to
    previously saved MeshCore BLE records when they did not carry an explicit
    protocol field. Treat the physical endpoint identity as authoritative so a
    MeshCore BLE device never gets routed through the Meshtastic adapter.
    """

    if _mesh_config_looks_like_meshcore(config):
        adapter_id = str(config.adapter_id or "").strip()
        if not _normalize_mesh_config_key(adapter_id).startswith("meshcore"):
            adapter_id = "meshcore-main"
        if config.protocol != "meshcore" or adapter_id != config.adapter_id:
            return replace(config, protocol="meshcore", adapter_id=adapter_id)
    return config


def _load_mesh_connection_library(
    values: Mapping[str, object],
    *,
    include_disabled: bool = False,
) -> tuple[MeshConnectionConfig, ...]:
    raw = values.get("mesh_connection_library", ())
    parsed: object = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            parsed = ()
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = ()
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes, bytearray)):
        return ()
    configs: list[MeshConnectionConfig] = []
    for item in parsed:
        if not isinstance(item, Mapping):
            continue
        config = normalize_mesh_connection_config(MeshConnectionConfig.from_mapping(item, prefix=""))
        if include_disabled or config.enabled:
            configs.append(config)
    return tuple(configs)


def _mesh_config_looks_like_meshcore(config: MeshConnectionConfig) -> bool:
    values = (
        config.adapter_id,
        config.ble_device_name,
        config.endpoint_address if config.connection_type is MeshConnectionType.BLE else "",
    )
    return any(_normalize_mesh_config_key(value).startswith("meshcore") for value in values)


def _dedupe_mesh_connection_configs(configs: Sequence[MeshConnectionConfig]) -> tuple[MeshConnectionConfig, ...]:
    configs = _prune_obvious_stale_transport_siblings(configs)
    by_key: dict[str, MeshConnectionConfig] = {}
    for config in configs:
        by_key[mesh_connection_config_key(config)] = config
    unique: list[MeshConnectionConfig] = []
    used_adapter_ids: set[str] = set()
    for config in by_key.values():
        adapter_key = _normalize_mesh_config_key(config.adapter_id)
        if adapter_key and adapter_key not in used_adapter_ids:
            unique.append(config)
            used_adapter_ids.add(adapter_key)
            continue
        replacement_id = next_mesh_adapter_id(
            config.protocol,
            unique,
            preferred_name=config.ble_device_name or config.connection_name,
        )
        connection_name = str(config.connection_name or "").strip()
        replacement_name_is_auto = (
            not connection_name or _normalize_mesh_config_key(connection_name) == adapter_key
        )
        if replacement_name_is_auto:
            connection_name = re.sub(
                r"^(meshcore|meshtastic|mesh)[\s:_-]+",
                "",
                str(config.ble_device_name or replacement_id).strip(),
                flags=re.IGNORECASE,
            ).strip()
        repaired = replace(
            config,
            adapter_id=replacement_id,
            connection_name=connection_name,
            connection_name_auto=replacement_name_is_auto,
        )
        unique.append(repaired)
        used_adapter_ids.add(_normalize_mesh_config_key(replacement_id))
    return tuple(unique)


def _prune_obvious_stale_transport_siblings(
    configs: Sequence[MeshConnectionConfig],
) -> tuple[MeshConnectionConfig, ...]:
    """Drop incomplete transport-switch copies of a saved BLE endpoint.

    Older Settings saves could leave the BLE identity fields on a record after
    changing its selected transport, then append that record under a TCP/serial
    key.  Only remove a sibling when an actual BLE record owns the exact device
    id and the sibling has no endpoint for its selected transport.  A complete
    TCP or serial connection is preserved even when it retains historical BLE
    metadata.
    """

    ble_owners = {
        (str(config.protocol or "").strip().lower(), _normalize_mesh_config_key(config.ble_device_id))
        for config in configs
        if config.connection_type is MeshConnectionType.BLE
        and _normalize_mesh_config_key(config.ble_device_id)
        and _mesh_connection_has_selected_transport_endpoint(config)
    }
    return tuple(
        config
        for config in configs
        if not (
            config.connection_type is not MeshConnectionType.BLE
            and (
                str(config.protocol or "").strip().lower(),
                _normalize_mesh_config_key(config.ble_device_id),
            )
            in ble_owners
            and not _mesh_connection_has_selected_transport_endpoint(config)
        )
    )


def _mesh_connection_has_endpoint(config: MeshConnectionConfig) -> bool:
    return bool(
        str(config.ble_device_id or "").strip()
        or str(config.ble_device_name or "").strip()
        or str(config.tcp_host or "").strip()
        or str(config.serial_port or "").strip()
        or str(config.http_base_url or "").strip()
        or str(config.mqtt_broker or "").strip()
    )


def _mesh_prefix_has_explicit_config(values: Mapping[str, object], prefix: str) -> bool:
    fields = (
        "adapter_id",
        "protocol",
        "enabled",
        "connection_type",
        "tcp_host",
        "serial_port",
        "ble_device_id",
        "ble_device_name",
        "http_base_url",
        "mqtt_broker",
    )
    return any(f"{prefix}_{field}" in values for field in fields)


def _mesh_configs_refer_to_same_endpoint(left: MeshConnectionConfig, right: MeshConnectionConfig) -> bool:
    if left.connection_type is not right.connection_type:
        return False
    left_tokens = _mesh_config_identity_tokens(left)
    right_tokens = _mesh_config_identity_tokens(right)
    return bool(left_tokens and right_tokens and left_tokens.intersection(right_tokens))


def _mesh_config_is_obvious_stale_sibling(
    candidate: MeshConnectionConfig,
    selected: MeshConnectionConfig,
) -> bool:
    """Identify an invalid transport-switch artifact without merging real endpoints.

    The Settings editor retains hidden transport fields while an operator changes
    the transport selector. Older saves appended the changed record under its new
    key, so a BLE device could acquire enabled TCP siblings with no TCP host. An
    exact BLE id match plus a missing selected-transport endpoint makes that
    artifact unambiguous and safe to prune when the real BLE record is saved.
    """

    if selected.connection_type is not MeshConnectionType.BLE:
        return False
    selected_ble_id = _normalize_mesh_config_key(selected.ble_device_id)
    candidate_ble_id = _normalize_mesh_config_key(candidate.ble_device_id)
    if not selected_ble_id or candidate_ble_id != selected_ble_id:
        return False
    if candidate.connection_type is MeshConnectionType.BLE:
        return False
    return not _mesh_connection_has_selected_transport_endpoint(candidate)


def _mesh_connection_has_selected_transport_endpoint(config: MeshConnectionConfig) -> bool:
    if config.connection_type is MeshConnectionType.TCP:
        return bool(str(config.tcp_host or "").strip())
    if config.connection_type is MeshConnectionType.SERIAL:
        return bool(str(config.serial_port or "").strip())
    if config.connection_type is MeshConnectionType.BLE:
        return bool(str(config.ble_device_id or config.ble_device_name or "").strip())
    if config.connection_type is MeshConnectionType.HTTP:
        return bool(str(config.http_base_url or "").strip())
    if config.connection_type is MeshConnectionType.MQTT:
        return bool(str(config.mqtt_broker or "").strip())
    return False


def _mesh_config_identity_tokens(config: MeshConnectionConfig) -> set[str]:
    return {
        token
        for token in (
            _normalize_mesh_config_key(config.ble_device_id),
            _normalize_mesh_config_key(config.ble_device_name),
            _normalize_mesh_config_key(config.endpoint_address),
            _normalize_mesh_config_key(config.adapter_id),
        )
        if token
    }


def _mesh_connection_identity_endpoint(config: MeshConnectionConfig) -> str:
    if config.connection_type is MeshConnectionType.BLE:
        endpoint = config.ble_device_id or config.ble_device_name or config.adapter_id
    elif config.connection_type is MeshConnectionType.TCP:
        endpoint = f"{config.tcp_host}:{config.tcp_port}" if config.tcp_host else config.adapter_id
    elif config.connection_type is MeshConnectionType.SERIAL:
        endpoint = config.serial_port or config.adapter_id
    elif config.connection_type is MeshConnectionType.HTTP:
        endpoint = config.http_base_url or config.adapter_id
    elif config.connection_type is MeshConnectionType.MQTT:
        endpoint = f"{config.mqtt_broker}|{config.mqtt_topic_root}" if config.mqtt_broker else config.adapter_id
    else:
        endpoint = config.endpoint_address or config.adapter_id
    return _normalize_mesh_config_key(endpoint)


def _normalize_mesh_config_key(value: object) -> str:
    text = str(value or "").strip().lower()
    return " ".join(text.split())


def _normalize_mesh_identity(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^(meshcore|meshtastic|mesh)[\s:_-]+", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _settings_values(settings_or_values: object) -> Mapping[str, object]:
    if isinstance(settings_or_values, Mapping):
        return settings_or_values
    all_values = getattr(settings_or_values, "all", None)
    if callable(all_values):
        loaded = all_values()
        if isinstance(loaded, Mapping):
            return loaded
    get_value = getattr(settings_or_values, "get", None)
    if callable(get_value):
        values: dict[str, object] = {"mesh_connection_library": get_value("mesh_connection_library", "")}
        defaults = (
            ("adapter_id", MeshConnectionConfig.adapter_id),
            ("protocol", MeshConnectionConfig.protocol),
            ("connection_name", MeshConnectionConfig.connection_name),
            ("connection_name_auto", MeshConnectionConfig.connection_name_auto),
            ("source_radio_id", MeshConnectionConfig.source_radio_id),
            ("source_role", MeshConnectionConfig.source_role),
            ("enabled", MeshConnectionConfig.enabled),
            ("connection_type", MeshConnectionConfig.connection_type.value),
            ("tcp_host", MeshConnectionConfig.tcp_host),
            ("tcp_port", MeshConnectionConfig.tcp_port),
            ("serial_port", MeshConnectionConfig.serial_port),
            ("serial_baud", MeshConnectionConfig.serial_baud),
            ("ble_device_id", MeshConnectionConfig.ble_device_id),
            ("ble_device_name", MeshConnectionConfig.ble_device_name),
            ("ble_scan_timeout_sec", MeshConnectionConfig.ble_scan_timeout_sec),
            ("http_base_url", MeshConnectionConfig.http_base_url),
            ("mqtt_enabled", MeshConnectionConfig.mqtt_enabled),
            ("mqtt_broker", MeshConnectionConfig.mqtt_broker),
            ("mqtt_topic_root", MeshConnectionConfig.mqtt_topic_root),
            ("send_enabled", MeshConnectionConfig.send_enabled),
            ("store_messages_enabled", MeshConnectionConfig.store_messages_enabled),
            ("map_positions_enabled", MeshConnectionConfig.map_positions_enabled),
            ("bridge_to_reticulum_enabled", MeshConnectionConfig.bridge_to_reticulum_enabled),
        )
        for prefix in _mesh_active_prefixes("all"):
            for name, default in defaults:
                values[f"{prefix}_{name}"] = get_value(f"{prefix}_{name}", default)
        return values
    return {}
