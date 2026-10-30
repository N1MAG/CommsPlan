from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from freqinout.core.group_utils import normalize_group_name
from freqinout.core.logger import log
from freqinout.core.sqlite_utils import connect_sqlite_readonly


@dataclass(frozen=True)
class OperatorGroupFamily:
    parent: str
    members: tuple[str, ...]
    trusted_callsigns: tuple[str, ...]
    total_callsigns: int
    trusted_callsigns_by_group: tuple[tuple[str, tuple[str, ...]], ...] = ()
    trusted_operator_details_by_group: tuple[tuple[str, tuple[tuple[str, str, str, str], ...]], ...] = ()
    trusted_role_counts_by_group: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = ()
    trusted_tier_counts_by_group: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = ()


def load_operator_group_families(db_path: str | Path | None) -> dict[str, OperatorGroupFamily]:
    """Load roster-derived parent/child group families from HF Operator history."""
    if not db_path:
        return {}
    path = Path(db_path)
    if not path.exists():
        return {}
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = connect_sqlite_readonly(path, timeout=1.0)
        # Startup/migration owns repairs. A traffic-context read must never
        # rebuild an index or invalidate a concurrent scheduler snapshot.
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(operator_checkins)")}
        if "callsign" not in columns:
            return {}
        names = ("callsign", "groups_json", "group1", "group2", "group3", "trusted",
                 "roster_parent_group", "roster_region", "group_role", "tier", "state")
        fields = []
        for name in names:
            default = "0" if name == "trusted" else "''"
            fields.append(f"COALESCE({name}, {default})" if name in columns else default)
        cur = conn.cursor()
        cur.execute("SELECT " + ", ".join(fields) + " FROM operator_checkins")
        return build_operator_group_families(
            {
                "callsign": callsign,
                "groups_json": groups_json,
                "group1": group1,
                "group2": group2,
                "group3": group3,
                "trusted": trusted,
                "roster_parent_group": roster_parent_group,
                "roster_region": roster_region,
                "group_role": group_role,
                "tier": tier,
                "state": state,
            }
            for callsign, groups_json, group1, group2, group3, trusted, roster_parent_group, roster_region, group_role, tier, state
            in cur.fetchall()
        )
    except Exception as exc:
        log.debug("operator_groups: failed to load group families: %s", exc)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def build_operator_group_families(rows: Iterable[Mapping[str, object]]) -> dict[str, OperatorGroupFamily]:
    family_members: dict[str, set[str]] = {}
    family_callsigns: dict[str, set[str]] = {}
    family_trusted_callsigns: dict[str, set[str]] = {}
    family_group_trusted_callsigns: dict[str, dict[str, set[str]]] = {}
    family_group_operator_details: dict[str, dict[str, dict[str, tuple[str, str, str, str]]]] = {}
    family_group_role_counts: dict[str, dict[str, dict[str, int]]] = {}
    family_group_tier_counts: dict[str, dict[str, dict[str, int]]] = {}

    for row in rows:
        callsign = str(row.get("callsign") or "").strip().upper()
        trusted = bool(row.get("trusted"))
        role = _clean_summary_value(row.get("group_role"))
        tier = _clean_summary_value(row.get("tier"))
        state = _clean_summary_value(row.get("state"))
        groups = _row_groups(row)
        parent = normalize_group_name(row.get("roster_parent_group"))
        region = normalize_group_name(row.get("roster_region"))
        if region:
            groups.add(region)
        if not parent:
            parent = _infer_parent_from_groups(groups)
        if not parent:
            continue
        members = family_members.setdefault(parent, set())
        members.add(parent)
        members.update(groups)
        if callsign:
            family_callsigns.setdefault(parent, set()).add(callsign)
            if trusted:
                family_trusted_callsigns.setdefault(parent, set()).add(callsign)
                by_group = family_group_trusted_callsigns.setdefault(parent, {})
                details_by_group = family_group_operator_details.setdefault(parent, {})
                for group in groups | {parent}:
                    by_group.setdefault(group, set()).add(callsign)
                    details_by_group.setdefault(group, {})[callsign] = (callsign, role, tier, state)
                    if role:
                        _inc_nested_count(family_group_role_counts, parent, group, role)
                    if tier:
                        _inc_nested_count(family_group_tier_counts, parent, group, tier)

    return {
        parent: OperatorGroupFamily(
            parent=parent,
            members=tuple(sorted(members)),
            trusted_callsigns=tuple(sorted(family_trusted_callsigns.get(parent, set()))),
            total_callsigns=len(family_callsigns.get(parent, set())),
            trusted_callsigns_by_group=tuple(
                (group, tuple(sorted(callsigns)))
                for group, callsigns in sorted(family_group_trusted_callsigns.get(parent, {}).items())
            ),
            trusted_operator_details_by_group=tuple(
                (group, tuple(detail_map[callsign] for callsign in sorted(detail_map)))
                for group, detail_map in sorted(family_group_operator_details.get(parent, {}).items())
            ),
            trusted_role_counts_by_group=_freeze_nested_counts(family_group_role_counts.get(parent, {})),
            trusted_tier_counts_by_group=_freeze_nested_counts(family_group_tier_counts.get(parent, {})),
        )
        for parent, members in sorted(family_members.items())
    }


def expand_group_selection(
    groups: Iterable[object],
    families: Mapping[str, OperatorGroupFamily | Iterable[object]],
) -> set[str]:
    expanded = {normalize_group_name(group) for group in groups if normalize_group_name(group)}
    for group in list(expanded):
        family = families.get(group)
        if family is not None:
            expanded.update(_family_members(family))
    return expanded


def trusted_callsigns_for_groups(
    groups: Iterable[object],
    families: Mapping[str, OperatorGroupFamily | Iterable[object]],
) -> tuple[str, ...]:
    selected = {normalize_group_name(group) for group in groups if normalize_group_name(group)}
    expanded = expand_group_selection(selected, families)
    callsigns: set[str] = set()
    for group in expanded:
        family = families.get(group)
        if isinstance(family, OperatorGroupFamily):
            callsigns.update(family.trusted_callsigns)
    for family in families.values():
        if not isinstance(family, OperatorGroupFamily):
            continue
        by_group = {group: set(values) for group, values in family.trusted_callsigns_by_group}
        for group in selected:
            if group == family.parent:
                continue
            callsigns.update(by_group.get(group, set()))
    return tuple(sorted(callsigns))


def trusted_operator_details_for_groups(
    groups: Iterable[object],
    families: Mapping[str, OperatorGroupFamily | Iterable[object]],
) -> tuple[tuple[str, str, str, str], ...]:
    selected = {normalize_group_name(group) for group in groups if normalize_group_name(group)}
    details: dict[str, tuple[str, str, str, str]] = {}
    for family in families.values():
        if not isinstance(family, OperatorGroupFamily):
            continue
        by_group = {group: tuple(values) for group, values in family.trusted_operator_details_by_group}
        for group in selected:
            if group == family.parent:
                rows = by_group.get(family.parent, ())
            else:
                rows = by_group.get(group, ())
            for row in rows:
                if row and row[0]:
                    details[row[0]] = row
    return tuple(details[callsign] for callsign in sorted(details))


def group_family_label(group: object, families: Mapping[str, OperatorGroupFamily]) -> str:
    name = normalize_group_name(group)
    family = families.get(name)
    if family is None:
        return name
    child_count = max(0, len(set(family.members) - {family.parent}))
    op_count = int(family.total_callsigns)
    parts = [family.parent, "family"]
    if child_count:
        parts.append(f"{child_count} subgroups")
    if op_count:
        parts.append(f"{op_count} operators")
    return " - ".join(parts)


def group_access_summary(
    groups: Iterable[object],
    families: Mapping[str, OperatorGroupFamily | Iterable[object]],
    *,
    max_items: int = 3,
) -> str:
    selected = {normalize_group_name(group) for group in groups if normalize_group_name(group)}
    role_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    for family in families.values():
        if not isinstance(family, OperatorGroupFamily):
            continue
        role_by_group = {group: dict(counts) for group, counts in family.trusted_role_counts_by_group}
        tier_by_group = {group: dict(counts) for group, counts in family.trusted_tier_counts_by_group}
        for group in selected:
            if group == family.parent:
                group_roles = role_by_group.get(family.parent, {})
                group_tiers = tier_by_group.get(family.parent, {})
            else:
                group_roles = role_by_group.get(group, {})
                group_tiers = tier_by_group.get(group, {})
            for key, count in group_roles.items():
                role_counts[key] = role_counts.get(key, 0) + int(count)
            for key, count in group_tiers.items():
                tier_counts[key] = tier_counts.get(key, 0) + int(count)
    parts: list[str] = []
    if role_counts:
        parts.append("Roles: " + _format_counts(role_counts, max_items=max_items))
    if tier_counts:
        parts.append("Tiers: " + _format_counts(tier_counts, max_items=max_items))
    return "; ".join(parts)


def _row_groups(row: Mapping[str, object]) -> set[str]:
    groups = {
        normalize_group_name(row.get("group1")),
        normalize_group_name(row.get("group2")),
        normalize_group_name(row.get("group3")),
    }
    raw = row.get("groups_json")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = []
        if isinstance(parsed, list):
            groups.update(normalize_group_name(value) for value in parsed)
    elif isinstance(raw, list):
        groups.update(normalize_group_name(value) for value in raw)
    return {group for group in groups if group}


def _infer_parent_from_groups(groups: set[str]) -> str:
    if len(groups) < 2:
        return ""
    region_like = {group for group in groups if _looks_like_region_group(group)}
    if not region_like:
        return ""
    non_region = sorted(group for group in groups if group not in region_like)
    return non_region[0] if non_region else ""


def _looks_like_region_group(group: str) -> bool:
    return group.startswith("MR") and any(ch.isdigit() for ch in group[2:])


def _family_members(family: OperatorGroupFamily | Iterable[object]) -> set[str]:
    if isinstance(family, OperatorGroupFamily):
        values = family.members
    else:
        values = family
    return {normalize_group_name(value) for value in values if normalize_group_name(value)}


def _clean_summary_value(value: object) -> str:
    return str(value or "").strip().upper()


def _inc_nested_count(store: dict[str, dict[str, dict[str, int]]], parent: str, group: str, value: str) -> None:
    parent_counts = store.setdefault(parent, {})
    group_counts = parent_counts.setdefault(group, {})
    group_counts[value] = group_counts.get(value, 0) + 1


def _freeze_nested_counts(group_counts: dict[str, dict[str, int]]) -> tuple[tuple[str, tuple[tuple[str, int], ...]], ...]:
    return tuple(
        (group, tuple(sorted(counts.items(), key=lambda item: _count_sort_key(item[0]))))
        for group, counts in sorted(group_counts.items())
    )


def _format_counts(counts: Mapping[str, int], *, max_items: int) -> str:
    ordered = sorted(counts.items(), key=lambda item: (-int(item[1]), _count_sort_key(item[0])))
    shown = ordered[:max_items]
    text = ", ".join(f"{key} {count}" for key, count in shown)
    remaining = sum(int(count) for _key, count in ordered[max_items:])
    if remaining:
        text += f", +{remaining} more"
    return text


def _count_sort_key(value: str) -> tuple[int, object]:
    text = str(value or "")
    if text.isdigit():
        return (0, int(text))
    return (1, text)


def load_hf_roster_group_names(db_path: str | Path) -> tuple[str, ...]:
    """Read explicit HF roster memberships; no operating-group inference or writes."""
    conn = connect_sqlite_readonly(db_path, timeout=1.0)
    try:
        columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(operator_checkins)')}
        names = [key for key in ('group1', 'group2', 'group3', 'groups_json') if key in columns]
        if not names:
            return ()
        cursor = conn.execute('SELECT ' + ','.join(names) + ' FROM operator_checkins')
        groups = set()
        while rows := cursor.fetchmany(200):
            for row in rows:
                groups.update(_row_groups(dict(zip(names, row))))
            if len(groups) > 1000:
                raise ValueError('Roster group list exceeds 1000 choices')
        return tuple(sorted(groups))
    finally:
        conn.close()
