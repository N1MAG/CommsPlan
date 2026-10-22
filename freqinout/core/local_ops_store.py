from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Dict, List, Optional

from freqinout.core.config_paths import get_config_dir
from freqinout.core.logger import log
from freqinout.core.message_intelligence import TOPIC_TAXONOMY, collect_topic_evidence
from freqinout.core.sqlite_utils import connect_sqlite_readonly, table_exists


SITREP_ALLOWED = {"GREEN", "YELLOW", "RED"}
REPORT_STATUS_ALLOWED = {"INFO", "WATCH", "PRIORITY", "EMERGENCY"}
CONFIRMED_ALLOWED = {"UNCONFIRMED", "CONFIRMED", "SECOND_HAND", "NEEDS_FOLLOWUP"}
_SCHEMA_READY = False
_SCHEMA_READY_DB = ""


def _db_path():
    return get_config_dir() / "config" / "freqinout_nets.db"


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _norm_callsign(value: str) -> str:
    return (value or "").strip().upper()


def _norm_status(value: str) -> str:
    txt = (value or "").strip().upper()
    return txt if txt in SITREP_ALLOWED else "GREEN"


def _norm_report_status(value: str) -> str:
    txt = (value or "").strip().upper()
    return txt if txt in REPORT_STATUS_ALLOWED else "INFO"


def _norm_confirmed_state(value: str) -> str:
    txt = (value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return txt if txt in CONFIRMED_ALLOWED else "UNCONFIRMED"


def _norm_topics(values: Any) -> List[str]:
    if isinstance(values, str):
        raw_values = [part.strip() for part in values.split(",")]
    elif isinstance(values, (list, tuple, set)):
        raw_values = [str(part or "").strip() for part in values]
    else:
        raw_values = []
    by_lower = {topic.lower(): topic for topic in TOPIC_TAXONOMY}
    out: List[str] = []
    for value in raw_values:
        topic = by_lower.get(value.lower())
        if topic and topic not in out:
            out.append(topic)
    return out


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _json_loads(value: Any, default: Any) -> Any:
    try:
        if value is None or value == "":
            return default
        return json.loads(str(value))
    except Exception:
        return default


def _split_name_parts(value: str) -> tuple[str, str]:
    txt = (value or "").strip()
    if not txt:
        return "", ""
    parts = [p for p in txt.split() if p]
    if len(parts) <= 1:
        return (parts[0] if parts else ""), ""
    return parts[0], " ".join(parts[1:])


def _compose_name(first_name: str, last_name: str) -> str:
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    return " ".join([p for p in (first, last) if p]).strip()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    for name, col_type in columns.items():
        if name in existing:
            continue
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def ensure_tables() -> None:
    global _SCHEMA_READY, _SCHEMA_READY_DB
    db_path = _db_path()
    db_key = str(db_path.resolve()) if db_path.exists() else str(db_path)
    if _SCHEMA_READY and _SCHEMA_READY_DB == db_key and db_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS local_operator_checkins (
                callsign TEXT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                name TEXT,
                city TEXT,
                state TEXT,
                category TEXT,
                first_seen_utc TEXT,
                last_seen_utc TEXT,
                checkin_count INTEGER DEFAULT 0,
                notes TEXT,
                sitrep_status TEXT DEFAULT 'GREEN',
                updated_utc TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "local_operator_checkins",
            {
                "first_name": "TEXT",
                "last_name": "TEXT",
                "name": "TEXT",
                "city": "TEXT",
                "state": "TEXT",
                "category": "TEXT",
                "first_seen_utc": "TEXT",
                "last_seen_utc": "TEXT",
                "checkin_count": "INTEGER DEFAULT 0",
                "notes": "TEXT",
                "sitrep_status": "TEXT DEFAULT 'GREEN'",
                "updated_utc": "TEXT",
            },
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ops_last_seen ON local_operator_checkins(last_seen_utc)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ops_category ON local_operator_checkins(category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ops_status ON local_operator_checkins(sitrep_status)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS local_ncs_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checkin_utc TEXT NOT NULL,
                net_name TEXT,
                channels TEXT,
                callsign TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                name TEXT,
                city TEXT,
                state TEXT,
                category TEXT,
                sitrep_status TEXT DEFAULT 'GREEN',
                notes TEXT,
                updated_utc TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "local_ncs_checkins",
            {
                "checkin_utc": "TEXT",
                "net_name": "TEXT",
                "channels": "TEXT",
                "callsign": "TEXT",
                "first_name": "TEXT",
                "last_name": "TEXT",
                "name": "TEXT",
                "city": "TEXT",
                "state": "TEXT",
                "category": "TEXT",
                "sitrep_status": "TEXT DEFAULT 'GREEN'",
                "notes": "TEXT",
                "updated_utc": "TEXT",
            },
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ncs_checkins_ts ON local_ncs_checkins(checkin_utc)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ncs_checkins_callsign ON local_ncs_checkins(callsign)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS local_operator_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_utc TEXT NOT NULL,
                updated_utc TEXT,
                source_kind TEXT,
                source_channel TEXT,
                net_session_id TEXT,
                callsign TEXT,
                operator_id TEXT,
                from_name TEXT,
                city TEXT,
                county TEXT,
                state TEXT,
                grid TEXT,
                lat REAL,
                lon REAL,
                location_source TEXT,
                location_confidence TEXT,
                status TEXT,
                topics_json TEXT,
                topic_evidence_json TEXT,
                subject TEXT,
                body TEXT,
                confirmed_state TEXT,
                followup_state TEXT,
                exercise_flag INTEGER DEFAULT 0,
                source_radio_id INTEGER,
                source_app TEXT,
                raw_reference TEXT,
                created_by TEXT,
                updated_by TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "local_operator_reports",
            {
                "created_utc": "TEXT",
                "updated_utc": "TEXT",
                "source_kind": "TEXT",
                "source_channel": "TEXT",
                "net_session_id": "TEXT",
                "callsign": "TEXT",
                "operator_id": "TEXT",
                "from_name": "TEXT",
                "city": "TEXT",
                "county": "TEXT",
                "state": "TEXT",
                "grid": "TEXT",
                "lat": "REAL",
                "lon": "REAL",
                "location_source": "TEXT",
                "location_confidence": "TEXT",
                "status": "TEXT",
                "topics_json": "TEXT",
                "topic_evidence_json": "TEXT",
                "subject": "TEXT",
                "body": "TEXT",
                "confirmed_state": "TEXT",
                "followup_state": "TEXT",
                "exercise_flag": "INTEGER DEFAULT 0",
                "source_radio_id": "INTEGER",
                "source_app": "TEXT",
                "raw_reference": "TEXT",
                "created_by": "TEXT",
                "updated_by": "TEXT",
            },
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_created ON local_operator_reports(created_utc)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_callsign ON local_operator_reports(callsign)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_state ON local_operator_reports(state)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_grid ON local_operator_reports(grid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_status ON local_operator_reports(status)")
        conn.commit()
        _SCHEMA_READY = True
        _SCHEMA_READY_DB = db_key
    finally:
        conn.close()


def get_all_operators() -> List[Dict[str, Any]]:
    db_path = _db_path()
    if not db_path.exists():
        return []
    # Startup owns schema.  Keep lookup/autocomplete consumers read-only so an
    # active projection writer cannot turn a tab click into schema work.
    conn = connect_sqlite_readonly(db_path)
    try:
        if not table_exists(conn, "local_operator_checkins"):
            return []
        cur = conn.execute(
            """
            SELECT
                callsign,
                first_name,
                last_name,
                name,
                city,
                state,
                category,
                first_seen_utc,
                last_seen_utc,
                IFNULL(checkin_count, 0),
                notes,
                sitrep_status,
                updated_utc
            FROM local_operator_checkins
            ORDER BY callsign COLLATE NOCASE
            """
        )
        out: List[Dict[str, Any]] = []
        for row in cur.fetchall():
            first_name = (row[1] or "").strip()
            last_name = (row[2] or "").strip()
            full_name = (row[3] or "").strip()
            if (not first_name and not last_name) and full_name:
                first_name, last_name = _split_name_parts(full_name)
            if not full_name:
                full_name = _compose_name(first_name, last_name)
            out.append(
                {
                    "callsign": row[0] or "",
                    "first_name": first_name,
                    "last_name": last_name,
                    "name": full_name,
                    "city": row[4] or "",
                    "state": row[5] or "",
                    "category": row[6] or "",
                    "first_seen_utc": row[7] or "",
                    "last_seen_utc": row[8] or "",
                    "checkin_count": int(row[9] or 0),
                    "notes": row[10] or "",
                    "sitrep_status": _norm_status(row[11] or "GREEN"),
                    "updated_utc": row[12] or "",
                }
            )
        return out
    finally:
        conn.close()


def get_operator(callsign: str) -> Optional[Dict[str, Any]]:
    cs = _norm_callsign(callsign)
    if not cs:
        return None
    ensure_tables()
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.execute(
            """
            SELECT
                callsign,
                first_name,
                last_name,
                name,
                city,
                state,
                category,
                first_seen_utc,
                last_seen_utc,
                IFNULL(checkin_count, 0),
                notes,
                sitrep_status,
                updated_utc
            FROM local_operator_checkins
            WHERE callsign=?
            """,
            (cs,),
        )
        row = cur.fetchone()
        if not row:
            return None
        first_name = (row[1] or "").strip()
        last_name = (row[2] or "").strip()
        full_name = (row[3] or "").strip()
        if (not first_name and not last_name) and full_name:
            first_name, last_name = _split_name_parts(full_name)
        if not full_name:
            full_name = _compose_name(first_name, last_name)
        return {
            "callsign": row[0] or "",
            "first_name": first_name,
            "last_name": last_name,
            "name": full_name,
            "city": row[4] or "",
            "state": row[5] or "",
            "category": row[6] or "",
            "first_seen_utc": row[7] or "",
            "last_seen_utc": row[8] or "",
            "checkin_count": int(row[9] or 0),
            "notes": row[10] or "",
            "sitrep_status": _norm_status(row[11] or "GREEN"),
            "updated_utc": row[12] or "",
        }
    finally:
        conn.close()


def upsert_operator(
    callsign: str,
    *,
    first_name: str = "",
    last_name: str = "",
    name: str = "",
    city: str = "",
    state: str = "",
    category: str = "",
    notes: Optional[str] = None,
    sitrep_status: Optional[str] = None,
    first_seen_utc: Optional[str] = None,
    last_seen_utc: Optional[str] = None,
    checkin_count: Optional[int] = None,
    touch_seen: bool = False,
    increment_checkins: bool = False,
    seen_utc: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cs = _norm_callsign(callsign)
    if not cs:
        return None

    ensure_tables()
    now_iso = _utc_now_iso()
    seen_stamp = (seen_utc or now_iso).strip()
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                first_name,
                last_name,
                name,
                city,
                state,
                category,
                first_seen_utc,
                last_seen_utc,
                IFNULL(checkin_count, 0),
                notes,
                sitrep_status
            FROM local_operator_checkins
            WHERE callsign=?
            """,
            (cs,),
        )
        row = cur.fetchone()

        ex_first_name = (row[0] or "") if row else ""
        ex_last_name = (row[1] or "") if row else ""
        ex_name = (row[2] or "") if row else ""
        ex_city = (row[3] or "") if row else ""
        ex_state = (row[4] or "") if row else ""
        ex_category = (row[5] or "") if row else ""
        ex_first_seen = (row[6] or "") if row else ""
        ex_last_seen = (row[7] or "") if row else ""
        ex_count = int(row[8] or 0) if row else 0
        ex_notes = (row[9] or "") if row else ""
        ex_status = _norm_status(row[10] or "GREEN") if row else "GREEN"

        provided_first = (first_name or "").strip()
        provided_last = (last_name or "").strip()
        provided_name = (name or "").strip()
        if provided_name and not provided_first and not provided_last:
            split_first, split_last = _split_name_parts(provided_name)
            provided_first = split_first
            provided_last = split_last

        out_first_name = provided_first or ex_first_name
        out_last_name = provided_last or ex_last_name
        out_name = provided_name or _compose_name(out_first_name, out_last_name) or ex_name
        out_city = (city or "").strip() or ex_city
        out_state = (state or "").strip().upper() or ex_state
        out_category = (category or "").strip() or ex_category
        out_notes = ex_notes if notes is None else (notes or "").strip()
        out_status = _norm_status(sitrep_status or ex_status)
        out_first_seen = (first_seen_utc or "").strip() or ex_first_seen or seen_stamp
        out_last_seen = (last_seen_utc or "").strip() or ex_last_seen
        if touch_seen:
            out_last_seen = seen_stamp
            if not out_first_seen:
                out_first_seen = seen_stamp
        elif not out_last_seen and row:
            out_last_seen = ex_last_seen or ""

        out_count = ex_count + 1 if increment_checkins else ex_count
        if not row and increment_checkins and out_count <= 0:
            out_count = 1
        if checkin_count is not None:
            try:
                out_count = max(0, int(checkin_count))
            except Exception:
                pass

        cur.execute(
            """
            INSERT INTO local_operator_checkins (
                callsign,
                first_name,
                last_name,
                name,
                city,
                state,
                category,
                first_seen_utc,
                last_seen_utc,
                checkin_count,
                notes,
                sitrep_status,
                updated_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(callsign)
            DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                name=excluded.name,
                city=excluded.city,
                state=excluded.state,
                category=excluded.category,
                first_seen_utc=COALESCE(local_operator_checkins.first_seen_utc, excluded.first_seen_utc),
                last_seen_utc=
                    CASE
                        WHEN excluded.last_seen_utc IS NOT NULL AND excluded.last_seen_utc <> '' THEN excluded.last_seen_utc
                        ELSE local_operator_checkins.last_seen_utc
                    END,
                checkin_count=excluded.checkin_count,
                notes=excluded.notes,
                sitrep_status=excluded.sitrep_status,
                updated_utc=excluded.updated_utc
            """,
            (
                cs,
                out_first_name,
                out_last_name,
                out_name,
                out_city,
                out_state,
                out_category,
                out_first_seen,
                out_last_seen,
                out_count,
                out_notes,
                out_status,
                now_iso,
            ),
        )
        conn.commit()
    except Exception as e:
        log.error("local_ops_store.upsert_operator failed for %s: %s", cs, e)
        return None
    finally:
        conn.close()

    return get_operator(cs)


def insert_reference_operator(row: Dict[str, Any]) -> bool:
    """Explicit reviewed reference import; never overwrite or fabricate activity."""
    ensure_tables()
    with sqlite3.connect(_db_path()) as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO local_operator_checkins
               (callsign,first_name,last_name,name,city,state,category,notes,
                sitrep_status,first_seen_utc,last_seen_utc,checkin_count)
               VALUES (?,?,?,?,?,?,?,?,?,'','',0)""",
            (_norm_callsign(row["callsign"]), row.get("first_name", ""), row.get("last_name", ""),
             _compose_name(row.get("first_name", ""), row.get("last_name", "")), row.get("city", ""),
             str(row.get("state", "")).upper(), row.get("category", ""), row.get("notes", ""),
             _norm_status(row.get("sitrep_status", "GREEN"))),
        )
        return cursor.rowcount == 1


def delete_operators(callsigns: List[str]) -> int:
    keys = sorted({_norm_callsign(cs) for cs in callsigns if _norm_callsign(cs)})
    if not keys:
        return 0
    ensure_tables()
    conn = sqlite3.connect(_db_path())
    deleted = 0
    try:
        with conn:
            for cs in keys:
                cur = conn.execute("DELETE FROM local_operator_checkins WHERE callsign=?", (cs,))
                deleted += int(cur.rowcount or 0)
        return deleted
    finally:
        conn.close()


def record_checkin(
    *,
    callsign: str,
    net_name: str = "",
    channels: str = "",
    first_name: str = "",
    last_name: str = "",
    name: str = "",
    city: str = "",
    state: str = "",
    category: str = "",
    sitrep_status: str = "GREEN",
    notes: str = "",
    checkin_utc: Optional[str] = None,
) -> Optional[int]:
    cs = _norm_callsign(callsign)
    if not cs:
        return None
    stamp = (checkin_utc or _utc_now_iso()).strip()
    status = _norm_status(sitrep_status)
    op_row = upsert_operator(
        cs,
        first_name=first_name,
        last_name=last_name,
        name=name,
        city=city,
        state=state,
        category=category,
        notes=notes,
        sitrep_status=status,
        touch_seen=True,
        increment_checkins=True,
        seen_utc=stamp,
    )
    if op_row is None:
        return None

    ensure_tables()
    conn = sqlite3.connect(_db_path())
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO local_ncs_checkins (
                    checkin_utc,
                    net_name,
                    channels,
                    callsign,
                    first_name,
                    last_name,
                    name,
                    city,
                    state,
                    category,
                    sitrep_status,
                    notes,
                    updated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stamp,
                    (net_name or "").strip(),
                    (channels or "").strip(),
                    cs,
                    (op_row.get("first_name") or "").strip(),
                    (op_row.get("last_name") or "").strip(),
                    (op_row.get("name") or "").strip(),
                    (op_row.get("city") or "").strip(),
                    (op_row.get("state") or "").strip().upper(),
                    (op_row.get("category") or "").strip(),
                    status,
                    (notes or "").strip(),
                    _utc_now_iso(),
                ),
            )
            return int(cur.lastrowid or 0)
    except Exception as e:
        log.error("local_ops_store.record_checkin failed for %s: %s", cs, e)
        return None
    finally:
        conn.close()


def update_checkin_entry(entry_id: int, *, sitrep_status: str, notes: str) -> bool:
    if int(entry_id or 0) <= 0:
        return False
    status = _norm_status(sitrep_status)
    note_text = (notes or "").strip()
    now_iso = _utc_now_iso()

    ensure_tables()
    conn = sqlite3.connect(_db_path())
    callsign = ""
    try:
        with conn:
            row = conn.execute(
                "SELECT callsign FROM local_ncs_checkins WHERE id=?",
                (int(entry_id),),
            ).fetchone()
            if row:
                callsign = _norm_callsign(row[0] or "")
            conn.execute(
                """
                UPDATE local_ncs_checkins
                SET sitrep_status=?, notes=?, updated_utc=?
                WHERE id=?
                """,
                (status, note_text, now_iso, int(entry_id)),
            )
    except Exception as e:
        log.error("local_ops_store.update_checkin_entry failed for id=%s: %s", entry_id, e)
        return False
    finally:
        conn.close()

    if callsign:
        upsert_operator(
            callsign,
            notes=note_text,
            sitrep_status=status,
            touch_seen=False,
            increment_checkins=False,
        )
    return True


def list_checkins(limit: int = 500) -> List[Dict[str, Any]]:
    ensure_tables()
    lim = max(1, int(limit or 500))
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.execute(
            """
            SELECT
                id,
                checkin_utc,
                net_name,
                channels,
                callsign,
                first_name,
                last_name,
                name,
                city,
                state,
                category,
                sitrep_status,
                notes
            FROM local_ncs_checkins
            ORDER BY checkin_utc DESC, id DESC
            LIMIT ?
            """,
            (lim,),
        )
        out: List[Dict[str, Any]] = []
        for row in cur.fetchall():
            first_name = (row[5] or "").strip()
            last_name = (row[6] or "").strip()
            full_name = (row[7] or "").strip()
            if (not first_name and not last_name) and full_name:
                first_name, last_name = _split_name_parts(full_name)
            if not full_name:
                full_name = _compose_name(first_name, last_name)
            out.append(
                {
                    "id": int(row[0]),
                    "checkin_utc": row[1] or "",
                    "net_name": row[2] or "",
                    "channels": row[3] or "",
                    "callsign": row[4] or "",
                    "first_name": first_name,
                    "last_name": last_name,
                    "name": full_name,
                    "city": row[8] or "",
                    "state": row[9] or "",
                    "category": row[10] or "",
                    "sitrep_status": _norm_status(row[11] or "GREEN"),
                    "notes": row[12] or "",
                }
            )
        return out
    finally:
        conn.close()


def record_local_report(
    *,
    callsign: str = "",
    source_kind: str = "voice",
    source_channel: str = "",
    net_session_id: str = "",
    operator_id: str = "",
    from_name: str = "",
    city: str = "",
    county: str = "",
    state: str = "",
    grid: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    location_source: str = "",
    location_confidence: str = "",
    status: str = "INFO",
    topics: Any = None,
    subject: str = "",
    body: str = "",
    confirmed_state: str = "UNCONFIRMED",
    followup_state: str = "",
    exercise_flag: bool = False,
    source_radio_id: Optional[int] = None,
    source_app: str = "",
    raw_reference: str = "",
    created_by: str = "",
    created_utc: Optional[str] = None,
) -> Optional[int]:
    ensure_tables()
    stamp = (created_utc or _utc_now_iso()).strip()
    cs = _norm_callsign(callsign)
    topic_values = _norm_topics(topics)
    topic_evidence = collect_topic_evidence(
        (
            ("manual_topics", " ".join(topic_values)),
            ("subject", subject),
            ("body", body),
            ("source", source_kind),
            ("channel", source_channel),
            ("state", state),
            ("grid", grid),
        )
    )
    if topic_values:
        merged = {topic: list(topic_evidence.get(topic, ())) for topic in topic_values}
        for topic in topic_values:
            evidence = f"manual:{topic}"
            if evidence not in merged[topic]:
                merged[topic].insert(0, evidence)
        for topic, values in topic_evidence.items():
            if topic not in topic_values:
                topic_values.append(topic)
            merged.setdefault(topic, [])
            for value in values:
                if value not in merged[topic]:
                    merged[topic].append(value)
        topic_evidence_json = {topic: tuple(values) for topic, values in merged.items()}
    else:
        topic_values = list(topic_evidence.keys())
        topic_evidence_json = topic_evidence
    try:
        source_radio = int(source_radio_id) if source_radio_id is not None else None
    except Exception:
        source_radio = None
    conn = sqlite3.connect(_db_path())
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO local_operator_reports (
                    created_utc,
                    updated_utc,
                    source_kind,
                    source_channel,
                    net_session_id,
                    callsign,
                    operator_id,
                    from_name,
                    city,
                    county,
                    state,
                    grid,
                    lat,
                    lon,
                    location_source,
                    location_confidence,
                    status,
                    topics_json,
                    topic_evidence_json,
                    subject,
                    body,
                    confirmed_state,
                    followup_state,
                    exercise_flag,
                    source_radio_id,
                    source_app,
                    raw_reference,
                    created_by,
                    updated_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stamp,
                    stamp,
                    str(source_kind or "voice").strip().lower() or "voice",
                    str(source_channel or "").strip(),
                    str(net_session_id or "").strip(),
                    cs,
                    str(operator_id or "").strip(),
                    str(from_name or "").strip(),
                    str(city or "").strip(),
                    str(county or "").strip(),
                    str(state or "").strip().upper(),
                    str(grid or "").strip().upper(),
                    lat,
                    lon,
                    str(location_source or "").strip(),
                    str(location_confidence or "").strip(),
                    _norm_report_status(status),
                    _json_dumps(topic_values),
                    _json_dumps(topic_evidence_json),
                    str(subject or "").strip(),
                    str(body or "").strip(),
                    _norm_confirmed_state(confirmed_state),
                    str(followup_state or "").strip(),
                    1 if bool(exercise_flag) else 0,
                    source_radio,
                    str(source_app or "").strip(),
                    str(raw_reference or "").strip(),
                    str(created_by or "").strip(),
                    str(created_by or "").strip(),
                ),
            )
            report_id = int(cur.lastrowid or 0)
        if cs:
            upsert_operator(
                cs,
                name=from_name,
                city=city,
                state=state,
                sitrep_status=_report_status_to_sitrep(status),
                touch_seen=True,
                seen_utc=stamp,
            )
        _mirror_local_report_to_observation(
            {
                "id": report_id,
                "created_utc": stamp,
                "updated_utc": stamp,
                "source_kind": str(source_kind or "voice").strip().lower() or "voice",
                "source_channel": str(source_channel or "").strip(),
                "net_session_id": str(net_session_id or "").strip(),
                "callsign": cs,
                "operator_id": str(operator_id or "").strip(),
                "from_name": str(from_name or "").strip(),
                "city": str(city or "").strip(),
                "county": str(county or "").strip(),
                "state": str(state or "").strip().upper(),
                "grid": str(grid or "").strip().upper(),
                "lat": lat,
                "lon": lon,
                "location_source": str(location_source or "").strip(),
                "location_confidence": str(location_confidence or "").strip(),
                "status": _norm_report_status(status),
                "topics": topic_values,
                "topic_evidence": topic_evidence_json,
                "subject": str(subject or "").strip(),
                "body": str(body or "").strip(),
                "confirmed_state": _norm_confirmed_state(confirmed_state),
                "followup_state": str(followup_state or "").strip(),
                "exercise_flag": bool(exercise_flag),
                "source_radio_id": source_radio,
                "source_app": str(source_app or "").strip(),
                "raw_reference": str(raw_reference or "").strip(),
                "created_by": str(created_by or "").strip(),
                "updated_by": str(created_by or "").strip(),
            }
        )
        return report_id
    except Exception as e:
        log.error("local_ops_store.record_local_report failed for %s: %s", cs or from_name, e)
        return None
    finally:
        conn.close()


def list_local_reports(
    *,
    limit: int = 200,
    callsign: str = "",
    topic: str = "",
    status: str = "",
    query: str = "",
) -> List[Dict[str, Any]]:
    ensure_tables()
    clauses: List[str] = []
    params: List[Any] = []
    cs = _norm_callsign(callsign)
    if cs:
        clauses.append("callsign=?")
        params.append(cs)
    if status:
        clauses.append("status=?")
        params.append(_norm_report_status(status))
    topic_value = _norm_topics([topic])
    if topic_value:
        clauses.append("topics_json LIKE ?")
        params.append(f"%{topic_value[0]}%")
    q = str(query or "").strip().lower()
    if q:
        clauses.append(
            "(LOWER(callsign) LIKE ? OR LOWER(from_name) LIKE ? OR LOWER(subject) LIKE ? OR LOWER(body) LIKE ? "
            "OR LOWER(city) LIKE ? OR LOWER(county) LIKE ? OR LOWER(state) LIKE ? OR LOWER(grid) LIKE ? "
            "OR LOWER(status) LIKE ? OR LOWER(source_kind) LIKE ? OR LOWER(source_channel) LIKE ? OR LOWER(topics_json) LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like] * 12)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    lim = max(1, int(limit or 200))
    params.append(lim)
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.execute(
            f"""
            SELECT
                id,
                created_utc,
                updated_utc,
                source_kind,
                source_channel,
                net_session_id,
                callsign,
                operator_id,
                from_name,
                city,
                county,
                state,
                grid,
                lat,
                lon,
                location_source,
                location_confidence,
                status,
                topics_json,
                topic_evidence_json,
                subject,
                body,
                confirmed_state,
                followup_state,
                exercise_flag,
                source_radio_id,
                source_app,
                raw_reference,
                created_by,
                updated_by
            FROM local_operator_reports
            {where}
            ORDER BY created_utc DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [_report_row_to_dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def list_reports_for_operator(callsign: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    return list_local_reports(callsign=callsign, limit=limit)


def delete_local_reports(report_ids: List[int]) -> int:
    ensure_tables()
    ids = sorted({int(report_id) for report_id in report_ids if int(report_id or 0) > 0})
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn = sqlite3.connect(_db_path())
    try:
        ref_rows = conn.execute(
            f"SELECT id, raw_reference FROM local_operator_reports WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        with conn:
            cur = conn.execute(
                f"DELETE FROM local_operator_reports WHERE id IN ({placeholders})",
                tuple(ids),
            )
        deleted = int(cur.rowcount or 0)
        refs = [row[1] or f"local_operator_reports:{int(row[0] or 0)}" for row in ref_rows]
        _delete_local_report_observations(refs)
        return deleted
    finally:
        conn.close()


def latest_report_summaries_for_callsigns(
    callsigns: List[str],
    *,
    per_call_limit: int = 20,
) -> Dict[str, Dict[str, Any]]:
    ensure_tables()
    normalized = sorted({_norm_callsign(cs) for cs in callsigns if _norm_callsign(cs)})
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    conn = sqlite3.connect(_db_path())
    try:
        rows = conn.execute(
            f"""
            SELECT
                id,
                created_utc,
                updated_utc,
                source_kind,
                source_channel,
                net_session_id,
                callsign,
                operator_id,
                from_name,
                city,
                county,
                state,
                grid,
                lat,
                lon,
                location_source,
                location_confidence,
                status,
                topics_json,
                topic_evidence_json,
                subject,
                body,
                confirmed_state,
                followup_state,
                exercise_flag,
                source_radio_id,
                source_app,
                raw_reference,
                created_by,
                updated_by
            FROM local_operator_reports
            WHERE callsign IN ({placeholders})
            ORDER BY callsign ASC, created_utc DESC, id DESC
            """,
            tuple(normalized),
        ).fetchall()
    finally:
        conn.close()

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for raw in rows:
        report = _report_row_to_dict(raw)
        cs = _norm_callsign(report.get("callsign", ""))
        if not cs:
            continue
        bucket = grouped.setdefault(cs, [])
        if len(bucket) < max(1, int(per_call_limit or 20)):
            bucket.append(report)

    out: Dict[str, Dict[str, Any]] = {}
    for cs, reports in grouped.items():
        if not reports:
            continue
        latest = reports[0]
        highest = max(
            reports,
            key=lambda item: (
                _report_status_rank(str(item.get("status", ""))),
                str(item.get("created_utc", "")),
            ),
        )
        highest_rank = _report_status_rank(str(highest.get("status", "")))
        latest_rank = _report_status_rank(str(latest.get("status", "")))
        chosen = highest if highest_rank > latest_rank else latest
        out[cs] = {
            "callsign": cs,
            "latest": latest,
            "highest": highest,
            "display": _format_report_summary(chosen),
            "latest_display": _format_report_summary(latest),
            "highest_display": _format_report_summary(highest),
            "count": len(reports),
            "topics": sorted({topic for report in reports for topic in report.get("topics", [])}),
        }
    return out


def _report_status_to_sitrep(status: str) -> str:
    normalized = _norm_report_status(status)
    if normalized == "EMERGENCY":
        return "RED"
    if normalized in {"WATCH", "PRIORITY"}:
        return "YELLOW"
    return "GREEN"


def _report_status_rank(status: str) -> int:
    return {"INFO": 0, "WATCH": 1, "PRIORITY": 2, "EMERGENCY": 3}.get(_norm_report_status(status), 0)


def _format_report_summary(report: Dict[str, Any]) -> str:
    status = _norm_report_status(str(report.get("status", "")))
    subject = str(report.get("subject", "")).strip()
    body = str(report.get("body", "")).strip()
    topics = [str(topic).strip() for topic in report.get("topics", []) if str(topic).strip()]
    topic_text = ", ".join(topics[:2])
    if len(topics) > 2:
        topic_text += f" +{len(topics) - 2}"
    title = subject or body[:60].strip()
    parts = [status]
    if topic_text:
        parts.append(topic_text)
    if title:
        parts.append(title)
    return " | ".join(parts)


def _mirror_local_report_to_observation(report: Dict[str, Any]) -> None:
    try:
        from freqinout.core.observation_projection import observation_from_local_report
        from freqinout.core.observation_store import upsert_observation

        upsert_observation(_db_path(), observation_from_local_report(report))
    except Exception as exc:
        log.debug("local_ops_store: observation projection mirror failed: %s", exc)


def _delete_local_report_observations(source_refs: List[str]) -> None:
    try:
        from freqinout.core.observation_store import delete_observations_by_source_refs

        delete_observations_by_source_refs(
            _db_path(),
            source_refs,
            source_family="local_report",
        )
    except Exception as exc:
        log.debug("local_ops_store: observation projection delete mirror failed: %s", exc)


def _report_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": int(row[0] or 0),
        "created_utc": row[1] or "",
        "updated_utc": row[2] or "",
        "source_kind": row[3] or "",
        "source_channel": row[4] or "",
        "net_session_id": row[5] or "",
        "callsign": row[6] or "",
        "operator_id": row[7] or "",
        "from_name": row[8] or "",
        "city": row[9] or "",
        "county": row[10] or "",
        "state": row[11] or "",
        "grid": row[12] or "",
        "lat": row[13],
        "lon": row[14],
        "location_source": row[15] or "",
        "location_confidence": row[16] or "",
        "status": _norm_report_status(row[17] or "INFO"),
        "topics": _json_loads(row[18], []),
        "topic_evidence": _json_loads(row[19], {}),
        "subject": row[20] or "",
        "body": row[21] or "",
        "confirmed_state": _norm_confirmed_state(row[22] or "UNCONFIRMED"),
        "followup_state": row[23] or "",
        "exercise_flag": bool(row[24]),
        "source_radio_id": row[25],
        "source_app": row[26] or "",
        "raw_reference": row[27] or "",
        "created_by": row[28] or "",
        "updated_by": row[29] or "",
    }
