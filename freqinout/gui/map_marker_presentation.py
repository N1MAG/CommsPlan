"""Pure, bounded presentation helpers for native Map marker text.

The native renderer receives retained marker dictionaries from several older
projections.  Convert their historical HTML-shaped tooltip values once while
publishing the snapshot, so QML can treat the result as ordinary text.
"""

from __future__ import annotations

import html
import re
from typing import Mapping


_MAX_INPUT_CHARS = 8_192
_MAX_OUTPUT_CHARS = 2_048
_MAX_LINES = 12
_BR_TAG = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")


def plain_tooltip_text(value: object, *, max_lines: int = _MAX_LINES) -> str:
    """Return bounded plain text from legacy tooltip-shaped input.

    The conversion intentionally recognizes only line-break tags.  All other
    markup is discarded, never handed to a rich-text control, and entities are
    decoded before line-break recognition.  This keeps existing ``<br/>``
    evidence readable without allowing a marker payload to execute markup.
    """
    text = str(value or "")[:_MAX_INPUT_CHARS]
    for _ in range(2):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = _BR_TAG.sub("\n", text)
    text = _TAG.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    limit = max(1, int(max_lines))
    clean_lines = [line for line in lines if line][:limit]
    return "\n".join(clean_lines)[:_MAX_OUTPUT_CHARS]


def marker_tooltip_text(marker: Mapping[str, object]) -> str:
    """Format one marker tooltip from retained fields without changing identity.

    Own-station fields are authoritative because legacy station titles already
    repeat the callsign.  Other marker kinds retain their existing tooltip
    semantics after the plain-text compatibility conversion.
    """
    legacy = plain_tooltip_text(
        marker.get("tooltip")
        or marker.get("title")
        or marker.get("label")
        or marker.get("callsign")
        or "Map item"
    )
    if not bool(marker.get("is_own_station")):
        return legacy

    callsign = plain_tooltip_text(marker.get("callsign"), max_lines=1)
    if not callsign:
        return legacy
    lines = [f"{callsign} · My station"]
    name = plain_tooltip_text(marker.get("name"), max_lines=1)
    region = plain_tooltip_text(
        marker.get("region") or marker.get("state") or marker.get("country"),
        max_lines=1,
    )
    if name and region:
        lines.append(f"{name} · {region}")
    elif name or region:
        lines.append(name or region)
    grid = plain_tooltip_text(marker.get("grid"), max_lines=1)
    if grid:
        lines.append(grid)
    location = marker.get("location_source")
    if isinstance(location, Mapping):
        location = location.get("label") or location.get("type")
    location_text = plain_tooltip_text(location, max_lines=1)
    if location_text:
        lines.append(location_text)

    # Keep useful legacy evidence, but remove station fields already presented
    # structurally.  A mesh-own marker can still expose its saved-grid note.
    known = {line.casefold() for line in lines}
    for line in legacy.splitlines():
        compact = line.strip()
        lowered = compact.casefold()
        if lowered in known or lowered == callsign.casefold():
            continue
        if lowered.startswith("my station ·"):
            compact = compact.split("·", 1)[-1].strip()
            lowered = compact.casefold()
        if not compact or lowered in known or lowered == callsign.casefold():
            continue
        if name and lowered == f"name: {name}".casefold():
            continue
        if region and lowered in {
            f"state: {region}".casefold(),
            f"region: {region}".casefold(),
            f"country: {region}".casefold(),
        }:
            continue
        if grid and lowered == f"grid: {grid}".casefold():
            continue
        lines.append(compact)
        known.add(lowered)
        if len(lines) >= _MAX_LINES:
            break
    return "\n".join(lines)[:_MAX_OUTPUT_CHARS]
