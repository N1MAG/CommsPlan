"""Pure schedule outlook/time rules shared by worker and isolated legacy fixtures."""
from __future__ import annotations
import datetime as dt
import math
from typing import Any, Dict, List, Optional, Tuple
from freqinout.core.schedule_recurrence import biweekly_week_is_eligible
from freqinout.utils.timezones import get_timezone

class OutlookRules:
    @staticmethod
    def _action_enabled(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return int(value) != 0
        txt = str(value).strip().lower()
        if not txt:
            return False
        return txt in {"1", "true", "yes", "y", "on", "enabled"}


    def _build_sop_actions_in_window(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        only_active: bool = True,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if end <= start:
            return out
        max_total_rows = 400
        condition_levels = self._sop_manager._condition_level_map()  # type: ignore[attr-defined]
        profiles = self._sop_manager.list_profiles()
        for profile in profiles:
            if only_active and not bool(profile.get("active")):
                continue
            profile_id = int(profile.get("id") or 0)
            if profile_id <= 0:
                continue
            full = self._sop_manager.get_profile(profile_id)
            if not full:
                continue
            operating_group = str(full.get("operating_group") or "")
            for action in list(full.get("actions") or []):
                if not self._action_enabled(action.get("enabled", True)):
                    continue
                action_group = str(action.get("group_name") or "").strip().upper() or str(operating_group).strip().upper()
                group_level = condition_levels.get(action_group)
                if not self._sop_manager._action_condition_match(  # type: ignore[attr-defined]
                    str(action.get("condition_levels") or "ALL"),
                    group_level,
                ):
                    continue
                interval_m = int(action.get("interval_minutes") or 0)
                if interval_m <= 0:
                    interval_m = max(1, int(action.get("interval_hours") or 3)) * 60
                interval_m = max(1, interval_m)
                interval_phase_m = max(0, int(action.get("interval_phase_minutes") or 0)) % interval_m
                action_for_occurrence = dict(action)
                if not str(action_for_occurrence.get("daily_start_utc") or "").strip():
                    action_for_occurrence["daily_start_utc"] = str(full.get("sop_start_utc") or "00:00")
                if not str(action_for_occurrence.get("daily_end_utc") or "").strip():
                    action_for_occurrence["daily_end_utc"] = "23:59"
                action_for_occurrence["interval_minutes"] = interval_m
                action_for_occurrence["interval_phase_minutes"] = interval_phase_m
                occurrences = self._sop_manager.build_action_occurrences_in_window(
                    action_for_occurrence,
                    window_start_utc=start,
                    window_end_utc=end + dt.timedelta(seconds=1),
                )
                if not occurrences:
                    continue
                max_per_action = min(
                    240,
                    int(math.ceil((end - start).total_seconds() / max(60, interval_m * 60))) + 2,
                )
                emitted = 0
                rule = str(action.get("contact_rule") or "none").strip()
                selected_target = (action.get("contact_target") or "").strip().upper()
                targets: List[str] = []
                if rule in {"hub_or_hub_alt", "ncs_or_ancs"}:
                    if selected_target and selected_target != "__ANY_ROLE__":
                        targets = [selected_target]
                    else:
                        targets = ["Any (Role Match)"]
                elif rule == "group":
                    targets = [selected_target] if selected_target else ["Any (Group Match)"]
                elif rule in {"callsign", "peer", "local_profile", "local_group"}:
                    targets = [selected_target] if selected_target else []
                action_band = (action.get("band") or "").strip().upper()
                action_freq = (action.get("frequency") or "").strip() or str(full.get("frequency") or "")
                action_id = int(action.get("id") or 0)
                software = str(action.get("software") or "")
                mode = str(action.get("mode") or "").strip().upper()
                action_key = str(action.get("action_key") or "")
                action_label = str(action.get("action_label") or "")
                description = str(action.get("description") or "")
                for due, _due_end in occurrences:
                    if due < start or due > end:
                        continue
                    if emitted >= max_per_action:
                        break
                    if len(out) >= max_total_rows:
                        break
                    # ControlFreq outlook does not render alignment warnings for SOP rows;
                    # keep this lightweight to avoid per-occurrence DB schedule scans.
                    aligned = True
                    out.append(
                        {
                            "profile_id": profile_id,
                            "profile_name": str(full.get("name") or ""),
                            "operating_group": action_group or operating_group,
                            "band": action_band,
                            "frequency": action_freq,
                            "action_id": action_id,
                            "software": software,
                            "mode": mode,
                            "action_key": action_key,
                            "action_label": action_label,
                            "description": description,
                            "contact_rule": rule,
                            "contact_target": selected_target,
                            "contact_targets": targets,
                            "interval_minutes": interval_m,
                            "interval_phase_minutes": interval_phase_m,
                            "next_due_utc": due,
                            "aligned": aligned,
                            "status": "Upcoming",
                            "is_completed": False,
                        }
                    )
                    emitted += 1
                if len(out) >= max_total_rows:
                    break
            if len(out) >= max_total_rows:
                break
        out.sort(
            key=lambda x: (
                x.get("next_due_utc") if isinstance(x.get("next_due_utc"), dt.datetime) else dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                str(x.get("profile_name") or ""),
                str(x.get("action_label") or ""),
            )
        )
        return out


    def _collect_schedule_rows(
        self,
        start: dt.datetime,
        end: dt.datetime,
        include_day: bool = False,
        include_hf: bool = True,
        resolve_multiday: bool = False,
        preloaded_sop_actions: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()
        tz = self._get_display_tz()
        scan_multi_day = bool(resolve_multiday or include_day)

        # SOP upcoming actions
        if preloaded_sop_actions is None:
            horizon_hours = max(1, int(math.ceil(max(0.0, (end - start).total_seconds()) / 3600.0)))
            actions = self._sop_manager.build_upcoming_actions(
                horizon_hours=horizon_hours, only_active=True, now_utc=start
            )
        else:
            actions = preloaded_sop_actions
        for a in actions:
            due = a.get("next_due_utc") or a.get("due_utc")
            if not isinstance(due, dt.datetime):
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=dt.timezone.utc)
            else:
                due = due.astimezone(dt.timezone.utc)
            status_txt = str(a.get("status") or "").strip().upper()
            due_window_start = start
            if status_txt in {"DUE NOW", "OVERDUE", "COMPLETED"}:
                # SOP due-now items are represented by the current interval start,
                # which may be a few minutes before "now". Keep them visible.
                due_window_start = start - dt.timedelta(minutes=30)
            if not (due_window_start <= due <= end):
                continue
            grp = (a.get("operating_group") or "").strip().upper()
            if group_filter and grp != group_filter:
                continue
            label = a.get("action_label") or a.get("action") or "SOP"
            band = (a.get("band") or "").strip().upper()
            freq = (a.get("frequency") or "").strip()
            software = str(a.get("software") or "").strip()
            software_norm = software.lower()
            action_key = str(a.get("action_key") or "").strip()
            action_key_norm = action_key.lower()
            contact_rule = str(a.get("contact_rule") or "").strip().lower()
            profile_name = str(a.get("profile_name") or "").strip()
            description = str(a.get("description") or "").strip()
            targets = [str(x).strip() for x in (a.get("contact_targets") or []) if str(x).strip()]
            contact_target = str(a.get("contact_target") or "").strip()
            if not contact_target and targets:
                contact_target = targets[0]
            is_local_net_action = (
                software_norm in {"local net", "local"}
                or contact_rule in {"local_profile", "local_group"}
                or action_key_norm.startswith("local_")
            )
            if is_local_net_action:
                # Keep Local Net Group/Net text predictable for quick NCS scanning.
                profile_part = profile_name or "Local Net"
                target_part = contact_target or "-"
                description_part = description or str(label).strip() or "-"
                group_text = f"{profile_part} - {target_part} - {description_part}"
            else:
                group_text = f"{grp} {label}".strip() or profile_name or "SOP"
            display_due = due if due >= start else start
            when = self._format_display_time(display_due, include_day, tz)
            search_blob = " ".join(
                [
                    grp,
                    str(label),
                    group_text,
                    profile_name,
                    contact_target,
                    description,
                    "SOP",
                ]
            ).upper()
            if search and search not in search_blob:
                continue
            rows_out.append(
                {
                    "when_text": when,
                    "when_utc": due,
                    "type": "SOP",
                    "group": group_text,
                    "band_freq": f"{band} {freq}".strip(),
                    "band": band,
                    "freq_mhz": None,
                    "action": "Open SOP",
                    "action_kind": "open_sop",
                }
            )
        # HF + Net schedule (simple view)
        if include_hf:
            rows_out.extend(self._load_hf_schedule(start, end, include_day, tz, scan_multi_day=scan_multi_day))
        rows_out.extend(self._load_net_schedule(start, end, include_day, tz, scan_multi_day=scan_multi_day))
        rows_out.sort(
            key=lambda r: (
                r.get("when_utc") if isinstance(r.get("when_utc"), dt.datetime) else dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                str(r.get("type") or ""),
                str(r.get("group") or ""),
            )
        )
        return rows_out[:200]


    def _load_hf_schedule(
        self,
        start: dt.datetime,
        end: dt.datetime,
        include_day: bool,
        tz: dt.tzinfo,
        *,
        scan_multi_day: bool = False,
    ) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        rows = self._daily_schedule_rows()
        if not rows:
            return rows_out
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()
        for r in rows:
            row = r
            day = (row.get("day") or row.get("day_utc") or row.get("day_name") or "").strip()
            grp = (row.get("group_name") or row.get("group") or "").strip().upper()
            if group_filter and grp != group_filter:
                continue
            start_hm = (row.get("start") or row.get("start_utc") or "").strip()
            band = (row.get("band") or "").strip().upper()
            freq_raw = row.get("frequency") or row.get("freq") or ""
            recurrence = row.get("recurrence") or "Weekly"
            month_weeks = row.get("month_weeks") or ""
            when_utc = self._resolve_schedule_time_utc(
                start,
                end,
                day or "ALL",
                start_hm,
                scan_multi_day,
                recurrence,
                month_weeks,
                row.get("biweekly_offset_weeks", 0),
            )
            if when_utc is None:
                continue
            try:
                freq_mhz = float(str(freq_raw).strip())
            except Exception:
                freq_mhz = None
            freq_txt = f"{freq_mhz:.3f}" if isinstance(freq_mhz, float) else str(freq_raw).strip()
            when = self._format_display_time(when_utc, include_day, tz)
            if search and search not in grp and search not in (band + freq_txt).upper() and search not in "HF":
                continue
            rows_out.append(
                {
                    "when_text": when,
                    "when_utc": when_utc,
                    "type": "HF",
                    "schedule_source": row.get('frequency_plan_name') or 'HF Daily schedule',
                    "group": grp,
                    "band_freq": f"{band} {freq_txt}".strip(),
                    "band": band,
                    "freq_mhz": freq_mhz,
                    "action": "QSY + Hold" if isinstance(freq_mhz, float) else "",
                    "action_kind": "qsy" if isinstance(freq_mhz, float) else "",
                }
            )
        return rows_out


    def _load_net_schedule(
        self,
        start: dt.datetime,
        end: dt.datetime,
        include_day: bool,
        tz: dt.tzinfo,
        *,
        scan_multi_day: bool = False,
    ) -> List[Dict[str, Any]]:
        from freqinout.core.net_source_identity import net_source_identity
        from freqinout.core.net_preparation import NetValues
        import hashlib

        rows_out: List[Dict[str, Any]] = []
        rows = self._net_schedule_rows()
        if not rows:
            return rows_out
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()
        for r in rows:
            row = r
            day = (row.get("day") or row.get("day_utc") or "").strip()
            grp = (row.get("group_name") or row.get("group") or "").strip().upper()
            if group_filter and grp != group_filter:
                continue
            start_hm = (row.get("start") or row.get("start_utc") or "").strip()
            band = (row.get("band") or "").strip().upper()
            freq_raw = row.get("frequency") or row.get("freq") or ""
            net_name = (row.get("net_name") or "").strip()
            recurrence = row.get("recurrence") or "Weekly"
            if ('_outlook_source_row' in row and str(recurrence).upper() == 'DAILY'
                    and day.upper() not in {'', 'ALL', 'DAILY'}):
                # A generated daily plan has one concrete ref per weekday;
                # each ref must not independently recur on every other day.
                recurrence = 'Weekly'
            month_weeks = row.get("month_weeks") or ""
            when_utc = self._resolve_schedule_time_utc(
                start,
                end,
                day or "ALL",
                start_hm,
                scan_multi_day,
                recurrence,
                month_weeks,
                row.get("biweekly_offset_weeks", 0),
            )
            if when_utc is None:
                continue
            try:
                freq_mhz = float(str(freq_raw).strip())
            except Exception:
                freq_mhz = None
            freq_txt = f"{freq_mhz:.3f}" if isinstance(freq_mhz, float) else str(freq_raw).strip()
            when = self._format_display_time(when_utc, include_day, tz)
            if search and search not in grp and search not in net_name.upper() and search not in "NET":
                continue
            # Composed plans can clip a NET window and override its radio
            # target. Preparation must still bind to the canonical saved NET,
            # never mistake the composed row for an editable source record.
            source_row = row.get('_outlook_source_row', row)
            action, action_kind = 'Prepare net', 'prepare_net'
            source_label = row.get('frequency_plan_name') or 'HF Net schedule'
            if source_row is None:
                source_row = {}
                action, action_kind = '', ''
                source_label += ' · saved HF Net source could not be verified; review in Plan Builder'
            elif '_outlook_source_row' in row:
                comparison = dict(source_row, **row)
                for key in ('target_scope', 'target_device_profile_id', 'target_operating_profile_id'):
                    comparison[key] = source_row.get(key)
                if (str(source_row.get('recurrence') or '').upper() == 'DAILY'
                        or str(source_row.get('day_utc') or '').upper() in {'ALL', 'DAILY'}):
                    comparison['day_utc'] = source_row.get('day_utc')
                if net_source_identity(comparison) != net_source_identity(source_row):
                    action, action_kind = 'Review schedule', 'open_net'
            rows_out.append(
                {
                    "when_text": when,
                    "when_utc": when_utc,
                    "type": "NET",
                    "schedule_source": source_label,
                    "group": net_name or grp,
                    "band_freq": f"{band} {freq_txt}".strip(),
                    "band": band,
                    "freq_mhz": freq_mhz,
                    "action": action,
                    "action_kind": action_kind,
                    "source_identity": net_source_identity(source_row),
                    "source_values_identity": hashlib.sha256(NetValues.from_row(source_row).payload.encode()).hexdigest(),
                    "source_row_id": source_row.get("source_row_id") or source_row.get("id"),
                    "source_key": source_row.get('source_key') or (f"NET:{source_row['id']}" if source_row.get('id') else ''),
                }
            )
        return rows_out


    def _resolve_schedule_time_utc(
        self,
        start_utc: dt.datetime,
        end_utc: dt.datetime,
        day_value: str,
        hhmm: str,
        include_day: bool,
        recurrence_value: str = "Weekly",
        month_weeks_value: str = "",
        biweekly_offset_weeks: object = 0,
    ) -> Optional[dt.datetime]:
        hm = self._parse_hhmm(hhmm)
        if not hm:
            return None
        recurrence = self._normalize_recurrence(recurrence_value)
        month_weeks = self._parse_month_weeks(month_weeks_value)
        day_norm = (day_value or "ALL").strip().upper()
        day_span = max(0, (end_utc.date() - start_utc.date()).days)
        for i in range(day_span + 1):
            day_dt = start_utc + dt.timedelta(days=i)
            candidate = day_dt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if candidate < start_utc or candidate > end_utc:
                continue
            if self._row_applies_on_date(day_norm, candidate, recurrence, month_weeks, biweekly_offset_weeks):
                return candidate
            if not include_day:
                break
        return None


    def _normalize_recurrence(self, value: object) -> str:
        recurrence = str(value or "Weekly").strip().upper()
        if recurrence == "MONTHLY":
            recurrence = "PERIODIC"
        if recurrence not in {"WEEKLY", "DAILY", "PERIODIC", "BI-WEEKLY"}:
            return "WEEKLY"
        return recurrence


    def _parse_month_weeks(self, txt: object) -> List[int]:
        weeks: List[int] = []
        for token in str(txt or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                val = int(token)
            except Exception:
                continue
            if 1 <= val <= 5:
                weeks.append(val)
        return sorted(set(weeks))


    def _month_week_index(self, date_val: dt.date) -> int:
        return 1 + ((date_val.day - 1) // 7)


    def _day_matches(self, day_norm: str, candidate_utc: dt.datetime) -> bool:
        if day_norm in {"", "ALL", "DAILY"}:
            return True
        day_name = candidate_utc.strftime("%A").upper()
        day_key = day_norm[:3]
        return day_name.startswith(day_key) or day_norm.startswith(day_name[:3])


    def _row_applies_on_date(
        self,
        day_norm: str,
        candidate_utc: dt.datetime,
        recurrence: str,
        month_weeks: List[int],
        biweekly_offset_weeks: object = 0,
    ) -> bool:
        if recurrence == "DAILY":
            return True
        if not self._day_matches(day_norm, candidate_utc):
            return False
        if recurrence == "BI-WEEKLY":
            return biweekly_week_is_eligible({"biweekly_offset_weeks": biweekly_offset_weeks}, candidate_utc.date())
        if recurrence == "PERIODIC":
            weeks = month_weeks or [1]
            return self._month_week_index(candidate_utc.date()) in weeks
        return True


    def _get_display_tz(self) -> dt.tzinfo:
        if not self._show_local:
            return dt.timezone.utc
        try:
            tz_name = self.settings.get("timezone", "UTC") or "UTC"
            return get_timezone(tz_name)
        except Exception:
            return dt.timezone.utc


    def _format_display_time(
        self, utc_dt: dt.datetime, include_day: bool, tz: dt.tzinfo
    ) -> str:
        if not isinstance(utc_dt, dt.datetime):
            return "--:--"
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=dt.timezone.utc)
        local_dt = utc_dt.astimezone(tz) if self._show_local else utc_dt.astimezone(dt.timezone.utc)
        return local_dt.strftime("%a %H:%M") if include_day else local_dt.strftime("%H:%M")


    def _parse_hhmm(self, value: str) -> Optional[Tuple[int, int]]:
        txt = (value or "").strip()
        if not txt:
            return None
        parts = txt.split(":")
        if len(parts) == 1 and txt.isdigit() and len(txt) in (3, 4):
            h = int(txt[:-2])
            m = int(txt[-2:])
        elif len(parts) == 2:
            h = int(parts[0])
            m = int(parts[1])
        else:
            return None
        if h < 0 or h > 23 or m < 0 or m > 59:
            return None
        return h, m


    def _load_today_schedule_local(
        self, now_local: dt.datetime, tz: dt.tzinfo
    ) -> List[Tuple[dt.datetime, str, str]]:
        entries: List[Tuple[dt.datetime, str, str]] = []
        today_local = now_local.date()
        now_utc = now_local.astimezone(dt.timezone.utc)
        today_utc_name = now_utc.strftime("%A")
        group_filter = self.group_combo.currentData() or ""
        search = (self.search_edit.text() or "").strip().upper()

        def parse_hhmm(value: str) -> Optional[Tuple[int, int]]:
            txt = (value or "").strip()
            if not txt:
                return None
            parts = txt.split(":")
            if len(parts) == 1 and txt.isdigit() and len(txt) in (3, 4):
                h = int(txt[:-2])
                m = int(txt[-2:])
            elif len(parts) == 2:
                h = int(parts[0])
                m = int(parts[1])
            else:
                return None
            if h < 0 or h > 23 or m < 0 or m > 59:
                return None
            return h, m

        # HF schedule from settings DB
        for row in self._daily_schedule_rows():
            day = (row.get("day_utc") or row.get("day") or "ALL").strip().upper()
            if day not in {"ALL", today_utc_name.upper()}:
                continue
            start_hm = row.get("start_utc") or row.get("start") or ""
            hm = parse_hhmm(str(start_hm))
            if not hm:
                continue
            band = (row.get("band") or "").strip().upper()
            label = (row.get("group_name") or row.get("group") or "").strip().upper()
            if group_filter and label != group_filter:
                continue
            if search and search not in label and search not in band:
                continue
            start_utc = now_utc.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            start_local = start_utc.astimezone(tz)
            if start_local.date() == today_local:
                entries.append((start_local, band, label))

        # Net schedule from nets DB
        for row in self._net_schedule_rows():
            day = (row.get("day_utc") or row.get("day") or "ALL").strip().upper()
            if day not in {"ALL", today_utc_name.upper()}:
                continue
            start_hm = row.get("start_utc") or row.get("start") or ""
            hm = parse_hhmm(str(start_hm))
            if not hm:
                continue
            band = (row.get("band") or "").strip().upper()
            label = (row.get("net_name") or row.get("group_name") or "").strip().upper()
            if group_filter and label != group_filter:
                continue
            if search and search not in label and search not in band:
                continue
            start_utc = now_utc.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            start_local = start_utc.astimezone(tz)
            if start_local.date() == today_local:
                entries.append((start_local, band, label))
        return entries
