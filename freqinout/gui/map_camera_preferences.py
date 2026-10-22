"""Value-only camera validation and explicit country focus calculations."""
import math


def validated_view(value):
    if not isinstance(value, dict):
        return None
    try:
        view = {key: float(value[key]) for key in ('lat','lon','zoom')}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (not all(math.isfinite(x) for x in view.values()) or not -85.05112878 <= view['lat'] <= 85.05112878
            or not -180 <= view['lon'] <= 180 or not 0 <= view['zoom'] <= 18):
        return None
    return view


def country_focus_view(bounds, width, height):
    try:
        west,south,east,north = map(float, bounds)
        if not all(math.isfinite(x) for x in (west,south,east,north)) or west >= east or south >= north:
            return None
        def y(lat):
            return math.asinh(math.tan(math.radians(max(-85.05112878,min(85.05112878,lat)))))
        low, high = y(south),y(north)
        zoom = min(math.log2(max(1,width-64) * 360 / (256*(east-west))),
                   math.log2(max(1,height-64) * 2*math.pi / (256*(high-low))))
        return validated_view(dict(lat=math.degrees(math.atan(math.sinh((low+high)/2))),
                                   lon=(west+east)/2, zoom=max(0,min(18,zoom))))
    except (TypeError,ValueError,OverflowError,ZeroDivisionError):
        return None
