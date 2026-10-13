from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from freqinout.core.schedule_targeting import schedule_targets_may_overlap


UTC = dt.timezone.utc
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
DAY_INDEX = {name.upper(): index for index, name in enumerate(DAY_NAMES)}


class DailyScheduleResolutionError(ValueError):
    """Raised when Daily HF rows cannot be resolved without guessing."""


@dataclass(frozen=True)
class ResolvedDailySegment:
    start_utc: dt.datetime
    end_utc: dt.datetime
    row: Mapping[str, Any]
    source_index: int
    occurrence_start_utc: dt.datetime
    occurrence_end_utc: dt.datetime
    implicit_continuation: bool = False
    provenance: str = "explicit"

    def resolved_row(self) -> dict[str, Any]:
        out = dict(self.row)
        out["_resolution_source_index"] = self.source_index
        out["_resolution_start_utc"] = self.start_utc
        out["_resolution_end_utc"] = self.end_utc
        out["_resolution_occurrence_start_utc"] = self.occurrence_start_utc
        out["_resolution_occurrence_end_utc"] = self.occurrence_end_utc
        out["_resolution_implicit_continuation"] = self.implicit_continuation
        out["_resolution_provenance"] = self.provenance
        return out


@dataclass(frozen=True)
class _Occurrence:
    row: Mapping[str, Any]
    source_index: int
    start_utc: dt.datetime
    end_utc: dt.datetime
    daily_continuity: bool


def resolve_daily_schedule_segments(
    rows: Sequence[Mapping[str, Any]],
    window_start_utc: dt.datetime,
    window_end_utc: dt.datetime,
) -> Tuple[ResolvedDailySegment, ...]:
    """Resolve HF rows into deterministic, non-overlapping UTC segments.

    A shorter contained row is an override. When it ends, the containing row
    resumes. Rows that recur every day also carry through an otherwise empty
    gap until the next daily row starts. A compiled named-weekday pattern also
    carries cyclically when it has an explicit start on every day of the week.
    An isolated named weekly row retains only its explicit window, including an
    overnight carry into the following UTC day.
    """

    window_start = _as_utc(window_start_utc)
    window_end = _as_utc(window_end_utc)
    if window_end <= window_start:
        raise ValueError("window_end_utc must be after window_start_utc")
    if not rows:
        return ()
    _validate_single_target_lane(rows)

    # Eight days on each side provides the preceding and following occurrence
    # needed to resolve cyclic daily/weekly continuity at both window edges.
    occurrence_start = window_start - dt.timedelta(days=8)
    occurrence_end = window_end + dt.timedelta(days=8)
    occurrences = _build_occurrences(rows, occurrence_start, occurrence_end)
    if not occurrences:
        return ()
    _validate_occurrences(occurrences)

    daily_occurrences = [occurrence for occurrence in occurrences if occurrence.daily_continuity]
    daily_segments = _resolve_occurrence_timeline(
        daily_occurrences,
        occurrence_start,
        occurrence_end,
        carry_gaps=True,
    )
    weekly_occurrences = [occurrence for occurrence in occurrences if not occurrence.daily_continuity]
    resolved = _overlay_weekly_occurrences(
        daily_segments,
        weekly_occurrences,
        occurrence_start,
        occurrence_end,
    )
    clipped = [_clip_segment(segment, window_start, window_end) for segment in resolved]
    return tuple(_merge_segments(segment for segment in clipped if segment is not None))


def resolve_active_daily_entry(
    now_utc: dt.datetime,
    rows: Sequence[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return the effective HF row at ``now_utc`` with resolution provenance."""

    now = _as_utc(now_utc)
    day_start = dt.datetime.combine(now.date(), dt.time(), tzinfo=UTC)
    segments = resolve_daily_schedule_segments(
        rows,
        day_start - dt.timedelta(days=1),
        day_start + dt.timedelta(days=2),
    )
    for segment in segments:
        if segment.start_utc <= now < segment.end_utc:
            return segment.resolved_row()
    return None


def next_effective_daily_boundary(
    now_utc: dt.datetime,
    rows: Sequence[Mapping[str, Any]],
    *,
    horizon_days: int = 62,
) -> tuple[Optional[dt.datetime], Optional[dict[str, Any]]]:
    """Return the next UTC instant where the effective HF row changes."""

    now = _as_utc(now_utc)
    horizon = max(1, int(horizon_days))
    recurrence_values = {_normalize_recurrence(row.get("recurrence")) for row in rows}
    if "PERIODIC" in recurrence_values:
        search_days = min(horizon, 40)
    elif "BI-WEEKLY" in recurrence_values:
        search_days = min(horizon, 15)
    else:
        search_days = min(horizon, 8)
    search_end = now + dt.timedelta(days=search_days)
    segments = resolve_daily_schedule_segments(rows, now, search_end)
    if not segments:
        return None, None
    active = next((segment for segment in segments if segment.start_utc <= now < segment.end_utc), None)
    active_key = _effective_identity(active.row) if active is not None else None
    cursor = now
    for segment in segments:
        if segment.end_utc <= now:
            continue
        if segment.start_utc > cursor:
            if active_key is not None:
                return cursor, None
            return segment.start_utc, segment.resolved_row()
        segment_key = _effective_identity(segment.row)
        if active_key is None:
            active_key = segment_key
        elif segment_key != active_key:
            return segment.start_utc, segment.resolved_row()
        cursor = max(cursor, segment.end_utc)
    if active_key is not None and cursor < search_end:
        return cursor, None
    return None, None


def rows_have_ambiguous_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two rows overlap in a way that requires user choice.

    Identical effective rows and strict containment are deterministic. Equal
    starts with different effective values and partial crossings are ambiguous.
    """

    if not schedule_targets_may_overlap(left, right):
        return False
    if _effective_identity(left) == _effective_identity(right):
        return False
    anchor = dt.datetime(2026, 1, 4, tzinfo=UTC)  # Sunday; date is otherwise immaterial.
    occurrences = _build_occurrences([left, right], anchor, anchor + dt.timedelta(days=15))
    left_occurrences = [occurrence for occurrence in occurrences if occurrence.source_index == 0]
    right_occurrences = [occurrence for occurrence in occurrences if occurrence.source_index == 1]
    for left_occurrence in left_occurrences:
        for right_occurrence in right_occurrences:
            if not _overlap(left_occurrence, right_occurrence):
                continue
            if left_occurrence.start_utc == right_occurrence.start_utc:
                return True
            left_contains = (
                left_occurrence.start_utc <= right_occurrence.start_utc
                and right_occurrence.end_utc <= left_occurrence.end_utc
            )
            right_contains = (
                right_occurrence.start_utc <= left_occurrence.start_utc
                and left_occurrence.end_utc <= right_occurrence.end_utc
            )
            if not left_contains and not right_contains:
                return True
    return False


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_hhmm(value: Any) -> Optional[int]:
    try:
        hour_text, minute_text = str(value or "").strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def _normalize_day(value: Any) -> str:
    text = str(value or "ALL").strip().upper()
    if text in {"", "ALL", "DAILY", "EVERYDAY"}:
        return "ALL"
    for name in DAY_NAMES:
        if text in {name.upper(), name[:3].upper()}:
            return name.upper()
    raise DailyScheduleResolutionError(f"Invalid Daily schedule weekday: {value!r}")


def _normalize_recurrence(value: Any) -> str:
    text = str(value or "Weekly").strip().upper()
    if text == "MONTHLY":
        return "PERIODIC"
    return text


def _parse_month_weeks(value: Any) -> tuple[int, ...]:
    weeks = []
    for token in str(value or "").replace(";", ",").split(","):
        try:
            week = int(token.strip())
        except Exception:
            continue
        if 1 <= week <= 5:
            weeks.append(week)
    return tuple(sorted(set(weeks)))


def _row_starts_on_date(row: Mapping[str, Any], date_value: dt.date) -> bool:
    recurrence = _normalize_recurrence(row.get("recurrence"))
    day = _normalize_day(row.get("day_utc") or row.get("day"))
    if recurrence == "DAILY":
        day = "ALL"
    sunday_index = (date_value.weekday() + 1) % 7
    if day != "ALL" and DAY_INDEX.get(day) != sunday_index:
        return False
    if recurrence == "PERIODIC":
        weeks = _parse_month_weeks(row.get("month_weeks")) or (1,)
        if 1 + ((date_value.day - 1) // 7) not in weeks:
            return False
    if recurrence == "BI-WEEKLY":
        try:
            offset = int(row.get("biweekly_offset_weeks") or 0)
        except Exception:
            offset = 0
        if (int(date_value.isocalendar()[1]) - offset) % 2 != 0:
            return False
    return True


def _is_daily_continuity_row(row: Mapping[str, Any]) -> bool:
    recurrence = _normalize_recurrence(row.get("recurrence"))
    day = _normalize_day(row.get("day_utc") or row.get("day"))
    return recurrence == "DAILY" or (recurrence == "WEEKLY" and day == "ALL")


def _target_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    scope = str(row.get("target_scope") or "station").strip().lower() or "station"
    return (
        scope,
        str(row.get("target_device_profile_id") or "") if scope == "device_profile" else "",
        str(row.get("target_operating_profile_id") or "") if scope == "operating_profile" else "",
    )


def _validate_single_target_lane(rows: Sequence[Mapping[str, Any]]) -> None:
    device_ids: set[str] = set()
    operating_ids: set[str] = set()
    for row in rows:
        scope, device_id, operating_id = _target_identity(row)
        if scope == "device_profile" and device_id:
            device_ids.add(device_id)
        elif scope == "operating_profile" and operating_id:
            operating_ids.add(operating_id)
    if len(device_ids) > 1 or len(operating_ids) > 1:
        raise DailyScheduleResolutionError(
            "Daily schedule resolution requires one target lane; resolve disjoint radio targets separately."
        )


def _cyclic_week_row_indices(rows: Sequence[Mapping[str, Any]]) -> set[int]:
    groups: dict[tuple[str, str, str], list[tuple[int, str]]] = {}
    for index, row in enumerate(rows):
        if _normalize_recurrence(row.get("recurrence")) != "WEEKLY":
            continue
        day = _normalize_day(row.get("day_utc") or row.get("day"))
        if day == "ALL":
            continue
        groups.setdefault(_target_identity(row), []).append((index, day))
    cyclic: set[int] = set()
    all_days = set(DAY_INDEX)
    for indexed_days in groups.values():
        if {day for _index, day in indexed_days} == all_days:
            cyclic.update(index for index, _day in indexed_days)
    return cyclic


def _build_occurrences(
    rows: Sequence[Mapping[str, Any]],
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> list[_Occurrence]:
    occurrences: list[_Occurrence] = []
    cyclic_week_indices = _cyclic_week_row_indices(rows)
    first_date = (start_utc - dt.timedelta(days=1)).date()
    last_date = end_utc.date()
    day_count = (last_date - first_date).days
    for source_index, row in enumerate(rows):
        start_minute = _parse_hhmm(row.get("start_utc") or row.get("start"))
        end_minute = _parse_hhmm(row.get("end_utc") or row.get("end"))
        if start_minute is None or end_minute is None:
            raise DailyScheduleResolutionError(
                f"Invalid Daily schedule time in row {source_index + 1}: "
                f"{row.get('start_utc') or row.get('start')!r}-"
                f"{row.get('end_utc') or row.get('end')!r}"
            )
        duration_minutes = (end_minute - start_minute) % (24 * 60)
        if duration_minutes == 0:
            duration_minutes = 24 * 60
        for offset in range(day_count + 1):
            date_value = first_date + dt.timedelta(days=offset)
            if not _row_starts_on_date(row, date_value):
                continue
            occurrence_start = dt.datetime.combine(date_value, dt.time(), tzinfo=UTC) + dt.timedelta(
                minutes=start_minute
            )
            occurrence_end = occurrence_start + dt.timedelta(minutes=duration_minutes)
            if occurrence_end <= start_utc or occurrence_start >= end_utc:
                continue
            occurrences.append(
                _Occurrence(
                    row=row,
                    source_index=source_index,
                    start_utc=occurrence_start,
                    end_utc=occurrence_end,
                    daily_continuity=(
                        _is_daily_continuity_row(row) or source_index in cyclic_week_indices
                    ),
                )
            )
    return sorted(occurrences, key=lambda value: (value.start_utc, value.end_utc, value.source_index))


def _effective_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    frequency = str(row.get("frequency") or row.get("freq") or "").strip()
    try:
        frequency = f"{float(frequency):.6f}"
    except Exception:
        pass
    return (
        str(row.get("band") or "").strip().upper(),
        frequency,
        str(row.get("mode") or "").strip().upper(),
        str(row.get("vfo") or "A").strip().upper() or "A",
        str(row.get("group_name") or row.get("group") or "").strip().upper(),
        str(row.get("fldigi_mode") or "").strip().upper(),
        str(row.get("fldigi_offset") or "").strip(),
        str(row.get("primary_js8call_group") or "").strip().upper(),
        "1"
        if str(row.get("auto_tune") or "").strip().lower() in {"1", "true", "yes", "on"}
        else "0",
    )


def _overlap(left: _Occurrence, right: _Occurrence) -> bool:
    return left.start_utc < right.end_utc and right.start_utc < left.end_utc


def _validate_occurrences(occurrences: Sequence[_Occurrence]) -> None:
    for index, left in enumerate(occurrences):
        for right in occurrences[index + 1 :]:
            if right.start_utc >= left.end_utc:
                break
            if left.source_index == right.source_index or not _overlap(left, right):
                continue
            if not schedule_targets_may_overlap(left.row, right.row):
                continue
            if _effective_identity(left.row) == _effective_identity(right.row):
                continue
            if left.start_utc == right.start_utc:
                raise DailyScheduleResolutionError(
                    f"Daily schedule rows {left.source_index + 1} and {right.source_index + 1} "
                    f"start together at {left.start_utc.isoformat()} with different settings."
                )
            left_contains = left.start_utc <= right.start_utc and right.end_utc <= left.end_utc
            right_contains = right.start_utc <= left.start_utc and left.end_utc <= right.end_utc
            if not left_contains and not right_contains:
                raise DailyScheduleResolutionError(
                    f"Daily schedule rows {left.source_index + 1} and {right.source_index + 1} "
                    f"cross at {max(left.start_utc, right.start_utc).isoformat()}."
                )


def _winner(occurrences: Sequence[_Occurrence]) -> _Occurrence:
    return max(
        occurrences,
        key=lambda occurrence: (
            occurrence.start_utc,
            -occurrence.source_index,
        ),
    )


def _resolve_occurrence_timeline(
    occurrences: Sequence[_Occurrence],
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    *,
    carry_gaps: bool,
) -> list[ResolvedDailySegment]:
    if not occurrences:
        return []
    boundaries = {start_utc, end_utc}
    for occurrence in occurrences:
        boundaries.add(max(start_utc, occurrence.start_utc))
        boundaries.add(min(end_utc, occurrence.end_utc))
    ordered = sorted(boundary for boundary in boundaries if start_utc <= boundary <= end_utc)
    result: list[ResolvedDailySegment] = []
    carried: Optional[_Occurrence] = None
    for segment_start, segment_end in zip(ordered, ordered[1:]):
        if segment_end <= segment_start:
            continue
        active = [
            occurrence
            for occurrence in occurrences
            if occurrence.start_utc <= segment_start < occurrence.end_utc
        ]
        implicit = False
        if active:
            selected = _winner(active)
            carried = selected
        elif carry_gaps and carried is not None:
            selected = carried
            implicit = True
        else:
            continue
        provenance = "implicit_continuation" if implicit else "explicit"
        if not implicit and selected.start_utc < segment_start:
            provenance = "resumed_after_override"
        result.append(
            ResolvedDailySegment(
                start_utc=segment_start,
                end_utc=segment_end,
                row=selected.row,
                source_index=selected.source_index,
                occurrence_start_utc=selected.start_utc,
                occurrence_end_utc=selected.end_utc,
                implicit_continuation=implicit,
                provenance=provenance,
            )
        )
    return _merge_segments(result)


def _overlay_weekly_occurrences(
    daily_segments: Sequence[ResolvedDailySegment],
    weekly_occurrences: Sequence[_Occurrence],
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> list[ResolvedDailySegment]:
    if not weekly_occurrences:
        return list(daily_segments)
    boundaries = {start_utc, end_utc}
    for segment in daily_segments:
        boundaries.update((segment.start_utc, segment.end_utc))
    for occurrence in weekly_occurrences:
        boundaries.add(max(start_utc, occurrence.start_utc))
        boundaries.add(min(end_utc, occurrence.end_utc))
    ordered = sorted(boundary for boundary in boundaries if start_utc <= boundary <= end_utc)
    result: list[ResolvedDailySegment] = []
    for segment_start, segment_end in zip(ordered, ordered[1:]):
        if segment_end <= segment_start:
            continue
        daily = next(
            (segment for segment in daily_segments if segment.start_utc <= segment_start < segment.end_utc),
            None,
        )
        weekly = [
            occurrence
            for occurrence in weekly_occurrences
            if occurrence.start_utc <= segment_start < occurrence.end_utc
        ]
        candidates = list(weekly)
        if daily is not None:
            candidates.append(
                _Occurrence(
                    row=daily.row,
                    source_index=daily.source_index,
                    start_utc=daily.occurrence_start_utc,
                    end_utc=daily.occurrence_end_utc,
                    daily_continuity=True,
                )
            )
        if not candidates:
            continue
        selected = _winner(candidates)
        if (
            daily is not None
            and selected.source_index == daily.source_index
            and selected.start_utc == daily.occurrence_start_utc
        ):
            result.append(
                ResolvedDailySegment(
                    start_utc=segment_start,
                    end_utc=segment_end,
                    row=daily.row,
                    source_index=daily.source_index,
                    occurrence_start_utc=daily.occurrence_start_utc,
                    occurrence_end_utc=daily.occurrence_end_utc,
                    implicit_continuation=daily.implicit_continuation,
                    provenance=daily.provenance,
                )
            )
        else:
            provenance = "explicit" if selected.start_utc == segment_start else "resumed_after_override"
            result.append(
                ResolvedDailySegment(
                    start_utc=segment_start,
                    end_utc=segment_end,
                    row=selected.row,
                    source_index=selected.source_index,
                    occurrence_start_utc=selected.start_utc,
                    occurrence_end_utc=selected.end_utc,
                    implicit_continuation=False,
                    provenance=provenance,
                )
            )
    return _merge_segments(result)


def _clip_segment(
    segment: ResolvedDailySegment,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> Optional[ResolvedDailySegment]:
    clipped_start = max(segment.start_utc, start_utc)
    clipped_end = min(segment.end_utc, end_utc)
    if clipped_end <= clipped_start:
        return None
    return ResolvedDailySegment(
        start_utc=clipped_start,
        end_utc=clipped_end,
        row=segment.row,
        source_index=segment.source_index,
        occurrence_start_utc=segment.occurrence_start_utc,
        occurrence_end_utc=segment.occurrence_end_utc,
        implicit_continuation=segment.implicit_continuation,
        provenance=segment.provenance,
    )


def _merge_segments(segments: Iterable[ResolvedDailySegment]) -> list[ResolvedDailySegment]:
    merged: list[ResolvedDailySegment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].end_utc == segment.start_utc
            and merged[-1].source_index == segment.source_index
            and merged[-1].occurrence_start_utc == segment.occurrence_start_utc
            and merged[-1].occurrence_end_utc == segment.occurrence_end_utc
            and merged[-1].implicit_continuation == segment.implicit_continuation
            and merged[-1].provenance == segment.provenance
        ):
            previous = merged[-1]
            merged[-1] = ResolvedDailySegment(
                start_utc=previous.start_utc,
                end_utc=segment.end_utc,
                row=previous.row,
                source_index=previous.source_index,
                occurrence_start_utc=previous.occurrence_start_utc,
                occurrence_end_utc=previous.occurrence_end_utc,
                implicit_continuation=previous.implicit_continuation,
                provenance=previous.provenance,
            )
        else:
            merged.append(segment)
    return merged
