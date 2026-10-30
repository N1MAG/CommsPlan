"""Shared plain radio-window identity/overlap rules and worker preparation."""
from __future__ import annotations
import heapq
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

class PlannerRadioRules:
    def _radio_id_for_schedule_ref(self, ref: Mapping[str, Any]) -> int:
        lane_key = str(ref.get("lane_key") or "").strip()
        if lane_key.startswith("radio:"):
            radio_id = self._coerce_positive_int(lane_key.split(":", 1)[1])
            if radio_id > 0:
                return radio_id
        return self._coerce_positive_int(
            ref.get("radio_id") or ref.get("device_profile_id") or ref.get("target_device_profile_id")
        )

    def _coerce_positive_int(self, value: Any) -> int:
        try:
            number = int(value or 0)
        except Exception:
            number = 0
        return number if number > 0 else 0

    def _single_schedule_ref_overlap(self, left_ref: Dict[str, Any], right_ref: Dict[str, Any]) -> bool:
        left_start = self._parse_guard_hhmm(left_ref.get("start_utc") or left_ref.get("start"))
        left_end = self._parse_guard_hhmm(left_ref.get("end_utc") or left_ref.get("end"))
        right_start = self._parse_guard_hhmm(right_ref.get("start_utc") or right_ref.get("start"))
        right_end = self._parse_guard_hhmm(right_ref.get("end_utc") or right_ref.get("end"))
        if None in (left_start, left_end, right_start, right_end):
            return True
        left_segments = self._weekly_ref_segments(
            self._schedule_day_token(left_ref.get("day_utc") or left_ref.get("day")),
            int(left_start),
            int(left_end),
        )
        right_segments = self._weekly_ref_segments(
            self._schedule_day_token(right_ref.get("day_utc") or right_ref.get("day")),
            int(right_start),
            int(right_end),
        )
        for left_segment in left_segments:
            for right_segment in right_segments:
                if left_segment[0] == right_segment[0] and left_segment[1] < right_segment[2] and right_segment[1] < left_segment[2]:
                    return True
        return False

    def _weekly_ref_segments(self, day_token: str, start_minute: int, end_minute: int) -> List[Tuple[int, int, int]]:
        day_indices = range(7) if day_token == "ALL" else [self._day_index(day_token)]
        segments: List[Tuple[int, int, int]] = []
        for day_index in day_indices:
            if day_index < 0:
                continue
            if end_minute <= start_minute:
                segments.append((day_index, start_minute, 24 * 60))
                segments.append(((day_index + 1) % 7, 0, end_minute))
            else:
                segments.append((day_index, start_minute, end_minute))
        return segments

    def _day_index(self, day_token: str) -> int:
        normalized = str(day_token or "").strip().upper()
        for idx, day in enumerate(DAY_NAMES):
            if normalized == day.upper():
                return idx
        return -1

    def _parse_guard_hhmm(self, value: Any) -> Optional[int]:
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

    def _schedule_day_token(self, value: Any) -> str:
        text = re.sub(r"[^A-Z]", "", str(value or "ALL").strip().upper())
        if not text or text in {"ALL", "DAILY", "EVERYDAY"}:
            return "ALL"
        for day in DAY_NAMES:
            if text.startswith(day[:3].upper()):
                return day.upper()
        return text


def build_radio_windows(plan, assignments, devices, selected_radio, limit):
    rules = PlannerRadioRules()
    refs = (plan or {}).get('_catalog_schedule_refs', ())
    labels = {int(row['id']): str(row.get('name') or f"Radio {row['id']}") for row in devices}
    direct = any(rules._radio_id_for_schedule_ref(row) > 0 for row in refs)
    assigned = sorted({int(row['device_profile_id']) for row in assignments
                       if int(row.get('frequency_plan_id') or 0) == int((plan or {}).get('id') or 0)})
    def candidates():
        if direct:
            for ref in refs:
                radio_id = rules._radio_id_for_schedule_ref(ref)
                if radio_id > 0 and (not selected_radio or radio_id == selected_radio):
                    yield dict(ref)
        else:
            for radio_id in assigned:
                if selected_radio and radio_id != selected_radio:
                    continue
                for ref in refs:
                    row = dict(ref)
                    row.update(target_device_profile_id=radio_id, lane_key=f'radio:{radio_id}')
                    row.setdefault('lane_label', labels.get(radio_id, f'Radio {radio_id}'))
                    yield row
    count = 0
    def counted():
        nonlocal count
        for row in candidates():
            count += 1
            yield row
    def key(row):
        token = str(row.get('day_utc') or 'Sunday').upper()
        day = next((i for i, name in enumerate(DAY_NAMES) if name.upper() == token), 0)
        return day, rules._parse_guard_hhmm(row.get('start_utc')) or 0, rules._radio_id_for_schedule_ref(row), str(row.get('source') or '')
    rows = tuple(heapq.nsmallest(limit, counted(), key=key))
    overlap = {}
    for i, left in enumerate(rows):
        left_radio = rules._radio_id_for_schedule_ref(left)
        left_band = str(left.get('band') or '').strip().upper()
        messages = []
        for j, right in enumerate(rows):
            right_radio = rules._radio_id_for_schedule_ref(right)
            if i == j or right_radio == left_radio or not rules._single_schedule_ref_overlap(left, right):
                continue
            message = f"overlaps {labels.get(right_radio, f'Radio {right_radio}')}"
            if left_band and left_band == str(right.get('band') or '').strip().upper():
                message += f' on {left_band}'
            if message not in messages:
                messages.append(message)
        if messages:
            overlap[i] = '; '.join(messages)
    return {'refs': rows, 'total': count, 'overlap': overlap}
