"""Pure validation of optional JS8 receive context; no current-radio fallback."""
from __future__ import annotations

import math
from typing import Mapping


def finite_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def receive_context(*, sender: str, frequency_hz: object = None,
                    snr: object = None, kind: str = "",
                    received_ts: object = None) -> dict[str, object]:
    frequency = finite_number(frequency_hz)
    if frequency is not None and not 100_000 <= frequency <= 1_300_000_000:
        frequency = None
    signal = finite_number(snr)
    if signal is not None and not -100 <= signal <= 100:
        signal = None
    receipt_time = finite_number(received_ts)
    if receipt_time is not None and receipt_time <= 0:
        receipt_time = None
    band = None
    if frequency is not None:
        mhz = frequency / 1_000_000
        for name, low, high in (
            ("160M", 1.8, 2.0), ("80M", 3.5, 4.0), ("60M", 5.0, 5.5),
            ("40M", 7.0, 7.3), ("30M", 10.1, 10.15), ("20M", 14.0, 14.35),
            ("17M", 18.068, 18.168), ("15M", 21.0, 21.45), ("12M", 24.89, 24.99),
            ("10M", 28.0, 29.7), ("6M", 50.0, 54.0), ("2M", 144.0, 148.0),
        ):
            if low <= mhz <= high:
                band = name
                break
    return {
        "rx_freq_hz": frequency, "rx_band": band, "rx_snr": signal,
        "rx_sender": str(sender or "").strip().upper() or None,
        "receipt_kind": kind if kind in {"directed_txt", "rx_directed"} else None,
        "received_ts": receipt_time,
    }


def api_receive_frequency(params: Mapping[str, object]) -> float | None:
    """Prefer explicit dial; otherwise remove a supplied audio offset from FREQ."""
    dial = finite_number(params.get("DIAL"))
    if dial is not None:
        return dial
    frequency = finite_number(params.get("FREQ"))
    offset = finite_number(params.get("OFFSET"))
    return frequency - offset if frequency is not None and offset is not None else frequency
