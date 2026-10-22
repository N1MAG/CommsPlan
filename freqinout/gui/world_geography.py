"""Bundled geography preparation. No operator/event data or tile services.

Pure functions run on projection workers. GeographyLane publishes only the newest
viewport's bounded value snapshot, independently of operational event updates.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[2] / "config" / "leaflet"
MAX_POLYGONS = 480
MAX_POINTS = 160
MAX_LABELS = 400
MERCATOR_LIMIT = 85.05112878


def wrapped_longitude(lon):
    return (float(lon) + 180) % 360 - 180


def split_path(points):
    """Split a short-way map path at the dateline; preserve noncrossing inputs."""
    if not points:
        return []
    parts = [[dict(points[0])]]
    for start, end in zip(points, points[1:]):
        a, b = float(parts[-1][-1]["lon"]), float(end["lon"])
        delta = b - a
        if abs(abs(delta)-360) < 1e-9:
            parts[-1].append({**end,"lon":a})
            continue
        if abs(delta) > 180:
            adjusted = b - math.copysign(360, delta)
            boundary = 180 if adjusted > a else -180
            fraction = (boundary - a) / (adjusted - a)
            # Qt's Map reference surface draws straight Mercator segments.
            def y(lat):
                return math.asinh(math.tan(math.radians(max(-MERCATOR_LIMIT, min(MERCATOR_LIMIT, lat)))))
            latitude = math.degrees(math.atan(math.sinh(y(start["lat"]) + fraction * (y(end["lat"]) - y(start["lat"])))))
            parts[-1].append({"lat": latitude, "lon": boundary})
            parts.append([{"lat": latitude, "lon": -boundary}])
        parts[-1].append(dict(end))
    return [part for part in parts if len(part) >= 2]


def split_ring(points):
    """Clip an unwrapped exterior to world strips and the Mercator latitude range."""
    if len(points) < 3:
        return []
    unwrapped = []
    for point in points:
        lon, lat = float(point["lon"]), float(point["lat"])
        if unwrapped:
            lon += 360 * round((unwrapped[-1][0] - lon) / 360)
        unwrapped.append((lon, lat))

    def clip(ring, axis, boundary, greater):
        output = []
        for a, b in zip(ring[-1:] + ring[:-1], ring):
            ai, bi = (a[axis] >= boundary, b[axis] >= boundary) if greater else (a[axis] <= boundary, b[axis] <= boundary)
            if ai != bi:
                t = (boundary - a[axis]) / (b[axis] - a[axis])
                output.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            if bi:
                output.append(b)
        return output

    parts = []
    first = math.floor((min(p[0] for p in unwrapped) + 180) / 360)
    last = math.floor((max(p[0] for p in unwrapped) + 180) / 360)
    for strip in range(first, last + 1):
        ring = unwrapped
        for axis, boundary, greater in ((0, -180 + strip * 360, True), (0, 180 + strip * 360, False),
                                        (1, -MERCATOR_LIMIT, True), (1, MERCATOR_LIMIT, False)):
            ring = clip(ring, axis, boundary, greater) if ring else []
        if len(ring) >= 3:
            parts.append([{"lon": max(-180, min(180, lon - strip * 360)), "lat": lat} for lon, lat in ring])
    return parts


def _simplify(points, tolerance):
    """Ramer–Douglas–Peucker, bounded by the packaged source and final point cap."""
    if len(points) < 4:
        return points
    closed = points[0] == points[-1]
    values = points[:-1] if closed else points
    values = values + values[:1] if closed else values
    keep, pending = {0, len(values) - 1}, [(0, len(values) - 1)]
    while pending:
        start, end = pending.pop()
        a, b = values[start], values[end]
        dx, dy = b[0] - a[0], b[1] - a[1]
        denominator = dx * dx + dy * dy
        maximum, selected = tolerance * tolerance, None
        for index in range(start + 1, end):
            p = values[index]
            t = max(0, min(1, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / denominator)) if denominator else 0
            distance = (p[0]-a[0]-t*dx)**2 + (p[1]-a[1]-t*dy)**2
            if distance > maximum:
                maximum, selected = distance, index
        if selected is not None:
            keep.add(selected)
            pending.extend(((start, selected), (selected, end)))
    result = [values[i] for i in sorted(keep)]
    if len(result) < 4:
        result = points  # Tiny countries/islands must not vanish at world LOD.
    if len(result) > MAX_POINTS:
        stride = math.ceil((len(result)-1) / (MAX_POINTS-1))
        result = result[::stride] + result[-1:]
    return result


@lru_cache(maxsize=3)
def _countries(detail):
    data = json.loads((ASSETS / "world_countries.geojson").read_text(encoding="utf-8"))
    output = []
    tolerance = (0.20, 0.06, 0.015)[detail]
    for feature in data["features"]:
        props, geometry = feature["properties"], feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        rings = []
        for index, polygon in enumerate(polygons):
            # Decorative basemap uses exteriors; holes are not operational areas.
            raw = polygon[0]
            for piece, ring in enumerate(split_ring([{"lon": p[0], "lat": p[1]} for p in raw])):
                area = abs(sum(a["lon"]*b["lat"]-b["lon"]*a["lat"] for a,b in zip(ring[-1:]+ring[:-1],ring)))
                coords = _simplify([(p["lon"],p["lat"]) for p in ring], tolerance)
                if len(coords) < 3:
                    continue
                points = [{"lon": x,"lat": y} for x,y in coords]
                bbox = (min(p["lon"] for p in points), min(p["lat"] for p in points),
                        max(p["lon"] for p in points), max(p["lat"] for p in points))
                rings.append((area, index, piece, points, bbox))
        rings.sort(key=lambda r: (-r[0], r[1], r[2]))
        for rank, (_, index, piece, points, bbox) in enumerate(rings):
            output.append({"id": f"world:{props['ADM0_A3']}:{index}:{piece}", "kind": "base",
                           "name": props["name"], "area_code": props["ADM0_A3"], "points": points,
                           "bbox": bbox, "rank": rank, "color_role": "muted", "fill_opacity": 0.035})
    return tuple(output)


@lru_cache(maxsize=1)
def _places():
    data = json.loads((ASSETS / "ne_populated_places.geojson").read_text(encoding="utf-8"))
    cells = {}
    for index, feature in enumerate(data["features"]):
        props = feature["properties"]
        lon, lat = feature["geometry"]["coordinates"][:2]
        population = float(props.get("pop_max") or 0)
        place = {"id": f"world-city:{index}", "kind": "city", "text": props["name"], "label": props["name"],
                 "lat":lat, "lon":lon, "population":population, "min_zoom": 2 if props.get("adm0cap") else 4}
        cells.setdefault((math.floor(lon/10),math.floor(lat/10)), []).append(place)
    return cells


def viewport_bounds(view, width=1000, height=700):
    zoom = max(0, min(18, float(view.get("zoom", 3.6))))
    lon = wrapped_longitude(view.get("lon", -98.35))
    lat = max(-MERCATOR_LIMIT, min(MERCATOR_LIMIT, float(view.get("lat",39.5))))
    world = 256 * 2**zoom
    half_lon = min(180, (max(1,width)+160) / world * 180)
    y = math.asinh(math.tan(math.radians(lat)))
    half_y = (max(1,height)+120) / world * math.pi
    south = max(-MERCATOR_LIMIT, math.degrees(math.atan(math.sinh(y-half_y))))
    north = min(MERCATOR_LIMIT, math.degrees(math.atan(math.sinh(y+half_y))))
    if half_lon >= 180:
        intervals = [(-180,180)]
    else:
        west, east = lon-half_lon, lon+half_lon
        intervals = [(west,east)] if -180 <= west and east <=180 else [(wrapped_longitude(west),180),(-180,wrapped_longitude(east))]
    return intervals,south,north


def prepare_geography(view=None, width=1000, height=700, *, show_cities=False, min_population=0):
    view = dict(view or {"lat":39.5,"lon":-98.35,"zoom":3.6})
    intervals, south, north = viewport_bounds(view,width,height)
    zoom = float(view.get("zoom",3.6))
    detail = 0 if zoom < 3 else 1 if zoom < 5 else 2
    visible = [p for p in _countries(detail) if p["bbox"][3]>=south and p["bbox"][1]<=north
               and any(p["bbox"][2]>=west and p["bbox"][0]<=east for west,east in intervals)]
    # One visible piece of every declared country precedes secondary islands.
    # Country coverage cannot depend on source order or a global truncation.
    first_visible = {}
    for item in sorted(visible, key=lambda p:(p["rank"],p["id"])):
        first_visible.setdefault(item["area_code"], item["id"])
    visible.sort(key=lambda p:(p["id"] != first_visible[p["area_code"]],p["rank"],p["area_code"],p["id"]))
    polygons = [{k:v for k,v in p.items() if k not in ("bbox","rank")} for p in visible[:MAX_POLYGONS]]
    if zoom >= 3:
        # Retain the familiar North American subdivision context at regional
        # scales, using the existing worker cache. World level has no subdivisions.
        from freqinout.gui.native_map_projection import _base_polygons
        regional = []
        for item in _base_polygons():
            points = item["points"]
            if (max(p["lat"] for p in points)>=south and min(p["lat"] for p in points)<=north
                    and any(max(p["lon"] for p in points)>=west and min(p["lon"] for p in points)<=east for west,east in intervals)):
                regional.append(dict(item))
        regional = regional[:max(0,MAX_POLYGONS-len(first_visible))]
        polygons = polygons[:MAX_POLYGONS-len(regional)] + regional
    labels = []
    if show_cities:
        cells = _places()
        for (x,y), entries in cells.items():
            if (y+1)*10<south or y*10>north or not any((x+1)*10>=west and x*10<=east for west,east in intervals):
                continue
            labels.extend(p for p in entries if south<=p["lat"]<=north and p["population"]>=min_population
                          and p["min_zoom"]<=zoom and any(west<=p["lon"]<=east for west,east in intervals))
        labels.sort(key=lambda p:(-p["population"],p["text"],p["id"]))
    return {"base_polygons":polygons,"city_labels":labels[:MAX_LABELS],
            "geography_source":"Natural Earth v5.1.2 countries; bundled selected places (legacy vintage)", "_world_geography":True}


# Qt owner is separate from the pure worker functions and the native renderer.
from PySide6.QtCore import QObject, QTimer, Signal
import threading


class GeographyLane(QObject):
    ready = Signal(object)
    _done = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation = 0
        self._closed = False
        self._running = False
        self._pending = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._dispatch)
        self._done.connect(self._receive)

    def request(self, view, width, height, *, show_cities=False, min_population=0):
        if self._closed:
            return
        self._generation += 1
        self._pending = (self._generation, dict(view), width, height, show_cities, min_population)
        self._timer.start()

    def invalidate(self):
        self._generation += 1
        self._pending = None
        self._timer.stop()

    def shutdown(self):
        self._closed = True
        self.invalidate()

    def _dispatch(self):
        if self._closed or self._running or self._pending is None:
            return
        generation, view, width, height, show_cities, minimum = self._pending
        self._pending = None
        self._running = True
        signal = self._done
        def run():
            try:
                result = prepare_geography(view,width,height,show_cities=show_cities,min_population=minimum)
            except (OSError,ValueError,KeyError,TypeError):
                result = None  # Preserve previous geography; never erase events.
            try:
                signal.emit(generation,result)
            except RuntimeError:
                pass
        threading.Thread(target=run,daemon=True,name="world-geography").start()

    def _receive(self,generation,result):
        self._running = False
        if self._closed:
            return
        if generation == self._generation and result is not None:
            self.ready.emit(result)
        if self._pending is not None:
            self._timer.start()
