"""Shared pure Planner RF review rules, also used by worker preflight."""
from __future__ import annotations
import datetime
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from freqinout.core.logger import log
from freqinout.core.schedule_source_inputs import ScheduleSourceError
from freqinout.core.operating_group_frequency import format_frequency_mhz, resolve_operating_group_frequency
from freqinout.core.planner_radio_projection import PlannerRadioRules


def guard_key(payload, radio_id=None, guided_id=0):
    nonce = payload.get('_catalog_action_nonce')
    if nonce:
        return json.dumps((radio_id, int(guided_id or 0), nonce), separators=(',', ':'))
    token = payload.get('_catalog_guard_token')
    if token is not None and 'schedule_refs' not in payload and payload.get('name') == payload.get('_catalog_guard_name'):
        return json.dumps((radio_id, int(guided_id or 0), token), default=str, separators=(',', ':'))
    clean = {key:value for key,value in payload.items() if not key.startswith('_catalog_')}
    return json.dumps((radio_id, int(guided_id or 0), clean), sort_keys=True, default=str, separators=(',', ':'))


class PlannerGuardRules(PlannerRadioRules):
    def _schedule_refs_from_plan_row(self, plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
        if "schedule_refs" not in plan and "_catalog_schedule_refs" in plan:
            if plan.get("_catalog_error"):
                raise ScheduleSourceError(plan["_catalog_error"])
            return plan["_catalog_schedule_refs"]
        raw = plan.get("schedule_refs", plan.get("schedule_refs_json", "[]"))
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = []
        else:
            parsed = raw
        refs: List[Dict[str, Any]] = []
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    refs.append(dict(item))
        return refs

    def _assigned_radio_ids_for_plan(self, plan: Mapping[str, Any]) -> List[int]:
        if "_catalog_assigned_ids" in plan:
            return plan["_catalog_assigned_ids"]
        try:
            plan_id = int(plan.get("id") or 0)
        except Exception:
            plan_id = 0
        if plan_id <= 0:
            return []
        try:
            assignments = self.plan_context_service.store.list_effective_assigned_plans()
        except Exception:
            assignments = []
        ids: List[int] = []
        for assignment in assignments:
            try:
                assigned_plan_id = int(assignment.get("frequency_plan_id") or 0)
                radio_id = int(assignment.get("device_profile_id") or 0)
            except Exception:
                continue
            if assigned_plan_id == plan_id and radio_id > 0:
                ids.append(radio_id)
        return sorted(set(ids))

    @staticmethod
    def _plan_context_radio_id(context: Any) -> int:
        raw = getattr(context, "radio_profile_id", "") if context is not None else ""
        try:
            return int(str(raw).split("_")[-1])
        except Exception:
            return 0

    def _merge_frequency_variant_validation(
        self, validation: Mapping[str, Any], plan_payload: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Add cache-only exact frequency-reference checks to review/save validation."""
        result = dict(validation)
        operating_groups = self.settings.get("operating_groups", [])
        if not isinstance(operating_groups, (list, tuple)):
            operating_groups = []
        issues: List[str] = []
        for ref in self._schedule_refs_from_plan_row(plan_payload):
            variant_key = str(ref.get("operating_group_frequency_key") or "").strip()
            group_key = str(ref.get("operating_group_key") or "").strip()
            if not variant_key and not group_key and not any(
                str(ref.get(field) or "").strip() for field in ("band", "frequency", "freq", "frequency_mhz")
            ):
                continue
            group = ref.get("group_name") or ref.get("group")
            resolution = resolve_operating_group_frequency(
                operating_groups,
                group=group,
                group_key=group_key,
                frequency_key=variant_key,
                band=ref.get("band"),
                mode=ref.get("mode"),
                frequency=ref.get("frequency") or ref.get("freq") or ref.get("frequency_mhz"),
            )
            missing_keyed_band = bool(variant_key and not str(ref.get("band") or "").strip())
            if resolution.resolved and not missing_keyed_band:
                continue
            family = resolve_operating_group_frequency(operating_groups, group=group, group_key=group_key)
            # Explicit variant/group identities and references into a known
            # configured family must fail closed. Unknown keyless legacy groups
            # remain compatible with existing explicit scheduler rows.
            if not variant_key and not group_key and not family.candidates:
                continue
            group = str(group or "unknown group").strip()
            band = str(ref.get("band") or "").strip().upper()
            frequency = format_frequency_mhz(ref.get("frequency") or ref.get("freq") or ref.get("frequency_mhz"))
            label = " ".join(bit for bit in (group, band, frequency) if bit)
            issues.append(
                f"{label}: unresolved operating-group frequency variant ({'band is missing' if missing_keyed_band else resolution.reason or resolution.state}). "
                "Choose the intended configured frequency before saving or assigning this plan."
            )
        if issues:
            blocked = [str(item) for item in result.get("blocked", []) or []]
            messages = [str(item) for item in result.get("messages", []) or []]
            result["blocked"] = list(dict.fromkeys(blocked + issues))
            result["messages"] = list(dict.fromkeys(messages + issues))
            result["state"] = "blocked"
        return result

    def _rf_guard_preflight_for_plan(self, plan_payload: Dict[str, Any]) -> Dict[str, Any]:
        radio_lane_ids = self._radio_lane_ids_for_plan(plan_payload)
        if radio_lane_ids:
            validations: List[Dict[str, Any]] = []
            for radio_id in radio_lane_ids:
                subset_payload = self._plan_payload_for_radio_lane(plan_payload, radio_id)
                validation = self.plan_context_service.store.validate_frequency_plan_for_device(radio_id, subset_payload)
                validations.append(validation)
            sibling_validation = self._sibling_radio_lane_guard_validation(plan_payload, radio_lane_ids)
            if sibling_validation:
                validations.append(sibling_validation)
            return self._merge_rf_guard_validations(validations)
        assigned_radio_ids = self._assigned_radio_ids_for_plan(plan_payload)
        if assigned_radio_ids:
            validations = []
            for radio_id in assigned_radio_ids:
                validations.append(
                    self.plan_context_service.store.validate_frequency_plan_for_device(radio_id, plan_payload)
                )
            return self._merge_rf_guard_validations(validations)
        # Add Radio deliberately opens Plan Builder before the new plan has an
        # assignment row.  Preserve that explicit handoff as the RF Guard
        # context instead of falling through to the unrelated global/current
        # radio (or reporting that no radio was selected).
        try:
            guided_radio_id = int(
                getattr(self, "_guided_plan_handoff_device_profile_id", 0) or 0
            )
        except (TypeError, ValueError):
            guided_radio_id = 0
        if guided_radio_id > 0:
            return self.plan_context_service.store.validate_frequency_plan_for_device(
                guided_radio_id,
                plan_payload,
            )
        context = self.plan_context_service.context_for_tab("freqplanner", refresh=True)
        radio_id = self._plan_context_radio_id(context)
        if radio_id <= 0:
            return {
                "state": "off",
                "rf_guard_validation": "not_enforced",
                "messages": ["RF Guard preflight skipped because no radio context is selected."],
            }
        return self.plan_context_service.store.validate_frequency_plan_for_device(radio_id, plan_payload)

    def _radio_lane_ids_for_plan(self, plan_payload: Dict[str, Any]) -> List[int]:
        if "schedule_refs" not in plan_payload and "_catalog_lane_ids" in plan_payload:
            return plan_payload["_catalog_lane_ids"]
        ids: List[int] = []
        for ref in self._schedule_ref_mappings(plan_payload):
            radio_id = self._radio_id_for_schedule_ref(ref)
            if radio_id > 0:
                ids.append(radio_id)
        return sorted(set(ids))

    def _schedule_ref_mappings(self, plan_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if "schedule_refs" not in plan_payload and "_catalog_schedule_refs" in plan_payload:
            return plan_payload["_catalog_schedule_refs"]
        raw = plan_payload.get("schedule_refs", plan_payload.get("schedule_refs_json", []))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        refs: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    refs.append(dict(item))
        return refs

    def _plan_payload_for_radio_lane(self, plan_payload: Dict[str, Any], radio_id: int) -> Dict[str, Any]:
        lane_key = f"radio:{int(radio_id)}"
        refs = [
            ref
            for ref in self._schedule_ref_mappings(plan_payload)
            if str(ref.get("lane_key") or "").strip() == lane_key
            or self._radio_id_for_schedule_ref(ref) == int(radio_id)
        ]
        subset = dict(plan_payload)
        subset["name"] = f"{str(plan_payload.get('name') or 'SOP Schedule Plan').strip()} / Radio {radio_id}"
        subset["schedule_refs"] = refs
        subset["schedule_refs_json"] = json.dumps(refs)
        subset["frequency_refs"] = self._frequency_refs_for_schedule_refs(refs)
        subset["frequency_refs_json"] = json.dumps(subset["frequency_refs"])
        subset["group_refs"] = list(
            dict.fromkeys(str(ref.get("group_name") or "").strip().upper() for ref in refs if str(ref.get("group_name") or "").strip())
        )
        subset["group_refs_json"] = json.dumps(subset["group_refs"])
        return subset

    def _frequency_refs_for_schedule_refs(self, refs: List[Dict[str, Any]]) -> List[str]:
        out: List[str] = []
        for ref in refs:
            band = str(ref.get("band") or "").strip().upper()
            freq = str(ref.get("frequency") or ref.get("freq") or "").strip()
            if band and freq:
                out.append(f"{band}:{freq}")
            elif band:
                out.append(band)
            elif freq:
                out.append(freq)
        return list(dict.fromkeys(out))

    def _sibling_radio_lane_guard_validation(
        self,
        plan_payload: Dict[str, Any],
        radio_lane_ids: List[int],
    ) -> Optional[Dict[str, Any]]:
        if len(radio_lane_ids) < 2:
            return None
        store = self.plan_context_service.store
        devices = {
            int(radio_id): store.get_device_profile(int(radio_id))
            for radio_id in radio_lane_ids
            if int(radio_id) > 0
        }
        lane_payloads = {
            int(radio_id): self._plan_payload_for_radio_lane(plan_payload, int(radio_id))
            for radio_id in radio_lane_ids
            if int(radio_id) > 0
        }
        warnings: List[str] = []
        blocked: List[str] = []
        ids = sorted(lane_payloads)
        for left_index, left_id in enumerate(ids):
            left_device = devices.get(left_id) or {}
            left_payload = lane_payloads[left_id]
            for right_id in ids[left_index + 1 :]:
                right_device = devices.get(right_id) or {}
                right_payload = lane_payloads[right_id]
                for band in self._sibling_overlap_bands(left_payload, right_payload):
                    group = self._normalized_group(left_device.get("band_overlap_guard_group"))
                    if not group or group != self._normalized_group(right_device.get("band_overlap_guard_group")):
                        continue
                    mode = self._stricter_guard_mode(
                        left_device.get("band_overlap_guard_mode"),
                        right_device.get("band_overlap_guard_mode"),
                    )
                    message = (
                        f"{left_device.get('name') or f'Radio {left_id}'} and "
                        f"{right_device.get('name') or f'Radio {right_id}'} both have SOP Schedule Plan lanes "
                        f"on {band} in Prevent Band Overlap group {group}."
                    )
                    (blocked if mode == "block" else warnings).append(message)
                close_frequency_message = self._sibling_close_frequency_message(
                    left_device,
                    right_device,
                    left_payload,
                    right_payload,
                    left_id,
                    right_id,
                )
                if close_frequency_message:
                    mode, message = close_frequency_message
                    (blocked if mode == "block" else warnings).append(message)
        if not warnings and not blocked:
            return None
        return {
            "state": "blocked" if blocked else "warning",
            "rf_guard_validation": "enforced",
            "messages": warnings + blocked,
            "warnings": warnings,
            "blocked": blocked,
        }

    def _sibling_close_frequency_message(
        self,
        left_device: Dict[str, Any],
        right_device: Dict[str, Any],
        left_payload: Dict[str, Any],
        right_payload: Dict[str, Any],
        left_id: int,
        right_id: int,
    ) -> Optional[Tuple[str, str]]:
        group = self._normalized_group(left_device.get("advanced_frequency_guard_group"))
        if not group or group != self._normalized_group(right_device.get("advanced_frequency_guard_group")):
            return None
        left_window = self._coerce_nonnegative_int(left_device.get("advanced_frequency_guard_window_hz"))
        right_window = self._coerce_nonnegative_int(right_device.get("advanced_frequency_guard_window_hz"))
        threshold = max(left_window, right_window)
        if threshold <= 0 or not self._schedule_refs_overlap(left_payload, right_payload):
            return None
        left_freqs = self._frequency_hz_values_for_plan(left_payload)
        right_freqs = self._frequency_hz_values_for_plan(right_payload)
        from bisect import bisect_left, bisect_right
        ordered_right = sorted((value, index) for index, value in enumerate(right_freqs))
        right_values = [value for value, _ in ordered_right]
        for left_freq in left_freqs:
            lower = bisect_left(right_values, left_freq - threshold)
            upper = bisect_right(right_values, left_freq + threshold)
            if lower >= upper:
                continue
            # Match the previous nested loop's first original right-hand value,
            # preserving message ordering while avoiding a quadratic miss scan.
            right_index = min(index for _, index in ordered_right[lower:upper])
            right_freq = right_freqs[right_index]
            mode = self._stricter_guard_mode(
                left_device.get("advanced_frequency_guard_mode"),
                right_device.get("advanced_frequency_guard_mode"))
            return (
                mode,
                f"{left_device.get('name') or f'Radio {left_id}'} and "
                f"{right_device.get('name') or f'Radio {right_id}'} have SOP Schedule Plan lanes "
                f"within {threshold} Hz in Advanced Guard group {group} "
                f"({left_freq} Hz vs {right_freq} Hz).",
            )
        return None

    def _sibling_overlap_bands(self, left_payload, right_payload):
        def grouped(payload):
            result = {}
            for ref in self._schedule_ref_mappings(payload):
                band = str(ref.get("band") or "").strip().upper()
                if band:
                    result.setdefault(band, []).append(ref)
            return result
        left, right = grouped(left_payload), grouped(right_payload)
        return sorted(band for band in left.keys() & right.keys()
                      if self._schedule_refs_overlap({"schedule_refs": left[band]}, {"schedule_refs": right[band]}))


    def _schedule_refs_overlap(self, left_payload, right_payload):
        left = self._schedule_ref_mappings(left_payload)
        right = self._schedule_ref_mappings(right_payload)
        if not left or not right:
            return False
        def intervals(rows):
            days = [[] for _ in range(7)]
            for row in rows:
                start = self._parse_guard_hhmm(row.get("start_utc") or row.get("start"))
                end = self._parse_guard_hhmm(row.get("end_utc") or row.get("end"))
                if start is None or end is None:
                    return None  # Preserve the existing conservative rule.
                for day, begin, finish in self._weekly_ref_segments(
                        self._schedule_day_token(row.get("day_utc") or row.get("day")), start, end):
                    days[day].append((begin, finish))
            return [sorted(values) for values in days]
        left_days, right_days = intervals(left), intervals(right)
        if left_days is None or right_days is None:
            return True
        for left_values, right_values in zip(left_days, right_days):
            i = j = 0
            while i < len(left_values) and j < len(right_values):
                a, b = left_values[i], right_values[j]
                if a[0] < b[1] and b[0] < a[1]:
                    return True
                if a[1] <= b[1]:
                    i += 1
                else:
                    j += 1
        return False


    def _frequency_hz_values_for_plan(self, plan_payload: Dict[str, Any]) -> List[int]:
        values: List[int] = []
        for ref in self._schedule_ref_mappings(plan_payload):
            for key in ("frequency_hz", "freq_hz", "frequency", "freq"):
                parsed = self._parse_frequency_hz(ref.get(key))
                if parsed:
                    values.append(parsed)
        return list(dict.fromkeys(values))

    def _parse_frequency_hz(self, value: Any) -> Optional[int]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if number <= 0:
                return None
            if number < 1000:
                return int(round(number * 1_000_000))
            if number < 1_000_000:
                return int(round(number * 1000))
            return int(round(number))
        text = str(value or "").strip().upper().replace(" ", "")
        if not text:
            return None
        match = re.search(r"(\d{1,3}\.\d+|\d+(?:\.\d+)?)(MHZ|KHZ|HZ)?", text)
        if not match:
            return None
        try:
            number = float(match.group(1))
        except Exception:
            return None
        suffix = match.group(2) or ""
        if suffix == "HZ":
            return int(round(number))
        if suffix == "KHZ":
            return int(round(number * 1000))
        if suffix == "MHZ" or number < 1000:
            return int(round(number * 1_000_000))
        return None

    def _normalized_group(self, value: Any) -> str:
        text = str(value or "").strip().upper()
        return re.sub(r"\s+", " ", text)

    def _guard_mode(self, value: Any) -> str:
        text = str(value or "warn").strip().lower().replace("_", "-").replace(" ", "-")
        aliases = {"warn-only": "warn", "warning": "warn", "blocked": "block", "confirmation": "confirm"}
        text = aliases.get(text, text)
        return text if text in {"warn", "confirm", "block"} else "warn"

    def _stricter_guard_mode(self, left: Any, right: Any) -> str:
        order = {"warn": 0, "confirm": 1, "block": 2}
        left_mode = self._guard_mode(left)
        right_mode = self._guard_mode(right)
        return left_mode if order[left_mode] >= order[right_mode] else right_mode

    def _coerce_nonnegative_int(self, value: Any) -> int:
        try:
            parsed = int(float(str(value).strip()))
        except Exception:
            parsed = 0
        return max(0, parsed)

    def _merge_rf_guard_validations(self, validations: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not validations:
            return {"state": "ok", "rf_guard_validation": "enforced", "messages": ["RF guard validation completed."]}
        if any(str(value.get("state") or "").lower() in {"pending", "unavailable"}
               or str(value.get("rf_guard_validation") or "").lower() == "unavailable"
               for value in validations):
            return {"state": "pending", "rf_guard_validation": "unavailable",
                    "messages": ["RF Guard inputs are still being checked."]}
        messages: List[str] = []
        warnings: List[str] = []
        blocked: List[str] = []
        for validation in validations:
            radio_id = int(validation.get("device_profile_id") or 0)
            prefix = f"Radio {radio_id}: " if radio_id > 0 else ""
            for item in validation.get("warnings", []) or []:
                warnings.append(prefix + str(item))
            for item in validation.get("blocked", []) or []:
                blocked.append(prefix + str(item))
            for item in validation.get("messages", []) or []:
                text = str(item or "").strip()
                if text and text not in warnings and text not in blocked:
                    messages.append(prefix + text)
        state = "blocked" if blocked else "warning" if warnings else "ok"
        return {
            "state": state,
            "rf_guard_validation": "enforced",
            "messages": messages + warnings + blocked,
            "warnings": warnings,
            "blocked": blocked,
            "radio_lane_ids": [int(v.get("device_profile_id") or 0) for v in validations if int(v.get("device_profile_id") or 0) > 0],
        }
