"""Read canonical schedule inputs without schema assurance or derived-row fallback."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from freqinout.core.config_paths import get_config_dir
from freqinout.core.sqlite_utils import connect_sqlite_readonly
from freqinout.core.operating_group_frequency import reattach_sop_frequency_key


class ScheduleSourceError(RuntimeError):
    """A required schedule input could not be read completely."""


def load_sop_projection_sources(settings: Any, profile_ids: set[int]) -> tuple[list[dict], list[dict]]:
    """Return canonical rows and arbitration policies for declared SOP profiles.

    Empty rows from a successful query represent deletion/disable, not failure.
    No SOPManager is constructed: its startup schema writes do not belong here.
    """
    if not profile_ids:
        return [], []
    conn = None
    try:
        config_dir = getattr(settings, "config_dir", None)
        if config_dir is None:
            db_path = getattr(settings, "db_path", None)
            config_dir = Path(db_path).parent if db_path else get_config_dir() / "config"
        conn = connect_sqlite_readonly(Path(config_dir) / "freqinout_nets.db")
        conn.execute("BEGIN")
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"sop_profiles", "sop_schedule_layer", "sop_net_conflict_policy"}
        if not required.issubset(tables):
            raise ScheduleSourceError("Required SOP source schema is unavailable")
        marks = ",".join("?" for _ in profile_ids)
        profiles = {int(row["id"]): dict(row) for row in conn.execute(
            f"SELECT * FROM sop_profiles WHERE id IN ({marks})", tuple(sorted(profile_ids)))}
        condition_map = {}
        for group in settings.get("operating_groups", []) or []:
            if not isinstance(group, dict) or not group.get("use_condition_levels"):
                continue
            name = str(group.get("group") or "").strip().upper()
            try:
                level = int(group.get("condition_level") or 0)
            except (TypeError, ValueError):
                continue
            if name and 1 <= level <= 5:
                condition_map[name] = min(level, condition_map.get(name, level))
        sidecar = settings.get("sop_frequency_variant_references", {})
        rows = []
        for record in conn.execute(
            f"SELECT * FROM sop_schedule_layer WHERE profile_id IN ({marks}) "
            "ORDER BY profile_id, sort_order, id", tuple(sorted(profile_ids))):
            row = dict(record)
            profile = profiles.get(int(row["profile_id"]))
            if (not profile or not profile.get("active", 1) or not row.get("enabled", 1)
                    or str(profile.get("category") or "HF").upper() != "HF"):
                continue
            row.update(source="SOP", sop_profile_id=int(row["profile_id"]),
                       sop_layer_id=int(row["id"]), source_row_id=int(row["id"]),
                       source_table="sop_schedule_layer",
                       group_name=str(row.get("group_name") or profile.get("operating_group") or "").upper(),
                       profile_name=str(profile.get("name") or ""),
                       sop_priority=profile.get("priority", 100))
            allowed = str(row.get("condition_levels") or "ALL").upper()
            level = condition_map.get(row["group_name"])
            tokens = {part.strip() for part in allowed.replace(";", ",").split(",")}
            if level is not None and allowed != "ALL" and str(level) not in tokens:
                continue
            rows.append(reattach_sop_frequency_key(row, sidecar, row["sop_profile_id"]))
        policies = [dict(row) for row in conn.execute(
            f"SELECT * FROM sop_net_conflict_policy WHERE COALESCE(active, 1)=1 "
            f"AND sop_profile_id IN ({marks}) ORDER BY updated_utc DESC, id DESC",
            tuple(sorted(profile_ids)))]
        return rows, policies
    except Exception as exc:
        raise ScheduleSourceError(f"Cannot read canonical SOP schedule inputs: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
