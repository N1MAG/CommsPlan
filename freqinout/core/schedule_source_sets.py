from __future__ import annotations

import datetime
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional

from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.schedule_projection import build_blended_schedule_projection
from freqinout.core.schedule_source_inputs import ScheduleSourceError, load_sop_projection_sources
from freqinout.core.settings_manager import SettingsManager


LIVE_SOURCE_SET_ID = "__live__"
NO_NET_SOURCE_SET_ID = "__no_nets__"
NO_NET_SOURCE_SET_LABEL = "No Nets"
HF_DAILY_SOURCE_SETS_KEY = "freqplanner_hf_daily_schedule_sets"
HF_NET_SOURCE_SETS_KEY = "freqplanner_hf_net_schedule_sets"
SELECTED_HF_DAILY_SOURCE_SET_KEY = "freqplanner_selected_hf_daily_schedule_set_id"
SELECTED_HF_NET_SOURCE_SET_KEY = "freqplanner_selected_hf_net_schedule_set_id"
HF_DAILY_SOURCE_CATEGORY = "hf_daily_schedule"
HF_NET_SOURCE_CATEGORY = "hf_net_schedule"
SOP_SCHEDULE_LAYER_CATEGORY = "sop_schedule_layer"
_SOURCE_DEPENDENCY_PREFIXES = (HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY)


def slug_source_set_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or f"schedule-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"


def source_sets(settings: Any, key: str) -> List[Dict[str, Any]]:
    rows = settings.get(key, []) or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _parse_refs(value: Any) -> List[Dict[str, Any]]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if not isinstance(raw, list):
        return []
    return [dict(row) for row in raw if isinstance(row, dict)]


def _normalized_ref_items(value: Any) -> List[Any]:
    raw = value
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raw = []
        else:
            try:
                raw = json.loads(text)
            except Exception:
                raw = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        raw = list(raw)
    else:
        raw = []
    if not isinstance(raw, list):
        return []
    out: List[Any] = []
    seen: set[str] = set()
    for item in raw:
        if item in (None, ""):
            continue
        if isinstance(item, dict):
            clean_item: Any = {str(k): v for k, v in item.items() if v not in (None, "")}
        else:
            clean_item = str(item or "").strip()
        if clean_item in ({}, ""):
            continue
        key = json.dumps(clean_item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(clean_item)
    return out


def _refs_equivalent(left: Any, right: Any) -> bool:
    return _normalized_ref_items(left) == _normalized_ref_items(right)


def _parse_ref_values(value: Any) -> List[str]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if not isinstance(raw, list):
        return []
    refs: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            refs.append(text)
    return refs


def source_schedule_dependency_ref(category: str, set_id: str) -> str:
    clean_category = str(category or "").strip().lower()
    clean_set_id = str(set_id or "").strip()
    if clean_category not in _SOURCE_DEPENDENCY_PREFIXES:
        return ""
    if not clean_set_id or clean_set_id == LIVE_SOURCE_SET_ID:
        return ""
    return f"{clean_category}:{clean_set_id}"


def sop_schedule_dependency_ref(profile_id: Any) -> str:
    try:
        pid = int(profile_id or 0)
    except Exception:
        pid = 0
    return f"{SOP_SCHEDULE_LAYER_CATEGORY}:{pid}" if pid > 0 else ""


def sop_schedule_dependency_refs(rows: List[Dict[str, Any]]) -> List[str]:
    refs: List[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ref = sop_schedule_dependency_ref(row.get("sop_profile_id") or row.get("profile_id"))
        if ref:
            refs.append(ref)
    return list(dict.fromkeys(refs))


def selected_source_schedule_dependency_refs(settings: Any) -> List[str]:
    refs = [
        source_schedule_dependency_ref(
            HF_DAILY_SOURCE_CATEGORY,
            selected_source_set_id(settings, SELECTED_HF_DAILY_SOURCE_SET_KEY),
        ),
        source_schedule_dependency_ref(
            HF_NET_SOURCE_CATEGORY,
            selected_source_set_id(settings, SELECTED_HF_NET_SOURCE_SET_KEY),
        ),
    ]
    return [ref for ref in refs if ref]


def plan_source_usage_summary(
    store: MultiRadioStore,
    *,
    category: str,
    set_id: str,
    live_label: str,
    max_items: int = 4,
) -> Dict[str, Any]:
    """Return plans/radios that use a linked Daily or Net source schedule."""
    clean_category = str(category or "").strip().lower()
    clean_set_id = str(set_id or "").strip() or LIVE_SOURCE_SET_ID
    if clean_set_id == LIVE_SOURCE_SET_ID:
        if clean_category == HF_DAILY_SOURCE_CATEGORY:
            dependency_refs = {"hf_daily"}
        elif clean_category == HF_NET_SOURCE_CATEGORY:
            dependency_refs = {"hf_nets"}
        else:
            dependency_refs = {str(live_label or "live").strip()}
    else:
        dependency_refs = {source_schedule_dependency_ref(clean_category, clean_set_id)}
    dependency_refs = {ref for ref in dependency_refs if ref}

    try:
        plans = [dict(row) for row in store.list_frequency_plans()]
    except Exception:
        plans = []
    try:
        assignments = [dict(row) for row in store.list_effective_assigned_plans()]
    except Exception:
        assignments = []
    try:
        devices = [dict(row) for row in store.list_device_profiles()]
    except Exception:
        devices = []

    radio_names_by_id: Dict[int, str] = {}
    for row in devices:
        try:
            radio_id = int(row.get("id", 0) or 0)
        except Exception:
            continue
        if radio_id > 0:
            radio_names_by_id[radio_id] = str(row.get("name") or f"Radio {radio_id}").strip()
    radio_ids_by_plan_id: Dict[int, List[int]] = {}
    for assignment in assignments:
        try:
            plan_id = int(assignment.get("frequency_plan_id") or 0)
            radio_id = int(assignment.get("device_profile_id") or 0)
        except Exception:
            continue
        if plan_id > 0 and radio_id > 0:
            radio_ids_by_plan_id.setdefault(plan_id, []).append(radio_id)

    used_plans: List[Dict[str, Any]] = []
    radio_names: List[str] = []
    for plan in plans:
        try:
            plan_id = int(plan.get("id") or 0)
        except Exception:
            plan_id = 0
        if plan_id <= 0:
            continue
        refs = set(_parse_ref_values(plan.get("source_refs_json", plan.get("source_refs", "[]"))))
        if not (refs & dependency_refs):
            continue
        used_plans.append(plan)
        for radio_id in radio_ids_by_plan_id.get(plan_id, []):
            name = radio_names_by_id.get(radio_id, f"Radio {radio_id}")
            if name not in radio_names:
                radio_names.append(name)

    plan_names = [str(row.get("name") or f"Plan {row.get('id')}").strip() for row in used_plans]
    displayed_plans = plan_names[:max_items]
    displayed_radios = radio_names[:max_items]
    plan_suffix = f", +{len(plan_names) - max_items} more" if len(plan_names) > max_items else ""
    radio_suffix = f", +{len(radio_names) - max_items} more" if len(radio_names) > max_items else ""
    if plan_names:
        text = f"Used by: {', '.join(displayed_plans)}{plan_suffix}"
        if radio_names:
            text += f" | Radios: {', '.join(displayed_radios)}{radio_suffix}"
    else:
        text = "Not used by a saved plan yet."
    return {
        "plan_names": plan_names,
        "radio_names": radio_names,
        "text": text,
        "dependency_refs": sorted(dependency_refs),
    }


def _dependency_set_id(source_refs: List[str], category: str) -> str:
    prefix = f"{category}:"
    for ref in source_refs:
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ""


def _rows_for_dependency(settings: Any, category: str, set_id: str) -> List[Dict[str, Any]]:
    if category == HF_NET_SOURCE_CATEGORY and str(set_id or "").strip() == NO_NET_SOURCE_SET_ID:
        return []
    sets_key = HF_DAILY_SOURCE_SETS_KEY if category == HF_DAILY_SOURCE_CATEGORY else HF_NET_SOURCE_SETS_KEY
    if str(set_id).startswith("plan:"):
        try:
            plan = MultiRadioStore().get_frequency_plan(int(set_id.split(":", 1)[1]))
        except Exception as exc:
            raise ScheduleSourceError(f"Cannot read declared schedule source {set_id}: {exc}") from exc
        if not plan or str(plan.get("category") or "").lower() != category:
            raise ScheduleSourceError(f"Declared schedule source is unavailable: {category}:{set_id}")
        return _schedule_refs_for_plan(plan)
    row = source_set_row_by_id(settings, sets_key, set_id)
    if not row:
        raise ScheduleSourceError(f"Declared schedule source is unavailable: {category}:{set_id}")
    return [dict(item) for item in row.get("rows", []) if isinstance(item, dict)]


def _week_start_sunday_utc(now_utc: datetime.datetime) -> datetime.date:
    return (now_utc.date() - datetime.timedelta(days=(now_utc.weekday() + 1) % 7))


def _schedule_refs_for_plan(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _parse_refs(plan.get("schedule_refs_json", plan.get("schedule_refs", "[]")))


def _sop_sources_for_plan(settings: Any, plan: Dict[str, Any], source_refs: List[str]):
    """Declared SOP dependencies own recurrence; output occurrences are not inputs."""
    profile_ids = set()
    for ref in source_refs:
        if ref.startswith(f"{SOP_SCHEDULE_LAYER_CATEGORY}:"):
            try:
                profile_ids.add(int(ref.split(":", 1)[1]))
            except ValueError as exc:
                raise ScheduleSourceError(f"Invalid SOP dependency: {ref}") from exc
    if profile_ids:
        return load_sop_projection_sources(settings, profile_ids)
    # Old plans without dependency metadata retain their concrete saved windows.
    # New projections carry canonical rows, allowing exact source reconstruction.
    rows = []
    for ref in _schedule_refs_for_plan(plan):
        if str(ref.get("source") or "").upper() != "SOP":
            continue
        canonical = ref.get("canonical_source_row")
        row = dict(canonical) if isinstance(canonical, dict) else dict(ref)
        if not isinstance(canonical, dict) and str(row.get("day_utc") or "ALL").upper() != "ALL":
            if str(row.get("recurrence") or "").upper() == "DAILY":
                row["recurrence"] = "Weekly"
        if row not in rows:
            rows.append(row)
    return rows, []


def _replace_sop_refs_for_profile(
    current_refs: List[Dict[str, Any]],
    profile_id: int,
    updated_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for ref in current_refs:
        if not isinstance(ref, dict):
            continue
        source = str(ref.get("source") or "").strip().upper()
        try:
            ref_profile_id = int(ref.get("sop_profile_id") or ref.get("profile_id") or 0)
        except Exception:
            ref_profile_id = 0
        if source == "SOP" and ref_profile_id == int(profile_id):
            continue
        refs.append(dict(ref))
    for row in updated_rows:
        if not isinstance(row, dict):
            continue
        ref = dict(row)
        ref["source"] = "SOP"
        ref["sop_profile_id"] = int(profile_id)
        refs.append(ref)
    return refs


def _frequency_refs_for_schedule_refs(refs: List[Dict[str, Any]]) -> List[str]:
    values: List[str] = []
    for row in refs:
        if not isinstance(row, dict):
            continue
        band = str(row.get("band") or "").strip().upper()
        freq = str(row.get("frequency") or "").strip()
        if band and freq:
            values.append(f"{band}:{freq}")
        elif band:
            values.append(band)
        elif freq:
            values.append(freq)
    return list(dict.fromkeys(values))


def _group_refs_for_schedule_refs(refs: List[Dict[str, Any]]) -> List[str]:
    return sorted(
        {
            str(row.get("group_name") or row.get("group") or "").strip().upper()
            for row in refs
            if isinstance(row, dict) and str(row.get("group_name") or row.get("group") or "").strip()
        }
    )


def _reproject_refs_for_sop_update(
    settings: Any,
    plan: Dict[str, Any],
    source_refs: List[str],
    profile_id: int,
    updated_rows: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[str], List[str]]:
    daily_set_id = _dependency_set_id(source_refs, HF_DAILY_SOURCE_CATEGORY)
    net_set_id = _dependency_set_id(source_refs, HF_NET_SOURCE_CATEGORY)
    current_refs = _schedule_refs_for_plan(plan)
    if not daily_set_id and not net_set_id:
        # A direct SOP-only edit replaces the complete pending source supplied
        # by its editor. No composed recurrence rebuild or policy read is needed.
        refs = _replace_sop_refs_for_profile(current_refs, profile_id, [dict(row) for row in updated_rows])
        return refs, _frequency_refs_for_schedule_refs(refs), _group_refs_for_schedule_refs(refs)
    canonical_sop, policies = _sop_sources_for_plan(settings, plan, source_refs)
    other_sop_rows = [row for row in canonical_sop
                      if int(row.get("sop_profile_id") or row.get("profile_id") or 0) != int(profile_id)]
    sop_rows = other_sop_rows + [dict(row) for row in updated_rows if isinstance(row, dict)]
    if daily_set_id or net_set_id:
        projection = build_blended_schedule_projection(
            _rows_for_dependency(settings, HF_DAILY_SOURCE_CATEGORY, daily_set_id) if daily_set_id else [],
            _rows_for_dependency(settings, HF_NET_SOURCE_CATEGORY, net_set_id) if net_set_id else [],
            sop_rows,
            policies,
            week_start_utc=_week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc)),
        )
        return projection.schedule_refs(), projection.frequency_refs(), projection.group_refs()



def assigned_plan_rf_guard_impacts_for_sop_update(
    profile_id: int,
    updated_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    try:
        pid = int(profile_id or 0)
    except Exception:
        pid = 0
    dependency_ref = sop_schedule_dependency_ref(pid)
    if not dependency_ref:
        return []
    store = MultiRadioStore()
    settings = SettingsManager()
    plans_by_id = {int(row.get("id", 0) or 0): dict(row) for row in store.list_frequency_plans()}
    devices_by_id = {int(row.get("id", 0) or 0): dict(row) for row in store.list_device_profiles()}
    impacts: List[Dict[str, Any]] = []
    for assignment in store.list_effective_assigned_plans():
        plan_id = int(assignment.get("frequency_plan_id", 0) or 0)
        device_id = int(assignment.get("device_profile_id", 0) or 0)
        plan = plans_by_id.get(plan_id)
        if not plan:
            continue
        source_refs = _parse_ref_values(plan.get("source_refs_json", plan.get("source_refs", "[]")))
        if dependency_ref not in source_refs:
            continue
        refs, frequency_refs, group_refs = _reproject_refs_for_sop_update(settings, plan, source_refs, pid, updated_rows)
        payload = dict(plan)
        payload["schedule_refs"] = refs
        payload["schedule_refs_json"] = json.dumps(refs)
        payload["frequency_refs"] = frequency_refs
        payload["group_refs"] = group_refs
        try:
            validation = store.validate_frequency_plan_for_device(device_id, payload)
        except ValueError as exc:
            validation = {
                "state": "blocked",
                "rf_guard_validation": "enforced",
                "messages": [str(exc)],
                "blocked": [str(exc)],
            }
        state = str(validation.get("state") or "").strip().lower()
        if state not in {"blocked", "warning"}:
            continue
        impacts.append(
            {
                "assignment": dict(assignment),
                "plan": dict(plan),
                "device": dict(devices_by_id.get(device_id, {})),
                "validation": dict(validation),
            }
        )
    return impacts


def assigned_plan_rf_guard_impacts_for_source_update(
    settings: Any,
    category: str,
    set_id: str,
    updated_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    dependency_ref = source_schedule_dependency_ref(category, set_id)
    if not dependency_ref:
        return []
    store = MultiRadioStore()
    plans_by_id = {int(row.get("id", 0) or 0): dict(row) for row in store.list_frequency_plans()}
    devices_by_id = {int(row.get("id", 0) or 0): dict(row) for row in store.list_device_profiles()}
    impacts: List[Dict[str, Any]] = []
    for assignment in store.list_effective_assigned_plans():
        plan_id = int(assignment.get("frequency_plan_id", 0) or 0)
        device_id = int(assignment.get("device_profile_id", 0) or 0)
        plan = plans_by_id.get(plan_id)
        if not plan:
            continue
        source_refs = _parse_ref_values(plan.get("source_refs_json", plan.get("source_refs", "[]")))
        if dependency_ref not in source_refs:
            continue
        daily_set_id = _dependency_set_id(source_refs, HF_DAILY_SOURCE_CATEGORY)
        net_set_id = _dependency_set_id(source_refs, HF_NET_SOURCE_CATEGORY)
        daily_rows = _rows_for_dependency(settings, HF_DAILY_SOURCE_CATEGORY, daily_set_id) if daily_set_id else []
        net_rows = _rows_for_dependency(settings, HF_NET_SOURCE_CATEGORY, net_set_id) if net_set_id else []
        if category == HF_DAILY_SOURCE_CATEGORY:
            daily_rows = [dict(row) for row in updated_rows]
        elif category == HF_NET_SOURCE_CATEGORY:
            net_rows = [dict(row) for row in updated_rows]
        sop_rows, policies = _sop_sources_for_plan(settings, plan, source_refs)
        projection = build_blended_schedule_projection(
            daily_rows,
            net_rows,
            sop_rows,
            policies,
            week_start_utc=_week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc)),
        )
        payload = dict(plan)
        refs = projection.schedule_refs()
        payload["schedule_refs"] = refs
        payload["schedule_refs_json"] = json.dumps(refs)
        payload["frequency_refs"] = projection.frequency_refs()
        payload["group_refs"] = projection.group_refs()
        try:
            validation = store.validate_frequency_plan_for_device(device_id, payload)
        except ValueError as exc:
            validation = {
                "state": "blocked",
                "rf_guard_validation": "enforced",
                "messages": [str(exc)],
                "blocked": [str(exc)],
            }
        state = str(validation.get("state") or "").strip().lower()
        if state not in {"blocked", "warning"}:
            continue
        device = devices_by_id.get(device_id, {})
        impacts.append(
            {
                "assignment": dict(assignment),
                "plan": dict(plan),
                "device": dict(device),
                "validation": dict(validation),
            }
        )
    return impacts


def reproject_frequency_plans_for_source_update(
    settings: Any,
    category: str,
    set_id: str,
    updated_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Refresh saved Frequency Plans that depend on a named HF Daily/Net schedule.

    Saved Frequency Plans intentionally keep their own concrete schedule windows so
    scheduler startup does not need to rebuild planner projections. When a named
    Daily or Net component changes, every dependent plan must be reprojected before
    the scheduler reads it again; otherwise radios keep following stale windows.
    """

    dependency_ref = source_schedule_dependency_ref(category, set_id)
    if not dependency_ref:
        return []
    store = MultiRadioStore()
    updated: List[Dict[str, Any]] = []
    for plan in store.list_frequency_plans():
        plan_id = int(plan.get("id", 0) or 0)
        if plan_id <= 0:
            continue
        plan_category = str(plan.get("category") or "").strip().lower()
        if plan_category in {HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY}:
            continue
        source_refs = _parse_ref_values(plan.get("source_refs_json", plan.get("source_refs", "[]")))
        if dependency_ref not in source_refs:
            continue
        daily_set_id = _dependency_set_id(source_refs, HF_DAILY_SOURCE_CATEGORY)
        net_set_id = _dependency_set_id(source_refs, HF_NET_SOURCE_CATEGORY)
        daily_rows = _rows_for_dependency(settings, HF_DAILY_SOURCE_CATEGORY, daily_set_id) if daily_set_id else []
        net_rows = _rows_for_dependency(settings, HF_NET_SOURCE_CATEGORY, net_set_id) if net_set_id else []
        if category == HF_DAILY_SOURCE_CATEGORY:
            daily_rows = [dict(row) for row in updated_rows]
        elif category == HF_NET_SOURCE_CATEGORY:
            net_rows = [dict(row) for row in updated_rows]
        sop_rows, policies = _sop_sources_for_plan(settings, plan, source_refs)
        projection = build_blended_schedule_projection(
            daily_rows,
            net_rows,
            sop_rows,
            policies,
            week_start_utc=_week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc)),
        )
        refs = _normalized_ref_items(projection.schedule_refs())
        frequency_refs = projection.frequency_refs()
        group_refs = projection.group_refs()
        if (
            _refs_equivalent(refs, plan.get("schedule_refs_json", plan.get("schedule_refs", "[]")))
            and _refs_equivalent(
                frequency_refs,
                plan.get("frequency_refs_json", plan.get("frequency_refs", "[]")),
            )
            and _refs_equivalent(group_refs, plan.get("group_refs_json", plan.get("group_refs", "[]")))
        ):
            continue
        payload = dict(plan)
        payload["id"] = plan_id
        payload["schedule_refs"] = refs
        payload["frequency_refs"] = frequency_refs
        payload["group_refs"] = group_refs
        payload["notes"] = (
            f"Reprojected after {category} source update "
            f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}."
        )
        updated.append(payload)
    # Read/validate every dependency before persisting any replacement plan.
    return [store.save_frequency_plan(payload) for payload in updated]


def _source_backed_plan_payload(settings: Any, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate/materialize one plan without writing or affecting another plan."""
    plan_id = int(plan.get("id", 0) or 0)
    if plan_id <= 0:
        return None
    plan_category = str(plan.get("category") or "").strip().lower()
    if plan_category in {HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY}:
        return None
    source_refs = _parse_ref_values(plan.get("source_refs_json", plan.get("source_refs", "[]")))
    daily_set_id = _dependency_set_id(source_refs, HF_DAILY_SOURCE_CATEGORY)
    net_set_id = _dependency_set_id(source_refs, HF_NET_SOURCE_CATEGORY)
    if not daily_set_id and not net_set_id:
        if any(ref.startswith(f"{SOP_SCHEDULE_LAYER_CATEGORY}:") for ref in source_refs):
            _sop_sources_for_plan(settings, plan, source_refs)
        return None
    daily_rows = _rows_for_dependency(settings, HF_DAILY_SOURCE_CATEGORY, daily_set_id) if daily_set_id else []
    net_rows = _rows_for_dependency(settings, HF_NET_SOURCE_CATEGORY, net_set_id) if net_set_id else []
    sop_rows, policies = _sop_sources_for_plan(settings, plan, source_refs)
    projection = build_blended_schedule_projection(
        daily_rows,
        net_rows,
        sop_rows,
        policies,
        week_start_utc=_week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc)),
    )
    refs = _normalized_ref_items(projection.schedule_refs())
    frequency_refs = projection.frequency_refs()
    group_refs = projection.group_refs()
    if (
        _refs_equivalent(refs, plan.get("schedule_refs_json", plan.get("schedule_refs", "[]")))
        and _refs_equivalent(
            frequency_refs,
            plan.get("frequency_refs_json", plan.get("frequency_refs", "[]")),
        )
        and _refs_equivalent(group_refs, plan.get("group_refs_json", plan.get("group_refs", "[]")))
    ):
        return None
    payload = dict(plan)
    payload["id"] = plan_id
    payload["schedule_refs"] = refs
    payload["frequency_refs"] = frequency_refs
    payload["group_refs"] = group_refs
    payload["notes"] = (
        f"Refreshed from source schedules {datetime.datetime.now(datetime.timezone.utc).isoformat()}."
    )
    return payload


def refresh_source_backed_frequency_plans(
    settings: Any, *, source_errors: Optional[Dict[int, str]] = None,
    revision_writes: Optional[List[int]] = None,
    store: Optional[MultiRadioStore] = None,
) -> List[Dict[str, Any]]:
    """Rebuild plans; scheduler callers may isolate invalid dependencies by plan.

    Infrastructure/CAS failures still abort publication. Strict callers validate
    every plan before saving any derived rows, retaining their existing contract.
    """
    if store is None:
        store = MultiRadioStore()
        plans = store.list_frequency_plans()
    else:
        # Scheduler owns a migrated store; enumerating sources must not repeat
        # schema assurance or acquire a write lock on each materialization.
        with store.configuration_read_transaction():
            plans = store.list_frequency_plans()
    updated: List[Dict[str, Any]] = []
    expected_by_id = {}
    for plan in plans:
        try:
            payload = _source_backed_plan_payload(settings, plan)
        except (ScheduleSourceError, ValueError) as exc:
            cause = exc
            while cause is not None:
                if isinstance(cause, (sqlite3.Error, OSError)):
                    raise
                cause = cause.__cause__
            if source_errors is None:
                raise
            source_errors[int(plan["id"])] = str(exc)
            continue
        if payload is not None:
            expected_by_id[int(payload["id"])] = dict(plan)
            updated.append(payload)
    saved = []
    for payload in updated:
        expected = expected_by_id[int(payload["id"])]
        if revision_writes is None:
            saved.append(store.save_frequency_plan(payload, expected=expected))
        else:
            row, delta = store.materialize_frequency_plan_with_revision_delta(payload, expected=expected)
            saved.append(row)
            revision_writes.append(delta)
    return saved


def source_sets_for_category(settings: Any, legacy_key: str, category: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        store = MultiRadioStore()
        for plan in store.list_frequency_plans():
            if str(plan.get("category") or "").strip().lower() != category:
                continue
            plan_id = int(plan.get("id", 0) or 0)
            if plan_id <= 0:
                continue
            rows.append(
                {
                    "id": f"plan:{plan_id}",
                    "db_id": plan_id,
                    "name": str(plan.get("name") or f"Schedule #{plan_id}"),
                    "rows": _parse_refs(plan.get("schedule_refs_json", plan.get("schedule_refs", "[]"))),
                    "created_utc": str(plan.get("created_utc") or ""),
                    "updated_utc": str(plan.get("updated_utc") or ""),
                    "storage": "db",
                }
            )
    except Exception:
        rows = []
    for row in source_sets(settings, legacy_key):
        legacy = dict(row)
        legacy.setdefault("storage", "settings")
        rows.append(legacy)
    return rows


def selected_source_set_id(settings: Any, selected_key: str) -> str:
    return str(settings.get(selected_key, LIVE_SOURCE_SET_ID) or LIVE_SOURCE_SET_ID)


def source_set_row_by_id(settings: Any, sets_key: str, set_id: str) -> Optional[Dict[str, Any]]:
    target = str(set_id or "").strip()
    if not target or target == LIVE_SOURCE_SET_ID:
        return None
    for row in source_sets(settings, sets_key):
        if str(row.get("id") or "").strip() == target:
            return row
    return None


def source_set_row_by_id_for_category(
    settings: Any,
    sets_key: str,
    category: str,
    set_id: str,
) -> Optional[Dict[str, Any]]:
    target = str(set_id or "").strip()
    if not target or target == LIVE_SOURCE_SET_ID:
        return None
    for row in source_sets_for_category(settings, sets_key, category):
        if str(row.get("id") or "").strip() == target:
            return row
    return None


def save_source_set(settings: Any, sets_key: str, selected_key: str, name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Enter a clear schedule source name before saving.")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    base_id = slug_source_set_id(clean_name)
    set_id = base_id
    existing_sets = source_sets(settings, sets_key)
    taken = {str(row.get("id") or "") for row in existing_sets}
    if set_id in taken:
        set_id = f"{base_id}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    counter = 2
    while set_id in taken:
        set_id = f"{base_id}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{counter}"
        counter += 1
    saved = {
        "id": set_id,
        "name": clean_name,
        "rows": [dict(row) for row in rows],
        "created_utc": now,
        "updated_utc": now,
    }
    existing_sets.append(saved)
    settings.set(sets_key, existing_sets)
    settings.set(selected_key, set_id)
    if hasattr(settings, "save"):
        settings.save()
    return saved


def save_source_schedule(
    settings: Any,
    category: str,
    selected_key: str,
    name: str,
    rows: List[Dict[str, Any]],
    *,
    existing_plan_id: Optional[int] = None,
) -> Dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Enter a clear schedule name before saving.")
    payload: Dict[str, Any] = {
        "name": clean_name,
        "status": "saved",
        "category": category,
        "description": "Saved source schedule for FreqPlanner composition.",
        "source_refs": [category],
        "schedule_refs": [dict(row) for row in rows],
        "frequency_refs": [],
        "group_refs": sorted(
            {
                str(row.get("group_name") or row.get("group") or "").strip().upper()
                for row in rows
                if str(row.get("group_name") or row.get("group") or "").strip()
            }
        ),
        "notes": f"Source schedule saved {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
    }
    if existing_plan_id is not None and int(existing_plan_id) > 0:
        payload["id"] = int(existing_plan_id)
    saved = MultiRadioStore().save_frequency_plan(payload)
    saved_id = int(saved.get("id", 0) or 0)
    if saved_id > 0:
        settings.set(selected_key, f"plan:{saved_id}")
        if hasattr(settings, "save"):
            settings.save()
    return {
        "id": f"plan:{saved_id}" if saved_id > 0 else "",
        "db_id": saved_id,
        "name": str(saved.get("name") or clean_name),
        "rows": [dict(row) for row in rows],
        "storage": "db",
    }


def rename_source_schedule(
    settings: Any,
    category: str,
    selected_key: str,
    set_id: str,
    name: str,
) -> Dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Enter a clear schedule name before renaming.")
    target = str(set_id or "").strip()
    if not target or target == LIVE_SOURCE_SET_ID:
        raise ValueError("Select a saved schedule before renaming.")
    expected_key = HF_DAILY_SOURCE_SETS_KEY if category == HF_DAILY_SOURCE_CATEGORY else HF_NET_SOURCE_SETS_KEY
    if target.startswith("plan:"):
        plan_id = int(target.split(":", 1)[1] or 0)
        if plan_id <= 0:
            raise ValueError("Select a saved schedule before renaming.")
        store = MultiRadioStore()
        plan = store.get_frequency_plan(plan_id)
        if not plan or str(plan.get("category") or "").strip().lower() != str(category or "").strip().lower():
            raise ValueError("Only the matching HF Daily or HF Net schedule can be renamed here.")
        payload = dict(plan)
        payload["id"] = plan_id
        payload["name"] = clean_name
        saved = store.save_frequency_plan(payload)
        settings.set(selected_key, f"plan:{plan_id}")
        if hasattr(settings, "save"):
            settings.save()
        return {
            "id": f"plan:{plan_id}",
            "db_id": plan_id,
            "name": str(saved.get("name") or clean_name),
            "rows": _parse_refs(saved.get("schedule_refs_json", saved.get("schedule_refs", "[]"))),
            "storage": "db",
        }

    rows = source_sets(settings, expected_key)
    for row in rows:
        if str(row.get("id") or "").strip() == target:
            row["name"] = clean_name
            row["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            settings.set(expected_key, rows)
            settings.set(selected_key, target)
            if hasattr(settings, "save"):
                settings.save()
            return dict(row)
    raise ValueError("Could not find the selected saved schedule.")


def delete_source_schedule(settings: Any, sets_key: str, selected_key: str, set_id: str) -> bool:
    target = str(set_id or "").strip()
    if not target or target == LIVE_SOURCE_SET_ID:
        return False
    expected_category = HF_DAILY_SOURCE_CATEGORY if sets_key == HF_DAILY_SOURCE_SETS_KEY else HF_NET_SOURCE_CATEGORY
    if target.startswith("plan:"):
        plan_id = int(target.split(":", 1)[1] or 0)
        if plan_id > 0:
            store = MultiRadioStore()
            plan = store.get_frequency_plan(plan_id)
            if not plan or str(plan.get("category") or "").strip().lower() != expected_category:
                raise ValueError("Only the matching HF Daily or HF Net source schedule can be deleted here.")
            store.delete_frequency_plan(plan_id)
            settings.set(selected_key, LIVE_SOURCE_SET_ID)
            if hasattr(settings, "save"):
                settings.save()
            return True
    # Dependency check and source removal share the settings DB write lock.
    # Read the latest persisted list rather than overwriting concurrent edits
    # from this SettingsManager's potentially older in-memory source list.
    MultiRadioStore(getattr(settings, "db_path", None)).delete_legacy_schedule_source(expected_category, target)
    settings.reload()
    return True
