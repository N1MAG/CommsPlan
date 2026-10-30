"""Qt-free readable radio programming reference snapshots."""
from __future__ import annotations

from freqinout.core.resource_catalog_models import FrequencyResource


def frequency_mhz_text(value_hz: int | None) -> str:
    """Format a stored Hz value for operators without locale thousands separators."""
    if value_hz is None:
        return "Frequency unavailable"
    mhz = int(value_hz) / 1_000_000
    return f"{mhz:.6f}".rstrip("0").rstrip(".") + " MHz"


def frequency_programming_text(resource: FrequencyResource) -> str:
    """Readable snapshot suitable for a manual-programming SOP procedure."""
    receive = resource.receive_hz or resource.center_hz
    parts = [resource.label, f"RX {frequency_mhz_text(receive)}"]
    if resource.transmit_hz is None:
        parts.append("TX disabled or unspecified — consult source")
    else:
        parts.append(f"TX {frequency_mhz_text(resource.transmit_hz)}")
    if resource.offset_hz:
        if resource.transmit_hz is None or receive is None or resource.transmit_hz == receive:
            parts.append(f"Offset {frequency_mhz_text(resource.offset_hz)} (direction unspecified)")
        else:
            parts.append(f"Offset {'+' if resource.transmit_hz > receive else '-'}{frequency_mhz_text(resource.offset_hz)}")
    for name, value in (("Mode", resource.mode), ("Tone", resource.tone),
                        ("Bandwidth Hz", resource.bandwidth_hz), ("Channel", resource.channel),
                        ("Location", resource.locality), ("Notes", resource.notes)):
        if value is not None and value != "":
            parts.append(f"{name}: {value}")
    return " · ".join(parts)
