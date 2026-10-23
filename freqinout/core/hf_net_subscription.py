"""Canonical Net Directory to commandable HF schedule subscription boundary.

The module is Qt-free.  It creates reviewed schedule snapshots but never starts
the scheduler, tunes a radio, or silently applies later directory changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from freqinout.core.resource_catalog_models import CatalogValidationError
from freqinout.core.resource_catalog_store import ResourceCatalogStore
from freqinout.core.schedule_source_sets import (
    HF_NET_SOURCE_CATEGORY,
    HF_NET_SOURCE_SETS_KEY,
    SELECTED_HF_NET_SOURCE_SET_KEY,
    save_source_schedule,
    source_sets_for_category,
)


@dataclass(frozen=True, slots=True)
class HfNetSubscriptionDraft:
    net_session_key: str
    net_name: str
    schedule_row: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "schedule_row", MappingProxyType(dict(self.schedule_row)))


@dataclass(frozen=True, slots=True)
class HfNetSubscriptionUpdate:
    net_session_key: str
    state: str
    warning: str
    diffs: tuple[object, ...] = ()


def _mhz(resource: object | None) -> str:
    if resource is None:
        return ""
    hz = getattr(resource, "receive_hz", None) or getattr(resource, "center_hz", None)
    if hz is None:
        return ""
    return f"{int(hz) / 1_000_000:.6f}".rstrip("0").rstrip(".")


def _end_time(start: str, duration_minutes: int | None) -> str:
    try:
        hour, minute = (int(part) for part in str(start).split(":", 1))
        total = (hour * 60 + minute + int(duration_minutes or 0)) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"
    except (TypeError, ValueError):
        return ""


def _snapshot(session: object, frequency: object | None) -> dict[str, Any]:
    result = {
        "net_session_key": getattr(session, "net_session_key", ""),
        "frequency_resource_key": getattr(session, "frequency_resource_key", None),
        "day_utc": getattr(session, "day_utc", None),
        "recurrence": getattr(session, "recurrence", None),
        "local_start_time": getattr(session, "local_start_time", None),
        "duration_minutes": getattr(session, "duration_minutes", None),
        "timezone": getattr(session, "timezone", None),
        "mode": getattr(session, "mode", None),
        "frequency": _mhz(frequency),
    }
    if frequency is not None:
        result["frequency_snapshot"] = {
            name: getattr(frequency, name, None)
            for name in (
                "frequency_resource_key", "source_key", "resource_kind", "service",
                "band", "channel", "label", "receive_hz", "transmit_hz",
                "center_hz", "offset_hz", "mode", "tone", "locality", "coverage",
            )
        }
    return result


def build_hf_subscription_drafts(
    store: ResourceCatalogStore,
    session_keys: Sequence[str],
    *,
    overrides_by_session: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[HfNetSubscriptionDraft, ...]:
    """Build distinct review drafts from published sessions.

    Non-UTC published wall-clock times are deliberately flagged for review;
    FIO's current commandable HF schedule is UTC and must not silently reinterpret
    a civil-time publication across DST.
    """
    overrides = overrides_by_session or {}
    result: list[HfNetSubscriptionDraft] = []
    seen: set[str] = set()
    for raw_key in session_keys:
        key = str(raw_key or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        session = store.get_session(key)
        if session is None:
            raise CatalogValidationError(f"published session not found: {key}")
        entry = store.get_net_entry(session.net_entry_key)
        frequency = store.get_frequency(session.frequency_resource_key) if session.frequency_resource_key else None
        group_links = store.net_entry_group_links(session.net_entry_key)
        group_name = next((name for _group_key, name in group_links if name), "")
        start = str(session.local_start_time or "")
        row: dict[str, Any] = {
            "day_utc": str(session.day_utc or ""),
            "recurrence": str(session.recurrence or "Weekly"),
            "biweekly_offset_weeks": 0,
            "month_weeks": "",
            "group_name": group_name,
            "band": str(getattr(frequency, "band", None) or ""),
            "mode": str(session.mode or getattr(frequency, "mode", None) or ""),
            "frequency": _mhz(frequency),
            "start_utc": start,
            "end_utc": _end_time(start, session.duration_minutes),
            "early_checkin": int(session.reminder_minutes or 0),
            "primary_js8call_group": group_name,
            "comment": str(session.mode_details or ""),
            "net_name": str(getattr(entry, "name", None) or key),
            "fldigi_mode": "",
            "fldigi_offset": "",
            "fldigi_tuning_json": session.fldigi_tuning_json,
            "net_session_key": session.net_session_key,
            "frequency_resource_key": session.frequency_resource_key,
            "accepted_session_version_hash": session.version_hash,
            "accepted_resource_version_hash": getattr(frequency, "version_hash", None),
            "accepted_snapshot_json": json.dumps(_snapshot(session, frequency), sort_keys=True),
        }
        row.update(dict(overrides.get(key, {})))
        warnings: list[str] = []
        if session.retired or not session.active:
            warnings.append("Published session is retired; the saved schedule will retain its reviewed snapshot.")
        if str(session.timezone or "UTC").upper() != "UTC":
            warnings.append("Published time is not UTC; review the UTC day and time before saving.")
        for field, label in (("day_utc", "UTC day"), ("start_utc", "UTC start"), ("end_utc", "UTC end"), ("frequency", "frequency")):
            if not str(row.get(field) or "").strip():
                warnings.append(f"{label} is required before saving.")
        result.append(HfNetSubscriptionDraft(key, row["net_name"], row, tuple(warnings)))
    return tuple(result)


def hf_net_destination_choices(settings: Any) -> tuple[tuple[int | None, str], ...]:
    choices: list[tuple[int | None, str]] = []
    for item in source_sets_for_category(settings, HF_NET_SOURCE_SETS_KEY, HF_NET_SOURCE_CATEGORY):
        raw_id = str(item.get("id") or "")
        plan_id = int(item.get("db_id") or raw_id.removeprefix("plan:") or 0) if raw_id.startswith("plan:") or item.get("db_id") else 0
        choices.append((plan_id or None, str(item.get("name") or "Unnamed HF Net schedule")))
    return tuple(choices)


def hf_net_destination_for_session(settings: Any, net_session_key: str) -> tuple[str, str] | None:
    """Find the first named HF source containing the canonical session."""
    key = str(net_session_key or "").strip()
    if not key:
        return None
    for item in source_sets_for_category(settings, HF_NET_SOURCE_SETS_KEY, HF_NET_SOURCE_CATEGORY):
        for row in item.get("rows", ()):
            if isinstance(row, Mapping) and str(row.get("net_session_key") or "").strip() == key:
                return str(item.get("id") or ""), str(item.get("name") or "HF Net schedule")
    return None


def save_hf_subscriptions(
    settings: Any,
    store: ResourceCatalogStore,
    session_keys: Sequence[str],
    *,
    destination_name: str,
    existing_plan_id: int | None = None,
    overrides_by_session: Mapping[str, Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    name = str(destination_name or "").strip()
    if not name:
        raise ValueError("Choose or enter a named HF Net schedule.")
    drafts = build_hf_subscription_drafts(store, session_keys, overrides_by_session=overrides_by_session)
    blocking = [warning for draft in drafts for warning in draft.warnings if "required before saving" in warning]
    if blocking:
        raise ValueError(" ".join(blocking))
    existing_rows: list[dict[str, Any]] = []
    if existing_plan_id:
        for item in source_sets_for_category(settings, HF_NET_SOURCE_SETS_KEY, HF_NET_SOURCE_CATEGORY):
            if int(item.get("db_id") or 0) == int(existing_plan_id):
                existing_rows = [dict(row) for row in item.get("rows", ()) if isinstance(row, Mapping)]
                break
    by_session = {
        str(row.get("net_session_key") or ""): row
        for row in existing_rows
        if str(row.get("net_session_key") or "").strip()
    }
    unlinked = [row for row in existing_rows if not str(row.get("net_session_key") or "").strip()]
    for draft in drafts:
        by_session[draft.net_session_key] = dict(draft.schedule_row)
    rows = unlinked + list(by_session.values())
    return MappingProxyType(save_source_schedule(
        settings,
        HF_NET_SOURCE_CATEGORY,
        SELECTED_HF_NET_SOURCE_SET_KEY,
        name,
        rows,
        existing_plan_id=existing_plan_id,
    ))


def subscription_update_status(store: ResourceCatalogStore, schedule_row: Mapping[str, Any]) -> HfNetSubscriptionUpdate:
    key = str(schedule_row.get("net_session_key") or "").strip()
    if not key:
        return HfNetSubscriptionUpdate("", "unlinked", "This schedule row is not linked to a directory session.")
    session = store.get_session(key)
    if session is None:
        return HfNetSubscriptionUpdate(key, "missing", "The published session is unavailable; the accepted schedule snapshot is retained.")
    if session.retired or not session.active:
        return HfNetSubscriptionUpdate(key, "retired", "The published session is retired; the accepted schedule snapshot is retained.")
    raw_snapshot = schedule_row.get("accepted_snapshot_json")
    try:
        snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else dict(raw_snapshot or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        snapshot = {}
    session_snapshot = {
        name: value
        for name, value in snapshot.items()
        if name not in {"frequency", "frequency_snapshot"}
    }
    comparison = store.compare_session_version(
        key,
        schedule_row.get("accepted_session_version_hash"),
        session_snapshot,
    )
    update_available = comparison.update_available
    diffs = list(comparison.diffs)
    frequency_key = str(session.frequency_resource_key or "").strip()
    accepted_frequency_hash = schedule_row.get("accepted_resource_version_hash")
    if frequency_key and accepted_frequency_hash:
        frequency = store.get_frequency(frequency_key)
        if frequency is None:
            return HfNetSubscriptionUpdate(
                key,
                "missing",
                "The subscribed frequency resource is unavailable; the accepted schedule snapshot is retained.",
                tuple(diffs),
            )
        frequency_snapshot = snapshot.get("frequency_snapshot")
        frequency_comparison = store.compare_frequency_version(
            frequency_key,
            accepted_frequency_hash,
            frequency_snapshot if isinstance(frequency_snapshot, Mapping) else None,
        )
        update_available = update_available or frequency_comparison.update_available
        diffs.extend(frequency_comparison.diffs)
    return HfNetSubscriptionUpdate(
        key,
        "update_available" if update_available else "current",
        "Review directory changes before applying." if update_available else "Directory session is current.",
        tuple(diffs),
    )


__all__ = [
    "HfNetSubscriptionDraft",
    "HfNetSubscriptionUpdate",
    "build_hf_subscription_drafts",
    "hf_net_destination_choices",
    "hf_net_destination_for_session",
    "save_hf_subscriptions",
    "subscription_update_status",
]
