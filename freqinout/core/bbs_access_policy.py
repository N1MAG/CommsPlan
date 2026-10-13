"""Explicit access intent and reviewed roster snapshots; no native side effects."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from collections.abc import Mapping

from freqinout.core.operator_groups import build_operator_group_families, _row_groups, _infer_parent_from_groups
from freqinout.core.varac_bbs_sources import normalize_group_source_selections


def _calls(value):
    if isinstance(value, str):
        value = re.split(r"[,;\s]+", value)
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({str(v).strip().upper() for v in value if re.fullmatch(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)*", str(v).strip())})


def normalize_access_policy(value, legacy_callsigns=()):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return dict(version=1, manual_callsigns=_calls(legacy_callsigns), group_sources=[], resolved_group_callsigns=[])
    return dict(version=1, manual_callsigns=_calls(value.get("manual_callsigns")),
                group_sources=normalize_group_source_selections(value.get("group_sources")),
                resolved_group_callsigns=_calls(value.get("resolved_group_callsigns")))


def effective_callsigns(policy):
    p = normalize_access_policy(policy)
    return sorted(set(p["manual_callsigns"]) | set(p["resolved_group_callsigns"]))


def refresh_access_policy(policy, families):
    p = normalize_access_policy(policy)
    resolved = set()
    for source in p["group_sources"]:
        family = families.get(source["family"])
        if family is None:
            continue
        by_group = dict(family.trusted_callsigns_by_group)
        for group in source["groups"]:
            # Resolve inside the selected family, not across same-named subgroups.
            resolved.update(family.trusted_callsigns if group == family.parent else by_group.get(group, ()))
    p["resolved_group_callsigns"] = _calls(list(resolved))
    return p


def group_refresh_preview(old, new):
    before, after = set(effective_callsigns(old)), set(effective_callsigns(new))
    return dict(added=sorted(after-before), removed=sorted(before-after), unchanged=sorted(before & after))


def load_access_families(db_path):
    """Read-only bounded snapshot, including older roster schemas; errors propagate."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("HF Operators database is unavailable")
    deadline = time.monotonic() + 5
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1) as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(operator_checkins)")}
        if not {"callsign", "trusted"} <= columns:
            raise ValueError("HF Operators trust data is unavailable")
        wanted = ("callsign", "groups_json", "group1", "group2", "group3", "trusted",
                  "roster_parent_group", "roster_region", "group_role", "tier", "state")
        projection = ",".join(f'"{c}"' if c in columns else f"'' AS {c}" for c in wanted)
        rows = conn.execute(f"SELECT {projection} FROM operator_checkins LIMIT 50001").fetchall()
        if len(rows) > 50000:
            raise ValueError("HF Operators roster exceeds the safe review limit; narrow the roster first")
    records = [dict(zip(wanted, row)) for row in rows]
    for row in records:
        try:
            row["trusted"] = int(row["trusted"] or 0) != 0
        except (TypeError, ValueError):
            row["trusted"] = False
    baseline = build_operator_group_families(records)
    supplemental = []
    for row in records:
        groups = _row_groups(row)
        if row["roster_parent_group"] or _infer_parent_from_groups(groups):
            continue
        # Ordinary HF history groups need not carry imported roster hierarchy.
        # Join a uniquely known family, otherwise expose the group independently.
        for group in groups:
            owners = [name for name, family in baseline.items() if group in family.members]
            supplemental.append(dict(row, roster_parent_group=owners[0] if len(owners) == 1 else group,
                                     groups_json=json.dumps([group]), group1="", group2="", group3=""))
    return build_operator_group_families(records + supplemental)
