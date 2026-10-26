from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from freqinout.core.daily_schedule_resolution import resolve_daily_schedule_segments
from freqinout.core.operating_group_frequency import format_frequency_mhz
from freqinout.core.schedule_targeting import (
    normalize_schedule_target_fields,
    schedule_target_identity_parts,
    schedule_targets_may_overlap,
)
from freqinout.core.schedule_recurrence import biweekly_week_is_eligible


DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
DAY_UPPER = tuple(day.upper() for day in DAY_NAMES)
SOP_SCHEDULE_LAYER_DEPENDENCY_PREFIX = "sop_schedule_layer"


@dataclass(frozen=True)
class ScheduleSegment:
    source: str
    day_utc: str
    start_minute: int
    end_minute: int
    band: str = ""
    frequency: str = ""
    mode: str = ""
    vfo: str = "A"
    group_name: str = ""
    net_name: str = ""
    profile_name: str = ""
    recurrence: str = "Weekly"
    month_weeks: str = ""
    row_signature: str = ""
    operating_group_frequency_key: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def start_utc(self) -> str:
        return _format_hhmm(self.start_minute)

    @property
    def end_utc(self) -> str:
        return _format_hhmm(self.end_minute)

    @property
    def label(self) -> str:
        if self.source == "NET":
            return self.net_name or self.group_name or "Net"
        if self.source == "SOP":
            return f"SOP:{self.group_name or self.profile_name}".rstrip(":")
        return self.band or self.frequency or self.group_name

    def to_schedule_ref(self) -> Dict[str, Any]:
        ref: Dict[str, Any] = {
            "source": self.source,
            "day_utc": self.day_utc,
            "recurrence": self.recurrence,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "band": self.band,
            "frequency": self.frequency,
            "mode": self.mode,
            "vfo": self.vfo,
            "group_name": self.group_name,
            "operating_group_frequency_key": self.operating_group_frequency_key,
            "net_name": self.net_name,
            "profile_name": self.profile_name,
            "target_scope": str(self.raw.get("target_scope") or ""),
            "target_device_profile_id": self.raw.get("target_device_profile_id"),
            "target_operating_profile_id": self.raw.get("target_operating_profile_id"),
        }
        for key in (
            "source_table",
            "source_row_id",
            "source_key",
            "resource_id",
            "sop_profile_id",
            "sop_layer_id",
            "implicit_continuation",
            "resolution_provenance",
            "occurrence_start_utc",
            "occurrence_end_utc",
            "canonical_source_row",
        ):
            if key in self.raw and self.raw.get(key) not in (None, ""):
                ref[key] = self.raw.get(key)
        return ref


@dataclass(frozen=True)
class ProjectionCell:
    day_utc: str
    hour_utc: int
    start_utc: dt.datetime
    end_utc: dt.datetime
    effective_source: str = ""
    display_label: str = ""
    band: str = ""
    frequency: str = ""
    hf_segments: Tuple[ScheduleSegment, ...] = ()
    net_segments: Tuple[ScheduleSegment, ...] = ()
    sop_segments: Tuple[ScheduleSegment, ...] = ()


@dataclass(frozen=True)
class BlendedScheduleProjection:
    week_start_utc: dt.date
    effective_segments: Tuple[ScheduleSegment, ...]
    cells: Tuple[ProjectionCell, ...]
    source_counts: Mapping[str, int]
    source_segments: Tuple[ScheduleSegment, ...] = ()

    def schedule_refs(self) -> List[Dict[str, Any]]:
        return [segment.to_schedule_ref() for segment in self.effective_segments]

    def source_refs(self) -> List[str]:
        refs = []
        if self.source_counts.get("HF", 0):
            refs.append("hf_daily")
        if self.source_counts.get("NET", 0):
            refs.append("hf_nets")
        if self.source_counts.get("SOP", 0):
            refs.append("sop")
            for segment in self.source_segments:
                if segment.source != "SOP":
                    continue
                try:
                    profile_id = int(segment.raw.get("sop_profile_id") or segment.raw.get("profile_id") or 0)
                except Exception:
                    profile_id = 0
                if profile_id > 0:
                    refs.append(f"{SOP_SCHEDULE_LAYER_DEPENDENCY_PREFIX}:{profile_id}")
        return list(dict.fromkeys(refs))

    def frequency_refs(self) -> List[str]:
        refs: List[str] = []
        for segment in self.effective_segments:
            band = segment.band.strip().upper()
            freq = segment.frequency.strip()
            if band and freq:
                refs.append(f"{band}:{freq}")
            elif band:
                refs.append(band)
            elif freq:
                refs.append(freq)
        return list(dict.fromkeys(refs))

    def group_refs(self) -> List[str]:
        refs = [segment.group_name.strip() for segment in self.effective_segments if segment.group_name.strip()]
        return list(dict.fromkeys(refs))


def build_blended_schedule_projection(
    hf_rows: Sequence[Mapping[str, Any]],
    net_rows: Sequence[Mapping[str, Any]],
    sop_rows: Sequence[Mapping[str, Any]],
    policy_rows: Sequence[Mapping[str, Any]] | None = None,
    *,
    week_start_utc: Optional[dt.date] = None,
) -> BlendedScheduleProjection:
    week_start = week_start_utc or _current_week_start_sunday_utc()
    policies = list(policy_rows or [])
    hf_source = _segments_for_rows(hf_rows, "HF", week_start)
    hf = _resolved_hf_segments(hf_rows, week_start)
    net = _segments_for_rows(net_rows, "NET", week_start)
    sop = _segments_for_rows(sop_rows, "SOP", week_start)

    sop_priority = _sop_priority_segments(sop, net, policies, week_start)
    net_effective = _subtract_segments(net, sop_priority)
    sop_effective = _subtract_segments(sop, _subtract_segments(net, sop_priority))
    hf_effective = _subtract_segments(hf, net_effective + sop_effective)
    effective: List[ScheduleSegment] = []
    effective.extend(hf_effective)
    effective.extend(net_effective)
    effective.extend(sop_effective)
    effective = _merge_adjacent_segments(sorted(effective, key=_segment_sort_key))
    cells = tuple(_build_cells(week_start, hf_source, net, sop, effective))
    return BlendedScheduleProjection(
        week_start_utc=week_start,
        effective_segments=tuple(effective),
        cells=cells,
        source_counts={"HF": len(hf_source), "NET": len(net), "SOP": len(sop)},
        source_segments=tuple(sorted(hf_source + net + sop, key=_segment_sort_key)),
    )


def _current_week_start_sunday_utc() -> dt.date:
    now = dt.datetime.now(dt.timezone.utc)
    delta = (now.weekday() + 1) % 7
    return (now - dt.timedelta(days=delta)).date()


def _format_hhmm(minute: int) -> str:
    minute = max(0, min(int(minute), 24 * 60))
    if minute >= 24 * 60:
        return "00:00"
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _parse_hhmm(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def _normalize_day(value: Any) -> str:
    raw = str(value or "ALL").strip()
    if not raw:
        return "ALL"
    upper = raw.upper()
    if upper in {"ALL", "DAILY", "EVERYDAY"}:
        return "ALL"
    for day in DAY_NAMES:
        if upper.startswith(day[:3].upper()):
            return day
    return "ALL"


def _target_days(day_value: Any, recurrence: Any = "") -> List[str]:
    recurrence_text = str(recurrence or "").strip().upper()
    day = "ALL" if recurrence_text == "DAILY" else _normalize_day(day_value)
    return list(DAY_NAMES) if day == "ALL" else [day]


def _month_week_index(date_value: dt.date) -> int:
    return 1 + ((date_value.day - 1) // 7)


def _parse_month_weeks(value: Any) -> List[int]:
    out: List[int] = []
    for token in str(value or "").replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            week = int(token)
        except Exception:
            continue
        if 1 <= week <= 5:
            out.append(week)
    return sorted(set(out))


def _normalize_month_weeks(value: Any) -> str:
    return ",".join(str(week) for week in _parse_month_weeks(value))


def _normalize_recurrence(value: Any) -> str:
    raw = str(value or "Weekly").strip().upper()
    if raw == "MONTHLY":
        raw = "PERIODIC"
    if raw in {"DAILY", "PERIODIC", "BI-WEEKLY", "WEEKLY"}:
        return "Bi-Weekly" if raw == "BI-WEEKLY" else raw.title()
    return "Weekly"


def _normalize_hhmm(value: Any) -> str:
    minute = _parse_hhmm(value)
    if minute is None:
        minute = 0
    return _format_hhmm(minute)


def _normalize_frequency(value: Any) -> str:
    return format_frequency_mhz(value)


def _row_applies_this_week(row: Mapping[str, Any], targets: Iterable[str], week_start: dt.date) -> bool:
    for day in targets:
        if _row_applies_on_day(row, day, week_start):
            return True
    return False


def _row_applies_on_day(row: Mapping[str, Any], day: str, week_start: dt.date) -> bool:
    recurrence = str(row.get("recurrence") or "Weekly").strip().upper()
    if recurrence == "MONTHLY":
        recurrence = "PERIODIC"
    try:
        day_index = DAY_NAMES.index(day)
    except ValueError:
        return False
    date_value = week_start + dt.timedelta(days=day_index)
    if recurrence == "PERIODIC":
        weeks = _parse_month_weeks(row.get("month_weeks")) or [1]
        return _month_week_index(date_value) in weeks
    if recurrence == "BI-WEEKLY":
        return biweekly_week_is_eligible(row, date_value)
    return True


def _row_signature(row: Mapping[str, Any], source: str) -> str:
    data = normalize_schedule_target_fields(dict(row))
    day = _normalize_day(data.get("day_utc") or data.get("day") or "ALL")
    recurrence = _normalize_recurrence(data.get("recurrence"))
    group = str(data.get("group_name") or data.get("group") or "").strip().upper()
    band = str(data.get("band") or "").strip().upper()
    freq = _normalize_frequency(data.get("frequency") or data.get("freq"))
    start = _normalize_hhmm(data.get("start_utc") or data.get("start") or "00:00")
    end = _normalize_hhmm(data.get("end_utc") or data.get("end") or "23:59")
    if source == "NET":
        net_name = str(data.get("net_name") or data.get("name") or "").strip().upper()
        target_scope = str(data.get("target_scope") or "station").strip().lower() or "station"
        target_device_profile_id = int(data.get("target_device_profile_id") or 0)
        target_operating_profile_id = int(data.get("target_operating_profile_id") or 0)
        return (
            f"NET|{group}|{band}|{freq}|{day}|{recurrence}|{int(data.get('biweekly_offset_weeks') or 0)}|"
            f"{_normalize_month_weeks(data.get('month_weeks'))}|{start}|{end}|{net_name}|{target_scope}|"
            f"{target_device_profile_id}|{target_operating_profile_id}"
        )
    if source == "SOP":
        profile_id = int(data.get("sop_profile_id") or 0)
        layer_id = int(data.get("sop_layer_id") or data.get("id") or 0)
        return (
            f"SOP|{profile_id}|{layer_id}|{group}|{band}|{freq}|{day}|"
            f"{recurrence}|{int(data.get('biweekly_offset_weeks') or 0)}|{_normalize_month_weeks(data.get('month_weeks'))}|{start}|{end}"
        )
    return f"HF|{group}|{band}|{freq}|{day}|{start}|{end}"


def _source_row_id(row: Mapping[str, Any], source: str) -> int:
    keys = ("source_row_id", "_source_row_id", "id", "_row_id")
    if source == "SOP":
        keys = ("source_row_id", "_source_row_id", "sop_layer_id", "id", "_row_id")
    for key in keys:
        try:
            value = int(row.get(key) or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    return 0


def _default_source_table(source: str) -> str:
    if source == "NET":
        return "net_schedule_tab"
    if source == "SOP":
        return "sop_schedule_layer"
    return "daily_schedule_tab"


def _stable_source_key(row: Mapping[str, Any], source: str, row_signature: str, source_row_id: int) -> str:
    if source_row_id > 0:
        return f"{source}:{source_row_id}"
    payload = {
        "source": source,
        "row_signature": row_signature,
        "day_utc": row.get("day_utc") or row.get("day"),
        "start_utc": row.get("start_utc") or row.get("start"),
        "end_utc": row.get("end_utc") or row.get("end"),
        "band": row.get("band"),
        "frequency": row.get("frequency") or row.get("freq"),
        "group_name": row.get("group_name") or row.get("group"),
        "net_name": row.get("net_name") or row.get("name"),
        "sop_profile_id": row.get("sop_profile_id"),
        "sop_layer_id": row.get("sop_layer_id"),
        "resource_id": row.get("resource_id") or row.get("_resource_id"),
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    return f"{source}:stable:{digest}"


def _segment_raw(row: Mapping[str, Any], source: str, row_signature: str) -> Dict[str, Any]:
    raw = dict(row)
    source_row_id = _source_row_id(row, source)
    raw.setdefault("source_table", str(row.get("source_table") or _default_source_table(source)))
    if source_row_id > 0:
        raw.setdefault("source_row_id", source_row_id)
    raw.setdefault("source_key", str(row.get("source_key") or _stable_source_key(row, source, row_signature, source_row_id)))
    if source == "NET":
        resource_id = row.get("resource_id") if row.get("resource_id") not in (None, "") else row.get("_resource_id")
        if resource_id not in (None, ""):
            try:
                raw.setdefault("resource_id", int(resource_id or 0))
            except Exception:
                raw.setdefault("resource_id", resource_id)
    if source == "SOP":
        # Retain the authoring row, not this week's split/clipped occurrence.
        # Do not nest copies when an existing canonical row is already attached.
        raw.setdefault("canonical_source_row", {key: value for key, value in row.items()
                                                if key != "canonical_source_row"})
        raw.setdefault("sop_profile_id", int(row.get("sop_profile_id") or 0))
        raw.setdefault("sop_layer_id", int(row.get("sop_layer_id") or row.get("id") or 0))
    return raw


def _segments_for_rows(
    rows: Sequence[Mapping[str, Any]],
    source: str,
    week_start: dt.date,
) -> List[ScheduleSegment]:
    segments: List[ScheduleSegment] = []
    for row in rows:
        start = _parse_hhmm(row.get("start_utc") or row.get("start"))
        end = _parse_hhmm(row.get("end_utc") or row.get("end"))
        if start is None or end is None:
            continue
        targets = _target_days(row.get("day_utc") or row.get("day") or "ALL", row.get("recurrence"))
        if not _row_applies_this_week(row, targets, week_start):
            continue
        row_signature = _row_signature(row, source)
        raw = _segment_raw(row, source, row_signature)
        for day in targets:
            if not _row_applies_on_day(row, day, week_start):
                continue
            intervals = [(day, start, end)] if start < end else [(day, start, 24 * 60), (_next_day(day), 0, end)]
            for interval_day, interval_start, interval_end in intervals:
                if interval_end <= interval_start:
                    continue
                segments.append(
                    ScheduleSegment(
                        source=source,
                        day_utc=interval_day,
                        start_minute=interval_start,
                        end_minute=interval_end,
                        band=str(row.get("band") or "").strip().upper(),
                        frequency=str(row.get("frequency") or row.get("freq") or "").strip(),
                        mode=str(row.get("mode") or "").strip(),
                        vfo=(str(row.get("vfo") or "A").strip().upper() or "A"),
                        group_name=str(row.get("group_name") or row.get("group") or "").strip().upper(),
                        net_name=str(row.get("net_name") or "").strip(),
                        profile_name=str(row.get("profile_name") or "").strip(),
                        recurrence=str(row.get("recurrence") or "Weekly").strip() or "Weekly",
                        month_weeks=str(row.get("month_weeks") or "").strip(),
                        row_signature=row_signature,
                        operating_group_frequency_key=str(row.get("operating_group_frequency_key") or ""),
                        raw=raw,
                    )
                )
    return segments


def _resolved_hf_segments(
    rows: Sequence[Mapping[str, Any]],
    week_start: dt.date,
) -> List[ScheduleSegment]:
    window_start = dt.datetime.combine(week_start, dt.time(), tzinfo=dt.timezone.utc)
    window_end = window_start + dt.timedelta(days=7)
    station_rows: List[Mapping[str, Any]] = []
    target_rows: Dict[Tuple[str, str, str], List[Mapping[str, Any]]] = {}
    for row in rows:
        target = schedule_target_identity_parts(row)
        if target[0] == "station":
            station_rows.append(row)
        else:
            target_rows.setdefault(target, []).append(row)

    resolved = list(resolve_daily_schedule_segments(station_rows, window_start, window_end))
    for target, lane_rows in target_rows.items():
        lane_resolved = resolve_daily_schedule_segments(
            station_rows + lane_rows,
            window_start,
            window_end,
        )
        # The station timeline is already retained once. Add only target-owned
        # winners from this lane so a target override survives without cloning
        # the station baseline into every radio lane.
        resolved.extend(
            item
            for item in lane_resolved
            if schedule_target_identity_parts(item.row) == target
        )
    segments: List[ScheduleSegment] = []
    for item in resolved:
        row = item.row
        row_signature = _row_signature(row, "HF")
        raw = _segment_raw(row, "HF", row_signature)
        raw.update(
            {
                "implicit_continuation": bool(item.implicit_continuation),
                "resolution_provenance": item.provenance,
                "occurrence_start_utc": item.occurrence_start_utc.isoformat(),
                "occurrence_end_utc": item.occurrence_end_utc.isoformat(),
            }
        )
        cursor = item.start_utc
        while cursor < item.end_utc:
            next_midnight = dt.datetime.combine(
                cursor.date() + dt.timedelta(days=1),
                dt.time(),
                tzinfo=dt.timezone.utc,
            )
            part_end = min(item.end_utc, next_midnight)
            day_index = (cursor.date() - week_start).days
            if 0 <= day_index < len(DAY_NAMES):
                start_minute = cursor.hour * 60 + cursor.minute
                end_minute = 24 * 60 if part_end == next_midnight else part_end.hour * 60 + part_end.minute
                segments.append(
                    ScheduleSegment(
                        source="HF",
                        day_utc=DAY_NAMES[day_index],
                        start_minute=start_minute,
                        end_minute=end_minute,
                        band=str(row.get("band") or "").strip().upper(),
                        frequency=str(row.get("frequency") or row.get("freq") or "").strip(),
                        mode=str(row.get("mode") or "").strip(),
                        vfo=(str(row.get("vfo") or "A").strip().upper() or "A"),
                        group_name=str(row.get("group_name") or row.get("group") or "").strip().upper(),
                        recurrence=str(row.get("recurrence") or "Weekly").strip() or "Weekly",
                        month_weeks=str(row.get("month_weeks") or "").strip(),
                        row_signature=row_signature,
                        operating_group_frequency_key=str(row.get("operating_group_frequency_key") or ""),
                        raw=raw,
                    )
                )
            cursor = part_end
    return segments


def _next_day(day: str) -> str:
    try:
        idx = DAY_NAMES.index(day)
    except ValueError:
        return DAY_NAMES[0]
    return DAY_NAMES[(idx + 1) % 7]


def _same_day_overlap(left: ScheduleSegment, right: ScheduleSegment) -> bool:
    return (
        left.day_utc == right.day_utc
        and left.start_minute < right.end_minute
        and right.start_minute < left.end_minute
        and schedule_targets_may_overlap(left.raw, right.raw)
    )


def _subtract_segments(
    base_segments: Sequence[ScheduleSegment],
    blockers: Sequence[ScheduleSegment],
) -> List[ScheduleSegment]:
    out: List[ScheduleSegment] = []
    for base in base_segments:
        spans = [(base.start_minute, base.end_minute)]
        for blocker in blockers:
            if not _same_day_overlap(base, blocker):
                continue
            next_spans: List[Tuple[int, int]] = []
            for start, end in spans:
                if blocker.end_minute <= start or blocker.start_minute >= end:
                    next_spans.append((start, end))
                    continue
                if start < blocker.start_minute:
                    next_spans.append((start, blocker.start_minute))
                if blocker.end_minute < end:
                    next_spans.append((blocker.end_minute, end))
            spans = next_spans
            if not spans:
                break
        for start, end in spans:
            if end > start:
                out.append(_replace_segment_window(base, start, end))
    return out


def _replace_segment_window(segment: ScheduleSegment, start: int, end: int) -> ScheduleSegment:
    return ScheduleSegment(
        source=segment.source,
        day_utc=segment.day_utc,
        start_minute=start,
        end_minute=end,
        band=segment.band,
        frequency=segment.frequency,
        mode=segment.mode,
        vfo=segment.vfo,
        group_name=segment.group_name,
        net_name=segment.net_name,
        profile_name=segment.profile_name,
        recurrence=segment.recurrence,
        month_weeks=segment.month_weeks,
        row_signature=segment.row_signature,
        operating_group_frequency_key=segment.operating_group_frequency_key,
        raw=segment.raw,
    )


def _segment_datetime_window(segment: ScheduleSegment, week_start: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    try:
        day_index = DAY_NAMES.index(segment.day_utc)
    except ValueError:
        day_index = 0
    day = week_start + dt.timedelta(days=day_index)
    start = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc) + dt.timedelta(minutes=segment.start_minute)
    end = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc) + dt.timedelta(minutes=segment.end_minute)
    return start, end


def _parse_policy_datetime(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _policy_window(row: Mapping[str, Any]) -> Optional[Tuple[dt.datetime, dt.datetime]]:
    start = _parse_policy_datetime(row.get("start_utc") or row.get("window_start_utc"))
    end = _parse_policy_datetime(row.get("end_utc") or row.get("window_end_utc"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _minute_for_datetime(value: dt.datetime) -> int:
    return value.hour * 60 + value.minute


def _sop_priority_segments(
    sop_segments: Sequence[ScheduleSegment],
    net_segments: Sequence[ScheduleSegment],
    policies: Sequence[Mapping[str, Any]],
    week_start: dt.date,
) -> List[ScheduleSegment]:
    priority: List[ScheduleSegment] = []
    for sop in sop_segments:
        sop_start_dt, sop_end_dt = _segment_datetime_window(sop, week_start)
        for net in net_segments:
            if not _same_day_overlap(sop, net):
                continue
            net_start_dt, net_end_dt = _segment_datetime_window(net, week_start)
            for row in policies:
                if str(row.get("policy") or "").strip().upper() != "SOP_PRIORITY":
                    continue
                if str(row.get("net_row_signature") or "").strip() != net.row_signature:
                    continue
                if str(row.get("sop_row_signature") or "").strip() != sop.row_signature:
                    continue
                window = _policy_window(row)
                if window is None:
                    continue
                start_dt = max(sop_start_dt, net_start_dt, window[0])
                end_dt = min(sop_end_dt, net_end_dt, window[1])
                if end_dt <= start_dt or start_dt.date() != sop_start_dt.date():
                    continue
                priority.append(_replace_segment_window(sop, _minute_for_datetime(start_dt), _minute_for_datetime(end_dt)))
    return _merge_adjacent_segments(sorted(priority, key=_segment_sort_key))


def _segment_sort_key(segment: ScheduleSegment) -> Tuple[int, int, str, str]:
    try:
        day_index = DAY_NAMES.index(segment.day_utc)
    except ValueError:
        day_index = 0
    return (day_index, segment.start_minute, segment.source, segment.label)


def _merge_adjacent_segments(segments: Sequence[ScheduleSegment]) -> List[ScheduleSegment]:
    merged: List[ScheduleSegment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].source == segment.source
            and merged[-1].day_utc == segment.day_utc
            and merged[-1].end_minute == segment.start_minute
            and merged[-1].band == segment.band
            and merged[-1].frequency == segment.frequency
            and merged[-1].mode == segment.mode
            and merged[-1].vfo == segment.vfo
            and merged[-1].group_name == segment.group_name
            and merged[-1].operating_group_frequency_key == segment.operating_group_frequency_key
            and merged[-1].net_name == segment.net_name
            and merged[-1].raw.get("implicit_continuation") == segment.raw.get("implicit_continuation")
            and merged[-1].raw.get("resolution_provenance") == segment.raw.get("resolution_provenance")
            and merged[-1].raw.get("occurrence_start_utc") == segment.raw.get("occurrence_start_utc")
            and merged[-1].raw.get("occurrence_end_utc") == segment.raw.get("occurrence_end_utc")
        ):
            merged[-1] = _replace_segment_window(merged[-1], merged[-1].start_minute, segment.end_minute)
        else:
            merged.append(segment)
    return merged


def _segments_in_hour(segments: Sequence[ScheduleSegment], day: str, hour: int) -> Tuple[ScheduleSegment, ...]:
    start = hour * 60
    end = start + 60
    return tuple(
        segment
        for segment in segments
        if segment.day_utc == day and segment.start_minute < end and start < segment.end_minute
    )


def _build_cells(
    week_start: dt.date,
    hf: Sequence[ScheduleSegment],
    net: Sequence[ScheduleSegment],
    sop: Sequence[ScheduleSegment],
    effective: Sequence[ScheduleSegment],
) -> List[ProjectionCell]:
    cells: List[ProjectionCell] = []
    for day_index, day in enumerate(DAY_NAMES):
        date_value = week_start + dt.timedelta(days=day_index)
        for hour in range(24):
            hour_start = dt.datetime.combine(date_value, dt.time(hour=hour), tzinfo=dt.timezone.utc)
            effective_segments = _segments_in_hour(effective, day, hour)
            effective_source = effective_segments[0].source if effective_segments else ""
            labels = list(dict.fromkeys(segment.label for segment in effective_segments if segment.label))
            bands = list(dict.fromkeys(segment.band for segment in effective_segments if segment.band))
            freqs = list(dict.fromkeys(segment.frequency for segment in effective_segments if segment.frequency))
            cells.append(
                ProjectionCell(
                    day_utc=day,
                    hour_utc=hour,
                    start_utc=hour_start,
                    end_utc=hour_start + dt.timedelta(hours=1),
                    effective_source=effective_source,
                    display_label=" / ".join(labels),
                    band="/".join(bands),
                    frequency="/".join(freqs),
                    hf_segments=_segments_in_hour(hf, day, hour),
                    net_segments=_segments_in_hour(net, day, hour),
                    sop_segments=_segments_in_hour(sop, day, hour),
                )
            )
    return cells
