from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from freqinout.core.mesh.models import MeshChannel, MeshMessage, MeshNode, utc_now

MESHCORE_CMD_SYNC_NEXT_MESSAGE = 10
MESHCORE_CMD_GET_CONTACTS = 4
MESHCORE_CMD_DEVICE_QUERY = 22
MESHCORE_CMD_GET_CHANNEL = 31

MESHCORE_RESP_ERR = 1
MESHCORE_RESP_CONTACTS_START = 2
MESHCORE_RESP_CONTACT = 3
MESHCORE_RESP_CONTACTS_END = 4
MESHCORE_RESP_NO_MORE_MESSAGES = 10
MESHCORE_RESP_CONTACT_MSG_RECV = 7
MESHCORE_RESP_CHANNEL_MSG_RECV = 8
MESHCORE_RESP_CONTACT_MSG_RECV_V3 = 16
MESHCORE_RESP_CHANNEL_MSG_RECV_V3 = 17
MESHCORE_RESP_CHANNEL_INFO = 18
MESHCORE_PUSH_ADVERT = 0x80
MESHCORE_PUSH_MSG_WAITING = 0x83
MESHCORE_PUSH_LOG_RX_DATA = 0x88
MESHCORE_PUSH_TRACE_DATA = 0x89
MESHCORE_PUSH_NEW_ADVERT = 0x8A


def meshcore_companion_path_len_to_hops(path_len: object) -> int | None:
    try:
        raw = int(path_len)
    except (TypeError, ValueError):
        return None
    if raw < 0 or raw > 255:
        return None
    if raw == 0xFF:
        return 0
    return raw & 0x3F


def normalize_meshcore_channel(
    raw: Mapping[str, object],
    *,
    adapter_id: str,
    transport: str = "meshcore",
) -> MeshChannel | None:
    index = _coerce_int(raw.get("index", raw.get("channelIdx", raw.get("channel_idx"))))
    if index is None:
        return None
    name = _single_line(raw.get("name") or raw.get("channelName") or raw.get("channel_name") or f"Channel {index}")
    secret = raw.get("secret")
    secret_len = _byte_len(secret)
    role = "public" if index == 0 or name.casefold() in {"public", "default", "primary", "longfast"} else "private"
    privacy = "public" if role == "public" else "encrypted"
    psk_hint = ""
    if role == "private":
        psk_hint = "device key" if secret_len else ""
    return MeshChannel(
        adapter_id=adapter_id,
        transport=transport,
        index=index,
        name=name or f"Channel {index}",
        role=role,
        channel_id=str(index),
        privacy=privacy,
        psk_hint=psk_hint,
    )


def normalize_meshcore_channels(
    raw_channels: Sequence[object],
    *,
    adapter_id: str,
    transport: str = "meshcore",
) -> tuple[MeshChannel, ...]:
    channels: list[MeshChannel] = []
    for raw in raw_channels:
        if isinstance(raw, Mapping):
            channel = normalize_meshcore_channel(raw, adapter_id=adapter_id, transport=transport)
            if channel is not None:
                channels.append(channel)
    return tuple(channels)


def decode_meshcore_contact_frame(frame: bytes | bytearray | memoryview) -> dict[str, object] | None:
    return _decode_meshcore_contact_payload_frame(frame, expected_code=MESHCORE_RESP_CONTACT)


def decode_meshcore_new_advert_frame(frame: bytes | bytearray | memoryview) -> dict[str, object] | None:
    return _decode_meshcore_contact_payload_frame(frame, expected_code=MESHCORE_PUSH_NEW_ADVERT)


def _decode_meshcore_contact_payload_frame(
    frame: bytes | bytearray | memoryview,
    *,
    expected_code: int,
) -> dict[str, object] | None:
    data = bytes(frame)
    if not data or data[0] != expected_code or len(data) < 148:
        return None
    public_key = data[1:33]
    contact_type = data[33]
    flags = data[34]
    out_path_len = data[35]
    out_path = data[36:100]
    adv_name = _cstring(data[100:132])
    last_advert = int.from_bytes(data[132:136], "little", signed=False)
    adv_lat = int.from_bytes(data[136:140], "little", signed=True)
    adv_lon = int.from_bytes(data[140:144], "little", signed=True)
    last_mod = int.from_bytes(data[144:148], "little", signed=False)
    lat = adv_lat / 1_000_000 if adv_lat or adv_lon else None
    lon = adv_lon / 1_000_000 if adv_lat or adv_lon else None
    return {
        "publicKey": public_key.hex(),
        "pubKeyPrefix": public_key[:6].hex(),
        "type": contact_type,
        "flags": flags,
        "typeFlags": flags,
        "outPathLen": out_path_len,
        "outPath": out_path.hex(),
        "advLat": adv_lat,
        "advLon": adv_lon,
        "lastAdvert": last_advert,
        "lastMod": last_mod,
        "name": adv_name,
        "lat": lat,
        "lon": lon,
    }


def normalize_meshcore_node(
    raw: Mapping[str, object],
    *,
    adapter_id: str,
    transport: str = "meshcore",
) -> MeshNode | None:
    node_id = _identity_string(_first_non_none(
        raw.get("node_id"), raw.get("nodeId"), raw.get("id"),
        raw.get("pubKeyPrefix"), raw.get("pub_key_prefix"),
        raw.get("publicKey"), raw.get("public_key"),
    ))
    public_key = _identity_string(_first_non_none(raw.get("publicKey"), raw.get("public_key")))
    if public_key:
        node_id = node_id or public_key[:12]
    if not node_id:
        return None
    position = _mapping_value(raw, "position", "location", "gps")
    lat_key, lat_value = _first_mapping_item(raw, ("lat", "latitude")) or (
        _first_mapping_item(position, ("lat", "latitude"))
        if position else None
    ) or (
        None if any(key in raw for key in ("lat", "latitude"))
        else _first_mapping_item(raw, ("advLat", "adv_lat"))
    ) or ("", None)
    lon_key, lon_value = _first_mapping_item(raw, ("lon", "lng", "longitude")) or (
        _first_mapping_item(position, ("lon", "lng", "longitude"))
        if position else None
    ) or (
        None if any(key in raw for key in ("lon", "lng", "longitude"))
        else _first_mapping_item(raw, ("advLon", "adv_lon"))
    ) or ("", None)
    lat = _coerce_float(lat_value)
    lon = _coerce_float(lon_value)
    adv_lat_selected = lat_key in {"advLat", "adv_lat"}
    adv_lon_selected = lon_key in {"advLon", "adv_lon"}
    if adv_lat_selected and abs(lat or 0) > 180:
        lat = (lat or 0) / 1_000_000
    if adv_lon_selected and abs(lon or 0) > 180:
        lon = (lon or 0) / 1_000_000
    explicit_lat_lon = any(key in raw for key in ("lat", "latitude")) or any(
        key in position for key in ("lat", "latitude")
    ) or any(key in raw for key in ("advLat", "adv_lat"))
    explicit_lon = any(key in raw for key in ("lon", "lng", "longitude")) or any(
        key in position for key in ("lon", "lng", "longitude")
    ) or any(key in raw for key in ("advLon", "adv_lon"))
    if lat == 0 and lon == 0 and not (explicit_lat_lon and explicit_lon):
        lat = None
        lon = None
    name = _single_line(_first_non_none(
        raw.get("name"), raw.get("advName"), raw.get("adv_name"),
        raw.get("longName"), raw.get("long_name"),
    ))
    short_name = _single_line(raw.get("shortName") or raw.get("short_name"))
    callsign = _single_line(raw.get("callsign") or raw.get("callSign") or _callsign_from_name(name))
    hop_count = _meshcore_path_len_to_hops(_first_non_none(
        raw.get("hop_count"), raw.get("hopCount"), raw.get("pathLen"), raw.get("path_len")
    ))
    direct_receive = _coerce_bool(_first_non_none(raw.get("direct_receive"), raw.get("directReceive")))
    if direct_receive is None and hop_count is not None:
        direct_receive = hop_count == 0
    route_type = _single_line(raw.get("route_type") or raw.get("routeType"))
    if not route_type and hop_count is not None:
        route_type = "direct" if hop_count == 0 else "mesh"
    return MeshNode(
        adapter_id=adapter_id,
        transport=transport,
        node_id=node_id,
        long_name=name,
        short_name=short_name,
        public_key_or_hash=public_key,
        callsign=callsign,
        role=_single_line(raw.get("role") or raw.get("type")),
        last_heard=_coerce_datetime(_first_non_none(
            raw.get("last_heard"), raw.get("lastHeard"), raw.get("last_advert"),
            raw.get("lastAdvert"), raw.get("last_seen"),
        )),
        hop_count=hop_count,
        route_type=route_type,
        direct_receive=direct_receive,
        via_node=_single_line(raw.get("via_node") or raw.get("viaNode")),
        path_hops=_string_tuple(raw.get("path_hops") or raw.get("pathHops")),
        snr=_coerce_float(raw.get("snr")),
        rssi=_coerce_float(raw.get("rssi")),
        battery_percent=_coerce_float(_first_non_none(raw.get("battery_percent"), raw.get("batteryPercent"), raw.get("battery"))),
        lat=lat,
        lon=lon,
        grid=_single_line(raw.get("grid") or raw.get("locator") or raw.get("maidenhead")).upper(),
        raw=_node_raw_with_outgoing_route(raw),
    )


def normalize_meshcore_nodes(
    raw_nodes: Sequence[object],
    *,
    adapter_id: str,
    transport: str = "meshcore",
) -> tuple[MeshNode, ...]:
    nodes: list[MeshNode] = []
    for raw in raw_nodes:
        if isinstance(raw, Mapping):
            node = normalize_meshcore_node(raw, adapter_id=adapter_id, transport=transport)
            if node is not None:
                nodes.append(node)
    return tuple(nodes)


def normalize_meshcore_waiting_message(
    raw: Mapping[str, object],
    *,
    adapter_id: str,
    transport: str = "meshcore",
    received_at: datetime | None = None,
) -> MeshMessage | None:
    if isinstance(raw.get("channelMessage"), Mapping):
        return _normalize_channel_message(
            raw["channelMessage"],
            adapter_id=adapter_id,
            transport=transport,
            received_at=received_at,
            raw=raw,
        )
    if isinstance(raw.get("contactMessage"), Mapping):
        return _normalize_contact_message(
            raw["contactMessage"],
            adapter_id=adapter_id,
            transport=transport,
            received_at=received_at,
            raw=raw,
        )
    return None


def normalize_meshcore_waiting_messages(
    raw_messages: object,
    *,
    adapter_id: str,
    transport: str = "meshcore",
    received_at: datetime | None = None,
) -> tuple[MeshMessage, ...]:
    if raw_messages is None:
        return ()
    candidates = raw_messages if isinstance(raw_messages, Sequence) and not isinstance(raw_messages, (str, bytes)) else (raw_messages,)
    messages: list[MeshMessage] = []
    for raw in candidates:
        if isinstance(raw, Mapping):
            message = normalize_meshcore_waiting_message(
                raw,
                adapter_id=adapter_id,
                transport=transport,
                received_at=received_at,
            )
            if message is not None:
                messages.append(message)
    return tuple(messages)


def decode_meshcore_channel_info_frame(frame: bytes | bytearray | memoryview) -> dict[str, object] | None:
    data = bytes(frame)
    if not data or data[0] != MESHCORE_RESP_CHANNEL_INFO or len(data) < 34:
        return None
    index = data[1]
    name = _cstring(data[2:34])
    secret = data[34:]
    if len(secret) != 16:
        return None
    return {"channelIdx": index, "index": index, "name": name, "secret": secret}


def decode_meshcore_waiting_message_frame(frame: bytes | bytearray | memoryview) -> dict[str, object] | None:
    data = bytes(frame)
    if not data:
        return None
    code = data[0]
    is_contact = code in {MESHCORE_RESP_CONTACT_MSG_RECV, MESHCORE_RESP_CONTACT_MSG_RECV_V3}
    is_channel = code in {MESHCORE_RESP_CHANNEL_MSG_RECV, MESHCORE_RESP_CHANNEL_MSG_RECV_V3}
    if not is_contact and not is_channel:
        return None
    offset = 1
    snr: float | None = None
    if code in {MESHCORE_RESP_CONTACT_MSG_RECV_V3, MESHCORE_RESP_CHANNEL_MSG_RECV_V3}:
        if len(data) < offset + 3:
            return None
        snr = _signed_byte(data[offset]) / 4
        offset += 3
    if is_contact:
        if len(data) < offset + 12:
            return None
        prefix = data[offset : offset + 6]
        offset += 6
        path_len = data[offset]
        offset += 1
        txt_type = data[offset]
        offset += 1
        sender_ts = int.from_bytes(data[offset : offset + 4], "little", signed=False)
        offset += 4
        text = _decode_tail_text(data[offset:])
        return {
            "contactMessage": {
                "pubKeyPrefix": prefix,
                "pathLen": path_len,
                "txtType": txt_type,
                "senderTimestamp": sender_ts,
                "text": text,
                **({"snr": snr} if snr is not None else {}),
            }
        }
    if len(data) < offset + 7:
        return None
    channel_idx = _signed_byte(data[offset])
    offset += 1
    path_len = data[offset]
    offset += 1
    txt_type = data[offset]
    offset += 1
    sender_ts = int.from_bytes(data[offset : offset + 4], "little", signed=False)
    offset += 4
    text = _decode_tail_text(data[offset:])
    return {
        "channelMessage": {
            "channelIdx": channel_idx,
            "pathLen": path_len,
            "txtType": txt_type,
            "senderTimestamp": sender_ts,
            "text": text,
            **({"snr": snr} if snr is not None else {}),
        }
    }


def meshcore_frame_is_no_more_messages(frame: bytes | bytearray | memoryview) -> bool:
    data = bytes(frame)
    return bool(data and data[0] == MESHCORE_RESP_NO_MORE_MESSAGES)


def meshcore_frame_is_msg_waiting(frame: bytes | bytearray | memoryview) -> bool:
    data = bytes(frame)
    return bool(data and data[0] == MESHCORE_PUSH_MSG_WAITING)


def _normalize_channel_message(
    value: Mapping[str, object],
    *,
    adapter_id: str,
    transport: str,
    received_at: datetime | None,
    raw: Mapping[str, object],
) -> MeshMessage | None:
    channel_idx = _coerce_int(value.get("channelIdx", value.get("channel_idx")))
    raw_text = _single_line(value.get("text"))
    if channel_idx is None or not raw_text:
        return None
    parsed_sender, parsed_text = _split_meshcore_sender_prefix(raw_text)
    text = parsed_text or raw_text
    sender_time = _coerce_int(value.get("senderTimestamp", value.get("sender_timestamp")))
    rx_time = _timestamp_from_seconds(sender_time) or received_at or utc_now()
    hop_count = meshcore_companion_path_len_to_hops(value.get("pathLen", value.get("path_len")))
    explicit_sender = _single_line(value.get("senderId") or value.get("senderNodeId") or value.get("from"))
    return MeshMessage(
        adapter_id=adapter_id,
        transport=transport,
        message_id=_meshcore_message_id("channel", channel_idx, sender_time, raw_text),
        from_node=explicit_sender or parsed_sender,
        to_node=_single_line(value.get("to") or "channel"),
        channel=str(channel_idx),
        text=text,
        rx_time=rx_time,
        hop_count=hop_count,
        route_type="direct" if hop_count == 0 else ("mesh" if hop_count is not None else ""),
        direct_receive=True if hop_count == 0 else (False if hop_count is not None else None),
        snr=_coerce_float(_first_non_none(value.get("snr"), value.get("snr_db"))),
        rssi=_coerce_float(_first_non_none(value.get("rssi"), value.get("rssi_dbm"))),
        raw=raw,
    )


def _split_meshcore_sender_prefix(text: str) -> tuple[str, str]:
    prefix, separator, body = (text or "").partition(":")
    if not separator:
        return "", text
    sender = _single_line(prefix)
    message = _single_line(body)
    sender_key = sender.casefold()
    if (
        not sender
        or not message
        or len(sender) > 60
        or "://" in sender
        or sender_key in {"http", "https", "note", "status", "warning", "alert"}
    ):
        return "", text
    return sender, message


def _normalize_contact_message(
    value: Mapping[str, object],
    *,
    adapter_id: str,
    transport: str,
    received_at: datetime | None,
    raw: Mapping[str, object],
) -> MeshMessage | None:
    text = _single_line(value.get("text"))
    if not text:
        return None
    sender_time = _coerce_int(value.get("senderTimestamp", value.get("sender_timestamp")))
    rx_time = _timestamp_from_seconds(sender_time) or received_at or utc_now()
    prefix = _hex_bytes(
        value.get("pubKeyPrefix", value.get("pub_key_prefix", value.get("pubkey_prefix")))
    )
    hop_count = meshcore_companion_path_len_to_hops(value.get("pathLen", value.get("path_len")))
    contact = value.get("_cp_receive_contact")
    if not isinstance(contact, Mapping):
        contact = {}
    return MeshMessage(
        adapter_id=adapter_id,
        transport=transport,
        message_id=_meshcore_message_id("direct", prefix, sender_time, text),
        from_node=prefix,
        to_node="direct",
        channel="direct",
        text=text,
        rx_time=rx_time,
        hop_count=hop_count,
        route_type="direct" if hop_count == 0 else ("mesh" if hop_count is not None else ""),
        direct_receive=True if hop_count == 0 else (False if hop_count is not None else None),
        snr=_coerce_float(_first_non_none(value.get("snr"), value.get("snr_db"))),
        rssi=_coerce_float(_first_non_none(value.get("rssi"), value.get("rssi_dbm"))),
        raw=raw,
        public_key_or_hash=str(contact.get("public_key") or ""),
        contact_node_id=str(contact.get("node_id") or ""),
    )


def receive_time_contact_identity(payload: Mapping[str, object], contacts: object) -> dict[str, str]:
    """Resolve one native six-byte prefix against a bounded cached contact list.

    This is attribution from the contact list at receipt time, not authentication.
    Missing/ambiguous matches stay unknown and are never reconstructed later.
    """
    if not isinstance(contacts, Mapping) or len(contacts) > 2000:
        return {}
    prefix = _hex_bytes(payload.get("pubKeyPrefix", payload.get("pub_key_prefix", payload.get("pubkey_prefix")))).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", prefix):
        return {}
    matches = []
    try:
        values = tuple(contacts.values())
    except RuntimeError:
        return {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        key = _hex_bytes(value.get("publicKey", value.get("public_key"))).lower()
        if re.fullmatch(r"[0-9a-f]{64}", key) and key.startswith(prefix):
            node = normalize_meshcore_node(value, adapter_id="receive-context")
            if node is not None:
                matches.append((node.node_id, key))
    distinct = set(matches)
    if len(distinct) != 1:
        return {}
    node_id, key = next(iter(distinct))
    return {"node_id": node_id, "public_key": key, "evidence": "receive_time_contact_list"}


def _meshcore_message_id(kind: str, peer: object, sender_time: int | None, text: str) -> str:
    stamp = str(sender_time or "")
    digest = hashlib.sha1(f"{kind}|{peer}|{stamp}|{text}".encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"meshcore:{kind}:{peer}:{stamp or 'now'}:{digest}"


def _timestamp_from_seconds(value: int | None) -> datetime | None:
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _single_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = _single_line(value).casefold()
    if text in {"1", "true", "yes", "y", "direct"}:
        return True
    if text in {"0", "false", "no", "n", "mesh"}:
        return False
    return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    stamp = _coerce_int(value)
    if stamp is not None:
        return _timestamp_from_seconds(stamp)
    text = _single_line(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mapping_value(raw: Mapping[str, object], *keys: str) -> Mapping[str, object]:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _meshcore_path_len_to_hops(value: object) -> int | None:
    raw = _coerce_int(value)
    if raw is None:
        return None
    if raw == -1:
        return 0
    return meshcore_companion_path_len_to_hops(raw)


def _first_non_none(*values: object) -> object | None:
    """Return the first present value without treating numeric zero as absent."""
    return next((value for value in values if value is not None), None)


def _identity_string(value: object) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return _single_line(value)


def _first_mapping_item(mapping: Mapping[str, object], keys: Sequence[str]) -> tuple[str, object] | None:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return key, mapping[key]
    return None


def _node_raw_with_outgoing_route(raw: Mapping[str, object]) -> dict[str, object]:
    enriched = dict(raw)
    if not any(key in raw for key in ("outPathLen", "out_path_len")):
        return enriched
    path_len = _first_non_none(raw.get("outPathLen"), raw.get("out_path_len"))
    route: dict[str, object] = {
        "known": False,
        "hop_count": None,
        "hash_size": None,
        "path_hops": (),
        "updated_utc": "",
    }
    encoded = _coerce_int(path_len)
    # 0xFF/-1 is OUT_PATH_UNKNOWN for a contact's outgoing route. This differs
    # from the same byte in a received message, where it denotes direct receive.
    if encoded is not None and encoded not in {-1, 0xFF} and 0 <= encoded <= 0xFF:
        hop_count = encoded & 0x3F
        hash_size = (encoded >> 6) + 1
        expected_bytes = hop_count * hash_size
        if hash_size <= 3 and expected_bytes <= 64:
            path_value = _first_non_none(raw.get("outPath"), raw.get("out_path"))
            path_bytes = b"" if expected_bytes == 0 else _path_bytes(path_value)
            if path_bytes is not None and len(path_bytes) >= expected_bytes:
                hops = tuple(
                    path_bytes[offset : offset + hash_size].hex()
                    for offset in range(0, expected_bytes, hash_size)
                )
                route.update(
                    known=True,
                    hop_count=hop_count,
                    hash_size=hash_size,
                    path_hops=hops,
                )
    updated = _first_non_none(
        raw.get("updated_utc"), raw.get("updatedUtc"), raw.get("last_mod"),
        raw.get("lastMod"), raw.get("lastmod"), raw.get("last_advert"), raw.get("lastAdvert"),
    )
    stamp = _coerce_datetime(updated)
    if stamp is not None:
        route["updated_utc"] = stamp.astimezone(timezone.utc).isoformat()
    enriched["outgoing_route"] = route
    return enriched


def _path_bytes(value: object) -> bytes | None:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, Sequence) and not isinstance(value, str):
        try:
            return bytes(int(part) for part in value)
        except (TypeError, ValueError, OverflowError):
            return None
    text = _single_line(value).lower().replace("0x", "").replace(":", "").replace(" ", "")
    if not text:
        return b""
    if len(text) % 2:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_items = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else (value,)
    result: list[str] = []
    for item in raw_items:
        text = _single_line(item)
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _callsign_from_name(value: str) -> str:
    match = re.search(r"\b[A-Z]{1,2}\d[A-Z0-9]{1,4}\b", _single_line(value).upper())
    return match.group(0) if match else ""


def _byte_len(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, Sequence) and not isinstance(value, str):
        return len(value)
    return 0


def _hex_bytes(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, Sequence) and not isinstance(value, str):
        try:
            return bytes(int(part) & 0xFF for part in value).hex()
        except (TypeError, ValueError):
            return ""
    return _single_line(value)


def _cstring(value: bytes) -> str:
    try:
        value = value.split(b"\x00", 1)[0]
        return value.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _decode_tail_text(value: bytes) -> str:
    try:
        return value.decode("utf-8", errors="replace").strip("\x00")
    except Exception:
        return ""


def _signed_byte(value: int) -> int:
    value = int(value) & 0xFF
    return value - 256 if value & 0x80 else value
