from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, Any, List

from freqinout.core.logger import log
from freqinout.core.config_paths import get_config_dir
from freqinout.core.group_utils import normalize_group_name
from freqinout.core.operator_activity import newer_timestamp_text
from freqinout.core.operator_identity import (
    canonical_callsign,
    ensure_operator_identity_schema,
    get_or_create_operator_identity,
)
from freqinout.core.sqlite_utils import connect_sqlite_readonly, table_exists

ALLOWED_GROUP_ROLES = {"", "HUB", "HUB-ALT", "ALT-HUB", "NCS", "ANCS", "PEER"}
GROUP_ROLE_ALIASES = {"ALT-HUB": "HUB-ALT"}
OPERATOR_CHECKINS_COLUMNS = {
    "callsign",
    "name",
    "state",
    "grid",
    "group1",
    "group2",
    "group3",
    "group_role",
    "first_seen_utc",
    "last_seen_utc",
    "last_net",
    "last_role",
    "checkin_count",
    "groups_json",
    "trusted",
    "timezone",
    "tier",
    "roster_parent_group",
    "roster_region",
    "operator_id",
}


def _operator_checkins_create_ddl(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            callsign TEXT PRIMARY KEY,
            name TEXT,
            state TEXT,
            grid TEXT,
            group1 TEXT,
            group2 TEXT,
            group3 TEXT,
            group_role TEXT,
            first_seen_utc TEXT,
            last_seen_utc TEXT,
            last_net TEXT,
            last_role TEXT,
            checkin_count INTEGER DEFAULT 0,
            groups_json TEXT,
            trusted INTEGER DEFAULT 0,
            timezone TEXT,
            tier TEXT,
            roster_parent_group TEXT,
            roster_region TEXT,
            operator_id TEXT
        )
    """


def _canonical_callsign(value: object) -> str:
    return canonical_callsign(value)


def _normalize_groups_list(values: List[object]) -> List[str]:
    seen = set()
    groups: List[str] = []
    for value in values:
        group = normalize_group_name(value)
        if not group:
            continue
        if group in seen:
            continue
        seen.add(group)
        groups.append(group)
    return groups


def _normalize_group_role(value: object) -> str:
    role = str(value or "").strip().upper()
    role = GROUP_ROLE_ALIASES.get(role, role)
    return role if role in ALLOWED_GROUP_ROLES else ""


def _db_path() -> Path:
    """
    Returns the path to freqinout_nets.db under the shared config directory.
    """
    try:
        config_dir = get_config_dir() / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "freqinout_nets.db"
    except Exception as e:
        log.error("checkin_db: failed to determine DB path, falling back to home: %s", e)
        fallback = Path.home() / "freqinout_nets.db"
        return fallback


def _operator_checkins_select_expr(legacy_cols: set[str], column: str) -> str:
    if column == "callsign":
        return "callsign"
    if column == "first_seen_utc":
        sources = [name for name in ("first_seen_utc", "last_seen_utc", "date_added") if name in legacy_cols]
        return f"COALESCE({', '.join(sources)}, '')" if sources else "''"
    if column in {
        "name",
        "state",
        "grid",
        "group1",
        "group2",
        "group3",
        "group_role",
        "last_net",
        "last_role",
        "timezone",
        "tier",
        "roster_parent_group",
        "roster_region",
    }:
        return f"COALESCE({column}, '')" if column in legacy_cols else "''"
    if column == "last_seen_utc":
        return "COALESCE(last_seen_utc, '')" if "last_seen_utc" in legacy_cols else "''"
    if column == "checkin_count":
        return "COALESCE(checkin_count, 0)" if "checkin_count" in legacy_cols else "0"
    if column == "groups_json":
        return "groups_json" if "groups_json" in legacy_cols else "NULL"
    if column == "trusted":
        return "COALESCE(trusted, 0)" if "trusted" in legacy_cols else "0"
    if column == "operator_id":
        return "operator_id" if "operator_id" in legacy_cols else "NULL"
    return "NULL"


def _repair_operator_checkins_data(cur: sqlite3.Cursor) -> None:
    cur.execute("UPDATE operator_checkins SET trusted=0 WHERE trusted IS NULL")
    cur.execute(
        """
        UPDATE operator_checkins
           SET group_role = CASE
                WHEN TRIM(UPPER(COALESCE(group_role, ''))) = 'ALT-HUB'
                    THEN 'HUB-ALT'
                WHEN TRIM(UPPER(COALESCE(group_role, ''))) IN ('HUB','HUB-ALT','NCS','ANCS','PEER')
                    THEN TRIM(UPPER(COALESCE(group_role, '')))
                ELSE ''
              END
        """
    )
    cur.execute(
        """
        UPDATE operator_checkins
           SET first_seen_utc = last_seen_utc
         WHERE (first_seen_utc IS NULL OR first_seen_utc = '')
           AND COALESCE(last_seen_utc, '') <> ''
        """
    )
    cur.execute("SELECT callsign, group1, group2, group3, groups_json FROM operator_checkins")
    for callsign, group1, group2, group3, groups_json in cur.fetchall():
        groups: List[str] = []
        if groups_json:
            try:
                parsed = json.loads(groups_json)
                if isinstance(parsed, list):
                    groups.extend(parsed)
            except Exception:
                pass
        groups.extend([group1, group2, group3])
        normalized = _normalize_groups_list(groups)
        groups = json.dumps(normalized) if normalized else None
        cur.execute(
            """
            UPDATE operator_checkins
               SET group1=?,
                   group2=?,
                   group3=?,
                   groups_json=?
             WHERE callsign=?
            """,
            (
                normalized[0] if len(normalized) > 0 else "",
                normalized[1] if len(normalized) > 1 else "",
                normalized[2] if len(normalized) > 2 else "",
                groups,
                callsign,
            ),
        )


def ensure_operator_checkins_schema(conn: sqlite3.Connection, *, repair_data: bool = False):
    """
    Ensures operator_checkins exists with the unified schema, migrating
    from older layouts if necessary.
    """
    cur = conn.cursor()
    cur.execute(_operator_checkins_create_ddl("operator_checkins"))

    cur.execute("PRAGMA table_info(operator_checkins)")
    cols = {row[1] for row in cur.fetchall()}
    schema_changed = False
    if not OPERATOR_CHECKINS_COLUMNS.issubset(cols):
        schema_changed = True
        legacy_cols = set(cols)
        cur.execute("DROP TABLE IF EXISTS operator_checkins_new")
        cur.execute(_operator_checkins_create_ddl("operator_checkins_new"))
        if "callsign" in legacy_cols:
            ordered_columns = [
                "callsign",
                "name",
                "state",
                "grid",
                "group1",
                "group2",
                "group3",
                "group_role",
                "first_seen_utc",
                "last_seen_utc",
                "last_net",
                "last_role",
                "checkin_count",
                "groups_json",
                "trusted",
                "timezone",
                "tier",
                "roster_parent_group",
                "roster_region",
                "operator_id",
            ]
            select_exprs = [_operator_checkins_select_expr(legacy_cols, column) for column in ordered_columns]
            cur.execute(
                f"""
                INSERT OR REPLACE INTO operator_checkins_new
                    ({", ".join(ordered_columns)})
                SELECT
                    {", ".join(select_exprs)}
                FROM operator_checkins
                WHERE COALESCE(callsign, '') <> ''
                """
            )
        cur.execute("DROP TABLE operator_checkins")
        cur.execute("ALTER TABLE operator_checkins_new RENAME TO operator_checkins")
        cur.execute("PRAGMA table_info(operator_checkins)")
        cols = {row[1] for row in cur.fetchall()}
        log.info("checkins_db: migrated operator_checkins to unified schema.")

    for missing_col, ddl in (
        ("trusted", "INTEGER DEFAULT 0"),
        ("groups_json", "TEXT"),
        ("first_seen_utc", "TEXT"),
        ("last_seen_utc", "TEXT"),
        ("last_net", "TEXT"),
        ("last_role", "TEXT"),
        ("timezone", "TEXT"),
        ("tier", "TEXT"),
        ("roster_parent_group", "TEXT"),
        ("roster_region", "TEXT"),
        ("operator_id", "TEXT"),
    ):
        if missing_col not in cols:
            cur.execute(f"ALTER TABLE operator_checkins ADD COLUMN {missing_col} {ddl}")
            schema_changed = True

    if schema_changed or repair_data:
        _repair_operator_checkins_data(cur)
    ensure_operator_identity_schema(conn)
    conn.commit()


def _ensure_table(conn: sqlite3.Connection):
    """
    Backward-compatible alias for older callers.
    """
    ensure_operator_checkins_schema(conn)


def lookup_operator_identity(callsign: str) -> Dict[str, str]:
    """
    Return the canonical operator identity from operator_checkins only.

    This intentionally avoids local-history stores so FLDigi log-assisted intake
    uses the same shared source-of-truth everywhere.
    """
    cs = _canonical_callsign(callsign)
    if not cs:
        return {}

    db_path = _db_path()
    if not db_path.exists():
        return {}

    try:
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT name, state FROM operator_checkins WHERE callsign=?", (cs,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        log.error("checkins_db: lookup_operator_identity failed for %s: %s", cs, e)
        return {}

    if not row:
        return {}

    return {
        "callsign": cs,
        "name": (row[0] or "").strip(),
        "state": (row[1] or "").strip().upper(),
    }


def upsert_checkins(entries: List[Dict[str, Any]]):
    """
    Inserts or updates operator check-ins.

    Each entry should have:
        callsign, name, state, last_seen_utc, last_net, last_role

    checkin_count is incremented if the callsign already exists.
    """
    if not entries:
        return

    db_path = _db_path()

    try:
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)
        cur = conn.cursor()

        for e in entries:
            cs = _canonical_callsign(e.get("callsign"))
            if not cs:
                continue

            name = (e.get("name") or "").strip()
            state = (e.get("state") or "").upper().strip()
            grid = (e.get("grid") or "").strip().upper()
            group1 = normalize_group_name(e.get("group1"))
            group2 = normalize_group_name(e.get("group2"))
            group3 = normalize_group_name(e.get("group3"))
            group_role = _normalize_group_role(e.get("group_role"))
            last_seen = (e.get("last_seen_utc") or "").strip()
            first_seen = (e.get("first_seen_utc") or "").strip()
            last_net = (e.get("last_net") or "").strip()
            last_role = (e.get("last_role") or "").upper().strip()
            groups_json = e.get("groups_json")
            trusted_raw = e.get("trusted")

            identity = get_or_create_operator_identity(
                conn,
                cs,
                at_utc=last_seen or first_seen or None,
                provenance="checkin",
            )
            cs = identity.current_callsign
            operator_id = identity.operator_id

            # Load existing to preserve first_seen/groups/trusted/checkin_count
            cur.execute(
                """
                SELECT first_seen_utc, last_seen_utc, checkin_count, groups_json, trusted,
                       grid, group1, group2, group3, group_role
                FROM operator_checkins WHERE callsign=?
                """,
                (cs,),
            )
            existing = cur.fetchone()
            if existing:
                (
                    existing_first,
                    existing_last,
                    existing_count,
                    existing_groups_json,
                    existing_trusted,
                    existing_grid,
                    existing_g1,
                    existing_g2,
                    existing_g3,
                    existing_role,
                ) = existing
            else:
                existing_first = ""
                existing_last = ""
                existing_count = 0
                existing_groups_json = None
                existing_trusted = 0
                existing_grid = ""
                existing_g1 = existing_g2 = existing_g3 = ""
                existing_role = ""

            first_out = first_seen or existing_first or last_seen
            last_out = newer_timestamp_text(existing_last, last_seen)
            groups_json_out = groups_json if groups_json is not None else existing_groups_json
            if groups_json_out is not None:
                try:
                    parsed = json.loads(groups_json_out) if isinstance(groups_json_out, str) else groups_json_out
                    if isinstance(parsed, list):
                        normalized = _normalize_groups_list(parsed)
                        groups_json_out = json.dumps(normalized) if normalized else None
                except Exception:
                    pass
            trusted_out = (
                int(trusted_raw)
                if trusted_raw is not None
                else (int(existing_trusted) if existing_trusted is not None else 0)
            )
            grid_out = grid or existing_grid
            g1_out = group1 or normalize_group_name(existing_g1)
            g2_out = group2 or normalize_group_name(existing_g2)
            g3_out = group3 or normalize_group_name(existing_g3)
            role_out = group_role or existing_role
            insert_count = int(existing_count or 0) + 1

            cur.execute(
                """
                INSERT INTO operator_checkins
                    (callsign, name, state, grid, group1, group2, group3, group_role,
                     first_seen_utc, last_seen_utc, last_net, last_role,
                     checkin_count, groups_json, trusted, operator_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    name=excluded.name,
                    state=excluded.state,
                    grid=excluded.grid,
                    group1=excluded.group1,
                    group2=excluded.group2,
                    group3=excluded.group3,
                    group_role=excluded.group_role,
                    first_seen_utc=COALESCE(operator_checkins.first_seen_utc, excluded.first_seen_utc),
                    last_seen_utc=excluded.last_seen_utc,
                    last_net=excluded.last_net,
                    last_role=excluded.last_role,
                    checkin_count=operator_checkins.checkin_count + 1,
                    groups_json=COALESCE(excluded.groups_json, operator_checkins.groups_json),
                    trusted=COALESCE(operator_checkins.trusted, excluded.trusted),
                    operator_id=COALESCE(operator_checkins.operator_id, excluded.operator_id)
                """,
                (
                    cs,
                    name,
                    state,
                    grid_out,
                    g1_out,
                    g2_out,
                    g3_out,
                    role_out,
                    first_out,
                    last_out,
                    last_net,
                    last_role,
                    insert_count,
                    groups_json_out,
                    trusted_out,
                    operator_id,
                ),
            )

            # Mirror last_seen_utc into js8_links metadata if newer
            if last_out:
                _mirror_last_seen_to_js8_links(conn, cs, last_out)

        conn.commit()
        conn.close()
        log.info("checkin_db: saved %d check-in entries.", len(entries))

    except Exception as e:
        log.error("checkin_db: upsert failed: %s", e)


def upsert_operator_metadata(
    entries: List[Dict[str, Any]],
    conn: sqlite3.Connection | None = None,
    *,
    raise_on_error: bool = False,
    insert_only: bool = False,
    provenance: str = "operator_metadata",
) -> int:
    """
    Insert or update operator identity metadata without incrementing check-in counts.

    This is used by workflows such as manual peer schedule entry where we want
    operator identity and group data to become available across the UI, but we
    do not want to imply a received check-in event.
    """
    if not entries:
        return 0

    owns_conn = conn is None
    if owns_conn:
        db_path = _db_path()
        try:
            conn = sqlite3.connect(db_path)
        except Exception as e:
            log.error("checkins_db: metadata upsert open failed: %s", e)
            if raise_on_error:
                raise
            return 0

    written = 0
    try:
        assert conn is not None
        _ensure_table(conn)
        if insert_only:
            # Schema assurance commits. Acquire the insertion guard afterward so
            # a concurrent roster writer cannot turn reference import into edit.
            conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()

        for entry in entries:
            cs = _canonical_callsign(entry.get("callsign"))
            if not cs:
                continue

            name = str(entry.get("name") or "").strip()
            state = str(entry.get("state") or "").strip().upper()
            grid = str(entry.get("grid") or "").strip().upper()
            group_role = _normalize_group_role(entry.get("group_role"))
            last_seen = str(entry.get("last_seen_utc") or "").strip()
            first_seen = str(entry.get("first_seen_utc") or "").strip()
            trusted_raw = entry.get("trusted")
            timezone = str(entry.get("timezone") or "").strip()
            tier = str(entry.get("tier") or "").strip()
            roster_parent_group = normalize_group_name(entry.get("roster_parent_group"))
            roster_region = normalize_group_name(entry.get("roster_region"))
            identity = get_or_create_operator_identity(
                conn,
                cs,
                at_utc=last_seen or first_seen or None,
                provenance=provenance,
            )
            cs = identity.current_callsign
            operator_id = identity.operator_id

            provided_groups: List[str] = []
            groups_json_raw = entry.get("groups_json")
            if groups_json_raw is not None:
                try:
                    parsed = json.loads(groups_json_raw) if isinstance(groups_json_raw, str) else groups_json_raw
                    if isinstance(parsed, list):
                        provided_groups.extend(parsed)
                except Exception:
                    pass
            provided_groups.extend(
                [
                    entry.get("group1"),
                    entry.get("group2"),
                    entry.get("group3"),
                ]
            )
            normalized_groups = _normalize_groups_list(provided_groups)
            group1 = normalized_groups[0] if len(normalized_groups) > 0 else ""
            group2 = normalized_groups[1] if len(normalized_groups) > 1 else ""
            group3 = normalized_groups[2] if len(normalized_groups) > 2 else ""
            groups_json = json.dumps(normalized_groups) if normalized_groups else None

            cur.execute(
                """
                SELECT name, state, grid, group1, group2, group3, group_role,
                       first_seen_utc, last_seen_utc, checkin_count, groups_json, trusted,
                       timezone, tier, roster_parent_group, roster_region
                FROM operator_checkins
                WHERE callsign=?
                """,
                (cs,),
            )
            existing = cur.fetchone()
            if existing and insert_only:
                continue
            if existing:
                (
                    existing_name,
                    existing_state,
                    existing_grid,
                    existing_g1,
                    existing_g2,
                    existing_g3,
                    existing_role,
                    existing_first,
                    existing_last,
                    existing_count,
                    existing_groups_json,
                    existing_trusted,
                    existing_timezone,
                    existing_tier,
                    existing_roster_parent,
                    existing_roster_region,
                ) = existing
            else:
                existing_name = ""
                existing_state = ""
                existing_grid = ""
                existing_g1 = existing_g2 = existing_g3 = ""
                existing_role = ""
                existing_first = ""
                existing_last = ""
                existing_count = 0
                existing_groups_json = None
                existing_trusted = 0
                existing_timezone = ""
                existing_tier = ""
                existing_roster_parent = ""
                existing_roster_region = ""

            name_out = name or str(existing_name or "").strip()
            state_out = state or str(existing_state or "").strip().upper()
            grid_out = grid or str(existing_grid or "").strip().upper()
            role_out = group_role or str(existing_role or "").strip().upper()

            groups_json_out = groups_json if groups_json is not None else existing_groups_json
            if groups_json is not None:
                g1_out = group1
                g2_out = group2
                g3_out = group3
            else:
                g1_out = normalize_group_name(existing_g1)
                g2_out = normalize_group_name(existing_g2)
                g3_out = normalize_group_name(existing_g3)

            first_seen_out = str(existing_first or "").strip() or first_seen or last_seen
            last_seen_out = newer_timestamp_text(str(existing_last or "").strip(), last_seen)
            if not last_seen_out:
                last_seen_out = str(existing_last or "").strip() or last_seen

            try:
                existing_trusted_int = int(existing_trusted or 0)
            except Exception:
                existing_trusted_int = 0
            trusted_out = existing_trusted_int
            if trusted_raw is not None:
                try:
                    trusted_out = max(existing_trusted_int, int(trusted_raw))
                except Exception:
                    trusted_out = existing_trusted_int

            timezone_out = timezone or str(existing_timezone or "").strip()
            tier_out = tier or str(existing_tier or "").strip()
            roster_parent_out = roster_parent_group or normalize_group_name(existing_roster_parent)
            roster_region_out = roster_region or normalize_group_name(existing_roster_region)

            cur.execute(
                """
                INSERT INTO operator_checkins
                    (callsign, name, state, grid, group1, group2, group3, group_role,
                     first_seen_utc, last_seen_utc, last_net, last_role,
                     checkin_count, groups_json, trusted, timezone, tier,
                     roster_parent_group, roster_region, operator_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    name=excluded.name,
                    state=excluded.state,
                    grid=excluded.grid,
                    group1=excluded.group1,
                    group2=excluded.group2,
                    group3=excluded.group3,
                    group_role=excluded.group_role,
                    first_seen_utc=excluded.first_seen_utc,
                    last_seen_utc=excluded.last_seen_utc,
                    last_net=excluded.last_net,
                    last_role=excluded.last_role,
                    checkin_count=excluded.checkin_count,
                    groups_json=excluded.groups_json,
                    trusted=excluded.trusted,
                    timezone=excluded.timezone,
                    tier=excluded.tier,
                    roster_parent_group=excluded.roster_parent_group,
                    roster_region=excluded.roster_region,
                    operator_id=COALESCE(operator_checkins.operator_id, excluded.operator_id)
                """,
                (
                    cs,
                    name_out,
                    state_out,
                    grid_out,
                    g1_out,
                    g2_out,
                    g3_out,
                    role_out,
                    first_seen_out,
                    last_seen_out,
                    "",
                    "",
                    int(existing_count or 0),
                    groups_json_out,
                    trusted_out,
                    timezone_out,
                    tier_out,
                    roster_parent_out,
                    roster_region_out,
                    operator_id,
                ),
            )
            written += 1
        if owns_conn:
            conn.commit()
    except Exception as e:
        log.error("checkin_db: metadata upsert failed: %s", e)
        if owns_conn and conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        if raise_on_error:
            raise
        return 0
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return written


def get_all_operators() -> List[Dict[str, Any]]:
    """
    Returns all operators in the DB, sorted by callsign.
    """
    db_path = _db_path()
    if not db_path.exists():
        return []

    conn: sqlite3.Connection | None = None
    try:
        # This is a UI/search projection.  Startup owns schema assurance; a
        # list operation must never wait for a write lock or run identity
        # repairs merely because Settings/Spotter became visible.
        conn = connect_sqlite_readonly(db_path)
        if not table_exists(conn, "operator_checkins"):
            return []
        cur = conn.cursor()
        cur.execute(
            """
            SELECT callsign, name, state, grid, group1, group2, group3, group_role,
                   first_seen_utc, last_seen_utc, last_net, last_role, checkin_count,
                   groups_json, trusted, timezone, tier, roster_parent_group, roster_region
            FROM operator_checkins
            ORDER BY callsign COLLATE NOCASE
            """
        )
        rows = []
        for (
            cs,
            name,
            st,
            grid,
            g1,
            g2,
            g3,
            role,
            first_seen,
            last_seen,
            last_net,
            last_role,
            count,
            groups_json,
            trusted,
            timezone,
            tier,
            roster_parent_group,
            roster_region,
        ) in cur.fetchall():
            rows.append(
                {
                    "callsign": cs or "",
                    "name": name or "",
                    "state": st or "",
                    "grid": grid or "",
                    "group1": g1 or "",
                    "group2": g2 or "",
                    "group3": g3 or "",
                    "group_role": role or "",
                    "first_seen_utc": first_seen or "",
                    "last_seen_utc": last_seen or "",
                    "last_net": last_net or "",
                    "last_role": last_role or "",
                    "checkin_count": count or 0,
                    "groups_json": groups_json,
                    "trusted": trusted if trusted is not None else 0,
                    "timezone": timezone or "",
                    "tier": tier or "",
                    "roster_parent_group": roster_parent_group or "",
                    "roster_region": roster_region or "",
                }
            )
        return rows

    except Exception as e:
        log.error("checkin_db: get_all_operators failed: %s", e)
        return []
    finally:
        if conn is not None:
            conn.close()


def _ensure_js8_links_seen(conn: sqlite3.Connection) -> None:
    """
    Ensure js8_links has a last_seen_utc column for mirroring, adding if needed.
    """
    try:
        cur = conn.execute("PRAGMA table_info(js8_links)")
        cols = {row[1] for row in cur.fetchall()}
        if "last_seen_utc" not in cols:
            conn.execute("ALTER TABLE js8_links ADD COLUMN last_seen_utc TEXT")
            conn.commit()
    except Exception:
        # js8_links may not exist; ignore here
        return


def _mirror_last_seen_to_js8_links(conn: sqlite3.Connection, callsign: str, last_seen: str) -> None:
    """
    Mirror a newer last_seen_utc into js8_links rows that mention the callsign.
    """
    try:
        _ensure_js8_links_seen(conn)
        conn.execute(
            """
            UPDATE js8_links
               SET last_seen_utc=?
             WHERE (origin=? OR destination=?)
               AND (last_seen_utc IS NULL OR last_seen_utc < ?)
            """,
            (last_seen, callsign, callsign, last_seen),
        )
        conn.commit()
    except Exception:
        # best-effort; station map will still function without this mirror
        return
