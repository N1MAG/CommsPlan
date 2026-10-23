"""Pure geographic guidance, independent of propagation or delivery evidence."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class GeographicGuidance:
    distance_km: float
    distance_miles: float
    initial_true_bearing: float | None


def _coordinate(value: object) -> tuple[float, float] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    if any(isinstance(component, bool) for component in value):
        return None
    try:
        lat, lon = float(value[0]), float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return lat, lon


def geographic_guidance(origin: object, destination: object) -> GeographicGuidance | None:
    """Great-circle distance and initial true bearing on the mean Earth sphere.

    Missing coordinates return None. Coincident/antipodal locations have no
    unique initial bearing; zero coordinates themselves are valid positions.
    """
    start, end = _coordinate(origin), _coordinate(destination)
    if start is None or end is None:
        return None
    lat1, lon1 = map(math.radians, start)
    lat2, lon2 = map(math.radians, end)
    delta = lon2 - lon1
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2) ** 2
    angle = 2 * math.atan2(math.sqrt(max(0, min(1, a))), math.sqrt(max(0, 1 - min(1, a))))
    distance = 6371.0088 * angle
    y = math.sin(delta) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta)
    bearing = None if angle < 1e-10 or abs(math.pi - angle) < 1e-7 or math.hypot(x, y) < 1e-12 else math.degrees(math.atan2(y, x)) % 360
    return GeographicGuidance(distance, distance / 1.609344, bearing)
