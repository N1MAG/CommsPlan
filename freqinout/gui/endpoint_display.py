"""Pure, bounded presentation of endpoint evidence; never used as safety proof."""
from __future__ import annotations

import time
import math
from collections.abc import Mapping


def project_endpoint_display(evidence: Mapping, *, now_monotonic=None, ttl_s=30.0) -> dict:
    """Retain observations during normal polling, with independent field freshness.

    The worker owns immutable history. This projection performs no reads and
    never changes stale/generation/PTT safety evidence in the original mapping.
    """
    row = dict(evidence or {})
    if row.get("invalidated") or row.get("closed"):
        return {"display_state": "invalidated", "display_field_states": {},
                "display_observations": {}, "endpoint_key": row.get("endpoint_key")}
    now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    observations = row.get("display_observations") or {}
    # Compatibility for detached/older cached publishers, never stale fallback.
    if not observations and row.get("generation") and not row.get("stale", True):
        observations = {
            key: {"value": row[key], "collected_monotonic": row.get("collected_monotonic", 0)}
            for key in ("frequency_hz", "ptt_active", "vfo", "js8_frequency_hz",
                        "js8_offset_hz", "js8_busy", "fldigi_mode", "fldigi_offset_hz",
                        "fldigi_available", "varac_status")
            if row.get(key) is not None and (key != "ptt_active" or row.get("ptt_known"))
        }
    errors = row.get("errors") or {}
    states = {}
    for key, observation in observations.items():
        if not isinstance(observation, Mapping):
            continue
        row[key] = observation.get("value")
        try:
            stamp = float(observation.get("collected_monotonic") or 0)
            current = stamp > 0 and 0 <= now - stamp <= ttl_s
        except (TypeError, ValueError, OverflowError):
            current = False
        domain = ("fldigi" if key.startswith("fldigi") else "js8" if key.startswith("js8")
                  else "varac" if key == "varac_status" else "rig")
        failed = any(str(error).lower() in {"poll", "timeout"} or domain in str(error).lower()
                     for error in errors)
        if key in {"js8_busy", "varac_status"} and "companion_busy" in errors:
            failed = True
        availability = observations.get("fldigi_available")
        if (key.startswith("fldigi") and isinstance(availability, Mapping)
                and availability.get("value") is False):
            failed = True
        states[key] = "unavailable" if failed else "current" if current else "delayed"
    for key in ("frequency_hz", "js8_frequency_hz", "js8_offset_hz", "fldigi_mode",
                "fldigi_offset_hz", "vfo", "ptt_active", "js8_busy", "varac_status"):
        if key not in states:
            row.pop(key, None)
    frequency_states = [states[key] for key in ("frequency_hz", "js8_frequency_hz") if key in states]
    row["display_state"] = ("current" if "current" in frequency_states else
                            "unavailable" if "unavailable" in frequency_states else
                            "delayed" if frequency_states else "unavailable")
    row["display_field_states"] = states
    row["display_observations"] = observations
    return row


def application_display_text(name, observed, *, target=None, mode=None, state="current") -> str:
    """Actual app readbacks, with exceptions instead of routine confirmation copy."""
    parts = [str(name)]
    if mode:
        parts.append(str(mode))
    def hz(value):
        try:
            number = float(value)
            return int(number) if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None
    actual, desired = hz(observed), hz(target)
    if actual is not None:
        parts.append(f"{actual} Hz")
        if state == "current" and desired is not None and actual != desired:
            parts.append(f"configured {desired} Hz")
    elif not mode:
        parts.append("Not connected" if state == "unavailable" else "Not reported")
    if state != "current" and (actual is not None or mode):
        parts.append("unavailable · last confirmed" if state == "unavailable" else "last confirmed")
    return " · ".join(parts)


def varac_display_text(status, *, current: bool, now_monotonic=None) -> str:
    """Monitoring connectivity and observed activity; no modem/RF readiness claim."""
    if not current or not isinstance(status, Mapping):
        return "VarAC · Not reported"
    reader_verified = "process_running" in status
    if reader_verified:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        def fresh(key):
            stamp = float(status.get(key) or 0.0)
            return stamp > 0 and 0 <= now - stamp <= 30.0
        if not fresh("process_checked_monotonic") or status.get("process_running") is None:
            return "VarAC · Status unavailable"
        if status.get("process_running") is False:
            return "VarAC · Not running"
        if not status.get("reader_available") or not fresh("reader_checked_monotonic"):
            return "VarAC · Running · no status"
    if status.get("bbs_session_active"):
        return "VarAC · QSO-BBS"
    reason = str(status.get("reason") or "").lower()
    labels = {"connected": "QSO", "connecting": "Connecting", "incoming": "Incoming",
              "transfer": "Transfer", "file_wait": "Awaiting confirmation",
              "broadcast_incomplete": "Broadcast", "waiting_for_frequency": "Waiting for frequency",
              "transfer_cooldown": "Transfer complete"}
    label = "Transfer" if status.get("db_transfer_active") else labels.get(reason)
    if status.get("waiting_for_frequency"):
        label = "Waiting for frequency"
    return "VarAC · " + (label or ("Busy" if status.get("busy") else ("Connected" if reader_verified else "No session reported")))


def display_activity_text(evidence, *, net=False) -> str:
    """Positive activity only; never turn missing capability into Idle or RX."""
    states = evidence.get("display_field_states") or {}
    labels = []
    if states.get("ptt_active") == "current" and evidence.get("ptt_active"):
        labels.append("TX")
    # Generic JS8/FLDigi busy may include transmissions and is not receive proof.
    if states.get("rx_active") == "current" and evidence.get("rx_active"):
        labels.append("RX")
    if net:
        labels.append("NET")
    return " · ".join(labels)


def fldigi_receive_active(evidence, *, now_wall_time=None) -> bool:
    """Recent positively classified RX-log evidence, never generic busy."""
    if evidence.get("display_state") != "current" or not evidence.get("fldigi_activity_known"):
        return False
    if not evidence.get("fldigi_busy"):
        return False
    if evidence.get("fldigi_receive_reason") not in {"text", "callsign", "flmsg", "flamp"}:
        return False
    try:
        checked = float(evidence.get("fldigi_receive_checked_wall_time") or 0)
        age = float(evidence.get("fldigi_receive_age_s"))
        now = time.time() if now_wall_time is None else float(now_wall_time)
        return checked > 0 and math.isfinite(age) and 0 <= now - checked and 0 <= age + now - checked <= 5
    except (TypeError, ValueError, OverflowError):
        return False
