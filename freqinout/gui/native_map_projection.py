"""Bounded, worker-safe overlay projection for the native Map renderer.

The functions in this module perform no Qt work. Static geometry is read and
simplified only on the Map projection worker, then cached as immutable value
snapshots. The GUI thread receives bounded Python collections ready for QML.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_ASSET_DIR = Path(__file__).resolve().parents[2] / "config" / "leaflet"
_MAX_POINTS_PER_RING = 220
_MAX_POLYGONS = 180
_MAX_BASE_POLYGONS = 220
_MAX_BASE_POINTS_PER_RING = 120
_MAX_RINGS_PER_FEATURE = 6
_MAX_LABELS = 400
_MAX_STATE_LABELS = 64
_MAX_LEGEND_ITEMS = 12
_GRID_LAT_MIN = -85
_GRID_LAT_MAX = 85
_GRID_LON_MIN = -180
_GRID_LON_MAX = 180

_US_STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}

_CANADA_CODES = {
    "Alberta": "AB", "British Columbia": "BC", "Manitoba": "MB", "New Brunswick": "NB",
    "Newfoundland and Labrador": "NL", "Northwest Territories": "NT", "Nova Scotia": "NS",
    "Nunavut": "NU", "Ontario": "ON", "Prince Edward Island": "PE", "Quebec": "QC",
    "Saskatchewan": "SK", "Yukon": "YT",
}

_FEMA_REGION_BY_STATE = {
    state: region
    for region, states in {
        "R01": ("CT", "ME", "MA", "NH", "RI", "VT"),
        "R02": ("NJ", "NY", "PR", "VI"),
        "R03": ("DC", "DE", "MD", "PA", "VA", "WV"),
        "R04": ("AL", "FL", "GA", "KY", "MS", "NC", "SC", "TN"),
        "R05": ("IL", "IN", "MI", "MN", "OH", "WI"),
        "R06": ("AR", "LA", "NM", "OK", "TX"),
        "R07": ("IA", "KS", "MO", "NE"),
        "R08": ("CO", "MT", "ND", "SD", "UT", "WY"),
        "R09": ("AZ", "CA", "HI", "NV", "GU", "AS", "MP"),
        "R10": ("AK", "ID", "OR", "WA"),
    }.items()
    for state in states
}

# These retain the visual language of the previous Leaflet layer: five clear
# SNR bands, with green reserved for the strongest observed RF paths.  They
# are literal colours rather than theme roles because their meaning is data,
# not application chrome.
_LINK_SNR_LEGEND = (
    (">= 5 dB", "#1b5e20"),
    ("0 to <5 dB", "#2e7d32"),
    ("-5 to <0 dB", "#fbc02d"),
    ("-10 to <-5 dB", "#f57c00"),
    ("< -10 dB", "#c62828"),
)
_FEMA_REGION_COLORS = {
    "R01": "#1565c0", "R02": "#6a1b9a", "R03": "#00838f",
    "R04": "#2e7d32", "R05": "#558b2f", "R06": "#ef6c00",
    "R07": "#6d4c41", "R08": "#5e35b1", "R09": "#c62828",
    "R10": "#0277bd",
}
_REGIONAL_LEVEL_STYLE = {
    "gray": ("#78909c", "muted"),
    "green": ("#43a047", "success"),
    "blue": ("#1e88e5", "info"),
    "yellow": ("#fbc02d", "warning"),
    "orange": ("#ef6c00", "warning"),
    "red": ("#d32f2f", "danger"),
}
_SITREP_STATUS_STYLE = {
    "red": ("#d32f2f", "danger"),
    "yellow": ("#fbc02d", "warning"),
    "green": ("#43a047", "success"),
    "unknown": ("#4fc3f7", "info"),
}


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _outer_rings(geometry: object) -> Iterable[Sequence[Sequence[object]]]:
    if not isinstance(geometry, Mapping):
        return ()
    geometry_type = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, Sequence):
        return ()
    if geometry_type == "Polygon":
        return (coordinates[0],) if coordinates and isinstance(coordinates[0], Sequence) else ()
    if geometry_type == "MultiPolygon":
        return tuple(
            polygon[0]
            for polygon in coordinates
            if isinstance(polygon, Sequence) and polygon and isinstance(polygon[0], Sequence)
        )
    return ()


def _simplify_ring(ring: Sequence[Sequence[object]]) -> list[dict[str, float]]:
    points: list[dict[str, float]] = []
    for raw in ring:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)) or len(raw) < 2:
            continue
        lon = _number(raw[0], 999.0)
        lat = _number(raw[1], 999.0)
        if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            points.append({"lat": lat, "lon": lon})
    if len(points) <= _MAX_POINTS_PER_RING:
        return points
    stride = max(1, (len(points) + _MAX_POINTS_PER_RING - 1) // _MAX_POINTS_PER_RING)
    simplified = points[::stride]
    if points[-1] != simplified[-1]:
        simplified.append(points[-1])
    return simplified[: _MAX_POINTS_PER_RING + 1]


def _ring_area(ring: Sequence[Sequence[object]]) -> float:
    """Rank bundled exterior pieces by area, independent of vertex density."""
    points: list[tuple[float, float]] = []
    for raw in ring:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)) or len(raw) < 2:
            continue
        lon, lat = _number(raw[0], 999.0), _number(raw[1], 999.0)
        if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            points.append((lon, lat))
    if len(points) < 3:
        return 0.0
    return abs(sum(
        point[0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * point[1]
        for index, point in enumerate(points)
    )) / 2.0


@lru_cache(maxsize=4)
def _geometry_features(filename: str) -> tuple[dict[str, object], ...]:
    path = _ASSET_DIR / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ()
    output: list[dict[str, object]] = []
    for feature_index, feature in enumerate(payload.get("features", ())):
        if not isinstance(feature, Mapping):
            continue
        properties = feature.get("properties")
        props = dict(properties) if isinstance(properties, Mapping) else {}
        name = str(props.get("name") or props.get("NAME") or "").strip()
        code = _US_STATE_CODES.get(name) or _CANADA_CODES.get(name) or str(
            props.get("state_abbrev") or props.get("postal") or ""
        ).strip().upper()
        region = str(props.get("fema_region") or "").strip().upper()
        if region and not region.startswith("R"):
            region = f"R{region.zfill(2)}"
        # Source order often starts with tiny islands, with the mainland last.
        # Rank original geometry before decimation; retain source IDs for hits.
        ranked_rings = sorted(
            ((index, ring, _ring_area(ring)) for index, ring in
             enumerate(_outer_rings(feature.get("geometry")))),
            key=lambda item: -item[2],
        )
        retained = 0
        for ring_index, ring, area in ranked_rings:
            if area <= 0.0:
                continue
            points = _simplify_ring(ring)
            if len(points) < 3:
                continue
            output.append(
                {
                    "id": f"{filename}:{feature_index}:{ring_index}",
                    "name": name,
                    "area_code": code,
                    "region": region,
                    "points": points,
                }
            )
            if len(output) >= _MAX_POLYGONS:
                return tuple(output)
            retained += 1
            if retained >= _MAX_RINGS_PER_FEATURE:
                break
    return tuple(output)


def _best_band(entry: object) -> tuple[str, float]:
    bands = entry.get("bands") if isinstance(entry, Mapping) else None
    if not isinstance(bands, Mapping) or not bands:
        return "", 0.0
    scored = [(str(band), _number(score)) for band, score in bands.items()]
    return max(scored, key=lambda item: item[1])


def _regional_level(area_code: str, regional: Mapping[str, object]) -> str:
    states = regional.get("states")
    rollup = states.get(area_code) if isinstance(states, Mapping) else None
    return str(rollup.get("level") or "") if isinstance(rollup, Mapping) else ""


def _label_coordinate(points: object) -> dict[str, float] | None:
    """Return a cheap, deterministic in-ring label anchor approximation.

    Geometry arrives already bounded and simplified.  A bounds midpoint is
    stable across refreshes and avoids expensive polygon-centroid work on the
    projection worker.  The label is decorative; the underlying state polygon
    remains the selectable hit target.
    """
    if not isinstance(points, Sequence) or not points:
        return None
    latitudes = [_number(point.get("lat"), 999.0) for point in points if isinstance(point, Mapping)]
    longitudes = [_number(point.get("lon"), 999.0) for point in points if isinstance(point, Mapping)]
    latitudes = [value for value in latitudes if -90.0 <= value <= 90.0]
    longitudes = [value for value in longitudes if -180.0 <= value <= 180.0]
    if not latitudes or not longitudes:
        return None
    return {"lat": (min(latitudes) + max(latitudes)) / 2.0, "lon": (min(longitudes) + max(longitudes)) / 2.0}


@lru_cache(maxsize=1)
def _state_and_region_labels() -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    """Cache one state abbreviation and one FEMA label per region.

    Region labels are calculated from the most substantial ring of each state,
    preventing island fragments from moving a label unexpectedly.  No file
    read happens after this worker-cache is populated.
    """
    by_state: dict[str, dict[str, object]] = {}
    for feature in _geometry_features("us_states.geojson"):
        code = str(feature.get("area_code") or "").strip().upper()
        points = feature.get("points")
        if not code or not isinstance(points, Sequence):
            continue
        current = by_state.get(code)
        if current is None:
            by_state[code] = dict(feature)
    state_labels: list[dict[str, object]] = []
    region_points: dict[str, list[dict[str, float]]] = {}
    for code in sorted(by_state):
        feature = by_state[code]
        anchor = _label_coordinate(feature.get("points"))
        if anchor is None:
            continue
        state_labels.append(
            {"id": f"state-label:{code}", "kind": "state", "text": code, **anchor, "min_zoom": 3.5}
        )
        region = str(feature.get("region") or _FEMA_REGION_BY_STATE.get(code, "")).upper()
        if region in _FEMA_REGION_COLORS:
            region_points.setdefault(region, []).append(anchor)
    region_labels: list[dict[str, object]] = []
    for region in sorted(_FEMA_REGION_COLORS):
        anchors = region_points.get(region, [])
        if not anchors:
            continue
        region_labels.append(
            {
                "id": f"fema-label:{region}",
                "kind": "fema_region",
                "text": region,
                "lat": sum(point["lat"] for point in anchors) / len(anchors),
                "lon": sum(point["lon"] for point in anchors) / len(anchors),
                "color": _FEMA_REGION_COLORS[region],
                "color_role": "info",
                "min_zoom": 2.8,
            }
        )
    return tuple(state_labels[:_MAX_STATE_LABELS]), tuple(region_labels)


def _state_polygons(payload: Mapping[str, object]) -> list[dict[str, object]]:
    show_states = bool(payload.get("show_states"))
    show_regions = bool(payload.get("show_regions"))
    propagation_enabled = bool(payload.get("prop_overlay_enabled"))
    regional = payload.get("regional_intelligence")
    regional_payload = dict(regional) if isinstance(regional, Mapping) else {}
    regional_enabled = bool(regional_payload.get("enabled"))
    if not (show_states or show_regions or propagation_enabled or regional_enabled):
        return []
    state_scores = payload.get("prop_state_scores")
    state_score_map = state_scores if isinstance(state_scores, Mapping) else {}
    band_colors = payload.get("prop_band_colors")
    band_color_map = band_colors if isinstance(band_colors, Mapping) else {}
    polygons: list[dict[str, object]] = []
    sources = ("us_states.geojson", "canada_provinces.geojson") if show_states else ("us_states.geojson",)
    for filename in sources:
        for feature in _geometry_features(filename):
            code = str(feature.get("area_code") or "").strip().upper()
            region = str(feature.get("region") or _FEMA_REGION_BY_STATE.get(code, ""))
            level = _regional_level(code, regional_payload)
            band, score = _best_band(state_score_map.get(code))
            color_role = "muted"
            color = ""
            fill_opacity = 0.04 if show_states else 0.0
            kind = "state"
            # Regions is a real, independent FEMA overlay.  Each state keeps
            # its own payload/hit target, while the region colour makes the
            # ten operational areas immediately visible even when States is
            # off.  The R01–R10 labels are emitted separately below.
            if show_regions and region in _FEMA_REGION_COLORS:
                kind = "fema_region"
                color = _FEMA_REGION_COLORS[region]
                color_role = "info"
                fill_opacity = 0.16 if not show_states else 0.10
            # Match the proven Leaflet behavior: a visible FEMA overlay keeps
            # its region colors. Propagation then annotates the compact region
            # labels rather than silently replacing the selected layer.
            if propagation_enabled and band and not show_regions:
                color = str(band_color_map.get(band) or "")
                color_role = "info"
                fill_opacity = max(0.10, min(0.42, 0.10 + (score / 300.0)))
            if regional_enabled and level:
                normalized = level.lower()
                color, color_role = _REGIONAL_LEVEL_STYLE.get(
                    normalized,
                    ("#d32f2f", "danger") if normalized in {"critical", "severe"} else ("#fbc02d", "warning"),
                )
                fill_opacity = (
                    0.30 if normalized == "red" else 0.24 if normalized == "orange"
                    else 0.20 if normalized == "yellow" else 0.13 if normalized == "blue"
                    else 0.055
                )
            polygons.append(
                {
                    **feature,
                    "kind": kind,
                    "color": color,
                    "color_role": color_role,
                    "fill_opacity": fill_opacity,
                    "line_width": 1.6 if show_regions and region else 1.0,
                    "region": region,
                    "payload": {
                        "type": "regional_intelligence" if regional_enabled else "state",
                        "state": code,
                        "fema_region": region,
                        "label": str(feature.get("name") or code),
                        "level": level,
                        "best_band": band,
                        "score": score,
                    },
                }
            )
    return polygons[:_MAX_POLYGONS]


def _dynamic_legend(payload: Mapping[str, object]) -> list[dict[str, object]]:
    """Describe exactly the data and overlays visible in this snapshot.

    The native map is intentionally sparse.  A legend must never suggest a
    layer that is absent, and it must remain compact enough to leave the map
    usable.  Ordering reflects operator reading priority.
    """
    entries: list[dict[str, object]] = []

    def add(label: str, role: str = "info", color: str = "", *, heading: bool = False) -> None:
        if len(entries) >= _MAX_LEGEND_ITEMS:
            return
        entry: dict[str, object] = {"label": label, "color_role": role}
        if color:
            entry["color"] = color
        if heading:
            entry["heading"] = True
        entries.append(entry)

    def add_group(label: str, items: Sequence[Mapping[str, object]]) -> None:
        if len(entries) >= _MAX_LEGEND_ITEMS:
            return
        entries.append(
            {
                "label": label,
                "color_role": "text",
                "heading": True,
                "items": [dict(item) for item in items],
            }
        )

    paths = payload.get("paths", payload.get("links"))
    has_paths = (
        isinstance(paths, Sequence)
        and not isinstance(paths, (str, bytes, bytearray))
        and any(isinstance(item, Mapping) for item in paths)
    )
    markers = payload.get("markers")
    has_markers = (
        isinstance(markers, Sequence)
        and not isinstance(markers, (str, bytes, bytearray))
        and any(isinstance(item, Mapping) for item in markers)
    )
    has_reports = any(
        isinstance(payload.get(key), Sequence) and any(isinstance(row, Mapping) for row in payload[key])
        for key in ("weather_events", "alert_events", "infrastructure_events")
    )
    if (has_markers and not has_paths) or has_reports:
        status_items: list[dict[str, object]] = [
            {"label": "SitRep Status:", "color_role": "text", "heading": True}
        ]
        for status, label in (
            ("green", "Functioning"),
            ("yellow", "Partially Functioning"),
            ("red", "Not Functioning"),
            ("unknown", "Unknown / No Report"),
        ):
            color, role = _SITREP_STATUS_STYLE[status]
            status_items.append({"label": label, "color_role": role, "color": color})
        add_group("Stations" if has_markers else "Reports", status_items)
    if has_paths and payload.get("map_mode") == "mesh_nodes":
        if any(isinstance(link, Mapping) and link.get("evidence_kind") == "cached_route" for link in payload.get("links", ())):
            add("↝", "text_muted")
            entries[-1].update(symbol_only=True, tooltip="Cached outgoing routes. Direction is from companion toward contact; delivery and current reachability are unverified.")
        if any(isinstance(link, Mapping) and link.get("evidence_kind") == "observed_packet_path" for link in payload.get("links", ())):
            add("→", "info")
            entries[-1].update(symbol_only=True, tooltip="Observed flood packet hop order. Sender and recipient are not inferred; line color is not a per-hop signal measurement.")
    if has_paths and payload.get("map_mode") != "mesh_nodes":
        # Keep the complete historical RF key together.  It takes priority
        # over contextual layers when the bounded legend surface is full.
        for label, color in _LINK_SNR_LEGEND:
            add(label, "info", color)
        if payload.get("link_direction_markers"):
            add("→  ⇄", "text")
            entries[-1].update({
                "symbol_only": True,
                "tooltip": "→ Logged sender to addressee. ⇄ Traffic logged in both directions. Delivery and current reachability are unverified.",
            })

    if bool(payload.get("show_regions")):
        add("FEMA Regions", "info")
    # The provider-free basemap always draws geographic boundaries. Keep that
    # visible context honest even when the optional state-label overlay is off.
    add("Borders", "muted")
    if bool(payload.get("show_grids")):
        add("Maidenhead grid", "muted")
    if bool(payload.get("show_cities")) and bool(payload.get("show_city_labels", True)):
        add("Cities", "text_muted")
    if has_markers and has_paths:
        add("Stations", "accent")
    for key, label, role in (
        ("weather_events", "Weather", "info"),
        ("alert_events", "Alerts", "danger"),
        ("infrastructure_events", "Infrastructure", "warning"),
    ):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and any(isinstance(item, Mapping) for item in value):
            previous_count = len(entries)
            add(label, role)
            if len(entries) > previous_count:
                entries[-1]["symbol_only"] = True
                entries[-1]["tooltip"] = "Report symbols use the SitRep status colors."
    sitrep = payload.get("sitrep_state_summary")
    sitrep_roles = _sitrep_statuses(sitrep)
    for status in sitrep_roles:
        color, role = _SITREP_STATUS_STYLE[status]
        add(f"SitRep {status.title()}", role, color)
    regional = payload.get("regional_intelligence")
    if isinstance(regional, Mapping) and bool(regional.get("enabled")):
        add("Regional intelligence", "warning")
    if bool(payload.get("prop_overlay_enabled")):
        add("Propagation", "info")
    return entries[:_MAX_LEGEND_ITEMS]


def _sitrep_statuses(value: object) -> tuple[str, ...]:
    """Return statuses actually represented by the bounded SitRep rollup."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    counts = {status: 0 for status in _SITREP_STATUS_STYLE}
    for index, row in enumerate(value):
        if index >= 100:
            break
        if not isinstance(row, Mapping):
            continue
        for status in counts:
            counts[status] += int(max(0, _number(row.get(f"{status}_count"))))
    return tuple(status for status in ("red", "yellow", "green", "unknown") if counts[status] > 0)


def _sitrep_summary(payload: Mapping[str, object]) -> str:
    """Render the existing compact summary surface from state rollup data."""
    statuses = _sitrep_statuses(payload.get("sitrep_state_summary"))
    if not statuses:
        return ""
    rows = payload.get("sitrep_state_summary")
    totals = {status: 0 for status in statuses}
    state_count = 0
    source = rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)) else ()
    for index, row in enumerate(source):
        if index >= 100:
            break
        if not isinstance(row, Mapping):
            continue
        state_count += 1
        for status in totals:
            totals[status] += int(max(0, _number(row.get(f"{status}_count"))))
    parts = [f"{totals[status]} {status}" for status in statuses]
    group = str(payload.get("sitrep_summary_group") or "").strip().upper()
    scope = f" {group}" if group else ""
    return f"SitRep{scope}: {state_count} state{'s' if state_count != 1 else ''}; " + ", ".join(parts) + "."


@lru_cache(maxsize=1)
def _base_polygons() -> tuple[dict[str, object], ...]:
    """Return the always-present, provider-free North American basemap.

    These deliberately humble outlines are distinct from selectable state,
    propagation, and Regional Intel overlays. They give operators geographic
    context without a tile download, API key, or GUI-thread file access.
    """
    output: list[dict[str, object]] = []
    for filename in (
        "us_states.geojson",
        "canada_provinces.geojson",
        "mexico_states.geojson",
    ):
        for feature in _geometry_features(filename):
            points = list(feature.get("points") or ())
            if len(points) > _MAX_BASE_POINTS_PER_RING:
                stride = max(
                    1,
                    (len(points) + _MAX_BASE_POINTS_PER_RING - 1)
                    // _MAX_BASE_POINTS_PER_RING,
                )
                reduced = points[::stride]
                if points[-1] != reduced[-1]:
                    reduced.append(points[-1])
                points = reduced[: _MAX_BASE_POINTS_PER_RING + 1]
            output.append(
                {
                    "id": f"base:{feature.get('id')}",
                    "kind": "base",
                    "name": str(feature.get("name") or ""),
                    "area_code": str(feature.get("area_code") or ""),
                    "points": points,
                    "color_role": "muted",
                    "fill_opacity": 0.035,
                    "line_width": 0.8,
                    "payload": {
                        "type": "basemap",
                        "area": str(feature.get("area_code") or ""),
                        "label": str(feature.get("name") or ""),
                    },
                }
            )
            if len(output) >= _MAX_BASE_POLYGONS:
                return tuple(output)
    return tuple(output)


def _grid_projection() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    lines: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []
    for lon in range(_GRID_LON_MIN, _GRID_LON_MAX + 1, 20):
        lines.append({"id": f"grid-lon-{lon}", "kind": "grid", "color_role": "muted", "points": [{"lat": _GRID_LAT_MIN, "lon": lon}, {"lat": _GRID_LAT_MAX, "lon": lon}]})
    for lat in range(-80, 81, 10):
        for west,east in ((-180,-60),(-60,60),(60,180)):
            lines.append({"id": f"grid-lat-{lat}:{west}", "kind": "grid", "color_role": "muted", "points": [{"lat": lat, "lon": west}, {"lat": lat, "lon": east}]})
    for lon in range(_GRID_LON_MIN, _GRID_LON_MAX, 20):
        for lat in range(-90, 90, 10):
            field_lon = int((lon + 180) // 20)
            field_lat = int((lat + 90) // 10)
            if 0 <= field_lon < 18 and 0 <= field_lat < 18:
                label = chr(ord("A") + field_lon) + chr(ord("A") + field_lat)
                labels.append({"id": f"grid-label-{label}", "kind": "grid", "label": label, "lat": lat + 5.0, "lon": lon + 10.0, "min_zoom": 3.0})
    return lines, labels


def _regional_summary(payload: Mapping[str, object]) -> dict[str, object]:
    regional = payload.get("regional_intelligence")
    if not isinstance(regional, Mapping) or not regional.get("enabled"):
        return {}
    states = regional.get("states")
    rows = [dict(row) for row in states.values() if isinstance(row, Mapping)] if isinstance(states, Mapping) else []
    actionable = [row for row in rows if str(row.get("level") or "").lower() not in {"", "green", "normal"}]
    actionable.sort(key=lambda row: (-_number(row.get("score")), str(row.get("area_id") or "")))
    return {
        "title": "Regional intelligence",
        "summary": str(regional.get("summary") or "No active regional concerns from current evidence."),
        "rows": actionable[:5],
        "overflow_count": max(0, len(actionable) - 5),
    }


def build_native_overlay_projection(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a bounded copy enriched with static native overlay collections."""
    from freqinout.core.geographic_scope import filter_geographic_payload
    payload = filter_geographic_payload(payload, str(payload.get('_geographic_country') or ''))
    projected = dict(payload)
    projected["_native_projection_enriched"] = True
    marker_source = payload.get("markers")
    if isinstance(marker_source, Sequence) and not isinstance(marker_source, (str, bytes, bytearray)):
        station_markers: list[dict[str, object]] = []
        for raw in marker_source:
            if not isinstance(raw, Mapping):
                continue
            marker = dict(raw)
            status = str(marker.get("spotter_status_key") or "unknown").strip().lower()
            color, _role = _SITREP_STATUS_STYLE.get(status, _SITREP_STATUS_STYLE["unknown"])
            if not marker.get("is_mesh_node"):
                marker["color"] = color
            station_markers.append(marker)
        projected["markers"] = station_markers
    # Report symbols share the fixed status palette with station pins and the
    # legend, independently of light/dark application accent colors.
    for collection in ("weather_events", "alert_events", "infrastructure_events"):
        source = payload.get(collection)
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes, bytearray)):
            continue
        events = []
        for raw in source:
            if not isinstance(raw, Mapping):
                continue
            event = dict(raw)
            status = str(event.get("status") or event.get("severity") or "unknown").strip().lower()
            status = {"routine": "green", "caution": "yellow", "severe": "red"}.get(status, status)
            if status in _SITREP_STATUS_STYLE:
                event["color"], event["color_role"] = _SITREP_STATUS_STYLE[status]
            events.append(event)
        projected[collection] = events
    try:
        from freqinout.gui.world_geography import prepare_geography
        viewport = payload.get("geography_viewport") or {}
        geography = prepare_geography(payload.get("view"), viewport.get("width",1000), viewport.get("height",700),
                                      show_cities=bool(payload.get("show_cities")),
                                      min_population=float(payload.get("city_min_pop") or 0))
        projected.update(geography)
    except (OSError,ValueError,KeyError,TypeError):
        projected.pop("_world_geography",None)
        projected["base_polygons"] = [dict(item) for item in _base_polygons()]
    projected["polygons"] = _state_polygons(payload)
    state_labels, region_labels = _state_and_region_labels()
    projected["state_labels"] = [dict(item) for item in state_labels] if bool(payload.get("show_states")) else []
    if bool(payload.get("show_regions")):
        region_score_source = payload.get("prop_region_scores")
        region_scores = region_score_source if isinstance(region_score_source, Mapping) else {}
        projected_region_labels: list[dict[str, object]] = []
        for source in region_labels:
            item = dict(source)
            band, score = _best_band(region_scores.get(str(item.get("text") or "")))
            if bool(payload.get("prop_overlay_enabled")) and band:
                item["band"] = band
                item["score"] = score
                item["tooltip"] = f"{item['text']} propagation: {band} ({score:.0f})"
            projected_region_labels.append(item)
        projected["region_labels"] = projected_region_labels
    else:
        projected["region_labels"] = []
    if bool(payload.get("show_grids")):
        grid_lines, grid_labels = _grid_projection()
        projected["grid_lines"] = grid_lines
        projected["grid_labels"] = grid_labels if bool(payload.get("show_grid_labels", True)) else []
    else:
        projected["grid_lines"] = []
        projected["grid_labels"] = []
    city_labels = payload.get("city_labels")
    if isinstance(city_labels, Sequence) and not isinstance(city_labels, (str, bytes, bytearray)):
        # Highest-priority cities are considered first by QML's O(n) spatial
        # declutter pass.  This sort is deterministic and preserves a bounded
        # <=400 source list for every zoom level.
        cities = [dict(item) for item in city_labels if isinstance(item, Mapping)]
        cities.sort(
            key=lambda item: (
                -_number(item.get("population")),
                str(item.get("label") or item.get("name") or "").casefold(),
                _number(item.get("lat", item.get("latitude"))),
                _number(item.get("lon", item.get("lng", item.get("longitude")))),
            )
        )
        projected["city_labels"] = cities[:_MAX_LABELS]
    else:
        projected["city_labels"] = []
    if projected.get("_world_geography"):
        projected["city_labels"] = geography["city_labels"]
    projected["regional_summary"] = _regional_summary(payload)
    sitrep_summary = _sitrep_summary(payload)
    if sitrep_summary:
        # The native renderer already owns one non-modal summary surface.  A
        # brief SitRep count belongs there rather than in a second panel.
        projected["summary"] = sitrep_summary
    projected["legend"] = _dynamic_legend(payload)
    return projected
