"""Worker-only plotted-country classification using bundled country geometry.

Never infer geography from a callsign, mailing region or license jurisdiction.
The 5-degree candidate index and bounded point cache are independent of paint.
"""
from functools import lru_cache
import json
import math
from pathlib import Path

_ASSET = Path(__file__).resolve().parents[2] / 'config/leaflet/world_countries.geojson'


def _inside(lon, lat, ring):
    result = False
    for a, b in zip(ring[-1:] + ring[:-1], ring):
        if (a[1] > lat) != (b[1] > lat):
            crossing = (b[0]-a[0]) * (lat-a[1]) / (b[1]-a[1]) + a[0]
            if lon < crossing:
                result = not result
    return result


@lru_cache(maxsize=1)
def _index():
    data = json.loads(_ASSET.read_text(encoding='utf8'))
    cells, names, bounds = {}, {}, {}
    for feature in data['features']:
        props, geom = feature['properties'], feature['geometry']
        code = props['ADM0_A3']
        names[code] = props['name']
        polygons = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
        for rings in polygons:
            ring = rings[0]
            west, east = min(p[0] for p in ring), max(p[0] for p in ring)
            south, north = min(p[1] for p in ring), max(p[1] for p in ring)
            bbox = west, south, east, north
            area = abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring[-1:]+ring[:-1],ring)))
            if code not in bounds or area > bounds[code][0]:
                bounds[code] = area, bbox
            item = code, bbox, rings
            for x in range(math.floor(west/5), math.floor(east/5)+1):
                for y in range(math.floor(south/5), math.floor(north/5)+1):
                    cells.setdefault((x,y), []).append(item)
    return cells, names, bounds


@lru_cache(maxsize=1)
def country_catalog():
    """Worker-owned metadata, including an explicit largest-landmass focus."""
    _, names, bounds = _index()
    return tuple({'code': code, 'name': name, 'bounds': bounds[code][1]}
                 for code, name in sorted(names.items(), key=lambda item:item[1]))


@lru_cache(maxsize=8192)
def classify_point(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            return ''
    except (TypeError, ValueError):
        return ''
    candidates = _index()[0].get((math.floor(lon/5), math.floor(lat/5)), ())
    matches = set()
    for code, (w,s,e,n), rings in candidates:
        if w <= lon <= e and s <= lat <= n and _inside(lon,lat,rings[0]) and not any(_inside(lon,lat,hole) for hole in rings[1:]):
            matches.add(code)
    return next(iter(matches)) if len(matches) == 1 else ''


def filter_geographic_payload(payload, country=''):
    """Filter copied operational evidence on its worker; enrich countries once."""
    result = dict(payload)
    try:
        result['_country_catalog'] = country_catalog()
    except (OSError, ValueError, KeyError, TypeError):
        # Global geometry is optional for the ordinary operational map. Never
        # silently broaden an explicitly requested geographic filter, however.
        if country:
            raise ValueError('Country geometry unavailable; clear Country to use the base map')
        result['_country_catalog'] = ()
        result['_country_scope_error'] = 'Country geometry unavailable'
    if not country:
        return result
    def matches(item):
        point = item.get('position') or item
        if not isinstance(point, dict):
            return False
        classified = classify_point(point.get('lat', point.get('latitude')), point.get('lon', point.get('longitude')))
        return classified == ('' if country == 'unknown' else country)
    for key in ('markers','weather_events','alert_events','infrastructure_events','license_references'):
        if key in result:
            result[key] = [item for item in result[key] if matches(item)]
    # Links crossing the selected geography remain meaningful; require at least
    # one qualified endpoint. No invented location for an unpositioned endpoint.
    if 'links' in result:
        def endpoints(link):
            return link.get('points') or ({'lat':link.get('lat1'), 'lon':link.get('lon1')},
                                          {'lat':link.get('lat2'), 'lon':link.get('lon2')})
        result['links'] = [link for link in result['links'] if any(matches(p) for p in endpoints(link))]
    return result
