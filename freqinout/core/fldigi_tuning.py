"""Validated, one-shot net tuning. These helpers never enforce operator drift."""
from __future__ import annotations

import json
from collections.abc import Mapping

FAILURE_DETAILS = {
    "shared_resource_reserved": "A shared radio resource is busy. Try again when it is released.",
    "execution_ptt_active": "The radio is transmitting. Wait for receive before changing tuning.",
    "execution_ptt_unavailable": "Transmit state could not be verified. Check the radio connection.",
    "execution_shared_ptt_hold": "A radio sharing this station's resources is transmitting or its state is unknown.",
    "execution_rf_guard_hold": "RF Guard is holding this change. Review this radio's RF Guard status.",
    "execution_js8_busy": "JS8Call is active on this radio. Wait for that activity to finish.",
    "execution_varac_busy": "VarAC is protecting an active transfer or session on this radio.",
    "schedule_review_required": "Review and approve the current schedule for this radio before applying this preset.",
    "command_authority_changed": "Radio control changed while this action was waiting. Review current tuning and try again.",
    "schedule_occurrence_expired": "This scheduled net is no longer current. Review the radio's active net.",
    "schedule_authority_unavailable": "The current net schedule could not be verified. Wait for schedule refresh.",
    "fldigi_initial_setup_failed": "The net's starting FLDigi mode or audio offset could not be applied.",
    "operation_timeout": "The radio did not respond in time. Tuning may be partially changed; inspect the radio.",
}


def tuning_failure_detail(reason) -> str:
    return FAILURE_DETAILS.get(str(reason), str(reason))


DEFAULT_PRESET = dict(enabled=False, label="Center + Narrow", audio_offset_hz=1500,
                      filter_width_hz=500, preserve_signal=True, apply_at_start=False)


def normalize_tuning_preset(value=None) -> dict:
    if value in (None, ""):
        value = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("FLDigi tuning settings must contain valid JSON.") from exc
    if not isinstance(value, Mapping):
        raise ValueError("FLDigi tuning settings must be an object.")
    result = dict(DEFAULT_PRESET)
    for key in ("enabled", "preserve_signal", "apply_at_start"):
        item = value.get(key, result[key])
        if not isinstance(item, bool):
            raise ValueError(f"FLDigi tuning {key} must be true or false.")
        result[key] = item
    label = str(value.get("label", result["label"]) or "").strip()
    if not label or len(label) > 40 or any(ord(c) < 32 for c in label):
        raise ValueError("Use a FLDigi tuning button label of 1–40 characters.")
    result["label"] = label
    for key, low, high in (("audio_offset_hz", 100, 4000), ("filter_width_hz", 50, 12000)):
        raw = value.get(key, result[key])
        if key == "filter_width_hz" and raw in (None, ""):
            result[key] = None
            continue
        if isinstance(raw, bool) or not str(raw).isdigit() or not low <= int(raw) <= high:
            raise ValueError(f"FLDigi tuning {key.replace('_', ' ')} must be {low}–{high} Hz.")
        result[key] = int(raw)
    return result


def tuning_preset_json(value=None) -> str:
    preset = normalize_tuning_preset(value)
    return json.dumps(preset, sort_keys=True, separators=(",", ":")) if preset != DEFAULT_PRESET else "{}"


def compensated_dial(frequency_hz: int, old_offset: int, new_offset: int, sideband: str) -> int:
    sideband = str(sideband).strip().upper()
    if sideband not in {"USB", "LSB"}:
        raise ValueError("Preserving the signal requires a confirmed USB or LSB mode.")
    target = int(frequency_hz) + (1 if sideband == "USB" else -1) * (int(old_offset) - int(new_offset))
    if target <= 0:
        raise ValueError("Calculated radio frequency is invalid.")
    return target


def execute_tuning(rig, preset, *, preflight, set_target, restore=None) -> dict:
    """Run only inside the owning endpoint lane and shared-resource reservation.

    Read live values, then fence each write. Partial effects are retained for an
    explicit restore; no rollback or retry can unexpectedly move the operator.
    """
    from freqinout.radio_interface.rigctl_client import FrequencyCommand

    result = {"ok": False, "before": {}, "after": {}, "changed": False}
    try:
        reason = preflight()
        if reason:
            raise ValueError(reason)
        before = rig.get_net_tuning_state()
        result["before"] = before
        frequency, offset, vfo = before.get("frequency_hz"), before.get("audio_offset_hz"), before.get("vfo")
        if not frequency or offset is None or vfo not in {"A", "B"}:
            raise ValueError("Radio frequency, active VFO, or FLDigi audio offset could not be read.")
        if restore is not None:
            if restore.get("vfo") != vfo:
                raise ValueError("The active VFO changed; previous tuning cannot be restored safely.")
            if restore.get("sideband") != before.get("sideband"):
                raise ValueError("The radio sideband changed; previous tuning cannot be restored safely.")
            target = dict(restore)
        else:
            preset = normalize_tuning_preset(preset)
            target = dict(before)
            target["audio_offset_hz"] = preset["audio_offset_hz"]
            target["frequency_hz"] = (compensated_dial(frequency, offset, preset["audio_offset_hz"], before.get("sideband"))
                                      if preset["preserve_signal"] else frequency)
            target["filter_width_hz"] = preset["filter_width_hz"]
        # RF Guard must evaluate the computed destination, not the scheduled dial.
        set_target(int(target["frequency_hz"]))
        for name, apply in (
            ("frequency_hz", lambda: rig.set_frequency(FrequencyCommand(rig_hz=target["frequency_hz"], vfo=vfo))),
            ("audio_offset_hz", lambda: rig.set_fldigi_mode_offset(None, target["audio_offset_hz"])),
            ("filter_width_hz", lambda: rig.set_filter_width(target["filter_width_hz"])),
        ):
            if target.get(name) is None or target.get(name) == before.get(name):
                continue
            if rig.get_active_vfo() != vfo:
                raise ValueError("The active VFO changed; tuning was stopped. Inspect the radio.")
            reason = preflight()
            if reason:
                raise ValueError(reason)
            # A failed RPC may have reached the radio. Always allow explicit recovery.
            result["changed"] = True
            if not apply():
                raise ValueError(f"Could not apply {name.replace('_', ' ')}; tuning may be partially changed.")
        after = rig.get_net_tuning_state()
        result["after"] = after
        for name in ("frequency_hz", "audio_offset_hz", "filter_width_hz", "vfo", "sideband"):
            if target.get(name) is not None and after.get(name) != target[name]:
                raise ValueError(f"Could not verify {name.replace('_', ' ')}; inspect the radio and FLDigi.")
        reason = preflight()
        if reason:
            raise ValueError(reason)
        result["ok"] = True
        result["detail"] = "Tuning verified. Operator adjustments remain free."
    except Exception as exc:
        result["detail"] = tuning_failure_detail(exc)
    return result
