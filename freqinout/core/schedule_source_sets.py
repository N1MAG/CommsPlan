from __future__ import annotations

import datetime
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional

from freqinout.core.multi_radio_store import MultiRadioStore, schedule_name_key, _assert_schedule_name_available
from freqinout.core.schedule_projection import build_blended_schedule_projection
from freqinout.core.schedule_source_inputs import (
    ScheduleSourceError, load_sop_projection_sources, normalize_active_daily_rows,
    normalize_active_net_rows, read_plan_schedule_table,
)
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
    # Bare legacy layer labels record snapshot provenance, never live usage.
    dependency_refs = {f'{clean_category}:{clean_set_id}'} if clean_category in _SOURCE_DEPENDENCY_PREFIXES else set()
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


def plan_source_link_description(plan: Dict[str, Any]) -> str:
    """Pure presentation of actual dependencies; never infer a link by name."""
    refs = _parse_ref_values(plan.get('source_refs_json', plan.get('source_refs', [])))
    daily = any(ref.startswith(HF_DAILY_SOURCE_CATEGORY + ':') for ref in refs)
    nets = any(ref.startswith(HF_NET_SOURCE_CATEGORY + ':') and not ref.endswith(':' + NO_NET_SOURCE_SET_ID) for ref in refs)
    sop = any(ref.startswith(('sop_schedule_layer:', 'sop_schedule_plan:')) for ref in refs)
    sop_text = ' SOP sources remain linked.' if sop else ''
    if daily or nets:
        linked = ' and '.join(label for label, active in [('Daily', daily), ('Net', nets)] if active)
        return f'Linked {linked} schedules: this plan follows the selected sources when available and safety checks pass.' + sop_text
    return ('Saved Daily/Net schedule snapshot: source edits do not change its saved windows. '
            'The table previews the source choices below. Link to source schedules… to compare the saved windows before changing this plan.') + sop_text


def _dependency_set_id(source_refs: List[str], category: str) -> str:
    prefix = f"{category}:"
    for ref in source_refs:
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ""


def _rows_for_dependency(settings: Any, category: str, set_id: str, *, store: Any = None) -> List[Dict[str, Any]]:
    if category == HF_NET_SOURCE_CATEGORY and str(set_id or "").strip() == NO_NET_SOURCE_SET_ID:
        return []
    daily = category == HF_DAILY_SOURCE_CATEGORY
    if set_id == LIVE_SOURCE_SET_ID:
        db_name = "freqinout.db" if daily else "freqinout_nets.db"
        tables = ("daily_schedule_tab",) if daily else ("net_schedule_tab", "net_schedule")
        data = read_plan_schedule_table(settings.config_dir / db_name, tables)
        if data is not None:
            normalize = normalize_active_daily_rows if daily else normalize_active_net_rows
            return normalize(*data)
        fallback = (settings.get("hf_schedule") or settings.get("daily_schedule")) if daily else settings.get("net_schedule")
        return [dict(row) for row in fallback or () if isinstance(row, dict)]
    sets_key = HF_DAILY_SOURCE_SETS_KEY if daily else HF_NET_SOURCE_SETS_KEY
    if str(set_id).startswith("plan:"):
        try:
            plan = (store if store is not None else MultiRadioStore(getattr(settings, 'db_path', None))).get_frequency_plan(int(set_id.split(":", 1)[1]))
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
    *, settings: Any = None, store: Any = None,
) -> List[Dict[str, Any]]:
    try:
        pid = int(profile_id or 0)
    except Exception:
        pid = 0
    dependency_ref = sop_schedule_dependency_ref(pid)
    if not dependency_ref:
        return []
    if settings is None:
        from contextlib import closing
        with closing(SettingsManager()) as owned_settings:
            return assigned_plan_rf_guard_impacts_for_sop_update(pid, updated_rows, settings=owned_settings)
    if store is None:
        store = MultiRadioStore(getattr(settings, "db_path", None))
        with store.configuration_read_transaction():
            return assigned_plan_rf_guard_impacts_for_sop_update(pid, updated_rows, settings=settings, store=store)
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
    *, store: Any = None,
) -> List[Dict[str, Any]]:
    dependency_ref = (
        f"{category}:{LIVE_SOURCE_SET_ID}"
        if set_id == LIVE_SOURCE_SET_ID and category in _SOURCE_DEPENDENCY_PREFIXES
        else source_schedule_dependency_ref(category, set_id)
    )
    if not dependency_ref:
        return []
    if store is None:
        store = MultiRadioStore(getattr(settings, "db_path", None))
        with store.configuration_read_transaction():
            return assigned_plan_rf_guard_impacts_for_source_update(settings, category, set_id, updated_rows, store=store)
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
        daily_rows = _rows_for_dependency(settings, HF_DAILY_SOURCE_CATEGORY, daily_set_id, store=store) if daily_set_id else []
        net_rows = _rows_for_dependency(settings, HF_NET_SOURCE_CATEGORY, net_set_id, store=store) if net_set_id else []
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
    *, store: Any = None,
) -> List[Dict[str, Any]]:
    """Refresh saved Frequency Plans that depend on a named HF Daily/Net schedule.

    Saved Frequency Plans intentionally keep their own concrete schedule windows so
    scheduler startup does not need to rebuild planner projections. When a named
    Daily or Net component changes, every dependent plan must be reprojected before
    the scheduler reads it again; otherwise radios keep following stale windows.
    """

    dependency_ref = (
        f"{category}:{LIVE_SOURCE_SET_ID}"
        if set_id == LIVE_SOURCE_SET_ID and category in _SOURCE_DEPENDENCY_PREFIXES
        else source_schedule_dependency_ref(category, set_id)
    )
    if not dependency_ref:
        return []
    store = store if store is not None else MultiRadioStore(getattr(settings, 'db_path', None))
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
        daily_rows = _rows_for_dependency(settings, HF_DAILY_SOURCE_CATEGORY, daily_set_id, store=store) if daily_set_id else []
        net_rows = _rows_for_dependency(settings, HF_NET_SOURCE_CATEGORY, net_set_id, store=store) if net_set_id else []
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


def _source_backed_plan_payload(settings: Any, plan: Dict[str, Any], *, store: Any = None) -> Optional[Dict[str, Any]]:
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
    daily_rows = _rows_for_dependency(settings, HF_DAILY_SOURCE_CATEGORY, daily_set_id, store=store) if daily_set_id else []
    net_rows = _rows_for_dependency(settings, HF_NET_SOURCE_CATEGORY, net_set_id, store=store) if net_set_id else []
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
        store = MultiRadioStore(getattr(settings, 'db_path', None))
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
            payload = _source_backed_plan_payload(settings, plan, store=store)
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


def source_sets_for_category(settings: Any, legacy_key: str, category: str, *, strict: bool = False, store: Any = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        store = store if store is not None else MultiRadioStore(getattr(settings, 'db_path', None))
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
    except Exception as exc:
        if strict:
            raise ScheduleSourceError(f"Cannot read schedule source catalog: {exc}") from exc
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
    *, strict: bool = False,
) -> Optional[Dict[str, Any]]:
    target = str(set_id or "").strip()
    if not target or target == LIVE_SOURCE_SET_ID:
        return None
    for row in source_sets_for_category(settings, sets_key, category, strict=strict):
        if str(row.get("id") or "").strip() == target:
            return row
    return None


def describe_duplicate_schedule_choices(rows, category, plans, assignments, devices):
    """Pure labels: actual effective links outrank save time, never choose/delete."""
    result=[dict(row) for row in rows]
    groups={}
    for row in result:
        groups.setdefault(schedule_name_key(row.get('name')),[]).append(row)
    radio_names={int(row['id']):str(row.get('name') or 'Radio') for row in devices}
    reverse={}
    for plan in plans:
        for ref in _parse_ref_values(plan.get('source_refs_json',plan.get('source_refs',[]))):
            reverse.setdefault(ref,set()).add(int(plan['id']))
    def timestamp(value):
        try:
            parsed=datetime.datetime.fromisoformat(str(value).replace('Z','+00:00'))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)).timestamp()
        except (ValueError,TypeError,OverflowError):
            return float('-inf')
    for group in groups.values():
        if len(group)<2:
            continue
        latest=max(timestamp(row.get('updated_utc') or row.get('created_utc')) for row in group)
        for ordinal,row in enumerate(group,1):
            linked={int(row['db_id'])} if row.get('db_id') else set()
            pending=[source_schedule_dependency_ref(category,str(row['id']))]
            seen=set()
            while pending:
                ref=pending.pop()
                if ref in seen:
                    continue
                seen.add(ref)
                for plan_id in reverse.get(ref,()):
                    if plan_id not in linked:
                        linked.add(plan_id)
                        pending.extend((f'hf_daily_schedule:plan:{plan_id}',f'hf_net_schedule:plan:{plan_id}',f'sop_schedule_plan:{plan_id}'))
            uses=[dict(a) for a in assignments if int(a.get('frequency_plan_id') or 0) in linked]
            names=sorted({radio_names.get(int(a['device_profile_id']),'Radio') for a in uses})
            row['linked_radio_names']=names
            row['latest_effective_assignment_utc']=max((str(a.get('created_utc') or '') for a in uses),default='',key=timestamp)
            row['latest_saved']=latest!=float('-inf') and timestamp(row.get('updated_utc') or row.get('created_utc'))==latest
            status='assigned' if uses else 'no linked radio'
            if row['latest_saved']:
                status+=' · latest saved'
            row['choice_label']=f"Copy {ordinal}/{len(group)} · {row.get('name') or 'Schedule'} · {status}"
            row['choice_tooltip']=(f"Saved: {row.get('updated_utc') or row.get('created_utc') or 'Unknown'}\n"
                + ('Linked radios: '+', '.join(names) if names else 'No radio is explicitly linked to this copy. Legacy snapshot plans do not identify a saved copy.')
                + f"\nSource identity: {row['id']}. Updating this copy preserves this identity and its plan links.")
        ranked=[(timestamp(row['latest_effective_assignment_utc']),row) for row in group if timestamp(row['latest_effective_assignment_utc'])!=float('-inf')]
        if ranked:
            newest=max(item[0] for item in ranked)
            winners=[row for time,row in ranked if time==newest]
            if len(winners)==1:
                winners[0]['most_recently_assigned']=True
                winners[0]['choice_label']+=' · most recently assigned'
    return result


def resolve_source_schedule_save_target(settings, category, name, *, selected_id=''):
    """Resolve identities before RF review; ambiguity never chooses a winner."""
    key = HF_DAILY_SOURCE_SETS_KEY if category == HF_DAILY_SOURCE_CATEGORY else HF_NET_SOURCE_SETS_KEY
    store = MultiRadioStore(getattr(settings, "db_path", None))
    with store.configuration_read_transaction():
        catalog = source_sets_for_category(settings, key, category, strict=True, store=store)
    selected = next((row for row in catalog if row['id'] == selected_id), None)
    matches = [row for row in catalog if schedule_name_key(row.get('name')) == schedule_name_key(name)]
    if selected:
        if schedule_name_key(selected.get('name')) == schedule_name_key(name):
            return selected
        if matches:
            raise ValueError('Another schedule already uses that name. Keep this name or choose a different name; no schedules were changed.')
        return selected
    if selected_id and selected_id != LIVE_SOURCE_SET_ID:
        raise ValueError('The selected schedule is unavailable. Reload and select it again before saving.')
    if len(matches) > 1:
        raise ValueError('Multiple saved schedules already use this name. Select the copy whose rows you want to keep, or enter a different name. No schedules were changed.')
    return matches[0] if matches else None


def _save_legacy_source(settings, sets_key, selected_key, name, rows, *, existing_id=None, store=None):
    category = HF_DAILY_SOURCE_CATEGORY if sets_key == HF_DAILY_SOURCE_SETS_KEY else HF_NET_SOURCE_CATEGORY
    reload_settings = store is None
    store = store if store is not None else MultiRadioStore(getattr(settings, 'db_path', None))
    with store.configuration_save_transaction() as transaction:
        with store.connect() as conn:
            saved = conn.execute('SELECT value FROM kv WHERE key=?', (sets_key,)).fetchone()
            sources = json.loads(saved[0]) if saved else []
            if not isinstance(sources, list):
                raise ValueError('Saved schedules could not be verified. Reload before saving.')
            existing = next((row for row in sources if isinstance(row, dict) and row.get('id') == existing_id), None)
            if existing_id and not existing:
                raise ValueError('The selected schedule no longer exists. Reload before saving.')
            if not existing_id:
                matches = [row for row in sources if isinstance(row,dict) and schedule_name_key(row.get('name')) == schedule_name_key(name)]
                if len(matches)>1:
                    raise ValueError('Multiple schedules already use this name. Select one explicitly before updating.')
                existing = matches[0] if matches else None
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            set_id = str(existing['id']) if existing else slug_source_set_id(name)
            taken = {str(row.get('id')) for row in sources if isinstance(row,dict)}
            if not existing:
                suffix=2
                while set_id in taken:
                    set_id=slug_source_set_id(name)+'-'+str(suffix);suffix+=1
            _assert_schedule_name_available(conn,name,category,legacy_id=set_id,existing=existing)
            result = {**(existing or {}),'id':set_id,'name':str(name).strip(),'rows':[dict(row) for row in (rows if rows is not None else (existing or {}).get('rows',[]))],
                      'created_utc':(existing or {}).get('created_utc',now),'updated_utc':now}
            if existing:
                sources[sources.index(existing)] = result
            else:
                sources.append(result)
            conn.executemany('INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)',
                [(sets_key,json.dumps(sources)),(selected_key,json.dumps(set_id))])
        transaction.complete()
    if reload_settings:
        settings.reload()
    return result


def save_source_set(settings: Any, sets_key: str, selected_key: str, name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    clean_name=str(name or '').strip()
    if not clean_name:
        raise ValueError('Enter a clear schedule source name before saving.')
    return _save_legacy_source(settings,sets_key,selected_key,clean_name,rows)


def save_source_schedule(
    settings: Any,
    category: str,
    selected_key: str,
    name: str,
    rows: List[Dict[str, Any]],
    *,
    existing_plan_id: Optional[int] = None,
    existing_source_id: Optional[str] = None,
    store: Any = None,
) -> Dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Enter a clear schedule name before saving.")
    if existing_source_id and not str(existing_source_id).startswith('plan:'):
        key=HF_DAILY_SOURCE_SETS_KEY if category==HF_DAILY_SOURCE_CATEGORY else HF_NET_SOURCE_SETS_KEY
        return _save_legacy_source(settings,key,selected_key,clean_name,rows,existing_id=existing_source_id, store=store)
    atomic = store is not None
    store = store if atomic else MultiRadioStore(getattr(settings, 'db_path', None))
    if existing_plan_id:
        current=store.get_frequency_plan(int(existing_plan_id))
        if not current or current.get('category') != category:
            raise ValueError('Select the matching saved source schedule before updating.')
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
    saved = store.save_frequency_plan(payload)
    saved_id = int(saved.get("id", 0) or 0)
    if saved_id > 0:
        selection = f"plan:{saved_id}"
        if atomic:
            with store.connect() as conn:
                conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
                             (selected_key, json.dumps(selection)))
        else:
            settings.set(selected_key, selection)
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
        store = MultiRadioStore(getattr(settings, 'db_path', None))
        plan = store.get_frequency_plan(plan_id)
        if not plan or str(plan.get("category") or "").strip().lower() != str(category or "").strip().lower():
            raise ValueError("Only the matching HF Daily or HF Net schedule can be renamed here.")
        saved = store.save_frequency_plan({'id':plan_id,'name':clean_name},expected=plan)
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

    current=source_set_row_by_id(settings,expected_key,target)
    if current is None:
        raise ValueError('Could not find the selected saved schedule.')
    return _save_legacy_source(settings,expected_key,selected_key,clean_name,None,existing_id=target)


def delete_source_schedule(settings: Any, sets_key: str, selected_key: str, set_id: str) -> bool:
    target = str(set_id or "").strip()
    if not target or target == LIVE_SOURCE_SET_ID:
        return False
    expected_category = HF_DAILY_SOURCE_CATEGORY if sets_key == HF_DAILY_SOURCE_SETS_KEY else HF_NET_SOURCE_CATEGORY
    if target.startswith("plan:"):
        plan_id = int(target.split(":", 1)[1] or 0)
        if plan_id > 0:
            store = MultiRadioStore(getattr(settings, 'db_path', None))
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
