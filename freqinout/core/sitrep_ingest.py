from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from freqinout.core.commstat_brevity import brevity_metadata, cached_catalogs, analyze_brevity_codes
from freqinout.core.commstat_sitrep import (
    commstat_origin_path,
    commstat_reach_mode,
    decode_brevity_summary,
    extract_brevity_code,
    infer_state_and_geo,
    parse_commstat_message,
    report_group_for_target,
    transport_mode_for_source,
)
from freqinout.core.commstat_artifacts import (
    KIND_ALERT,
    KIND_MESSAGE,
    KIND_STATREP,
    build_alert_artifact_key,
    build_message_artifact_key,
    build_statrep_artifact_key,
    ensure_commstat_artifact_tables,
    normalize_status_label,
    upsert_commstat_artifact,
)
from freqinout.core.config_paths import get_config_dir
from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.core.group_utils import normalize_group_name
from freqinout.core.js8_spotter_forms import (
    MAPPER_SETTINGS_KEY,
    extract_form_codes,
    forms_enabled_for,
    normalize_form_code,
)
from freqinout.core.js8_spotter_decode import parse_spotter_bracket_fields
from freqinout.core.logger import log


SPOTTER_SITREP_FORMS = {
    "F!104": "SPOTTER_104",
    "F!301": "SPOTTER_301",
    "F!304": "SPOTTER_304",
    "F!701B": "SPOTTER_701B",
    "F!701C": "SPOTTER_701C",
}

_INGEST_LOCK = threading.Lock()
_LAST_RUN_MONO = 0.0
_LAST_RUN_MONO_BY_SCOPE: Dict[str, float] = {}
_MIN_INGEST_INTERVAL_SECONDS = 10.0


def ingest_sitreps(settings, *, max_rows_per_source: int = 500, ingest_scope_key: str = "") -> Dict[str, int]:
    """
    Incrementally ingest SitRep-capable source data into local staging tables.
    This phase is additive only (raw source staging + checkpoints).
    """
    if not _is_enabled(settings, "sitrep_unified_ingest_enabled", True):
        return {
            "sources_attempted": 0,
            "sources_ok": 0,
            "rows_scanned": 0,
            "events_inserted": 0,
            "errors": 0,
        }

    global _LAST_RUN_MONO
    scope_key = str(ingest_scope_key or "legacy").strip() or "legacy"
    now_mono = time.monotonic()
    last_run = _LAST_RUN_MONO_BY_SCOPE.get(scope_key, 0.0)
    if now_mono - last_run < _MIN_INGEST_INTERVAL_SECONDS:
        return {
            "sources_attempted": 0,
            "sources_ok": 0,
            "rows_scanned": 0,
            "events_inserted": 0,
            "errors": 0,
        }

    if not _INGEST_LOCK.acquire(blocking=False):
        return {
            "sources_attempted": 0,
            "sources_ok": 0,
            "rows_scanned": 0,
            "events_inserted": 0,
            "errors": 0,
        }

    try:
        _LAST_RUN_MONO = now_mono
        _LAST_RUN_MONO_BY_SCOPE[scope_key] = now_mono
        local_db = _local_db_path()
        try:
            local_db.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        stats = {
            "sources_attempted": 0,
            "sources_ok": 0,
            "rows_scanned": 0,
            "events_inserted": 0,
            "errors": 0,
        }
        conn = sqlite3.connect(local_db)
        try:
            _ensure_local_tables(conn)
            conn.commit()

            if _is_enabled(settings, "sitrep_ingest_local_spotter_backfill_enabled", True):
                if not _is_enabled(settings, "sitrep_local_spotter_backfill_done", False):
                    stats["sources_attempted"] += 1
                    _merge_stats(
                        stats,
                        _ingest_local_spotter_backfill(
                            conn,
                            settings,
                            str(local_db),
                            max_rows=max(int(max_rows_per_source), 500),
                        ),
                    )
                    stats["sources_ok"] += 1

            if _is_enabled(settings, "sitrep_ingest_imported_js8spotter_archive_enabled", True):
                stats["sources_attempted"] += 1
                archive_stats = _ingest_imported_js8spotter_archive(
                    conn,
                    str(local_db),
                    max_rows=max_rows_per_source,
                )
                _merge_stats(stats, archive_stats)
                if int(archive_stats.get("errors", 0) or 0) <= 0:
                    stats["sources_ok"] += 1

            if _is_enabled(settings, "sitrep_ingest_js8spotter_enabled", True):
                stats["sources_attempted"] += 1
                path = _resolve_js8spotter_db_path(settings)
                if path:
                    _merge_stats(stats, _ingest_js8spotter(conn, path, settings=settings, max_rows=max_rows_per_source))
                    stats["sources_ok"] += 1

            if _is_enabled(settings, "sitrep_ingest_commstat3_enabled", True):
                stats["sources_attempted"] += 1
                path = _resolve_commstat_db_path(settings, prefer_v3=True)
                if path:
                    _merge_stats(stats, _ingest_commstat3(conn, path, max_rows=max_rows_per_source))
                    stats["sources_ok"] += 1

            if _is_enabled(settings, "sitrep_ingest_commstat23_enabled", True):
                stats["sources_attempted"] += 1
                path = _resolve_commstat_db_path(settings, prefer_v3=False)
                if path:
                    _merge_stats(stats, _ingest_commstat23(conn, path, max_rows=max_rows_per_source))
                    stats["sources_ok"] += 1
        finally:
            conn.commit()
            conn.close()
        return stats
    finally:
        _INGEST_LOCK.release()


def _local_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout_nets.db"


def _is_enabled(settings, key: str, default: bool) -> bool:
    try:
        val = settings.get(key, default)
    except Exception:
        val = default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    txt = str(val or "").strip().lower()
    if txt in {"1", "true", "yes", "on", "enabled"}:
        return True
    if txt in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _merge_stats(target: Dict[str, int], delta: Dict[str, int]) -> None:
    for key in ("rows_scanned", "events_inserted", "errors"):
        target[key] = int(target.get(key, 0)) + int(delta.get(key, 0))


def _ensure_local_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_ingest_checkpoint (
            source_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_db_path TEXT,
            last_id INTEGER NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL,
            last_error TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_source_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_db_path TEXT,
            source_id INTEGER NOT NULL,
            subtype TEXT NOT NULL,
            from_call TEXT,
            target TEXT,
            report_group TEXT,
            grid TEXT,
            scope TEXT,
            transport_mode TEXT,
            reach_mode TEXT,
            origin_path TEXT,
            remarks_text TEXT,
            brevity_code TEXT,
            brevity_summary TEXT,
            state_code TEXT,
            state_confidence TEXT,
            geo_confidence TEXT,
            status_payload TEXT,
            raw_payload TEXT,
            event_ts REAL,
            event_ts_utc TEXT,
            ingested_ts REAL NOT NULL,
            UNIQUE(source, source_table, source_db_path, source_id)
        )
        """
    )
    _ensure_columns(
        conn,
        "sitrep_source_events",
        {
            "report_group": "TEXT",
            "transport_mode": "TEXT",
            "reach_mode": "TEXT",
            "origin_path": "TEXT",
            "remarks_text": "TEXT",
            "brevity_code": "TEXT",
            "brevity_summary": "TEXT",
            "state_code": "TEXT",
            "state_confidence": "TEXT",
            "geo_confidence": "TEXT",
        },
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_source_events_recent
            ON sitrep_source_events(source, event_ts DESC, id DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_source_events_call_recent
            ON sitrep_source_events(from_call, event_ts DESC, id DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_checkpoint_updated
            ON sitrep_ingest_checkpoint(updated_ts)
        """
    )
    ensure_commstat_artifact_tables(conn)


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {str(row[1] or "").strip().lower() for row in cur.fetchall()}
    for name, ddl in columns.items():
        if str(name).strip().lower() in existing:
            continue
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _resolve_js8spotter_db_path(settings) -> Optional[Path]:
    candidates = []
    explicit = (settings.get("js8spotter_db_path", "") or "").strip()
    launch = (settings.get("path_js8spotter", "") or "").strip()
    forms_path = (settings.get("js8_forms_path", "") or "").strip()
    candidates.extend(_candidate_db_paths(explicit, "js8spotter.db"))
    candidates.extend(_candidate_db_paths(launch, "js8spotter.db"))
    if forms_path:
        try:
            fp = Path(forms_path)
            # MCF forms folder is usually <install>/forms for external JS8Spotter.
            candidates.extend(_candidate_db_paths(str(fp.parent), "js8spotter.db"))
        except Exception:
            pass
    return _pick_existing_path(candidates)


def _resolve_commstat_db_path(settings, *, prefer_v3: bool) -> Optional[Path]:
    candidates = []
    common = (settings.get("commstat_db_path", "") or "").strip()
    v3 = (settings.get("commstat3_db_path", "") or "").strip()
    v23 = (settings.get("commstat23_db_path", "") or "").strip()
    launch = (settings.get("path_commstat", "") or "").strip()
    multi_rig_launch = (settings.get("commstat_launch_path", "") or "").strip()

    if prefer_v3:
        for raw in (v3, common, launch, multi_rig_launch):
            candidates.extend(_candidate_db_paths(raw, "traffic.db3"))
        # Ignore template DB when looking for live data.
    else:
        for raw in (v23, common, launch, multi_rig_launch):
            candidates.extend(_candidate_db_paths(raw, "traffic.db3"))

    path = _pick_existing_path(candidates)
    if not path:
        return None
    log.info("SitrepIngest: resolved CommStat traffic DB: %s", path)
    return path


def _candidate_db_paths(raw: str, default_db_name: str) -> Iterable[Path]:
    txt = (raw or "").strip()
    if not txt:
        return []
    out = []
    try:
        p = Path(txt)
    except Exception:
        return out

    if p.is_file():
        suffix = p.suffix.lower()
        if suffix in {".db", ".db3", ".sqlite", ".sqlite3"}:
            out.append(p)
        out.append(p.parent / default_db_name)
        return out
    if p.is_dir():
        out.append(p / default_db_name)
        return out
    # Raw may point to a file that does not exist yet.
    if p.suffix:
        suffix = p.suffix.lower()
        if suffix in {".db", ".db3", ".sqlite", ".sqlite3"}:
            out.append(p)
        out.append(p.parent / default_db_name)
    else:
        out.append(p / default_db_name)
    return out


def _pick_existing_path(candidates: Iterable[Path]) -> Optional[Path]:
    seen = set()
    for c in candidates:
        try:
            resolved = c.resolve()
        except Exception:
            resolved = c
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if resolved.exists() and resolved.is_file():
                return resolved
        except Exception:
            continue
    return None


def _open_source_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.35)
    try:
        conn.execute("PRAGMA busy_timeout=350")
    except Exception:
        pass
    return conn


def _record_source_db_success(key: str, source_db: Path, started: float) -> None:
    try:
        get_dependency_health_registry().record_success(
            key,
            owner="SitrepIngest",
            duration_ms=(time.monotonic() - started) * 1000.0,
            slow_ms=500.0,
            metadata={"path": str(source_db)},
        )
    except Exception:
        pass


def _record_source_db_failure(key: str, source_db: Path, started: float, error: object) -> None:
    try:
        get_dependency_health_registry().record_failure(
            key,
            owner="SitrepIngest",
            error=f"data unavailable: {error}",
            duration_ms=(time.monotonic() - started) * 1000.0,
            cooldown_sec=30.0,
            metadata={"path": str(source_db)},
        )
    except Exception:
        pass


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1] or "").strip().lower() for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _checkpoint_key(source: str, table: str) -> str:
    return f"{source}:{table}"


def _get_last_id(local_conn: sqlite3.Connection, source: str, table: str, source_db_path: str) -> int:
    key = _checkpoint_key(source, table)
    cur = local_conn.cursor()
    cur.execute(
        """
        SELECT last_id, source_db_path
        FROM sitrep_ingest_checkpoint
        WHERE source_key=?
        """,
        (key,),
    )
    row = cur.fetchone()
    if not row:
        return 0
    last_id = int(row[0] or 0)
    prev_path = str(row[1] or "")
    if prev_path and prev_path != source_db_path:
        return 0
    return max(last_id, 0)


def _set_last_id(
    local_conn: sqlite3.Connection,
    source: str,
    table: str,
    source_db_path: str,
    last_id: int,
    *,
    error_text: str = "",
) -> None:
    key = _checkpoint_key(source, table)
    now_ts = float(time.time())
    local_conn.execute(
        """
        INSERT INTO sitrep_ingest_checkpoint
            (source_key, source, source_table, source_db_path, last_id, updated_ts, last_error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            source=excluded.source,
            source_table=excluded.source_table,
            source_db_path=excluded.source_db_path,
            last_id=excluded.last_id,
            updated_ts=excluded.updated_ts,
            last_error=excluded.last_error
        """,
        (
            key,
            source,
            table,
            source_db_path,
            int(last_id),
            now_ts,
            (error_text or "").strip(),
        ),
    )


def _insert_source_event(
    local_conn: sqlite3.Connection,
    *,
    source: str,
    source_table: str,
    source_db_path: str,
    source_id: int,
    subtype: str,
    from_call: str,
    target: str,
    grid: str,
    scope: str,
    report_group: str = "",
    transport_mode: str = "",
    reach_mode: str = "",
    origin_path: str = "",
    remarks_text: str = "",
    brevity_code: str = "",
    brevity_summary: str = "",
    state_code: str = "",
    state_confidence: str = "",
    geo_confidence: str = "",
    status_payload: Dict,
    raw_payload: Dict,
    event_ts: float,
    event_ts_utc: str,
) -> bool:
    if source.startswith("COMMSTAT"):
        asset_dir = raw_payload.get("brevity_asset_dir") or (Path(source_db_path).parent if source_db_path else None)
        raw_payload = {**raw_payload, **brevity_metadata(remarks_text, asset_dir)}
    cur = local_conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO sitrep_source_events
            (source, source_table, source_db_path, source_id, subtype, from_call, target, report_group, grid, scope,
             transport_mode, reach_mode, origin_path, remarks_text, brevity_code, brevity_summary, state_code, state_confidence, geo_confidence,
             status_payload, raw_payload, event_ts, event_ts_utc, ingested_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            source_table,
            source_db_path,
            int(source_id),
            subtype,
            _clean_call(from_call),
            (target or "").strip().upper(),
            normalize_group_name(report_group),
            (grid or "").strip().upper(),
            (scope or "").strip(),
            (transport_mode or "").strip().lower(),
            (reach_mode or "").strip().lower(),
            (origin_path or "").strip().lower(),
            (remarks_text or "").strip(),
            (brevity_code or "").strip().upper(),
            (brevity_summary or "").strip(),
            (state_code or "").strip().upper(),
            (state_confidence or "").strip().lower(),
            (geo_confidence or "").strip().lower(),
            json.dumps(status_payload or {}, separators=(",", ":"), ensure_ascii=True),
            json.dumps(raw_payload or {}, separators=(",", ":"), ensure_ascii=True),
            float(event_ts or 0.0),
            (event_ts_utc or "").strip(),
            float(time.time()),
        ),
    )
    return bool(cur.rowcount)


def _clean_call(val: str) -> str:
    return (val or "").strip().upper()


def _parse_ts(value, fallback: str = "") -> Tuple[float, str]:
    text = str(value or fallback or "").strip()
    if not text:
        return 0.0, ""
    if text.isdigit():
        try:
            ts = float(text)
            if ts > 1e12:
                ts = ts / 1000.0
            dt = datetime.datetime.utcfromtimestamp(ts)
            return ts, dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0.0, ""
    normalized = text.replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(normalized, fmt)
            ts = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
            return ts, dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    try:
        dt = datetime.datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ts = dt.timestamp()
        return ts, dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0.0, ""


def _extract_form_id(typeid: str) -> str:
    return normalize_form_code(typeid)


def _custom_mapper_configured(settings) -> bool:
    try:
        return bool(settings is not None and settings.get(MAPPER_SETTINGS_KEY, []))
    except Exception:
        return False


def _mapped_sitrep_forms(settings) -> set[str]:
    # Only forms with an explicit, reviewed status contract enter status
    # fusion. Other mapped forms still participate in Messages/Map/Alerts.
    mapped = forms_enabled_for(settings, flag="status") & set(SPOTTER_SITREP_FORMS.keys())
    if mapped:
        return mapped
    if _custom_mapper_configured(settings):
        return set()
    return set(SPOTTER_SITREP_FORMS.keys())


def _spotter_sitrep_subtype(form_code: str) -> str:
    code = normalize_form_code(form_code)
    if not code:
        return ""
    return SPOTTER_SITREP_FORMS.get(code, f"SPOTTER_{code[2:]}")


def _is_sitrep_like_message(text: str, settings=None, mapped_forms: Optional[set[str]] = None) -> bool:
    upper = (text or "").upper()
    if not upper:
        return False
    mapped = mapped_forms if mapped_forms is not None else _mapped_sitrep_forms(settings)
    return (
        ("{&%}" in upper)
        or ("{&%3}" in upper)
        or ("{F%}" in upper)
        or ("{F%3}" in upper)
        or any(code in mapped for code in extract_form_codes(upper))
    )


def _parse_spotter_response(raw_text: str) -> str:
    parts = str(raw_text or "").strip().split()
    if len(parts) < 2:
        return ""
    first = str(parts[0] or "").strip().upper()
    if not first.startswith("F!"):
        return ""
    return str(parts[1] or "").strip()


def _commstat_asset_dir(source_db: Path) -> Optional[Path]:
    try:
        base = source_db.parent
    except Exception:
        return None
    if not base.exists():
        return None
    return base


def _commstat_metadata(
    *,
    target: str,
    grid: str,
    remarks_text: str,
    source_value: object,
    global_id: object = 0,
    raw_message: str = "",
    asset_dir: Optional[Path] = None,
) -> Dict[str, str]:
    state_code, state_confidence, geo_confidence = infer_state_and_geo(grid, remarks_text)
    brevity_code = extract_brevity_code(remarks_text)
    return {
        "report_group": report_group_for_target(target),
        "transport_mode": transport_mode_for_source(source_value, raw_message, global_id=global_id),
        "reach_mode": commstat_reach_mode(source_value, global_id=global_id, raw_message=raw_message),
        "origin_path": commstat_origin_path(source_value),
        "remarks_text": str(remarks_text or "").strip(),
        "brevity_code": brevity_code,
        **brevity_metadata(remarks_text, asset_dir),
        "state_code": state_code,
        "state_confidence": state_confidence,
        "geo_confidence": geo_confidence,
    }


def _status_code_char(value: object) -> str:
    txt = str(value or "").strip().upper()
    if txt in {"3", "R", "RED"}:
        return "R"
    if txt in {"2", "Y", "YELLOW"}:
        return "Y"
    if txt in {"1", "G", "GREEN"}:
        return "G"
    if txt in {"4", "5", "U", "UNKNOWN"}:
        return "U"
    return "N"


def _commstat_status_signature(status_payload: Dict) -> str:
    status_txt = str((status_payload or {}).get("status") or "").strip()
    if len(status_txt) >= 12:
        chars = [_status_code_char(ch) for ch in status_txt[:12]]
        return "".join(chars)
    dims = (
        "overall_status",
        "power",
        "water",
        "medical",
        "communications",
        "travel",
        "internet",
        "fuel",
        "food",
        "crime",
        "civil_unrest",
        "political",
    )
    return "".join(_status_code_char((status_payload or {}).get(dim)) for dim in dims)


def _commstat_status_label(status_payload: Dict) -> str:
    status_txt = str((status_payload or {}).get("status") or "").strip()
    if status_txt:
        return normalize_status_label(status_txt[:1])
    return normalize_status_label((status_payload or {}).get("overall_status"))


def _preview_title(text: str, *, fallback: str, limit: int = 60) -> str:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        normalized = str(fallback or "").strip()
    if len(normalized) > limit:
        return normalized[: limit - 3].rstrip() + "..."
    return normalized


def _parse_js8_relay_route(text: object) -> Dict[str, str]:
    match = re.match(
        r"^\s*(?P<origin>[A-Z0-9/]{3,12})\s*:\s*(?P<via>[A-Z0-9/]{3,12})>(?P<dest>[A-Z0-9/]{3,12})\b",
        str(text or "").strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return {}
    return {
        "relay_origin": match.group("origin").upper(),
        "relay_via": match.group("via").upper(),
        "relay_to": match.group("dest").upper(),
    }


def _upsert_commstat_statrep_artifact(
    local_conn: sqlite3.Connection,
    *,
    source: str,
    source_table: str,
    source_id: int,
    event_ts: float,
    event_ts_utc: str,
    from_call: str,
    target: str,
    report_group: str,
    grid: str,
    state_code: str,
    scope: str,
    transport_mode: str,
    status_payload: Dict,
    remarks_text: str,
    payload: Dict,
    reach_mode: str = "",
    origin_path: str = "",
    subtype: str = "COMMSTAT_12",
    external_ids: Iterable[str] | None = None,
) -> None:
    payload = {**payload, **brevity_metadata(remarks_text, payload.get("brevity_asset_dir"))}
    status_signature = _commstat_status_signature(status_payload)
    status_label = _commstat_status_label(status_payload)
    scope_txt = str(scope or "").strip()
    title_parts = ["CommStat StatRep"]
    if scope_txt:
        title_parts.append(scope_txt)
    if status_label and status_label != "NOT REPORTED":
        title_parts.append(status_label)
    title = " | ".join(title_parts)
    upsert_commstat_artifact(
        local_conn,
        artifact_key=build_statrep_artifact_key(
            from_call=from_call,
            target=target,
            grid=grid,
            scope=scope_txt,
            status_signature=status_signature,
            event_ts=event_ts,
        ),
        artifact_kind=KIND_STATREP,
        subtype=subtype,
        event_ts=event_ts,
        event_ts_utc=event_ts_utc,
        from_call=from_call,
        target=target,
        report_group=report_group,
        grid=grid,
        state_code=state_code,
        scope=scope_txt,
        transport_mode=transport_mode,
        reach_mode=reach_mode,
        origin_path=origin_path,
        status_label=status_label,
        title=title,
        body_text=str(remarks_text or "").strip(),
        remarks_text=str(remarks_text or "").strip(),
        source=source,
        source_ref=f"{source_table}:{int(source_id or 0)}",
        external_ids=external_ids or [],
        payload=payload,
    )


def _upsert_commstat_message_artifact(
    local_conn: sqlite3.Connection,
    *,
    source: str,
    source_table: str,
    source_id: int,
    event_ts: float,
    event_ts_utc: str,
    from_call: str,
    target: str,
    report_group: str,
    transport_mode: str,
    body_text: str,
    payload: Dict,
    reach_mode: str = "",
    origin_path: str = "",
    external_ids: Iterable[str] | None = None,
) -> None:
    body = str(body_text or "").strip()
    payload = {**payload, **brevity_metadata(body, payload.get("brevity_asset_dir"))}
    title = _preview_title(body, fallback="CommStat Message")
    upsert_commstat_artifact(
        local_conn,
        artifact_key=build_message_artifact_key(
            from_call=from_call,
            target=target,
            body_text=body,
            event_ts=event_ts,
        ),
        artifact_kind=KIND_MESSAGE,
        subtype="COMMSTAT_TEXT",
        event_ts=event_ts,
        event_ts_utc=event_ts_utc,
        from_call=from_call,
        target=target,
        report_group=report_group,
        transport_mode=transport_mode,
        reach_mode=reach_mode,
        origin_path=origin_path,
        status_label="INFO",
        title=title,
        body_text=body,
        source=source,
        source_ref=f"{source_table}:{int(source_id or 0)}",
        external_ids=external_ids or [],
        payload=payload,
    )


def _upsert_commstat_alert_artifact(
    local_conn: sqlite3.Connection,
    *,
    source: str,
    source_table: str,
    source_id: int,
    event_ts: float,
    event_ts_utc: str,
    from_call: str,
    target: str,
    report_group: str,
    transport_mode: str,
    alert_color: str,
    title: str,
    body_text: str,
    payload: Dict,
    reach_mode: str = "",
    origin_path: str = "",
    external_ids: Iterable[str] | None = None,
) -> None:
    color_txt = str(alert_color or "").strip().upper()
    title_txt = _preview_title(title, fallback="CommStat Alert")
    body = str(body_text or "").strip()
    visible_title = f"{color_txt} ALERT | {title_txt}" if color_txt else f"ALERT | {title_txt}"
    upsert_commstat_artifact(
        local_conn,
        artifact_key=build_alert_artifact_key(
            from_call=from_call,
            target=target,
            alert_color=color_txt,
            title=title_txt,
            body_text=body,
            event_ts=event_ts,
        ),
        artifact_kind=KIND_ALERT,
        subtype="COMMSTAT_ALERT",
        event_ts=event_ts,
        event_ts_utc=event_ts_utc,
        from_call=from_call,
        target=target,
        report_group=report_group,
        transport_mode=transport_mode,
        reach_mode=reach_mode,
        origin_path=origin_path,
        status_label=color_txt or "ALERT",
        alert_color=color_txt,
        title=visible_title,
        body_text=body,
        source=source,
        source_ref=f"{source_table}:{int(source_id or 0)}",
        external_ids=external_ids or [],
        payload=payload,
    )


def _ingest_local_spotter_backfill(
    local_conn: sqlite3.Connection,
    settings,
    source_db_path: str,
    *,
    max_rows: int,
) -> Dict[str, int]:
    out = {"rows_scanned": 0, "events_inserted": 0, "errors": 0}
    source = "FIO_LOCAL"
    table = "spotter_traffic"
    done_key = "sitrep_local_spotter_backfill_done"

    try:
        if _is_enabled(settings, done_key, False):
            return out
    except Exception:
        pass

    if not _table_exists(local_conn, table):
        try:
            settings.set(done_key, True)
        except Exception:
            pass
        return out

    last_id = _get_last_id(local_conn, source, table, source_db_path)
    cur = local_conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, utc_ts, utc_str, from_call, to_call, form_id, raw_text, decoded_text, spotter_token
            FROM spotter_traffic
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, int(max_rows)),
        )
        rows = cur.fetchall()
    except Exception as e:
        out["errors"] += 1
        _set_last_id(local_conn, source, table, source_db_path, last_id, error_text=str(e))
        return out

    if not rows:
        _set_last_id(local_conn, source, table, source_db_path, last_id)
        try:
            settings.set(done_key, True)
        except Exception:
            pass
        return out

    max_seen = last_id
    mapped_sitrep_forms = _mapped_sitrep_forms(settings)
    for row in rows:
        rid = int(row[0] or 0)
        max_seen = max(max_seen, rid)
        out["rows_scanned"] += 1
        form_id = _extract_form_id(str(row[5] or ""))
        subtype = _spotter_sitrep_subtype(form_id) if form_id in mapped_sitrep_forms else ""
        if not subtype:
            continue
        event_ts, event_ts_utc = _parse_ts(row[1], fallback=str(row[2] or ""))
        raw_text = str(row[6] or "")
        responses = _parse_spotter_response(raw_text)
        structured = parse_spotter_bracket_fields(raw_text)
        grid = str(structured.get("GR", "") or "").strip().upper()
        state_code, state_confidence, geo_confidence = infer_state_and_geo(grid, raw_text)
        inserted = _insert_source_event(
            local_conn,
            source=source,
            source_table=table,
            source_db_path=source_db_path,
            source_id=rid,
            subtype=subtype,
            from_call=str(row[3] or ""),
            target=str(row[4] or ""),
            grid=grid,
            scope="",
            state_code=state_code,
            state_confidence=state_confidence,
            geo_confidence=geo_confidence,
            status_payload={
                "form_id": form_id,
                "responses": responses,
            },
            raw_payload={
                "form_id": form_id,
                "raw_text": raw_text,
                "decoded_text": str(row[7] or ""),
                "spotter_token": str(row[8] or ""),
            },
            event_ts=event_ts,
            event_ts_utc=event_ts_utc,
        )
        if inserted:
            out["events_inserted"] += 1

    _set_last_id(local_conn, source, table, source_db_path, max_seen)
    if len(rows) < int(max_rows):
        try:
            settings.set(done_key, True)
        except Exception:
            pass
    return out


def _ingest_imported_js8spotter_archive(
    local_conn: sqlite3.Connection,
    source_db_path: str,
    *,
    max_rows: int,
) -> Dict[str, int]:
    out = {"rows_scanned": 0, "events_inserted": 0, "errors": 0}
    source = "JS8SPOTTER_IMPORT"
    table = "csstatrep"

    if not _table_exists(local_conn, "js8spotter_import_archive"):
        return out

    last_id = _get_last_id(local_conn, source, table, source_db_path)
    cur = local_conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, source_db, source_id, payload_json, imported_ts
            FROM js8spotter_import_archive
            WHERE id > ?
              AND lower(source_table)=?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, table, int(max_rows)),
        )
        rows = cur.fetchall()
    except Exception as e:
        out["errors"] += 1
        _set_last_id(local_conn, source, table, source_db_path, last_id, error_text=str(e))
        return out

    max_seen = last_id
    for archive_id, original_db, original_id, payload_json, imported_ts in rows:
        rid = int(archive_id or 0)
        max_seen = max(max_seen, rid)
        out["rows_scanned"] += 1
        try:
            payload = json.loads(str(payload_json or "{}"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        status_txt = str(payload.get("cssr_status", "") or "").strip()
        subtype = "COMMSTAT_12" if len(status_txt) >= 12 else "COMMSTAT_FWD"
        from_call = str(payload.get("cssr_from", "") or "")
        target = str(payload.get("cssr_group", "") or "")
        grid = str(payload.get("cssr_grid", "") or "")
        priority = str(payload.get("cssr_prio", "") or "")
        notes = str(payload.get("cssr_notes", "") or "")
        event_ts, event_ts_utc = _parse_ts(payload.get("cssr_timestamp"), fallback=str(imported_ts or ""))
        metadata = _commstat_metadata(
            target=target,
            grid=grid,
            remarks_text=notes,
            source_value=1,
            raw_message=notes,
        )
        raw_payload = {
            "source_db": str(original_db or ""),
            "source_id": str(original_id or ""),
            "cssr_from": from_call,
            "cssr_group": target,
            "cssr_grid": grid,
            "cssr_prio": priority,
            "cssr_msgid": str(payload.get("cssr_msgid", "") or ""),
            "cssr_status": status_txt,
            "cssr_notes": notes,
            "cssr_timestamp": str(payload.get("cssr_timestamp", "") or ""),
        }
        inserted = _insert_source_event(
            local_conn,
            source=source,
            source_table=table,
            source_db_path=source_db_path,
            source_id=rid,
            subtype=subtype,
            from_call=from_call,
            target=target,
            report_group=metadata["report_group"] or target,
            grid=grid,
            scope=priority,
            transport_mode=metadata["transport_mode"] or "js8",
            remarks_text=metadata["remarks_text"],
            brevity_code=metadata["brevity_code"],
            brevity_summary=metadata["brevity_summary"],
            state_code=metadata["state_code"],
            state_confidence=metadata["state_confidence"],
            geo_confidence=metadata["geo_confidence"],
            status_payload={
                "status": status_txt,
                "priority": priority,
            },
            raw_payload=raw_payload,
            event_ts=event_ts,
            event_ts_utc=event_ts_utc,
        )
        if inserted:
            out["events_inserted"] += 1
        _upsert_commstat_statrep_artifact(
            local_conn,
            source=source,
            source_table=table,
            source_id=rid,
            event_ts=event_ts,
            event_ts_utc=event_ts_utc,
            from_call=from_call,
            target=target,
            report_group=metadata["report_group"] or target,
            grid=grid,
            state_code=metadata["state_code"],
            scope=priority,
            transport_mode=metadata["transport_mode"] or "js8",
            status_payload={
                "status": status_txt,
                "priority": priority,
            },
            remarks_text=notes,
            external_ids=[str(payload.get("cssr_msgid", "") or "").strip(), str(original_id or "").strip()],
            payload={
                "source": source,
                "source_table": table,
                **raw_payload,
            },
        )

    _set_last_id(local_conn, source, table, source_db_path, max_seen)
    return out


def _ingest_js8spotter(local_conn: sqlite3.Connection, source_db: Path, *, settings=None, max_rows: int) -> Dict[str, int]:
    out = {"rows_scanned": 0, "events_inserted": 0, "errors": 0}
    source = "JS8SPOTTER"
    source_db_path = str(source_db)
    mapped_sitrep_forms = _mapped_sitrep_forms(settings)
    try:
        src = _open_source_db(source_db)
    except Exception as e:
        log.debug("SitrepIngest: cannot open JS8Spotter DB %s: %s", source_db, e)
        out["errors"] += 1
        return out
    try:
        # forms table: mapper-selected SitRep/StatRep status forms.
        table = "forms"
        try:
            if _table_exists(src, table):
                last_id = _get_last_id(local_conn, source, table, source_db_path)
                cur = src.cursor()
                cur.execute(
                    """
                    SELECT id, fromcall, tocall, typeid, responses, msgtxt, timesig, lm, gwtx
                    FROM forms
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (last_id, int(max_rows)),
                )
                rows = cur.fetchall()
                max_seen = last_id
                for row in rows:
                    rid = int(row[0] or 0)
                    max_seen = max(max_seen, rid)
                    out["rows_scanned"] += 1
                    form_id = _extract_form_id(str(row[3] or ""))
                    subtype = _spotter_sitrep_subtype(form_id) if form_id in mapped_sitrep_forms else ""
                    if not subtype:
                        continue
                    event_ts, event_ts_utc = _parse_ts(row[7], fallback=str(row[6] or ""))
                    inserted = _insert_source_event(
                        local_conn,
                        source=source,
                        source_table=table,
                        source_db_path=source_db_path,
                        source_id=rid,
                        subtype=subtype,
                        from_call=str(row[1] or ""),
                        target=str(row[2] or ""),
                        grid="",
                        scope="",
                        status_payload={
                            "form_id": form_id,
                            "responses": str(row[4] or ""),
                        },
                        raw_payload={
                            "typeid": str(row[3] or ""),
                            "responses": str(row[4] or ""),
                            "msgtxt": str(row[5] or ""),
                            "timesig": str(row[6] or ""),
                            "lm": str(row[7] or ""),
                            "gwtx": str(row[8] or ""),
                        },
                        event_ts=event_ts,
                        event_ts_utc=event_ts_utc,
                    )
                    if inserted:
                        out["events_inserted"] += 1
                _set_last_id(local_conn, source, table, source_db_path, max_seen)
        except Exception as e:
            out["errors"] += 1
            _set_last_id(local_conn, source, table, source_db_path, _get_last_id(local_conn, source, table, source_db_path), error_text=str(e))
            log.debug("SitrepIngest: MCF forms ingest failed: %s", e)

        # csstatrep table: CommStat 12-digit status seen by JS8Spotter.
        table = "csstatrep"
        try:
            if _table_exists(src, table):
                last_id = _get_last_id(local_conn, source, table, source_db_path)
                cur = src.cursor()
                cur.execute(
                    """
                    SELECT id, cssr_from, cssr_group, cssr_grid, cssr_prio, cssr_msgid, cssr_status, cssr_notes, cssr_timestamp
                    FROM csstatrep
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (last_id, int(max_rows)),
                )
                rows = cur.fetchall()
                max_seen = last_id
                for row in rows:
                    rid = int(row[0] or 0)
                    max_seen = max(max_seen, rid)
                    out["rows_scanned"] += 1
                    status_txt = str(row[6] or "").strip()
                    subtype = "COMMSTAT_12" if len(status_txt) >= 12 else "COMMSTAT_FWD"
                    event_ts, event_ts_utc = _parse_ts(row[8])
                    inserted = _insert_source_event(
                        local_conn,
                        source=source,
                        source_table=table,
                        source_db_path=source_db_path,
                        source_id=rid,
                        subtype=subtype,
                        from_call=str(row[1] or ""),
                        target=str(row[2] or ""),
                        grid=str(row[3] or ""),
                        scope=str(row[4] or ""),
                        status_payload={
                            "status": status_txt,
                            "priority": str(row[4] or ""),
                        },
                        raw_payload={
                            "cssr_from": str(row[1] or ""),
                            "cssr_group": str(row[2] or ""),
                            "cssr_grid": str(row[3] or ""),
                            "cssr_prio": str(row[4] or ""),
                            "cssr_msgid": str(row[5] or ""),
                            "cssr_status": status_txt,
                            "cssr_notes": str(row[7] or ""),
                            "cssr_timestamp": str(row[8] or ""),
                        },
                        event_ts=event_ts,
                        event_ts_utc=event_ts_utc,
                    )
                    if inserted:
                        out["events_inserted"] += 1
                _set_last_id(local_conn, source, table, source_db_path, max_seen)
        except Exception as e:
            out["errors"] += 1
            _set_last_id(local_conn, source, table, source_db_path, _get_last_id(local_conn, source, table, source_db_path), error_text=str(e))
            log.debug("SitrepIngest: JS8Spotter csstatrep ingest failed: %s", e)
    finally:
        src.close()
    return out


def _refresh_commstat_brevity(local_conn, source_db, asset_dir, *, max_rows=250):
    """Reinterpret bounded batches in FIO tables only; never alter receipt identity."""
    if not asset_dir:
        return 0
    catalogs = cached_catalogs(asset_dir)
    revision = hashlib.sha256(json.dumps(
        [(key, value.get("_fio_fingerprint", "")) for key, value in sorted(catalogs.items())],
        separators=(",", ":")
    ).encode()).hexdigest() + ":adapter1"
    owner = str(asset_dir)
    source_key = "BREVITY_" + hashlib.sha256(str(source_db).encode()).hexdigest()[:24]
    changed = 0
    for table, payload_column, text_column in (
        ("sitrep_source_events", "raw_payload", "remarks_text"),
        ("commstat_artifacts", "payload_json", "body_text"),
        ("sitrep_events", "raw_payload_json", "remarks_text"),
    ):
        if not _table_exists(local_conn, table):
            continue
        cursor = _get_last_id(local_conn, source_key, table, revision)
        rows = local_conn.execute(
            f"SELECT id, {payload_column}, {text_column} FROM {table} WHERE id>? ORDER BY id LIMIT ?",
            (cursor, max(1, min(int(max_rows), 250))),
        ).fetchall()
        for row_id, raw, text in rows:
            cursor = int(row_id)
            try:
                payload = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("brevity_asset_dir") != owner:
                continue  # Another profile's catalog must never reinterpret this receipt.
            records = analyze_brevity_codes(text, catalogs)
            derived = {"brevity_models": records, "brevity_asset_dir": owner,
                       "brevity_code": records[-1]["code"] if records else "",
                       "brevity_summary": "\n".join(r["summary"] for r in records)}
            if all(payload.get(key) == value for key, value in derived.items()):
                continue
            payload.update(derived)
            columns = [f"{payload_column}=?"]
            params = [json.dumps(payload, separators=(",", ":"), ensure_ascii=True)]
            if table != "commstat_artifacts":
                columns += ["brevity_code=?", "brevity_summary=?"]
                params += [derived["brevity_code"], derived["brevity_summary"]]
            if table != "sitrep_source_events":
                columns += ["updated_ts=?"]
                params += [time.time()]
            local_conn.execute(f"UPDATE {table} SET {', '.join(columns)} WHERE id=?", (*params, row_id))
            if table == "sitrep_events" and _table_exists(local_conn, "sitrep_latest_by_callsign"):
                local_conn.execute(
                    "UPDATE sitrep_latest_by_callsign SET latest_brevity_code=?, latest_brevity_summary=? WHERE latest_event_id=?",
                    (derived["brevity_code"], derived["brevity_summary"], row_id),
                )
            changed += 1
        if rows or cursor == 0:
            _set_last_id(local_conn, source_key, table, revision, cursor)
    return changed


def _ingest_commstat3(local_conn: sqlite3.Connection, source_db: Path, *, max_rows: int) -> Dict[str, int]:
    out = {"rows_scanned": 0, "events_inserted": 0, "errors": 0}
    source = "COMMSTAT3"
    source_db_path = str(source_db)
    asset_dir = _commstat_asset_dir(source_db)
    _refresh_commstat_brevity(local_conn, source_db, asset_dir, max_rows=max_rows)
    health_key = "commstat:db:3"
    started = time.monotonic()
    try:
        src = _open_source_db(source_db)
    except Exception as e:
        log.debug("SitrepIngest: cannot open CommStat3 DB %s: %s", source_db, e)
        _record_source_db_failure(health_key, source_db, started, e)
        out["errors"] += 1
        return out
    _record_source_db_success(health_key, source_db, started)
    try:
        has_statrep = _table_exists(src, "statrep")
        has_messages = _table_exists(src, "messages")
        if not has_statrep and not has_messages:
            return out

        # statrep table
        table = "statrep"
        try:
            if has_statrep:
                last_id = _get_last_id(local_conn, source, table, source_db_path)
                cur = src.cursor()
                statrep_cols = _table_columns(src, table)
                global_select = "global_id" if "global_id" in statrep_cols else "0"
                cur.execute(
                    f"""
                    SELECT id, {global_select}, datetime, date, freq, db, source, sr_id, from_callsign, target, grid, scope,
                           map, power, water, med, telecom, travel, internet, fuel, food, crime, civil, political, comments
                    FROM statrep
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (last_id, int(max_rows)),
                )
                rows = cur.fetchall()
                max_seen = last_id
                for row in rows:
                    rid = int(row[0] or 0)
                    global_id = int(row[1] or 0)
                    max_seen = max(max_seen, rid)
                    out["rows_scanned"] += 1
                    event_ts, event_ts_utc = _parse_ts(row[2], fallback=str(row[3] or ""))
                    target = str(row[9] or "")
                    grid = str(row[10] or "")
                    remarks_text = str(row[24] or "")
                    metadata = _commstat_metadata(
                        target=target,
                        grid=grid,
                        remarks_text=remarks_text,
                        source_value=row[6],
                        global_id=global_id,
                        asset_dir=asset_dir,
                    )
                    status_payload = {
                        "overall_status": str(row[12] or ""),
                        "power": str(row[13] or ""),
                        "water": str(row[14] or ""),
                        "medical": str(row[15] or ""),
                        "communications": str(row[16] or ""),
                        "travel": str(row[17] or ""),
                        "internet": str(row[18] or ""),
                        "fuel": str(row[19] or ""),
                        "food": str(row[20] or ""),
                        "crime": str(row[21] or ""),
                        "civil_unrest": str(row[22] or ""),
                        "political": str(row[23] or ""),
                    }
                    raw_payload = {
                        "datetime": str(row[2] or ""),
                        "date": str(row[3] or ""),
                        "freq": row[4],
                        "db": row[5],
                        "source": row[6],
                        "source_value": row[6],
                        "global_id": global_id,
                        **brevity_metadata(remarks_text, asset_dir),
                        "origin_path": metadata["origin_path"],
                        "reach_mode": metadata["reach_mode"],
                        "sr_id": str(row[7] or ""),
                        "comments": remarks_text,
                    }
                    inserted = _insert_source_event(
                        local_conn,
                        source=source,
                        source_table=table,
                        source_db_path=source_db_path,
                        source_id=rid,
                        subtype="COMMSTAT_12",
                        from_call=str(row[8] or ""),
                        target=target,
                        report_group=metadata["report_group"],
                        grid=grid,
                        scope=str(row[11] or ""),
                        transport_mode=metadata["transport_mode"],
                        reach_mode=metadata["reach_mode"],
                        origin_path=metadata["origin_path"],
                        remarks_text=metadata["remarks_text"],
                        brevity_code=metadata["brevity_code"],
                        brevity_summary=metadata["brevity_summary"],
                        state_code=metadata["state_code"],
                        state_confidence=metadata["state_confidence"],
                        geo_confidence=metadata["geo_confidence"],
                        status_payload=status_payload,
                        raw_payload=raw_payload,
                        event_ts=event_ts,
                        event_ts_utc=event_ts_utc,
                    )
                    if inserted:
                        out["events_inserted"] += 1
                    _upsert_commstat_statrep_artifact(
                        local_conn,
                        source=source,
                        source_table=table,
                        source_id=rid,
                        event_ts=event_ts,
                        event_ts_utc=event_ts_utc,
                        from_call=str(row[8] or ""),
                        target=target,
                        report_group=metadata["report_group"],
                        grid=grid,
                        state_code=metadata["state_code"],
                        scope=str(row[11] or ""),
                        transport_mode=metadata["transport_mode"],
                        reach_mode=metadata["reach_mode"],
                        origin_path=metadata["origin_path"],
                        status_payload=status_payload,
                        remarks_text=remarks_text,
                        external_ids=[str(row[7] or "").strip(), str(global_id or "").strip()],
                        payload={
                            "source": source,
                            "source_table": table,
                            "source_value": row[6],
                            **raw_payload,
                        },
                    )
                _set_last_id(local_conn, source, table, source_db_path, max_seen)
        except Exception as e:
            out["errors"] += 1
            _set_last_id(local_conn, source, table, source_db_path, _get_last_id(local_conn, source, table, source_db_path), error_text=str(e))
            log.debug("SitrepIngest: CommStat3 statrep ingest failed: %s", e)

        # messages table (incremental checkpoint; ingest sitrep-like rows only).
        table = "messages"
        try:
            if has_messages:
                last_id = _get_last_id(local_conn, source, table, source_db_path)
                cur = src.cursor()
                cur.execute(
                    """
                    SELECT id, datetime, date, freq, db, source, msg_id, from_callsign, target, message
                    FROM messages
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (last_id, int(max_rows)),
                )
                rows = cur.fetchall()
                max_seen = last_id
                for row in rows:
                    rid = int(row[0] or 0)
                    max_seen = max(max_seen, rid)
                    out["rows_scanned"] += 1
                    message_text = str(row[9] or "")
                    event_ts, event_ts_utc = _parse_ts(row[1], fallback=str(row[2] or ""))
                    target = str(row[8] or "")
                    parsed = None
                    if _is_sitrep_like_message(message_text):
                        parsed = parse_commstat_message(
                            message_text,
                            target_hint=row[8],
                            source_value=row[5],
                            asset_dir=asset_dir,
                        )
                    if parsed:
                        metadata = dict(parsed.get("metadata") or {})
                        raw_payload = {**dict(parsed.get("raw_payload") or {}),
                                       **{k: metadata[k] for k in ("brevity_models", "brevity_asset_dir") if k in metadata}}
                        raw_payload.update(
                            {
                                "datetime": str(row[1] or ""),
                                "date": str(row[2] or ""),
                                "freq": row[3],
                                "db": row[4],
                                "source": row[5],
                                "source_value": row[5],
                                "origin_path": metadata.get("origin_path", ""),
                                "reach_mode": metadata.get("reach_mode", ""),
                                "msg_id": str(row[6] or ""),
                            }
                        )
                        inserted = _insert_source_event(
                            local_conn,
                            source=source,
                            source_table=table,
                            source_db_path=source_db_path,
                            source_id=rid,
                            subtype=str(parsed.get("subtype") or ""),
                            from_call=str(row[7] or ""),
                            target=target,
                            report_group=metadata.get("report_group", ""),
                            grid=str(parsed.get("grid") or ""),
                            scope=str(parsed.get("scope") or ""),
                            transport_mode=metadata.get("transport_mode", ""),
                            reach_mode=metadata.get("reach_mode", ""),
                            origin_path=metadata.get("origin_path", ""),
                            remarks_text=metadata.get("remarks_text", ""),
                            brevity_code=metadata.get("brevity_code", ""),
                            brevity_summary=metadata.get("brevity_summary", ""),
                            state_code=metadata.get("state_code", ""),
                            state_confidence=metadata.get("state_confidence", ""),
                            geo_confidence=metadata.get("geo_confidence", ""),
                            status_payload=dict(parsed.get("status_payload") or {}),
                            raw_payload=raw_payload,
                            event_ts=event_ts,
                            event_ts_utc=event_ts_utc,
                        )
                        if inserted:
                            out["events_inserted"] += 1
                        _upsert_commstat_statrep_artifact(
                            local_conn,
                            source=source,
                            source_table=table,
                            source_id=rid,
                            event_ts=event_ts,
                            event_ts_utc=event_ts_utc,
                            from_call=str(row[7] or ""),
                            target=target,
                            report_group=metadata.get("report_group", ""),
                            grid=str(parsed.get("grid") or ""),
                            state_code=metadata.get("state_code", ""),
                            scope=str(parsed.get("scope") or ""),
                            transport_mode=metadata.get("transport_mode", ""),
                            reach_mode=metadata.get("reach_mode", ""),
                            origin_path=metadata.get("origin_path", ""),
                            status_payload=dict(parsed.get("status_payload") or {}),
                            remarks_text=metadata.get("remarks_text", ""),
                            subtype=str(parsed.get("subtype") or ""),
                            external_ids=[str(row[6] or "").strip()],
                            payload=raw_payload,
                        )
                    else:
                        metadata = _commstat_metadata(
                            target=target,
                            grid="",
                            remarks_text="",
                            source_value=row[5],
                            raw_message=message_text,
                            asset_dir=asset_dir,
                        )
                        relay_route = _parse_js8_relay_route(message_text)
                        payload = {
                            "source": source,
                            "source_table": table,
                            "datetime": str(row[1] or ""),
                            "date": str(row[2] or ""),
                            "freq": row[3],
                            "db": row[4],
                            "source_value": row[5],
                            "brevity_asset_dir": str(asset_dir or ""),
                            "origin_path": metadata.get("origin_path", ""),
                            "reach_mode": metadata.get("reach_mode", ""),
                            "msg_id": str(row[6] or ""),
                            "message": message_text,
                        }
                        if relay_route:
                            payload.update(relay_route)
                        _upsert_commstat_message_artifact(
                            local_conn,
                            source=source,
                            source_table=table,
                            source_id=rid,
                            event_ts=event_ts,
                            event_ts_utc=event_ts_utc,
                            from_call=str(row[7] or ""),
                            target=target,
                            report_group=metadata.get("report_group", ""),
                            transport_mode=metadata.get("transport_mode", ""),
                            reach_mode=metadata.get("reach_mode", ""),
                            origin_path=metadata.get("origin_path", ""),
                            body_text=message_text,
                            external_ids=[str(row[6] or "").strip()],
                            payload=payload,
                        )
                _set_last_id(local_conn, source, table, source_db_path, max_seen)
        except Exception as e:
            out["errors"] += 1
            _set_last_id(local_conn, source, table, source_db_path, _get_last_id(local_conn, source, table, source_db_path), error_text=str(e))
            log.debug("SitrepIngest: CommStat3 messages ingest failed: %s", e)

        # alerts table (incremental checkpoint only in Phase 1).
        table = "alerts"
        try:
            if _table_exists(src, table):
                last_id = _get_last_id(local_conn, source, table, source_db_path)
                cur = src.cursor()
                cur.execute(
                    """
                    SELECT id, datetime, date, freq, db, source, alert_id, from_callsign, target, color, title, message
                    FROM alerts
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (last_id, int(max_rows)),
                )
                rows = cur.fetchall()
                max_seen = last_id
                for row in rows:
                    rid = int(row[0] or 0)
                    max_seen = max(max_seen, rid)
                    out["rows_scanned"] += 1
                    event_ts, event_ts_utc = _parse_ts(row[1], fallback=str(row[2] or ""))
                    target = str(row[8] or "")
                    metadata = _commstat_metadata(
                        target=target,
                        grid="",
                        remarks_text="",
                        source_value=row[5],
                        raw_message=str(row[11] or ""),
                        asset_dir=asset_dir,
                    )
                    _upsert_commstat_alert_artifact(
                        local_conn,
                        source=source,
                        source_table=table,
                        source_id=rid,
                        event_ts=event_ts,
                        event_ts_utc=event_ts_utc,
                        from_call=str(row[7] or ""),
                        target=target,
                        report_group=metadata.get("report_group", ""),
                        transport_mode=metadata.get("transport_mode", ""),
                        reach_mode=metadata.get("reach_mode", ""),
                        origin_path=metadata.get("origin_path", ""),
                        alert_color=str(row[9] or ""),
                        title=str(row[10] or ""),
                        body_text=str(row[11] or ""),
                        external_ids=[str(row[6] or "").strip()],
                        payload={
                            "source": source,
                            "source_table": table,
                            "datetime": str(row[1] or ""),
                            "date": str(row[2] or ""),
                            "freq": row[3],
                            "db": row[4],
                            "source_value": row[5],
                            "brevity_asset_dir": str(asset_dir or ""),
                            "origin_path": metadata.get("origin_path", ""),
                            "reach_mode": metadata.get("reach_mode", ""),
                            "alert_id": str(row[6] or ""),
                            "color": str(row[9] or ""),
                            "title": str(row[10] or ""),
                            "message": str(row[11] or ""),
                        },
                    )
                _set_last_id(local_conn, source, table, source_db_path, max_seen)
        except Exception as e:
            out["errors"] += 1
            _set_last_id(local_conn, source, table, source_db_path, _get_last_id(local_conn, source, table, source_db_path), error_text=str(e))
            log.debug("SitrepIngest: CommStat3 alerts ingest failed: %s", e)
    finally:
        src.close()
    return out


def _ingest_commstat23(local_conn: sqlite3.Connection, source_db: Path, *, max_rows: int) -> Dict[str, int]:
    out = {"rows_scanned": 0, "events_inserted": 0, "errors": 0}
    source = "COMMSTAT23"
    source_db_path = str(source_db)
    asset_dir = _commstat_asset_dir(source_db)
    _refresh_commstat_brevity(local_conn, source_db, asset_dir, max_rows=max_rows)
    health_key = "commstat:db:23"
    started = time.monotonic()
    try:
        src = _open_source_db(source_db)
    except Exception as e:
        log.debug("SitrepIngest: cannot open CommStat2.3 DB %s: %s", source_db, e)
        _record_source_db_failure(health_key, source_db, started, e)
        out["errors"] += 1
        return out
    _record_source_db_success(health_key, source_db, started)
    try:
        table = "StatRep_Data"
        if not _table_exists(src, table):
            return out
        last_id = _get_last_id(local_conn, source, table, source_db_path)
        cur = src.cursor()
        cur.execute(
            """
            SELECT id, datetime, date, freq, callsign, groupname, grid, SRid, prec,
                   status, commpwr, pubwtr, med, ota, trav, net, fuel, food, crime, civil, political, comments
            FROM StatRep_Data
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, int(max_rows)),
        )
        rows = cur.fetchall()
        max_seen = last_id
        for row in rows:
            rid = int(row[0] or 0)
            max_seen = max(max_seen, rid)
            out["rows_scanned"] += 1
            event_ts, event_ts_utc = _parse_ts(row[1], fallback=str(row[2] or ""))
            target = str(row[5] or "").strip().upper()
            if target and not target.startswith("@"):
                target = f"@{target}"
            grid = str(row[6] or "")
            remarks_text = str(row[21] or "")
            metadata = _commstat_metadata(
                target=target,
                grid=grid,
                remarks_text=remarks_text,
                source_value="",
                asset_dir=asset_dir,
            )
            inserted = _insert_source_event(
                local_conn,
                source=source,
                source_table=table,
                source_db_path=source_db_path,
                source_id=rid,
                subtype="COMMSTAT_12",
                from_call=str(row[4] or ""),
                target=target,
                report_group=metadata["report_group"],
                grid=grid,
                scope="",
                transport_mode=metadata["transport_mode"],
                remarks_text=metadata["remarks_text"],
                brevity_code=metadata["brevity_code"],
                brevity_summary=metadata["brevity_summary"],
                state_code=metadata["state_code"],
                state_confidence=metadata["state_confidence"],
                geo_confidence=metadata["geo_confidence"],
                status_payload={
                    "overall_status": str(row[9] or ""),
                    "power": str(row[10] or ""),
                    "water": str(row[11] or ""),
                    "medical": str(row[12] or ""),
                    "communications": str(row[13] or ""),
                    "travel": str(row[14] or ""),
                    "internet": str(row[15] or ""),
                    "fuel": str(row[16] or ""),
                    "food": str(row[17] or ""),
                    "crime": str(row[18] or ""),
                    "civil_unrest": str(row[19] or ""),
                    "political": str(row[20] or ""),
                },
                raw_payload={
                    "datetime": str(row[1] or ""),
                    "date": str(row[2] or ""),
                    "freq": row[3],
                    "sr_id": str(row[7] or ""),
                    "precedence": str(row[8] or ""),
                    "comments": remarks_text,
                },
                event_ts=event_ts,
                event_ts_utc=event_ts_utc,
            )
            if inserted:
                out["events_inserted"] += 1
            _upsert_commstat_statrep_artifact(
                local_conn,
                source=source,
                source_table=table,
                source_id=rid,
                event_ts=event_ts,
                event_ts_utc=event_ts_utc,
                from_call=str(row[4] or ""),
                target=target,
                report_group=metadata["report_group"],
                grid=grid,
                state_code=metadata["state_code"],
                scope="",
                transport_mode=metadata["transport_mode"],
                status_payload={
                    "overall_status": str(row[9] or ""),
                    "power": str(row[10] or ""),
                    "water": str(row[11] or ""),
                    "medical": str(row[12] or ""),
                    "communications": str(row[13] or ""),
                    "travel": str(row[14] or ""),
                    "internet": str(row[15] or ""),
                    "fuel": str(row[16] or ""),
                    "food": str(row[17] or ""),
                    "crime": str(row[18] or ""),
                    "civil_unrest": str(row[19] or ""),
                    "political": str(row[20] or ""),
                },
                remarks_text=remarks_text,
                external_ids=[str(row[7] or "").strip()],
                payload={
                    "source": source,
                    "source_table": table,
                    "brevity_asset_dir": str(asset_dir or ""),
                    "datetime": str(row[1] or ""),
                    "date": str(row[2] or ""),
                    "freq": row[3],
                    "sr_id": str(row[7] or ""),
                    "precedence": str(row[8] or ""),
                    "comments": remarks_text,
                },
            )
        _set_last_id(local_conn, source, table, source_db_path, max_seen)
    except Exception as e:
        out["errors"] += 1
        _set_last_id(local_conn, source, table, source_db_path, _get_last_id(local_conn, source, table, source_db_path), error_text=str(e))
        log.debug("SitrepIngest: CommStat2.3 ingest failed: %s", e)
    finally:
        src.close()
    return out
