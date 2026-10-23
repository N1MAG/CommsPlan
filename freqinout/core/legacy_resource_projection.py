"""Single compatibility writer for the legacy HF Net resource projection.

After LN-2 cutover, UI code may still consume the legacy row shape, but all
mutations pass through this module and refresh the canonical catalog in the same
caller-owned SQLite transaction.
"""

from __future__ import annotations

import datetime
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from freqinout.core.resource_catalog_migration import synchronize_legacy_rows_in_connection
from freqinout.core.fldigi_tuning import tuning_preset_json


LEGACY_COLUMNS = (
    "resource_set", "source_type", "source_ref", "readonly", "day_utc", "recurrence",
    "biweekly_offset_weeks", "month_weeks", "group_name", "band", "mode", "frequency",
    "start_utc", "end_utc", "early_checkin", "primary_js8call_group", "coverage", "comment",
    "net_name", "fldigi_mode", "fldigi_offset", "fldigi_tuning_json", "updated_utc",
)


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def upsert_legacy_resource(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    resource_set: str,
    source_type: str,
    source_ref: str,
    readonly: int = 1,
    resource_id: int | None = None,
    update_existing: bool = True,
    synchronize: bool = True,
) -> int:
    available = {str(row[1]) for row in conn.execute("PRAGMA table_info(net_resources)")}
    columns = tuple(name for name in LEGACY_COLUMNS if name in available)
    values = {
        "resource_set": resource_set,
        "source_type": source_type,
        "source_ref": source_ref,
        "readonly": int(readonly),
        "updated_utc": utc_now_iso(),
    }
    values.update({name: row.get(name, "") for name in columns if name not in values})
    values["biweekly_offset_weeks"] = int(values.get("biweekly_offset_weeks") or 0)
    values["early_checkin"] = int(values.get("early_checkin") or 0)
    if "fldigi_tuning_json" in columns:
        values["fldigi_tuning_json"] = tuning_preset_json(row.get("fldigi_tuning_json"))
    rid = int(resource_id or 0)
    if rid <= 0:
        tuning_clause = " AND COALESCE(NULLIF(TRIM(fldigi_tuning_json),''),'{}')=?" if "fldigi_tuning_json" in columns else ""
        existing = conn.execute(
            f"""SELECT id FROM net_resources
            WHERE TRIM(resource_set)=TRIM(?)
              AND TRIM(day_utc)=TRIM(?)
              AND LOWER(TRIM(COALESCE(recurrence,'')))=LOWER(TRIM(?))
              AND TRIM(COALESCE(month_weeks,''))=TRIM(?)
              AND UPPER(TRIM(COALESCE(group_name,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(band,'')))=UPPER(TRIM(?))
              AND UPPER(TRIM(COALESCE(mode,'')))=UPPER(TRIM(?))
              AND TRIM(COALESCE(frequency,''))=TRIM(?)
              AND TRIM(start_utc)=TRIM(?) AND TRIM(end_utc)=TRIM(?)
              AND UPPER(TRIM(COALESCE(net_name,'')))=UPPER(TRIM(?))
              {tuning_clause}
            ORDER BY id LIMIT 1""",
            (resource_set, values["day_utc"], values["recurrence"], values["month_weeks"],
             values["group_name"], values["band"], values["mode"], values["frequency"],
             values["start_utc"], values["end_utc"], values["net_name"],
             *([values["fldigi_tuning_json"]] if tuning_clause else [])),
        ).fetchone()
        if existing:
            rid = int(existing[0])
            if not update_existing:
                if synchronize:
                    synchronize_legacy_rows_in_connection(conn)
                return rid
    if rid > 0 and conn.execute("SELECT 1 FROM net_resources WHERE id=?", (rid,)).fetchone():
        assignments = ",".join(f"{name}=?" for name in columns)
        conn.execute(
            f"UPDATE net_resources SET {assignments} WHERE id=?",
            tuple(values[name] for name in columns) + (rid,),
        )
    else:
        marks = ",".join("?" for _ in columns)
        cursor = conn.execute(
            f"INSERT INTO net_resources ({','.join(columns)}) VALUES ({marks})",
            tuple(values[name] for name in columns),
        )
        rid = int(cursor.lastrowid or 0)
    if synchronize:
        synchronize_legacy_rows_in_connection(conn)
    return rid


def update_legacy_resource_fields(
    conn: sqlite3.Connection,
    resource_id: int,
    fields: Mapping[str, Any],
    *,
    synchronize: bool = True,
) -> bool:
    rid = int(resource_id or 0)
    available = {str(row[1]) for row in conn.execute("PRAGMA table_info(net_resources)")}
    allowed = (set(LEGACY_COLUMNS) & available) - {"updated_utc"}
    selected = {name: value for name, value in fields.items() if name in allowed}
    if rid <= 0 or not selected:
        return False
    selected["updated_utc"] = utc_now_iso()
    cursor = conn.execute(
        f"UPDATE net_resources SET {','.join(f'{name}=?' for name in selected)} WHERE id=?",
        tuple(selected.values()) + (rid,),
    )
    if int(cursor.rowcount or 0) <= 0:
        return False
    if synchronize:
        synchronize_legacy_rows_in_connection(conn)
    return True


def delete_legacy_resources(
    conn: sqlite3.Connection,
    resource_ids: Iterable[int],
    *,
    synchronize: bool = True,
) -> int:
    ids = sorted({int(value) for value in resource_ids if int(value) > 0})
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    cursor = conn.execute(f"DELETE FROM net_resources WHERE id IN ({marks})", ids)
    if synchronize:
        synchronize_legacy_rows_in_connection(conn)
    return max(0, int(cursor.rowcount or 0))


def dedupe_legacy_resources(
    conn: sqlite3.Connection,
    *,
    synchronize: bool = True,
) -> int:
    tuning_column = ",fldigi_tuning_json" if "fldigi_tuning_json" in {
        str(row[1]) for row in conn.execute("PRAGMA table_info(net_resources)")
    } else ""
    rows = conn.execute(
        f"""SELECT id,resource_set,day_utc,recurrence,month_weeks,group_name,band,mode,
                  frequency,start_utc,end_utc,net_name{tuning_column},updated_utc
           FROM net_resources ORDER BY COALESCE(updated_utc,'') DESC,id DESC"""
    ).fetchall()
    seen: set[tuple[str, ...]] = set()
    remove: list[int] = []
    for row in rows:
        key = tuple(str(value or "").strip().casefold() for value in row[1:-1])
        if key in seen:
            remove.append(int(row[0]))
        else:
            seen.add(key)
    return delete_legacy_resources(conn, remove, synchronize=synchronize) if remove else 0


def replace_nonstation_set(
    conn: sqlite3.Connection,
    resource_set: str,
    incoming_rows: Sequence[Mapping[str, Any]],
    *,
    source_type: str,
    source_ref: str,
) -> tuple[int, int]:
    """Replace one imported/bundled set while preserving station-owned rows."""
    existing = conn.execute(
        """SELECT id FROM net_resources WHERE TRIM(resource_set)=TRIM(?)
           AND LOWER(TRIM(COALESCE(source_type,''))) != 'manual'""",
        (resource_set,),
    ).fetchall()
    removed = delete_legacy_resources(conn, (row[0] for row in existing), synchronize=False)
    inserted = 0
    for row in incoming_rows:
        upsert_legacy_resource(
            conn, row, resource_set=resource_set, source_type=source_type,
            source_ref=source_ref, readonly=1, synchronize=False,
        )
        inserted += 1
    synchronize_legacy_rows_in_connection(conn)
    return inserted, removed


def finalize_legacy_resource_projection(conn: sqlite3.Connection) -> None:
    synchronize_legacy_rows_in_connection(conn)


__all__ = [
    "dedupe_legacy_resources",
    "delete_legacy_resources",
    "finalize_legacy_resource_projection",
    "replace_nonstation_set",
    "update_legacy_resource_fields",
    "upsert_legacy_resource",
]
