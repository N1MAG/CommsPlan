from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, Mapping


ShellDensity = Literal["condensed", "compact", "roomy"]
ScheduleProminence = Literal["calm", "upcoming", "urgent", "overdue"]


@dataclass(frozen=True)
class ShellLayoutState:
    density: ShellDensity
    show_source_context: bool
    show_clock_seconds: bool
    stack_primary_context: bool


@dataclass(frozen=True)
class NextActionState:
    text: str
    prominence: ScheduleProminence
    role: str


@dataclass(frozen=True)
class QsyActionState:
    qsy_enabled: bool
    timed_qsy_enabled: bool
    qsy_role: str
    timed_qsy_role: str


@dataclass(frozen=True)
class SchedulerActionState:
    timed_suspend_text: str
    timed_suspend_role: str
    resume_role: str


def _clean_text(value: object, fallback: str = "") -> str:
    """Return bounded display text without letting malformed state escape."""
    try:
        text = str(value or "").strip()
    except Exception:
        return fallback
    return text or fallback


def _duration_minutes(value: object, *, ceiling: bool = False) -> int | None:
    """Convert a numeric duration to whole minutes, or return no duration."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if ceiling:
        return max(1, int(math.ceil(number)))
    return int(number)


def _duration_text(minutes: int) -> str:
    minutes = max(0, int(minutes))
    hours, remainder = divmod(minutes, 60)
    if hours:
        return f"{hours}h" if not remainder else f"{hours}h {remainder}m"
    return f"{remainder}m"


def authority_text(
    manual_state: object,
    hold_remaining_seconds: float | None,
    fallback: str,
) -> str:
    """Describe scheduler authority and any remaining timed hold."""
    state = _clean_text(manual_state).lower().replace("-", "_").replace(" ", "_")
    if state == "manual_qsy":
        label = "Manual QSY"
        timed = True
    elif state == "manual_hold":
        label = "Schedule held"
        timed = True
    elif state == "manual_suspend":
        return "Schedule held · until Resume"
    else:
        return _clean_text(fallback)

    try:
        remaining = float(hold_remaining_seconds)
    except (TypeError, ValueError, OverflowError):
        remaining = 0.0
    if timed and math.isfinite(remaining) and remaining > 0:
        return f"{label} · {_duration_text(_duration_minutes(remaining / 60, ceiling=True) or 1)} remaining"
    return f"{label} · until Resume"


def planned_transition_text(
    label: str,
    minutes: object | None,
    held: bool,
) -> NextActionState:
    """Render the next transition while distinguishing held schedule authority."""
    try:
        state = next_action_state(label, minutes)
    except Exception:
        state = next_action_state(_clean_text(label, "No scheduled action"), None)
    prefix = "Next planned · " if bool(held) else "Next · "
    return NextActionState(
        text=prefix + state.text,
        prominence=state.prominence,
        role="muted" if bool(held) else state.role,
    )


def sop_action_text(label: str, minutes: object | None, count: int) -> str:
    """Render the primary SOP action and the count of additional actions."""
    action = _clean_text(label)
    try:
        action_count = max(0, int(count))
    except (TypeError, ValueError, OverflowError):
        action_count = 0

    if action:
        text = action
        parsed_minutes = _duration_minutes(minutes)
        if parsed_minutes is not None:
            if parsed_minutes == 0:
                timing = "due now"
            elif parsed_minutes < 0:
                timing = f"overdue {_duration_text(abs(parsed_minutes))}"
            else:
                timing = f"in {_duration_text(parsed_minutes)}"
            text += f" · {timing}"
        if action_count > 1:
            text += f" · +{action_count - 1}"
    else:
        text = "SOP actions"
        if action_count > 0:
            text += f" · +{action_count}"
    return f"SOP: {text}" if action else text


def application_readback_text(
    name: str,
    target: object,
    observed: object,
    fresh: bool,
) -> str:
    """Render an observed application value against its configured target."""
    label = _clean_text(name, "Application")

    def hz(value: object) -> int | None:
        try:
            if value in (None, ""):
                return None
            number = float(value)
            if not math.isfinite(number):
                return None
            return int(number)
        except (TypeError, ValueError, OverflowError):
            return None

    desired, actual = hz(target), hz(observed)
    if not fresh or actual is None:
        return f"{label} unknown" + (f" · target {desired} Hz" if desired is not None else "")
    if desired is None:
        return f"{label} {actual} Hz · observed"
    if actual == desired:
        return f"{label} {actual} Hz · matched"
    return f"{label} {actual} → {desired} Hz"


def radio_activity_state_text(evidence: object, net: bool = False) -> str:
    """Render activity with an explicit unknown marker for incomplete evidence."""
    try:
        text = radio_activity_text(evidence, net=net)
    except Exception:
        text = "Activity ?" + (" · NET" if net else "")
    return text.replace("Activity ?", "Activity unknown")


def shell_layout_state(
    width: object,
    *,
    source_count: int = 1,
    large_text: bool = False,
) -> ShellLayoutState:
    """Return a deterministic shell density without inspecting Qt widgets."""
    try:
        available = max(0, int(width))
    except Exception:
        available = 0
    pressure = max(0, int(source_count or 0) - 1) * 90
    if large_text:
        pressure += 140
    effective = available - pressure
    if effective < 720:
        return ShellLayoutState("condensed", False, False, True)
    if effective < 1120:
        return ShellLayoutState("compact", False, False, False)
    return ShellLayoutState("roomy", True, True, False)


def schedule_prominence(remaining_minutes: object | None) -> ScheduleProminence:
    if remaining_minutes is None:
        return "calm"
    try:
        minutes = float(remaining_minutes)
    except Exception:
        return "calm"
    if minutes < 0:
        return "overdue"
    if minutes <= 15:
        return "urgent"
    if minutes <= 30:
        return "upcoming"
    return "calm"


def next_action_state(label: object, remaining_minutes: object | None) -> NextActionState:
    text = str(label or "").strip() or "No scheduled action"
    prominence = schedule_prominence(remaining_minutes)
    if remaining_minutes is not None:
        try:
            minutes = int(float(remaining_minutes))
        except Exception:
            minutes = -1
        if minutes >= 0:
            hours, remainder = divmod(minutes, 60)
            duration = f"{hours}h {remainder}m" if hours else f"{remainder}m"
            text = f"{text} in {duration}"
    role = {
        "calm": "muted",
        "upcoming": "info",
        "urgent": "warning",
        "overdue": "danger",
    }[prominence]
    return NextActionState(text=text, prominence=prominence, role=role)


def application_offset_text(name: str, target: object, observed: object, *, fresh: bool) -> str:
    def hz(value):
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    desired, actual = hz(target), hz(observed)
    if not fresh or actual is None:
        return f"{name} ?" + (f" · target {desired} Hz" if desired is not None else "")
    if desired is None:
        return f"{name} {actual} Hz · observed"
    return f"{name} {actual} Hz · " + ("verified" if actual == desired else f"target {desired}")


def radio_activity_text(evidence: dict, *, net: bool = False) -> str:
    fresh = not evidence.get("stale", True) and not evidence.get("invalidated") and not evidence.get("closed")
    labels = []
    if fresh:
        if evidence.get("ptt_known") and evidence.get("ptt_active"):
            labels.append("TX")
        if (evidence.get("js8_busy") and not evidence.get("ptt_active")) or evidence.get("fldigi_busy"):
            labels.append("RX")
        varac = evidence.get("varac_status") or {}
        if varac.get("busy") or varac.get("connected") or varac.get("bbs_session_active"):
            labels.append("QSO")
    if net:
        labels.append("NET")
    if not fresh or not evidence.get("ptt_known") or (not labels and not evidence.get("activity_known")):
        labels.append("Activity ?")
    return " · ".join(labels) or "Idle"


def relevant_sop_actions(rows, *, radio_id: int, operating_profile_id: int, groups: set[str], softwares: set[str]):
    selected = []
    for row in rows:
        if row.get("is_completed") or row.get("status") == "Completed":
            continue
        scope = row.get("target_scope", "station")
        if scope == "device_profile" and int(row.get("target_device_profile_id") or 0) != radio_id:
            continue
        if scope == "operating_profile" and int(row.get("target_operating_profile_id") or 0) != operating_profile_id:
            continue
        group = str(row.get("operating_group") or "").strip().upper()
        if group and group not in groups:
            continue
        software = str(row.get("software") or "").strip().upper()
        if software and software not in softwares:
            continue
        selected.append(row)
    priority = {"Overdue": 0, "Due Now": 1, "Upcoming": 2}
    return sorted(selected, key=lambda row: (priority.get(row.get("status"), 3), row.get("next_due_utc")))


def source_chip_text(
    name: object,
    now_text: object,
    *,
    density: ShellDensity,
) -> str:
    radio_name = str(name or "Radio").strip() or "Radio"
    context = str(now_text or "").strip()
    if density == "roomy" and context:
        return f"{radio_name}  ·  {context}"
    return radio_name


def primary_context_text(radio_name: object, now_text: object) -> str:
    name = str(radio_name or "Radio").strip() or "Radio"
    destination = str(now_text or "Unavailable").strip() or "Unavailable"
    return f"{name}  ·  {destination}"


def qsy_key(meta: Mapping[str, object] | None) -> str:
    if not isinstance(meta, Mapping):
        return ""
    try:
        return f"{float(meta.get('freq')):.6f}"
    except Exception:
        return ""


def frequency_controls_available(profile: Mapping[str, object] | None) -> bool:
    if not isinstance(profile, Mapping):
        return False
    backend = str(profile.get("control_backend", "") or "").strip().lower()
    use_varac = _truthy(profile.get("use_varac", profile.get("uses_varac", False)))
    use_flrig = _truthy(profile.get("use_flrig", profile.get("uses_flrig", False)))
    use_js8call = _truthy(profile.get("use_js8call", profile.get("uses_js8call", False)))
    use_fldigi = _truthy(profile.get("use_fldigi", profile.get("uses_fldigi", False)))
    varac_only = use_varac and not any((use_flrig, use_js8call, use_fldigi))
    if varac_only:
        return False
    return backend in {"flrig", "rigctld", "js8call"}


def qsy_action_state(
    *,
    selected_meta: Mapping[str, object] | None,
    preferred_key: str,
    radio_id: int,
    selection_changed: bool,
    manual_qsy_active: bool,
    timed_qsy_active: bool,
) -> QsyActionState:
    selected_key = qsy_key(selected_meta)
    armed = bool(selected_key)
    changed = bool(selection_changed and selected_key)
    enabled_base = int(radio_id or 0) > 0 and armed
    qsy_enabled = enabled_base and changed
    qsy_active = bool(manual_qsy_active or timed_qsy_active)
    timed_qsy_enabled = enabled_base and (changed or qsy_active)
    return QsyActionState(
        qsy_enabled=qsy_enabled,
        timed_qsy_enabled=timed_qsy_enabled,
        qsy_role="info" if changed else "muted",
        timed_qsy_role="warning" if qsy_active else ("info" if changed else "muted"),
    )


def scheduler_action_state(
    *,
    manual_qsy_active: bool = False,
    timed_qsy_active: bool,
    timed_suspend_active: bool,
    scheduler_suspended_manual: bool,
    scheduler_state_text: str,
) -> SchedulerActionState:
    state = str(scheduler_state_text or "").strip().lower()
    suspend_active = timed_suspend_active or scheduler_suspended_manual or state == "scheduler suspended"
    resume_active = (
        manual_qsy_active
        or timed_qsy_active
        or timed_suspend_active
        or scheduler_suspended_manual
        or state in {"manual hold", "manual qsy", "scheduler suspended"}
    )
    return SchedulerActionState(
        timed_suspend_text=(
            "Indefinite Suspend"
            if scheduler_suspended_manual
            else "Extend Suspend"
            if timed_suspend_active
            else "Timed Suspend"
        ),
        timed_suspend_role="warning" if suspend_active else "muted",
        resume_role="warning" if resume_active else "muted",
    )


def timed_qsy_text(*, timed_qsy_active: bool) -> str:
    return "Extend QSY" if timed_qsy_active else "Timed QSY"


def countdown_text(remaining_sec: object) -> str:
    try:
        seconds = max(0, int(float(remaining_sec)))
    except Exception:
        return ""
    if seconds < 10 * 60:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes:02d}:{secs:02d}"
    minutes = max(1, int((seconds + 59) // 60))
    return f"{minutes}m"


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def compact_card_next_text(*, group: str, band: str, frequency: str,
                           current_group: str, current_band: str,
                           minutes: object, held: bool = False) -> str:
    """Short card summary; retain a changed group or same-band dial distinction."""
    changed_group = _clean_text(group) if _clean_text(group).casefold() != _clean_text(current_group).casefold() else ""
    destination = (_clean_text(frequency) if _clean_text(band).upper() == _clean_text(current_band).upper()
                   and frequency else _clean_text(band))
    label = " ".join(part for part in (changed_group, destination) if part) or _clean_text(group, "destination")
    count = _duration_minutes(minutes)
    timing = "now" if count == 0 else _duration_text(count) if count is not None and count > 0 else ""
    return ("Planned: " if held else "Next: ") + label + (" · " + timing if timing else "")
