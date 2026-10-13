"""Pure, bounded projection of cached MeshCore outgoing routes for the map.

This module deliberately does not perform IO or Qt work. A cached outgoing
route is routing metadata advertised by the companion; it is not evidence that
any packet was recently received along the displayed route.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from urllib.parse import quote


_MAX_NODES = 1500
_MAX_RECEIVED_PATH_RECORDS = 128
_MAX_RECEIVED_PATHS_PER_NODE = 32
_MAX_HOPS_PER_RECEIVED_PATH = 64
_MAX_CACHED_LINK_CANDIDATES = 1000
_MAX_OBSERVED_LINK_CANDIDATES = 1000
_DEFAULT_MAX_LINKS = 250


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _strict_true(value: object) -> bool:
    # These fields come from normalized JSON. Truthy strings such as "false"
    # must never be promoted to affirmative route/self evidence.
    return value is True


def _small_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdecimal() or (text.startswith("-") and text[1:].isdecimal())):
            try:
                return int(text)
            except (ValueError, OverflowError):
                return None
    return None


def _identity(row: Mapping[str, object]) -> tuple[str, str, str, str]:
    adapter = _text(row.get("adapter_id"))
    transport = _text(row.get("transport")).lower()
    public_key = _text(row.get("public_key_or_hash")).lower()
    node_id = _text(row.get("node_id")).lower()
    if public_key:
        # Include the native node ID when available so duplicate advertised
        # keys remain distinct (and make prefix resolution correctly
        # ambiguous) while the public key still supplies the route prefix.
        identity_value = f"{public_key}\x1f{node_id}" if node_id else public_key
        return adapter, transport, "key", identity_value
    return adapter, transport, "node", node_id


def _marker_id(row: Mapping[str, object]) -> str:
    adapter, transport, identity_kind, identity_value = _identity(row)
    parts = (adapter, transport, identity_kind, identity_value)
    return "mesh:" + ":".join(quote(part, safe="") for part in parts)


def _coordinates(row: Mapping[str, object]) -> tuple[float, float] | None:
    try:
        lat = float(row.get("lat"))
        lon = float(row.get("lon"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lon):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return lat, lon


def _route(row: Mapping[str, object]) -> Mapping[str, object]:
    raw = row.get("raw_payload")
    if not isinstance(raw, Mapping):
        return {}
    route = raw.get("outgoing_route")
    return route if isinstance(route, Mapping) else {}


def _prefix(value: object, hash_size: int) -> str:
    text = _text(value).lower()
    if text.startswith("0x"):
        text = text[2:]
    if hash_size < 1 or hash_size > 3 or len(text) != hash_size * 2:
        return ""
    if any(char not in "0123456789abcdef" for char in text):
        return ""
    return text


def _route_stamp(route: Mapping[str, object]) -> str:
    return _text(route.get("updated_utc"))


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _metric_text(value: float) -> str:
    return f"{value:g}"


def build_mesh_route_projection(
    nodes: Sequence[Mapping[str, object]], *, max_links: int = _DEFAULT_MAX_LINKS, include_paths: bool = True
) -> dict[str, object]:
    """Project bounded node rows into station markers and cached-route links.

    Hop resolution is scoped to one adapter and transport. Any missing,
    ambiguous, malformed, or unlocated hop breaks the chain; later resolved
    adjacent pairs may still be shown, but gaps are never bridged.
    """
    projection_limited = len(nodes) > _MAX_NODES
    bounded_nodes = [node for node in nodes[:_MAX_NODES] if isinstance(node, Mapping)]
    cap = max(0, min(int(max_links), 1000))
    rows_by_identity: dict[tuple[str, str, str, str], Mapping[str, object]] = {}
    identities_by_prefix: dict[tuple[str, str, int, str], list[Mapping[str, object]]] = {}
    markers: list[dict[str, object]] = []
    marker_by_identity: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for row in bounded_nodes:
        identity = _identity(row)
        if not identity[0] or not identity[1] or not identity[3] or identity in rows_by_identity:
            continue
        rows_by_identity[identity] = row
        source_ref = _marker_id(row)
        raw = row.get("raw_payload")
        raw_map = raw if isinstance(raw, Mapping) else {}
        label = next(
            (_text(row.get(key)) for key in ("callsign", "short_name", "long_name", "node_id") if _text(row.get(key))),
            "Mesh node",
        )
        coords = _coordinates(row)
        marker: dict[str, object] = {
            "id": source_ref,
            "type": "station",
            "callsign": source_ref,
            "source_ref": source_ref,
            "label": label,
            "lat": coords[0] if coords else None,
            "lon": coords[1] if coords else None,
            "source_family": _text(row.get("transport")).lower(),
            "kind": "station",
            "is_mesh_node": True,
            "is_mesh_self": _strict_true(raw_map.get("is_self")),
            "provenance": {
                "adapter_id": _text(row.get("adapter_id")),
                "transport": _text(row.get("transport")).lower(),
                "node_id": _text(row.get("node_id")),
                "public_key_or_hash": _text(row.get("public_key_or_hash")),
                "last_heard_utc": _text(row.get("last_heard_utc")),
            },
        }
        markers.append(marker)
        marker_by_identity[identity] = marker
        public_key = _text(row.get("public_key_or_hash")).lower()
        # A node's full key can be matched by 1, 2, or 3-byte route prefixes.
        for size in (1, 2, 3):
            key_prefix = public_key[: size * 2]
            if len(key_prefix) == size * 2 and all(ch in "0123456789abcdef" for ch in key_prefix):
                scope = (identity[0], identity[1], size, key_prefix)
                identities_by_prefix.setdefault(scope, []).append(row)

    self_rows: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for row in rows_by_identity.values():
        raw = row.get("raw_payload")
        if isinstance(raw, Mapping) and _strict_true(raw.get("is_self")):
            self_rows.setdefault((_text(row.get("adapter_id")), _text(row.get("transport")).lower()), []).append(row)

    raw_links: list[dict[str, object]] = []
    observed_links: list[dict[str, object]] = []
    seen_cached_candidates: set[tuple[object, object]] = set()
    seen_observed_candidates: set[tuple[object, object]] = set()
    omitted_candidates = 0
    known_routes = 0
    unmapped_routes = 0
    unresolved_hops = 0
    ambiguous_hops = 0
    located_route_pairs = 0
    routes_total = 0
    observed_unresolved_hops = 0
    observed_ambiguous_hops = 0
    for contact in (rows_by_identity.values() if include_paths else ()):
        route = _route(contact)
        raw_payload = contact.get("raw_payload")
        if isinstance(raw_payload, Mapping) and isinstance(raw_payload.get("outgoing_route"), Mapping):
            routes_total += 1
        if not _strict_true(route.get("known")):
            continue
        known_routes += 1
        hash_size = _small_int(route.get("hash_size"))
        if hash_size is None:
            hash_size = 0
        raw_hops = route.get("path_hops")
        hops = tuple(raw_hops) if isinstance(raw_hops, Sequence) and not isinstance(raw_hops, (str, bytes, bytearray)) else ()
        hop_count = _small_int(route.get("hop_count"))
        if hop_count is None:
            hop_count = -1
        scope = (_text(contact.get("adapter_id")), _text(contact.get("transport")).lower())
        selves = self_rows.get(scope, [])
        route_usable = 0 <= hop_count <= 64 and hop_count == len(hops) and 1 <= hash_size <= 3
        if not route_usable:
            unresolved_hops += max(1, len(hops))
        if len(selves) != 1:
            unresolved_hops += 1
        chain: list[Mapping[str, object] | None] = []
        if len(selves) == 1:
            chain.append(selves[0])
        else:
            chain.append(None)
        if route_usable:
            for raw_hop in hops:
                prefix = _prefix(raw_hop, hash_size)
                matches = identities_by_prefix.get((scope[0], scope[1], hash_size, prefix), ()) if prefix else ()
                if len(matches) == 1:
                    chain.append(matches[0])
                else:
                    unresolved_hops += 1
                    if len(matches) > 1:
                        ambiguous_hops += 1
                    chain.append(None)
        else:
            # Invalid route metadata cannot be reinterpreted as a direct path.
            chain.append(None)
        chain.append(contact)
        route_pairs = 0
        for first, second in zip(chain, chain[1:]):
            if first is None or second is None:
                continue
            first_id = _identity(first)
            second_id = _identity(second)
            first_marker = marker_by_identity.get(first_id)
            second_marker = marker_by_identity.get(second_id)
            first_coords = _coordinates(first)
            second_coords = _coordinates(second)
            if first_marker is None or second_marker is None or first_coords is None or second_coords is None:
                unresolved_hops += 1
                continue
            route_pairs += 1
            stamp = _route_stamp(route)
            first_label = str(first_marker["label"])
            second_label = str(second_marker["label"])
            segment = (first_marker["callsign"], second_marker["callsign"])
            if segment in seen_cached_candidates:
                continue
            if len(raw_links) >= _MAX_CACHED_LINK_CANDIDATES:
                omitted_candidates += 1
                projection_limited = True
                continue
            seen_cached_candidates.add(segment)
            raw_links.append({
                "id": f"mesh-route:{quote(first_marker['source_ref'], safe='')}->{quote(second_marker['source_ref'], safe='')}",
                "origin": first_marker["callsign"],
                "destination": second_marker["callsign"],
                "origin_label": first_label,
                "destination_label": second_label,
                "lat1": first_coords[0],
                "lon1": first_coords[1],
                "lat2": second_coords[0],
                "lon2": second_coords[1],
                "source_family": "meshcore",
                "evidence_kind": "cached_route",
                "direction_known": True,
                "updated_utc": stamp,
                "title": f"Cached outgoing route: {first_label} → {second_label}",
                "tooltip": f"MeshCore cached outgoing route · {first_label} → {second_label}" + (f" · {stamp}" if stamp else ""),
                "route_source_ref": _marker_id(contact),
            })
        if route_pairs:
            located_route_pairs += 1
        else:
            unmapped_routes += 1

    # Bound total packet records across the projection. Stored records are
    # newest first within each node; retain that order and at most 32 per node.
    observed_packet_records: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for source in (rows_by_identity.values() if include_paths else ()):
        raw = source.get("raw_payload")
        raw_map = raw if isinstance(raw, Mapping) else {}
        received_paths = raw_map.get("received_paths")
        if not isinstance(received_paths, Sequence) or isinstance(received_paths, (str, bytes, bytearray)):
            continue
        if len(received_paths) > _MAX_RECEIVED_PATHS_PER_NODE:
            projection_limited = True
        node_records = reversed(received_paths[-_MAX_RECEIVED_PATHS_PER_NODE:])
        for packet_path in node_records:
            if isinstance(packet_path, Mapping):
                if len(observed_packet_records) >= _MAX_RECEIVED_PATH_RECORDS:
                    projection_limited = True
                    break
                observed_packet_records.append((source, packet_path))

    # Receive paths contain only the hops observed in that packet record.
    # Never attach a sender, recipient, or companion to either end.
    for source, packet_path in observed_packet_records:
        source_ref = _marker_id(source)
        scope = (_text(source.get("adapter_id")), _text(source.get("transport")).lower())
        if packet_path.get("evidence_kind") != "observed_packet_path":
            continue
        hash_size = _small_int(packet_path.get("hash_size"))
        path_hops = packet_path.get("path_hops")
        if (
            hash_size is None
            or not 1 <= hash_size <= 3
            or not isinstance(path_hops, Sequence)
            or isinstance(path_hops, (str, bytes, bytearray))
        ):
            continue
        bounded_hops = path_hops[:_MAX_HOPS_PER_RECEIVED_PATH]
        if len(path_hops) < 2 or len(path_hops) > _MAX_HOPS_PER_RECEIVED_PATH:
            observed_unresolved_hops += 1
            continue
        resolved: list[tuple[Mapping[str, object], tuple[float, float]] | None] = []
        for raw_hop in bounded_hops:
            prefix = _prefix(raw_hop, hash_size)
            matches = identities_by_prefix.get((scope[0], scope[1], hash_size, prefix), ()) if prefix else ()
            if len(matches) != 1:
                observed_unresolved_hops += 1
                if len(matches) > 1:
                    observed_ambiguous_hops += 1
                resolved.append(None)
                continue
            match = matches[0]
            coords = _coordinates(match)
            if coords is None:
                observed_unresolved_hops += 1
                resolved.append(None)
                continue
            resolved.append((match, coords))
        timestamp = _text(packet_path.get("timestamp"))
        snr = _finite_number(packet_path.get("snr"))
        rssi = _finite_number(packet_path.get("rssi"))
        detail_parts = ["Observed packet path"]
        if timestamp:
            detail_parts.append(timestamp)
        if rssi is not None:
            detail_parts.append(f"RSSI {_metric_text(rssi)} dBm")
        if snr is not None:
            detail_parts.append(f"SNR {_metric_text(snr)} dB")
        tooltip = " · ".join(detail_parts)
        for first, second in zip(resolved, resolved[1:]):
            if first is None or second is None:
                continue
            first_row, first_coords = first
            second_row, second_coords = second
            first_marker = marker_by_identity.get(_identity(first_row))
            second_marker = marker_by_identity.get(_identity(second_row))
            if first_marker is None or second_marker is None:
                observed_unresolved_hops += 1
                continue
            origin = first_marker["callsign"]
            destination = second_marker["callsign"]
            segment = (origin, destination)
            if segment in seen_observed_candidates:
                continue
            if len(observed_links) >= _MAX_OBSERVED_LINK_CANDIDATES:
                omitted_candidates += 1
                projection_limited = True
                continue
            seen_observed_candidates.add(segment)
            observed_links.append({
                "id": f"mesh-observed:{quote(origin, safe='')}->{quote(destination, safe='')}",
                "origin": origin,
                "destination": destination,
                "origin_label": str(first_marker["label"]),
                "destination_label": str(second_marker["label"]),
                "lat1": first_coords[0],
                "lon1": first_coords[1],
                "lat2": second_coords[0],
                "lon2": second_coords[1],
                "source_family": "meshcore",
                "evidence_kind": "observed_packet_path",
                "direction_known": True,
                "timestamp": timestamp,
                "updated_utc": timestamp,
                "snr": snr,
                "rssi": rssi,
                "title": "Observed packet path",
                "tooltip": tooltip,
                "route_source_ref": source_ref,
            })

    # Deduplicate within each evidence class without merging provenance.
    # Observed paths have display priority if the shared cap is reached.
    deduped: list[dict[str, object]] = []
    seen_segments: set[tuple[object, object, object]] = set()
    for link in (*observed_links, *raw_links):
        segment = (link["origin"], link["destination"], link["evidence_kind"])
        if segment not in seen_segments:
            seen_segments.add(segment)
            deduped.append(link)
    links = deduped[:cap]
    return {
        "markers": markers,
        "links": links,
        "summary": {
            "nodes": len(markers),
            "routes": routes_total,
            "known": known_routes,
            "mappable": located_route_pairs,
            "unmapped": unmapped_routes,
            "known_routes": known_routes,
            "mappable_routes": located_route_pairs,
            "unmapped_routes": unmapped_routes,
            "unresolved_hops": unresolved_hops + observed_unresolved_hops,
            "ambiguous_hops": ambiguous_hops + observed_ambiguous_hops,
            "observed_links": sum(1 for link in links if link["evidence_kind"] == "observed_packet_path"),
            "observed_unresolved_hops": observed_unresolved_hops,
            "omitted_links": max(0, len(deduped) - len(links)),
            "omitted_candidates": omitted_candidates,
            "projection_limited": projection_limited,
        },
    }
