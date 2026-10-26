from __future__ import annotations

import datetime
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from freqinout.core.config_paths import get_config_dir
from freqinout.core.logger import log


CALLSIGN_RE = re.compile(r"^[A-Z0-9/]{3,12}$")
TRAILING_CALL_NOISE_RE = re.compile(r"[^A-Z0-9/]+$")
PORTABLE_SUFFIX_RE = re.compile(r"/(P|M|MM|QRP|SOTA|ROVER|[A-Z0-9]{1,4})$")
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
SECONDS_PER_WEEK = 7 * 24 * 60 * 60


def infer_peer_schedules(
    settings,
    *,
    lookback_days: int = 56,
    bucket_minutes: int = 15,
) -> Dict[str, int]:
    """
    Infer recurring peer schedules from observed traffic.

    Imported peer schedules always take precedence over inferred rows.
    """
    stats = {
        "rows_scanned": 0,
        "candidate_buckets": 0,
        "rows_inferred": 0,
        "callsigns_inferred": 0,
        "errors": 0,
    }
    if not _is_enabled(settings, "peer_schedule_infer_enabled", True):
        return stats
    db_path = _nets_db_path()
    if not db_path.exists():
        return stats

    now_ts = float(time.time())
    lookback_days = max(7, int(lookback_days or 56))
    bucket_minutes = max(5, min(60, int(bucket_minutes or 15)))
    since_ts = now_ts - (lookback_days * 24 * 60 * 60)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _ensure_inferred_tables(conn)
        explicit_calls = _load_explicit_callsigns(conn)
        my_call = _clean_call(str(settings.get("operator_callsign", "") or ""))
        bucket_map, rows_scanned = _collect_buckets(
            conn,
            since_ts=since_ts,
            bucket_minutes=bucket_minutes,
            explicit_calls=explicit_calls,
            my_call=my_call,
        )
        stats["rows_scanned"] = int(rows_scanned)
        if not bucket_map:
            _replace_inferred_rows(conn, [])
            conn.close()
            return stats
        stats["candidate_buckets"] = len(bucket_map)
        inferred_rows = _build_inferred_rows(
            bucket_map,
            bucket_minutes=bucket_minutes,
            min_week_hit_rate=0.50,
            min_bucket_hits=2,
            min_window_hits=4,
            min_confidence=0.55,
            min_observed_weeks=2,
            updated_ts=now_ts,
        )
        _replace_inferred_rows(conn, inferred_rows)
        conn.close()
        stats["rows_inferred"] = len(inferred_rows)
        stats["callsigns_inferred"] = len({r[0] for r in inferred_rows})
    except Exception as e:
        stats["errors"] = 1
        log.debug("PeerScheduleInfer: inference failed: %s", e)
    return stats


def _is_enabled(settings, key: str, default: bool) -> bool:
    try:
        raw = settings.get(key, default)
    except Exception:
        raw = default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    txt = str(raw or "").strip().lower()
    if txt in {"1", "true", "yes", "on", "enabled"}:
        return True
    if txt in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _nets_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout_nets.db"


def _ensure_inferred_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS peer_hf_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_callsign TEXT NOT NULL,
            day_utc TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            end_utc TEXT NOT NULL,
            band TEXT NOT NULL,
            mode TEXT NOT NULL,
            frequency TEXT NOT NULL,
            meta_json TEXT,
            imported_at TEXT,
            source_type TEXT DEFAULT 'IMPORTED',
            updated_at TEXT
        )
        """
    )
    cur.execute("PRAGMA table_info(peer_hf_schedule)")
    explicit_cols = {row[1] for row in cur.fetchall()}
    if "source_type" not in explicit_cols:
        cur.execute("ALTER TABLE peer_hf_schedule ADD COLUMN source_type TEXT DEFAULT 'IMPORTED'")
    if "updated_at" not in explicit_cols:
        cur.execute("ALTER TABLE peer_hf_schedule ADD COLUMN updated_at TEXT")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS peer_hf_schedule_inferred (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_callsign TEXT NOT NULL,
            day_utc TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            end_utc TEXT NOT NULL,
            band TEXT NOT NULL,
            mode TEXT NOT NULL,
            frequency TEXT NOT NULL,
            source TEXT,
            confidence REAL DEFAULT 0,
            sample_count INTEGER DEFAULT 0,
            weeks_seen INTEGER DEFAULT 0,
            weeks_observed INTEGER DEFAULT 0,
            last_seen_ts REAL,
            updated_ts REAL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_peer_hf_inferred_owner
        ON peer_hf_schedule_inferred(owner_callsign)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_peer_hf_inferred_owner_day
        ON peer_hf_schedule_inferred(owner_callsign, day_utc, start_utc, end_utc)
        """
    )
    view_sql = """
        CREATE VIEW IF NOT EXISTS peer_hf_schedule_effective AS
        SELECT
            owner_callsign,
            day_utc,
            start_utc,
            end_utc,
            band,
            mode,
            frequency,
            COALESCE(NULLIF(TRIM(source_type), ''), 'IMPORTED') AS source_type,
            NULL AS confidence,
            NULL AS sample_count,
            NULL AS weeks_seen,
            NULL AS weeks_observed,
            NULL AS last_seen_ts
        FROM peer_hf_schedule
        UNION ALL
        SELECT
            i.owner_callsign,
            i.day_utc,
            i.start_utc,
            i.end_utc,
            i.band,
            i.mode,
            i.frequency,
            'INFERRED' AS source_type,
            i.confidence,
            i.sample_count,
            i.weeks_seen,
            i.weeks_observed,
            i.last_seen_ts
        FROM peer_hf_schedule_inferred i
        WHERE NOT EXISTS (
            SELECT 1
            FROM peer_hf_schedule e
            WHERE UPPER(TRIM(e.owner_callsign)) = UPPER(TRIM(i.owner_callsign))
        )
        """
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='peer_hf_schedule_effective'"
    ).fetchone()
    def normalized_sql(value: str) -> str:
        return "".join(value.casefold().split()).replace("ifnotexists", "")
    if existing is None or normalized_sql(str(existing[0] or "")) != normalized_sql(view_sql):
        cur.execute("DROP VIEW IF EXISTS peer_hf_schedule_effective")
        cur.execute(view_sql)


def _load_explicit_callsigns(conn: sqlite3.Connection) -> Set[str]:
    out: Set[str] = set()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT UPPER(TRIM(owner_callsign))
            FROM peer_hf_schedule
            WHERE owner_callsign IS NOT NULL
              AND TRIM(owner_callsign) <> ''
            """
        )
        for (val,) in cur.fetchall():
            call = _clean_call(val)
            if call:
                out.add(call)
    except Exception:
        pass
    return out


def _clean_call(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    # Some log tokens include trailing punctuation (e.g. "K0ABC>").
    raw = TRAILING_CALL_NOISE_RE.sub("", raw)
    if not raw:
        return ""
    return PORTABLE_SUFFIX_RE.sub("", raw)


def _valid_call(value: str) -> bool:
    call = _clean_call(value)
    if not call or call.startswith("@"):
        return False
    if call in {"ALLCALL", "CQCQCQ", "CQ"}:
        return False
    return bool(CALLSIGN_RE.fullmatch(call))


def _freq_mhz(value: object) -> Optional[float]:
    try:
        f = float(value or 0.0)
    except Exception:
        return None
    if f <= 0:
        return None
    # Stored frequency is normally Hz in these tables. Keep a safe fallback for MHz.
    if f > 1_000:
        f = f / 1_000_000.0
    if f <= 0:
        return None
    return float(f)


def _band_from_mhz(freq_mhz: Optional[float]) -> str:
    if not freq_mhz:
        return ""
    bands = [
        ("160M", 1.8, 2.0),
        ("80M", 3.5, 4.0),
        ("60M", 5.0, 5.5),
        ("40M", 7.0, 7.3),
        ("30M", 10.1, 10.15),
        ("20M", 14.0, 14.35),
        ("17M", 18.068, 18.168),
        ("15M", 21.0, 21.45),
        ("12M", 24.89, 24.99),
        ("10M", 28.0, 29.7),
        ("6M", 50.0, 54.0),
        ("2M", 144.0, 148.0),
    ]
    for name, lo, hi in bands:
        if lo <= freq_mhz <= hi:
            return name
    return ""


def _collect_buckets(
    conn: sqlite3.Connection,
    *,
    since_ts: float,
    bucket_minutes: int,
    explicit_calls: Set[str],
    my_call: str,
) -> Tuple[Dict[Tuple[str, str, str, str, int, int], Dict[str, object]], int]:
    bucket_map: Dict[Tuple[str, str, str, str, int, int], Dict[str, object]] = {}
    rows_scanned = 0
    rows_scanned += _scan_link_table(
        conn,
        table_name="js8_links",
        since_ts=since_ts,
        source_name="JS8",
        mode_name="Digi",
        bucket_minutes=bucket_minutes,
        explicit_calls=explicit_calls,
        my_call=my_call,
        bucket_map=bucket_map,
    )
    rows_scanned += _scan_link_table(
        conn,
        table_name="varac_links",
        since_ts=since_ts,
        source_name="VarAC",
        mode_name="Digi",
        bucket_minutes=bucket_minutes,
        explicit_calls=explicit_calls,
        my_call=my_call,
        bucket_map=bucket_map,
    )
    return bucket_map, rows_scanned


def _scan_link_table(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    since_ts: float,
    source_name: str,
    mode_name: str,
    bucket_minutes: int,
    explicit_calls: Set[str],
    my_call: str,
    bucket_map: Dict[Tuple[str, str, str, str, int, int], Dict[str, object]],
) -> int:
    rows = []
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT ts, origin, destination, band, freq_hz
            FROM {table_name}
            WHERE ts >= ?
            ORDER BY ts ASC
            """,
            (float(since_ts),),
        )
        rows = cur.fetchall()
    except Exception:
        return 0
    for ts, origin, destination, band, freq_hz in rows:
        try:
            ts_val = float(ts or 0.0)
        except Exception:
            continue
        if ts_val <= 0:
            continue
        freq = _freq_mhz(freq_hz)
        if freq is None:
            continue
        freq_text = f"{round(freq, 3):.3f}"
        dt_utc = datetime.datetime.utcfromtimestamp(ts_val)
        day_idx = (dt_utc.weekday() + 1) % 7
        minute = dt_utc.hour * 60 + dt_utc.minute
        bucket_idx = int(minute // bucket_minutes)
        week_idx = int(ts_val // SECONDS_PER_WEEK)
        band_txt = (str(band or "").strip().upper() or _band_from_mhz(freq) or "")
        participants = {str(origin or "").strip().upper(), str(destination or "").strip().upper()}
        for raw_call in participants:
            call = _clean_call(raw_call)
            if not _valid_call(call):
                continue
            if call in explicit_calls:
                continue
            if my_call and call == my_call:
                continue
            key = (call, mode_name, freq_text, band_txt, day_idx, bucket_idx)
            slot = bucket_map.get(key)
            if slot is None:
                slot = {
                    "hits": 0,
                    "weeks": set(),
                    "last_seen_ts": 0.0,
                    "sources": set(),
                    # Preserve observed raw variants in memory for future diagnostics.
                    "raw_calls": set(),
                }
                bucket_map[key] = slot
            slot["hits"] = int(slot.get("hits", 0)) + 1
            weeks_set = slot.get("weeks")
            if isinstance(weeks_set, set):
                weeks_set.add(week_idx)
            else:
                slot["weeks"] = {week_idx}
            if ts_val > float(slot.get("last_seen_ts", 0.0) or 0.0):
                slot["last_seen_ts"] = ts_val
            src_set = slot.get("sources")
            if isinstance(src_set, set):
                src_set.add(source_name)
            else:
                slot["sources"] = {source_name}
            raw_set = slot.get("raw_calls")
            if isinstance(raw_set, set):
                raw_norm = str(raw_call or "").strip().upper()
                if raw_norm:
                    raw_set.add(raw_norm)
            else:
                slot["raw_calls"] = set()
    return len(rows)


def _hhmm_from_minute(minute: int) -> str:
    m = max(0, min(24 * 60, int(minute)))
    if m >= 24 * 60:
        m = 23 * 60 + 59
    return f"{m // 60:02d}:{m % 60:02d}"


def _source_label(sources: Iterable[str]) -> str:
    normalized = sorted({str(s or "").strip().upper() for s in sources if str(s or "").strip()})
    if not normalized:
        return "INFERRED"
    if len(normalized) == 1:
        return f"INFERRED:{normalized[0]}"
    return "INFERRED:MIXED"


def _build_inferred_rows(
    bucket_map: Dict[Tuple[str, str, str, str, int, int], Dict[str, object]],
    *,
    bucket_minutes: int,
    min_week_hit_rate: float,
    min_bucket_hits: int,
    min_window_hits: int,
    min_confidence: float,
    min_observed_weeks: int,
    updated_ts: float,
) -> List[Tuple[object, ...]]:
    grouped: Dict[Tuple[str, str, str, str, int], List[Tuple[int, Dict[str, object]]]] = defaultdict(list)
    observed_weeks_by_call: Dict[str, Set[int]] = defaultdict(set)
    for (call, mode, freq_text, band_txt, day_idx, bucket_idx), info in bucket_map.items():
        grouped[(call, mode, freq_text, band_txt, day_idx)].append((bucket_idx, info))
        weeks = info.get("weeks")
        if isinstance(weeks, set):
            observed_weeks_by_call[call].update({int(w) for w in weeks})

    out: List[Tuple[object, ...]] = []
    for (call, mode, freq_text, band_txt, day_idx), items in grouped.items():
        weeks_observed = len(observed_weeks_by_call.get(call, set()))
        if weeks_observed < max(1, int(min_observed_weeks or 1)):
            continue
        active: List[Tuple[int, Dict[str, object], float]] = []
        for bucket_idx, info in items:
            hits = int(info.get("hits", 0) or 0)
            weeks = info.get("weeks")
            week_set: Set[int] = set(weeks) if isinstance(weeks, set) else set()
            weeks_seen = len(week_set)
            rate = (weeks_seen / float(weeks_observed)) if weeks_observed > 0 else 0.0
            if hits < min_bucket_hits:
                continue
            if weeks_seen < 2:
                continue
            if rate < min_week_hit_rate:
                continue
            active.append((bucket_idx, info, rate))
        if not active:
            continue

        active.sort(key=lambda it: int(it[0]))
        segments: List[List[Tuple[int, Dict[str, object], float]]] = []
        for row in active:
            if not segments:
                segments.append([row])
                continue
            prev_idx = int(segments[-1][-1][0])
            cur_idx = int(row[0])
            if cur_idx <= prev_idx + 1:
                segments[-1].append(row)
            else:
                segments.append([row])

        for segment in segments:
            start_bucket = int(segment[0][0])
            end_bucket = int(segment[-1][0]) + 1
            start_min = start_bucket * bucket_minutes
            end_min = min(24 * 60, end_bucket * bucket_minutes)
            if end_min <= start_min:
                continue
            sample_count = sum(int(seg_info.get("hits", 0) or 0) for _, seg_info, _ in segment)
            if sample_count < min_window_hits:
                continue
            week_union: Set[int] = set()
            src_union: Set[str] = set()
            last_seen_ts = 0.0
            rate_sum = 0.0
            for _, seg_info, seg_rate in segment:
                seg_weeks = seg_info.get("weeks")
                if isinstance(seg_weeks, set):
                    week_union.update(seg_weeks)
                seg_sources = seg_info.get("sources")
                if isinstance(seg_sources, set):
                    src_union.update({str(s) for s in seg_sources})
                ts_val = float(seg_info.get("last_seen_ts", 0.0) or 0.0)
                if ts_val > last_seen_ts:
                    last_seen_ts = ts_val
                rate_sum += float(seg_rate)
            weeks_seen = len(week_union)
            if weeks_seen < 2:
                continue
            hit_rate = (weeks_seen / float(weeks_observed)) if weeks_observed > 0 else 0.0
            avg_bucket_rate = rate_sum / max(1, len(segment))
            confidence = min(0.99, (0.70 * hit_rate) + (0.30 * avg_bucket_rate))
            if confidence < min_confidence:
                continue
            source_txt = _source_label(src_union)
            day_txt = DAY_NAMES[int(day_idx) % 7]
            out.append(
                (
                    call,
                    day_txt,
                    _hhmm_from_minute(start_min),
                    _hhmm_from_minute(end_min),
                    band_txt or _band_from_mhz(float(freq_text)),
                    mode,
                    freq_text,
                    source_txt,
                    round(float(confidence), 4),
                    int(sample_count),
                    int(weeks_seen),
                    int(weeks_observed),
                    float(last_seen_ts),
                    float(updated_ts),
                )
            )
    return out


def _replace_inferred_rows(conn: sqlite3.Connection, rows: List[Tuple[object, ...]]) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM peer_hf_schedule_inferred")
    if rows:
        cur.executemany(
            """
            INSERT INTO peer_hf_schedule_inferred (
                owner_callsign,
                day_utc,
                start_utc,
                end_utc,
                band,
                mode,
                frequency,
                source,
                confidence,
                sample_count,
                weeks_seen,
                weeks_observed,
                last_seen_ts,
                updated_ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
