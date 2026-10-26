from __future__ import annotations

from typing import Dict, List, Optional
import datetime
import math
import time

from PySide6.QtWidgets import QComboBox, QMessageBox
from freqinout.core.logger import log
from freqinout.core.mode_utils import normalize_operating_group_mode
from freqinout.core.operating_group_frequency import (
    ensure_operating_group_frequency_keys,
    format_frequency_mhz,
    normalize_frequency_hz,
    resolve_operating_group_frequency,
)
from freqinout.core.multi_radio_store import (
    DEFAULT_HOLD_DURATION_MINUTES,
    SUPPORTED_HOLD_DURATION_MINUTES,
    normalize_rf_guard_mode,
)


HOLD_DURATION_PRESETS: tuple[int, ...] = tuple(sorted(SUPPORTED_HOLD_DURATION_MINUTES))
DEFAULT_HOLD_DURATION_MIN = DEFAULT_HOLD_DURATION_MINUTES
HOLD_WARNING_SECONDS = 10 * 60
HOLD_CRITICAL_SECONDS = 2 * 60
_HOLD_DURATION_DEFAULT_CACHE: Dict[str, Optional[int]] = {"minutes": None}
_SCHEDULER_ENABLED_OVERRIDE: Dict[str, Optional[bool]] = {"enabled": None}
_RESUME_GUARD_FEEDBACK_CACHE: Dict[str, float] = {}
_SUSPEND_GUARD_FEEDBACK_CACHE: Dict[str, float] = {}
RESUME_GUARD_FEEDBACK_SUPPRESS_SECONDS = 30.0
SUSPEND_GUARD_FEEDBACK_SUPPRESS_SECONDS = 30.0


def parse_frequency_mhz(value) -> Optional[float]:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    parts = text.split(".")
    try:
        if len(parts) == 3:
            if not all(part.isdigit() for part in parts) or any(len(part) > 3 for part in parts[1:]):
                return None
            hz = int(parts[0]) * 1_000_000 + int(parts[1]) * 1000 + int(parts[2])
            return hz / 1_000_000.0 if hz > 0 else None
        hz = normalize_frequency_hz(text)
        return hz / 1_000_000.0 if hz is not None else None
    except Exception:
        return None


def load_operating_groups(settings) -> List[Dict]:
    """
    Load operating_groups from settings with normalized fields.
    """
    data = settings.all()
    og = data.get("operating_groups", [])
    if not isinstance(og, list):
        return []
    prepared = []
    for row in og:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if item.get("frequency") in (None, "") and item.get("freq") not in (None, ""):
            item["frequency"] = item.get("freq")
        prepared.append(item)
    cleaned: List[Dict] = []
    for g in ensure_operating_group_frequency_keys(prepared):
        if not isinstance(g, dict):
            continue
        g = dict(g)
        freq = parse_frequency_mhz(g.get("frequency", 0))
        if freq is None:
            g["frequency"] = ""
        else:
            g["frequency"] = f"{freq:.6f}"
        g["mode"] = normalize_operating_group_mode(g.get("mode", ""), g.get("band", ""))
        g["auto_tune"] = bool(g.get("auto_tune", False))
        cleaned.append(g)
    return cleaned


def snapshot_operating_groups(og_list: List[Dict]) -> str:
    """
    Deterministic snapshot of operating groups used to detect changes.
    """
    parts = []
    for g in sorted(
        og_list, key=lambda x: (str(x.get("group", "")).lower(), str(x.get("band", "")).lower())
    ):
        parts.append(
            f"{g.get('group','')}|{g.get('mode','')}|{g.get('band','')}|{g.get('frequency','')}|"
            f"{g.get('vfo','')}|{g.get('fldigi_mode','')}|{g.get('fldigi_offset','')}|"
            f"{int(bool(g.get('auto_tune', False)))}"
        )
    return ";".join(parts)


def build_qsy_options(og_list: List[Dict]) -> Dict[str, Dict]:
    """
    Build QSY choices keyed by stable configured frequency-row identity.
    """
    meta: Dict[str, Dict] = {}
    normalized_rows = ensure_operating_group_frequency_keys(og_list)
    for g in normalized_rows:
        fval = parse_frequency_mhz(g.get("frequency", 0))
        if fval is None:
            continue
        row_key = str(g.get("operating_group_frequency_key") or "").strip()
        group = str(g.get("group") or "").strip().upper()
        band = str(g.get("band") or "").strip().upper()
        mode = str(g.get("mode") or "").strip().upper()
        hz = normalize_frequency_hz(fval)
        resolution = resolve_operating_group_frequency(
            normalized_rows,
            group=group,
            group_key=g.get("operating_group_key"),
            frequency_key=row_key,
            band=band,
            mode=mode,
            frequency=g.get("frequency"),
        )
        if not resolution.resolved or resolution.row is None:
            continue
        key = "row:" + row_key
        if not key.strip("|:"):
            continue
        meta[key] = {
            "freq": fval,
            "frequency": str(g.get("frequency") or fval),
            "mode": g.get("mode", ""),
            "band": g.get("band", ""),
            "auto_tune": bool(g.get("auto_tune", False)),
            "vfo": str(g.get("vfo") or "").strip().upper(),
            "fldigi_mode": str(g.get("fldigi_mode") or "").strip(),
            "fldigi_offset": str(g.get("fldigi_offset") or "").strip(),
            "group": group,
            "operating_group_key": g.get("operating_group_key", ""),
            "operating_group_frequency_key": row_key,
        }
    return meta


def refresh_qsy_combo(combo: QComboBox, meta: Dict[str, Dict]) -> None:
    items = sorted(meta.items(), key=lambda kv: (
        str(kv[1].get("group") or "").upper(),
        str(kv[1].get("band") or "").upper(),
        float(kv[1].get("freq") or 0.0),
        str(kv[1].get("mode") or "").upper(),
    ))
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("Select frequency", None)
    for key, m in items:
        label = " · ".join(part for part in (
            str(m.get("group") or "").strip(),
            str(m.get("band") or "").strip(),
            (format_frequency_mhz(m.get("frequency", m.get("freq"))) + " MHz"),
            str(m.get("mode") or "").strip(),
        ) if part)
        combo.addItem(label or f"{key} MHz", m)
    combo.blockSignals(False)


def selected_qsy_meta(combo: QComboBox) -> Optional[Dict]:
    data = combo.currentData()
    return data if isinstance(data, dict) else None


def current_scheduler_freq(window) -> Optional[float]:
    try:
        sched = getattr(window, "scheduler", None)
        entry = getattr(sched, "current_schedule_entry", {}) if sched else {}
        if not entry:
            return None
        return float(entry.get("frequency"))
    except Exception:
        return None


def _shared_ptt_block_reason(
    scheduler,
    target_device_profile_id: Optional[int] = None,
) -> str:
    if (
        scheduler is not None
        and target_device_profile_id is not None
        and hasattr(scheduler, "get_shared_ptt_status_for_target")
    ):
        try:
            target_status = scheduler.get_shared_ptt_status_for_target(
                int(target_device_profile_id)
            )
        except Exception:
            target_status = {}
        if isinstance(target_status, dict):
            # Incomplete cache evidence is handled as a pending verification by
            # SchedulerEngine. Only a known active shared-PTT owner is an
            # immediate GUI preflight block.
            if not bool(target_status.get("evidence_known", True)):
                return ""
            if not bool(target_status.get("blocked")):
                return ""
            return str(target_status.get("reason") or "Shared PTT interlock is active.").strip()
    if scheduler is None or not hasattr(scheduler, "get_status_summary"):
        return ""
    try:
        status = scheduler.get_status_summary()
    except Exception:
        return ""
    if not isinstance(status, dict) or not bool(status.get("shared_ptt_blocked")):
        return ""
    reason = str(status.get("shared_ptt_reason") or "").strip()
    if reason:
        return reason
    group = str(status.get("shared_ptt_group") or "").strip()
    owner = str(status.get("shared_ptt_owner_name") or "").strip()
    if group and owner:
        return f"Shared PTT group {group} is in use by {owner}."
    if group:
        return f"Shared PTT group {group} is currently busy."
    return "Shared PTT interlock is active."


def _coordination_conflict_warning(
    scheduler,
    entry: Dict,
    *,
    source: str = "QSY",
    force: bool = False,
) -> Dict[str, object]:
    if scheduler is None or not hasattr(scheduler, "evaluate_coordination_conflict"):
        return {}
    try:
        payload = scheduler.evaluate_coordination_conflict(entry, source=source, force=force)
    except TypeError:
        try:
            payload = scheduler.evaluate_coordination_conflict(entry, source=source)
        except TypeError:
            try:
                payload = scheduler.evaluate_coordination_conflict(entry)
            except Exception:
                return {}
        except Exception:
            return {}
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _qsy_feedback_target(window) -> tuple[Optional[str], str]:
    profile = getattr(window, "_active_runtime_profile", None) if window is not None else None
    if isinstance(profile, dict):
        profile_id = profile.get("id")
        label = str(profile.get("name") or profile.get("label") or "").strip()
        return (str(profile_id) if profile_id not in (None, "") else None, label or "Radio")
    return None, "Radio"


def _publish_qsy_blocked_feedback(
    window,
    summary: str,
    detail: str = "",
    *,
    source_surface: str = "qsy_helper",
) -> bool:
    try:
        service = getattr(window, "action_feedback_service", None) if window is not None else None
    except Exception:
        service = None
    if service is None or not hasattr(service, "publish"):
        return False
    radio_profile_id, target_label = _qsy_feedback_target(window)
    try:
        service.publish(
            scope="radio",
            action_type="qsy",
            status="blocked",
            summary=str(summary or "").strip(),
            radio_profile_id=radio_profile_id,
            target_label=target_label,
            detail=str(detail or "").strip(),
            source_surface=source_surface,
        )
        return True
    except Exception as e:
        log.debug("QSY helper: failed to publish blocked feedback: %s", e)
        return False


def _publish_qsy_override_feedback(
    window,
    *,
    status: str,
    summary: str,
    detail: str = "",
) -> bool:
    try:
        service = getattr(window, "action_feedback_service", None) if window is not None else None
    except Exception:
        service = None
    if service is None or not hasattr(service, "publish"):
        return False
    radio_profile_id, target_label = _qsy_feedback_target(window)
    try:
        service.publish(
            scope="radio",
            action_type="qsy_override",
            status=status,
            summary=str(summary or "").strip(),
            radio_profile_id=radio_profile_id,
            target_label=target_label,
            detail=str(detail or "").strip(),
            source_surface="qsy_helper_conflict",
        )
        return True
    except Exception as e:
        log.debug("QSY helper: failed to publish RF conflict feedback: %s", e)
        return False


def _publish_qsy_warning_feedback(window, summary: str, detail: str = "") -> bool:
    try:
        service = getattr(window, "action_feedback_service", None) if window is not None else None
    except Exception:
        service = None
    if service is None or not hasattr(service, "publish"):
        return False
    radio_profile_id, target_label = _qsy_feedback_target(window)
    try:
        service.publish(
            scope="radio",
            action_type="qsy",
            status="partial",
            summary=str(summary or "").strip(),
            radio_profile_id=radio_profile_id,
            target_label=target_label,
            detail=str(detail or "").strip(),
            source_surface="qsy_helper_warning",
        )
        return True
    except Exception as e:
        log.debug("QSY helper: failed to publish RF safety warning feedback: %s", e)
        return False


def _publish_schedule_control_feedback(
    window,
    *,
    action_type: str,
    status: str,
    summary: str,
    detail: str = "",
    source_surface: str = "qsy_helper_schedule_control",
    qsy_request: Optional[Dict] = None,
) -> bool:
    try:
        service = getattr(window, "action_feedback_service", None) if window is not None else None
    except Exception:
        service = None
    if service is None or not hasattr(service, "publish"):
        return False
    radio_profile_id, target_label = _qsy_feedback_target(window)
    if qsy_request and qsy_request.get("radio_profile_id"):
        requested_radio = str(qsy_request["radio_profile_id"])
        if requested_radio != radio_profile_id:
            target_label = f"Radio {requested_radio}"
        radio_profile_id = requested_radio
    try:
        event = service.publish(
            scope="scheduler",
            action_type=action_type,
            status=status,
            summary=str(summary or "").strip(),
            radio_profile_id=radio_profile_id,
            target_label=target_label,
            detail=str(detail or "").strip(),
            source_surface=source_surface,
        )
        tracker = getattr(window, "_track_qsy_feedback", None)
        if qsy_request and callable(tracker):
            tracker(event, qsy_request)
        return True
    except Exception as e:
        log.debug("QSY helper: failed to publish schedule control feedback: %s", e)
        return False


def _rf_guard_warning_detail(detail: str, *, mode_label: str) -> str:
    clean_detail = str(detail or "").strip()
    prefix = f"RF Safety Guard mode: {mode_label}."
    if clean_detail:
        return f"{prefix} {clean_detail}"
    return prefix


def _submit_manual_qsy(
    window,
    scheduler,
    entry: Dict,
    *,
    ignore_coordination_prompt: bool = False,
) -> bool:
    """Submit QSY without describing a pending safety check as a sent command."""

    requested_at = time.monotonic()
    radio_id = entry.get("target_device_profile_id") or _qsy_feedback_target(window)[0]
    frequency = parse_frequency_mhz(entry.get("frequency"))
    qsy_request = {
        "radio_profile_id": str(radio_id) if radio_id else None,
        "frequency_hz": round(frequency * 1_000_000) if frequency else None,
        "requested_monotonic": requested_at,
    }
    try:
        result = scheduler.apply_manual_qsy(
            entry,
            ignore_coordination_prompt=ignore_coordination_prompt,
        )
    except TypeError:
        result = scheduler.apply_manual_qsy(entry)
    disposition = "queued" if result is None else str(result).strip().lower()
    try:
        radio_id = int(entry.get("target_device_profile_id") or getattr(window, "_station_command_selected_profile_id", 0) or 0)
        dispositions = getattr(window, "_station_command_qsy_dispositions", None)
        if not isinstance(dispositions, dict):
            dispositions = window._station_command_qsy_dispositions = {}
        dispositions[radio_id] = disposition
    except (TypeError, ValueError, AttributeError):
        pass
    if result is None:
        # Compatibility integrations own their feedback. A new request must
        # nevertheless replace any old cached disposition for this radio.
        return True
    if disposition == "pending_verification":
        _publish_schedule_control_feedback(
            window,
            action_type="qsy",
            status="partial",
            summary="Checking target radio before QSY…",
            detail=(
                "CommsPlan is verifying that the selected radio is not transmitting. "
                "The QSY will continue automatically when safe."
            ),
            source_surface="qsy_helper_preflight",
            qsy_request=qsy_request,
        )
        return True
    if disposition in {"queued", "pending", "already_applied"}:
        _publish_schedule_control_feedback(
            window,
            action_type="qsy",
            status="succeeded" if disposition == "already_applied" else "partial",
            summary=(
                "The selected radio is already on this frequency."
                if disposition == "already_applied"
                else "QSY is queued for the selected radio."
            ),
            detail="CommsPlan will report the endpoint readback separately.",
            source_surface="qsy_helper_submit",
            qsy_request=qsy_request if disposition != "already_applied" else None,
        )
        return True
    if disposition == "manual":
        _publish_schedule_control_feedback(
            window,
            action_type="qsy",
            status="partial",
            summary="QSY selected · tune this radio manually.",
            detail="CommsPlan updated the manual hold but did not send a radio-control command.",
            source_surface="qsy_helper_manual",
        )
        return True
    _publish_qsy_blocked_feedback(
        window,
        "QSY blocked for the selected radio.",
        "Review the radio status or RF Safety Guard details, then try again.",
    )
    return False


def _resume_coordination_conflict(scheduler, target_device_profile_id=None) -> Dict[str, object]:
    try:
        if target_device_profile_id is not None:
            radio_id = int(target_device_profile_id or 0)
            if radio_id <= 0:
                return {}
            _, entry = scheduler._active_schedule_entry_for_radio(radio_id, force=False)
            entry = {**entry, "target_device_profile_id": radio_id} if entry else {}
        else:
            entry = getattr(scheduler, "current_schedule_entry", {}) if scheduler is not None else {}
    except Exception:
        entry = {}
    if not isinstance(entry, dict) or not entry:
        return {}
    return _coordination_conflict_warning(scheduler, entry, source="RESUME", force=True)


def _current_entry_coordination_conflict(scheduler, *, source: str, force: bool = False) -> Dict[str, object]:
    try:
        entry = getattr(scheduler, "current_schedule_entry", {}) if scheduler is not None else {}
    except Exception:
        entry = {}
    if not isinstance(entry, dict) or not entry:
        return {}
    return _coordination_conflict_warning(scheduler, entry, source=source, force=force)


def _resume_guard_signature(conflict: Dict[str, object]) -> str:
    signature = str(conflict.get("signature") or "").strip()
    if signature:
        return signature
    return "|".join(
        part
        for part in (
            str(conflict.get("summary") or "").strip(),
            str(conflict.get("detail") or "").strip(),
            str(conflict.get("guard_mode") or "").strip(),
        )
        if part
    )


def _guard_feedback_recent(
    cache: Dict[str, float],
    signature: str,
    *,
    mark: bool = False,
    suppress_seconds: float = 30.0,
) -> bool:
    sig = str(signature or "").strip()
    if not sig:
        return False
    now = time.time()
    expired = [
        key for key, ts in cache.items()
        if now - float(ts or 0.0) > suppress_seconds
    ]
    for key in expired:
        cache.pop(key, None)
    recent = sig in cache
    if mark:
        cache[sig] = now
    return recent


def _resume_guard_feedback_recent(signature: str, *, mark: bool = False) -> bool:
    return _guard_feedback_recent(
        _RESUME_GUARD_FEEDBACK_CACHE,
        signature,
        mark=mark,
        suppress_seconds=RESUME_GUARD_FEEDBACK_SUPPRESS_SECONDS,
    )


def _suspend_guard_feedback_recent(signature: str, *, mark: bool = False) -> bool:
    return _guard_feedback_recent(
        _SUSPEND_GUARD_FEEDBACK_CACHE,
        signature,
        mark=mark,
        suppress_seconds=SUSPEND_GUARD_FEEDBACK_SUPPRESS_SECONDS,
    )


def _warn_if_suspend_leaves_rf_conflict(window, scheduler) -> None:
    conflict = _current_entry_coordination_conflict(scheduler, source="SUSPEND", force=True)
    if not bool(conflict.get("warning")):
        return
    signature = _resume_guard_signature(conflict)
    if _suspend_guard_feedback_recent(signature):
        return
    summary = str(conflict.get("summary") or "RF Safety Guard warning while schedule is paused.").strip()
    detail = str(conflict.get("detail") or summary).strip()
    published = _publish_schedule_control_feedback(
        window,
        action_type="suspend_schedule",
        status="partial",
        summary=summary,
        detail=f"Schedule control is paused; this RF guard condition remains in place. {detail}".strip(),
    )
    if published:
        _suspend_guard_feedback_recent(signature, mark=True)


def _resume_allowed_by_rf_guard(window, scheduler, target_device_profile_id=None) -> tuple[bool, bool]:
    conflict = _resume_coordination_conflict(scheduler, target_device_profile_id)
    if not bool(conflict.get("warning")):
        return True, False
    summary = str(conflict.get("summary") or "Resume blocked by RF Safety Guard.").strip()
    detail = str(conflict.get("detail") or summary).strip()
    guard_mode = normalize_rf_guard_mode(conflict.get("guard_mode", "confirm"), "confirm")
    signature = _resume_guard_signature(conflict)
    if bool(conflict.get("blocked")) or guard_mode == "block":
        if not _resume_guard_feedback_recent(signature, mark=True):
            _publish_schedule_control_feedback(
                window,
                action_type="resume_schedule",
                status="blocked",
                summary=summary,
                detail=_rf_guard_warning_detail(detail, mode_label="Block"),
            )
        return False, False
    if guard_mode == "warn":
        _publish_schedule_control_feedback(
            window,
            action_type="resume_schedule",
            status="partial",
            summary=summary,
            detail=_rf_guard_warning_detail(detail, mode_label="Warn only"),
        )
        return True, True
    if _resume_guard_feedback_recent(signature):
        return False, False
    msg = QMessageBox(window)
    msg.setWindowTitle("RF Conflict Warning")
    msg.setText(summary or "RF conflict detected.")
    if detail:
        msg.setInformativeText(detail)
    proceed_btn = msg.addButton("Resume Anyway", QMessageBox.AcceptRole)
    msg.addButton("Cancel", QMessageBox.RejectRole)
    msg.exec()
    if msg.clickedButton() != proceed_btn:
        _resume_guard_feedback_recent(signature, mark=True)
        _publish_schedule_control_feedback(
            window,
            action_type="resume_schedule",
            status="blocked",
            summary="Resume cancelled: RF Safety Guard warning.",
            detail=_rf_guard_warning_detail(detail or summary, mode_label="Require confirmation"),
        )
        return False, False
    _publish_schedule_control_feedback(
        window,
        action_type="resume_schedule",
        status="succeeded",
        summary="Resume allowed: RF Safety Guard warning acknowledged.",
        detail=_rf_guard_warning_detail(detail or summary, mode_label="Require confirmation"),
    )
    return True, True


def perform_qsy(window, meta: Dict) -> bool:
    try:
        scheduler = getattr(window, "scheduler", None)
    except Exception:
        scheduler = None
    if not scheduler:
        if not _publish_qsy_blocked_feedback(
            window,
            "QSY blocked: scheduler is unavailable.",
            "Scheduler engine is unavailable.",
        ):
            QMessageBox.warning(window, "Scheduler", "Scheduler engine is unavailable.")
        return False
    freq = meta.get("freq")
    if freq is None:
        if not _publish_qsy_blocked_feedback(
            window,
            "QSY blocked: select a frequency first.",
            "Select a frequency before QSY.",
        ):
            QMessageBox.warning(window, "QSY", "Select a frequency before QSY.")
        return False
    try:
        frequency_mhz = float(freq)
        if not math.isfinite(frequency_mhz) or frequency_mhz <= 0:
            raise ValueError("Frequency must be positive and finite")
        freq_text = f"{frequency_mhz:.6f}".rstrip("0")
        freq_text += "0" * max(0, 3 - len(freq_text.partition(".")[2]))
    except Exception:
        if not _publish_qsy_blocked_feedback(
            window,
            "QSY blocked: selected frequency is invalid.",
            f"Frequency value {freq!r} could not be used for QSY.",
        ):
            QMessageBox.warning(window, "QSY", "Selected frequency is invalid.")
        return False
    entry = {
        "frequency": freq_text,
        "band": meta.get("band", ""),
        "mode": meta.get("mode", ""),
        "auto_tune": bool(meta.get("auto_tune", False)),
        "vfo": meta.get("vfo", ""),
        "group": (meta.get("group") or "").strip().upper(),
        "group_name": (meta.get("group") or "").strip().upper(),
        "operating_group_key": str(meta.get("operating_group_key") or ""),
        "operating_group_frequency_key": str(meta.get("operating_group_frequency_key") or ""),
        "fldigi_mode": str(meta.get("fldigi_mode") or ""),
        "fldigi_offset": str(meta.get("fldigi_offset") or ""),
    }
    # Cards make an explicit hold choice; other QSY entry points preserve
    # their established existing-future-deadline policy.
    if meta.get("manual_hold_policy") in {"until_resume", "until_transition"}:
        entry["manual_hold_policy"] = meta["manual_hold_policy"]
    try:
        target_device_profile_id = int(meta.get("target_device_profile_id") or 0)
    except Exception:
        target_device_profile_id = 0
    if target_device_profile_id > 0:
        entry["target_device_profile_id"] = target_device_profile_id
    block_reason = _shared_ptt_block_reason(
        scheduler,
        target_device_profile_id if target_device_profile_id > 0 else None,
    )
    if block_reason:
        if not _publish_qsy_blocked_feedback(
            window,
            "QSY blocked: shared PTT path is busy.",
            block_reason,
        ):
            QMessageBox.warning(window, "QSY Blocked", block_reason)
        return False
    conflict = _coordination_conflict_warning(scheduler, entry)
    if bool(conflict.get("blocked")):
        summary = str(conflict.get("summary") or "QSY blocked by RF Safety Guard.").strip()
        detail = str(conflict.get("detail") or summary).strip()
        if not _publish_qsy_blocked_feedback(window, summary, detail):
            QMessageBox.warning(window, "QSY Blocked", detail or summary)
        return False
    if bool(conflict.get("warning")):
        guard_mode = normalize_rf_guard_mode(conflict.get("guard_mode", "confirm"), "confirm")
        if guard_mode == "warn":
            summary = str(conflict.get("summary") or "RF Safety Guard warning.").strip()
            detail = str(conflict.get("detail") or summary).strip()
            _publish_qsy_warning_feedback(window, summary, _rf_guard_warning_detail(detail, mode_label="Warn only"))
            return _submit_manual_qsy(
                window,
                scheduler,
                entry,
                ignore_coordination_prompt=True,
            )
        msg = QMessageBox(window)
        msg.setWindowTitle("RF Conflict Warning")
        msg.setText(str(conflict.get("summary") or "RF conflict detected.").strip() or "RF conflict detected.")
        detail = str(conflict.get("detail") or "").strip()
        if detail:
            msg.setInformativeText(detail)
        proceed_btn = msg.addButton("Proceed QSY", QMessageBox.AcceptRole)
        msg.addButton("Cancel", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() != proceed_btn:
            _publish_qsy_override_feedback(
                window,
                status="blocked",
                summary="QSY cancelled: RF Safety Guard warning.",
                detail=_rf_guard_warning_detail(
                    detail or str(conflict.get("summary") or "RF conflict detected.").strip(),
                    mode_label="Require confirmation",
                ),
            )
            return False
        _publish_qsy_override_feedback(
            window,
            status="succeeded",
            summary="QSY allowed: RF Safety Guard warning acknowledged.",
            detail=_rf_guard_warning_detail(
                detail or str(conflict.get("summary") or "RF conflict detected.").strip(),
                mode_label="Require confirmation",
            ),
        )
        return _submit_manual_qsy(
            window,
            scheduler,
            entry,
            ignore_coordination_prompt=True,
        )
    return _submit_manual_qsy(window, scheduler, entry)


def normalize_hold_minutes(value) -> int:
    try:
        mins = int(value)
    except Exception:
        mins = DEFAULT_HOLD_DURATION_MIN
    return mins if mins in HOLD_DURATION_PRESETS else DEFAULT_HOLD_DURATION_MIN


def get_hold_duration_default(settings, profile: Optional[Dict[str, object]] = None) -> int:
    if isinstance(profile, dict) and profile.get("schedule_hold_minutes_default") not in (None, ""):
        return normalize_hold_minutes(profile.get("schedule_hold_minutes_default"))
    try:
        mins = normalize_hold_minutes(settings.get("schedule_hold_minutes_default", DEFAULT_HOLD_DURATION_MIN))
    except Exception:
        mins = DEFAULT_HOLD_DURATION_MIN
    _HOLD_DURATION_DEFAULT_CACHE["minutes"] = mins
    return mins


def set_hold_duration_default(settings, minutes: int) -> int:
    mins = normalize_hold_minutes(minutes)
    _HOLD_DURATION_DEFAULT_CACHE["minutes"] = mins
    try:
        if hasattr(settings, "set"):
            settings.set("schedule_hold_minutes_default", mins)
    except Exception:
        pass
    return mins


def hold_duration_profile_for_window(window) -> Optional[Dict[str, object]]:
    root = _top_level_hold_window(window)
    profile = getattr(root, "_active_runtime_profile", None) if root is not None else None
    return profile if isinstance(profile, dict) else None


def _top_level_hold_window(window):
    if window is None:
        return None
    try:
        root = window.window() if hasattr(window, "window") else window
    except Exception:
        root = window
    return root


def notify_hold_state_changed(window, *, force_reload: bool = False) -> None:
    root = _top_level_hold_window(window)
    if root is None:
        return
    try:
        if hasattr(root, "on_hold_state_changed"):
            root.on_hold_state_changed(force_reload=force_reload)
    except Exception:
        pass


def notify_hold_duration_default_changed(window) -> None:
    root = _top_level_hold_window(window)
    if root is None:
        return
    try:
        if hasattr(root, "on_hold_duration_default_changed"):
            root.on_hold_duration_default_changed()
    except Exception:
        pass


def refresh_hold_duration_combo(combo: QComboBox, settings, profile: Optional[Dict[str, object]] = None) -> None:
    current = get_hold_duration_default(settings, profile)
    combo.blockSignals(True)
    combo.clear()
    for mins in HOLD_DURATION_PRESETS:
        combo.addItem(f"{mins} min", mins)
    idx = combo.findData(current)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.blockSignals(False)


def selected_hold_duration(
    combo: Optional[QComboBox],
    settings,
    profile: Optional[Dict[str, object]] = None,
) -> int:
    if combo is None:
        return get_hold_duration_default(settings, profile)
    data = combo.currentData()
    if data is None:
        return get_hold_duration_default(settings, profile)
    return normalize_hold_minutes(data)


def suspend_snapshot(settings, *, allow_reload: bool = True) -> Dict[str, object]:
    dt = get_suspend_until(settings, allow_reload=allow_reload)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    active = dt is not None and now_utc < dt
    if not active:
        return {
            "active": False,
            "until": dt,
            "remaining_sec": None,
            "remaining_minutes": 0,
            "severity": "idle",
            "about_to_resume": False,
        }
    remaining_sec = max(0.0, (dt - now_utc).total_seconds())
    remaining_minutes = max(0, int((remaining_sec + 59) // 60))
    severity = "info"
    if remaining_sec <= HOLD_CRITICAL_SECONDS:
        severity = "critical"
    elif remaining_sec <= HOLD_WARNING_SECONDS:
        severity = "warning"
    return {
        "active": True,
        "until": dt,
        "remaining_sec": remaining_sec,
        "remaining_minutes": remaining_minutes,
        "severity": severity,
        "about_to_resume": severity in {"warning", "critical"},
    }


def active_hold_button_role(remaining_sec: Optional[float]) -> str:
    if remaining_sec is None:
        return "info"
    if remaining_sec <= HOLD_CRITICAL_SECONDS:
        return "danger"
    if remaining_sec <= HOLD_WARNING_SECONDS:
        return "warning"
    return "info"


def active_hold_button_text(remaining_sec: Optional[float]) -> str:
    if remaining_sec is None:
        return "Resume Schedule"
    mins = max(0, int((float(remaining_sec) + 59) // 60))
    return f"Resume ({mins}m)" if mins else "Resume Schedule"


def active_hold_status_text(remaining_sec: Optional[float]) -> str:
    if remaining_sec is None:
        return "Schedule resumes automatically."
    mins = max(0, int((float(remaining_sec) + 59) // 60))
    return f"Schedule resumes automatically in {mins} min." if mins else "Schedule resumes automatically."


def set_active_hold_duration(
    window,
    settings,
    minutes: Optional[int] = None,
    *,
    notify: bool = True,
    profile: Optional[Dict[str, object]] = None,
    target_device_profile_id: Optional[int] = None,
) -> int:
    active_profile = profile if isinstance(profile, dict) else hold_duration_profile_for_window(window)
    mins = normalize_hold_minutes(
        minutes if minutes is not None else get_hold_duration_default(settings, active_profile)
    )
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=mins)
    try:
        scheduler = getattr(window, "scheduler", None)
    except Exception:
        scheduler = None
    if scheduler and hasattr(scheduler, "suspend_schedule"):
        try:
            scheduler.suspend_schedule(mins, target_device_profile_id=target_device_profile_id)
        except TypeError:
            if target_device_profile_id is not None:
                return 0
            scheduler.suspend_schedule(mins)
    else:
        if target_device_profile_id is not None:
            return 0
        set_suspend_until(settings, until)
    if target_device_profile_id is None:
        _SUSPEND_CACHE["ts"] = until.timestamp()
        _SUSPEND_CACHE["loaded_at"] = time.time()
    if notify:
        notify_hold_state_changed(window, force_reload=False)
    return mins


def suspend_schedule_hold(
    window,
    settings,
    minutes: Optional[int] = None,
    *,
    warn_rf_conflict: bool = True,
    target_device_profile_id: Optional[int] = None,
) -> int:
    if warn_rf_conflict:
        try:
            scheduler = getattr(window, "scheduler", None)
        except Exception:
            scheduler = None
        if scheduler is not None:
            _warn_if_suspend_leaves_rf_conflict(window, scheduler)
    mins = set_active_hold_duration(
        window,
        settings,
        minutes=minutes,
        notify=False,
        target_device_profile_id=target_device_profile_id,
    )
    notify_hold_state_changed(window, force_reload=False)
    return mins


def resume_schedule_hold(window, settings, *, target_device_profile_id: Optional[int] = None) -> bool:
    try:
        scheduler = getattr(window, "scheduler", None)
    except Exception:
        scheduler = None
    if scheduler and hasattr(scheduler, "resume_schedule"):
        allowed, acknowledged = (_resume_allowed_by_rf_guard(window, scheduler)
                                 if target_device_profile_id is None else
                                 _resume_allowed_by_rf_guard(window, scheduler, target_device_profile_id))
        if not allowed:
            notify_hold_state_changed(window, force_reload=False)
            return False
        try:
            result = scheduler.resume_schedule(
                ignore_coordination_prompt=acknowledged,
                target_device_profile_id=target_device_profile_id,
            )
        except TypeError:
            if target_device_profile_id is not None:
                return False
            result = scheduler.resume_schedule()
        if result is False:
            notify_hold_state_changed(window, force_reload=False)
            return False
        if target_device_profile_id is None:
            _SUSPEND_CACHE["ts"] = 0
            _SUSPEND_CACHE["loaded_at"] = time.time()
        notify_hold_state_changed(window, force_reload=False)
        return True
    if target_device_profile_id is not None:
        return False
    set_suspend_until(settings, None)
    notify_hold_state_changed(window, force_reload=False)
    return False


def perform_qsy_with_hold(window, settings, meta: Dict, minutes: Optional[int] = None) -> int:
    if not perform_qsy(window, meta):
        return 0
    target_device_profile_id = None
    try:
        target_device_profile_id = int(meta.get("target_device_profile_id") or 0) or None
    except Exception:
        target_device_profile_id = None
    return suspend_schedule_hold(
        window,
        settings,
        minutes=minutes,
        warn_rf_conflict=False,
        target_device_profile_id=target_device_profile_id,
    )


# Suspend helpers (shared across tabs)
_SUSPEND_CACHE: Dict[str, Optional[float]] = {"ts": None, "loaded_at": 0.0}


def get_suspend_until(
    settings,
    max_age_sec: int = 10,
    *,
    allow_reload: bool = True,
) -> Optional[datetime.datetime]:
    """
    Read schedule_suspend_until (UTC timestamp) from settings, with a small cache to avoid frequent reloads.
    """
    now = time.time()
    cached_ts = _SUSPEND_CACHE.get("ts")
    loaded_at = _SUSPEND_CACHE.get("loaded_at", 0.0) or 0.0
    if cached_ts is not None and (not allow_reload or (now - loaded_at) < max_age_sec):
        return (
            datetime.datetime.fromtimestamp(cached_ts, tz=datetime.timezone.utc)
            if cached_ts > 0
            else None
        )
    if not allow_reload and cached_ts is None:
        return None
    try:
        if allow_reload and hasattr(settings, "reload"):
            settings.reload()
        ts = float(settings.get("schedule_suspend_until", 0) or 0)
        _SUSPEND_CACHE["ts"] = ts
        _SUSPEND_CACHE["loaded_at"] = now
        if ts > 0:
            return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except Exception:
        pass
    return None


def set_suspend_until(settings, dt: Optional[datetime.datetime]) -> None:
    try:
        if hasattr(settings, "set"):
            ts = dt.timestamp() if dt else 0
            settings.set("schedule_suspend_until", ts)
            _SUSPEND_CACHE["ts"] = ts
            _SUSPEND_CACHE["loaded_at"] = time.time()
    except Exception:
        pass


def suspend_active(settings) -> bool:
    dt = get_suspend_until(settings)
    return dt is not None and datetime.datetime.now(datetime.timezone.utc) < dt


def set_scheduler_enabled_override(enabled: Optional[bool]) -> None:
    _SCHEDULER_ENABLED_OVERRIDE["enabled"] = None if enabled is None else bool(enabled)


def scheduler_enabled(settings) -> bool:
    override = _SCHEDULER_ENABLED_OVERRIDE.get("enabled")
    if override is not None:
        return bool(override)
    try:
        return bool(settings.get("use_scheduler", True))
    except Exception:
        return True
