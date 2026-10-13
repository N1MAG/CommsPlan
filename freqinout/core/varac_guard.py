from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Tuple

from freqinout.core.config_paths import get_config_dir
from freqinout.core.checkins_db import ensure_operator_checkins_schema
from freqinout.core.logger import log
from freqinout.core.varac_log_parser import parse_varac_event_timestamp_to_epoch
from freqinout.core.varac_bbs_config import base_callsign, callsigns_match, normalize_callsign, parse_callsign_list
from freqinout.core.varac_file_action import delete_file, quarantine_file, VaracFileActionResult


EVENT_TS_RE = re.compile(r"^(?P<stamp>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\s+-\s+(?P<body>.*)$")
CALLSIGN_RE = re.compile(r"\b([A-Z0-9/]{3,15})\b")
CONNECTED_RE = re.compile(r"\bCONNECTED\s+(?:TO|FROM)\s+([A-Z0-9/]{3,15})\b", re.IGNORECASE)
DISCONNECT_RE = re.compile(r"\bDISCONNECTED(?:\s+(?:FROM|BY|TO)\s+[A-Z0-9/+\-]+)?\b", re.IGNORECASE)
FILENAME_RE = re.compile(r"(?:FILE|FILENAME|NAME|AS)\s*[:=]\s*([^\r\n]+)", re.IGNORECASE)
QUOTED_FILE_RE = re.compile(r'"([^"]+\.(?:b2s|k2s|txt|rtf|html?|sig|asc|gpg)(?:\.[A-Za-z0-9]+)?)"', re.IGNORECASE)
TRAILING_FILE_RE = re.compile(r"([A-Za-z0-9_.\- ]+\.(?:b2s|k2s|txt|rtf|html?|sig|asc|gpg))(?:\s|$)", re.IGNORECASE)
FIO_HELPER_FILE_PREFIXES = (
    "BBS MSG - ",
    "BBS_QUEUE_LIST",
    "BBS_BLOCK_LIST",
)


@dataclass(frozen=True)
class VaracTransferEvent:
    timestamp_utc: float
    sender: str
    filename: str
    raw_line: str
    log_path: str
    sender_source: str = ""
    sender_candidates: Tuple[str, ...] = ()


@dataclass(frozen=True)
class VaracGuardDecision:
    action: str
    reason: str
    sender: str = ""
    filename: str = ""
    source_path: str = ""
    destination_path: str = ""
    log_path: str = ""


@dataclass(frozen=True)
class VaracGuardRunResult:
    scanned_events: int
    processed_events: int
    allowed_events: int
    unauthorized_events: int
    deleted_files: int
    quarantined_files: int
    pending_events: int
    skipped_events: int
    summary: str


def _normalize_call(value: object) -> str:
    return normalize_callsign(value)


def _base_call(value: object) -> str:
    return base_callsign(value)


def _callsign_matches(candidate: object, expected: object) -> bool:
    return callsigns_match(candidate, expected)


def _callsign_set_matches(candidate: object, allowed: Iterable[object]) -> bool:
    return any(_callsign_matches(candidate, item) for item in allowed)


def _resolve_varac_base_paths(settings) -> List[Path]:
    bases: List[Path] = []
    for raw in (
        settings.get("varac_path", "") if settings is not None else "",
        settings.get("varac_db_path", "") if settings is not None else "",
    ):
        txt = str(raw or "").strip()
        if not txt:
            continue
        try:
            path = Path(txt).expanduser()
        except Exception:
            continue
        if path.is_file():
            bases.append(path.parent)
        else:
            bases.append(path)
    message_paths = settings.get("message_paths", {}) if settings is not None else {}
    incoming = str((message_paths or {}).get("varac", "") or "").strip()
    if incoming:
        try:
            path = Path(incoming).expanduser()
            if path.exists():
                bases.append(path.parent)
        except Exception:
            pass
    uniq: List[Path] = []
    seen: set[str] = set()
    for base in bases:
        key = str(base).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(base)
    return uniq


def resolve_varac_traffic_log_paths(settings) -> List[Path]:
    out: List[Path] = []
    for base in _resolve_varac_base_paths(settings):
        for name in ("VarAC_traffic.log", "VarAC.log", "varalog.log"):
            path = base / name
            if path.exists():
                out.append(path)
    return out


def _log_cursor_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except Exception:
        return str(path)


def _read_log_increment(
    path: Path,
    cursor: Mapping[str, object],
    *,
    initial_max_bytes: int = 262144,
    overlap_bytes: int = 4096,
) -> Tuple[str, dict, bool]:
    try:
        stat = path.stat()
    except Exception as exc:
        log.debug("varac_guard: failed to stat %s: %s", path, exc)
        return "", dict(cursor or {}), False

    size = int(getattr(stat, "st_size", 0) or 0)
    inode = int(getattr(stat, "st_ino", 0) or 0)
    device = int(getattr(stat, "st_dev", 0) or 0)
    previous_offset = int((cursor or {}).get("offset", 0) or 0)
    previous_inode = int((cursor or {}).get("inode", 0) or 0)
    previous_device = int((cursor or {}).get("device", 0) or 0)
    previous_mtime_ns = int((cursor or {}).get("mtime_ns", 0) or 0)
    current_mtime_ns = int(getattr(stat, "st_mtime_ns", 0) or 0)

    rotated = bool(
        previous_offset
        and (
            size < previous_offset
            or (previous_inode and inode and previous_inode != inode)
            or (previous_device and device and previous_device != device)
        )
    )
    if rotated:
        start = max(0, size - max(1, int(initial_max_bytes)))
    elif previous_offset > 0:
        if size <= previous_offset:
            if size == previous_offset and previous_mtime_ns and current_mtime_ns and current_mtime_ns != previous_mtime_ns:
                start = max(0, size - max(1, int(initial_max_bytes)))
            else:
                return "", {
                    "path": str(path),
                    "offset": size,
                    "size": size,
                    "mtime_ns": current_mtime_ns,
                    "inode": inode,
                    "device": device,
                }, True
        else:
            start = max(0, previous_offset - max(0, int(overlap_bytes)))
    else:
        start = max(0, size - max(1, int(initial_max_bytes)))

    try:
        with path.open("rb") as handle:
            handle.seek(start)
            text = handle.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.debug("varac_guard: failed to read %s: %s", path, exc)
        return "", dict(cursor or {}), False

    return text, {
        "path": str(path),
        "offset": size,
        "size": size,
        "mtime_ns": current_mtime_ns,
        "inode": inode,
        "device": device,
    }, True


def _read_tail(path: Path, max_bytes: int = 32768) -> str:
    try:
        with path.open("rb") as handle:
            try:
                handle.seek(-max_bytes, os.SEEK_END)
            except OSError:
                handle.seek(0)
            return handle.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.debug("varac_guard: failed to tail %s: %s", path, exc)
        return ""


def _parse_timestamp(stamp: str) -> float:
    return parse_varac_event_timestamp_to_epoch(stamp)


def _split_events(text: str) -> List[str]:
    events: List[str] = []
    current: List[str] = []
    for raw_line in str(text or "").splitlines():
        if EVENT_TS_RE.match(raw_line):
            if current:
                events.append("\n".join(current))
            current = [raw_line]
        elif current:
            current.append(raw_line)
    if current:
        events.append("\n".join(current))
    return events


def _extract_sender_from_fields(body: str) -> str:
    patterns = [
        r"\bFROM\b\s*[:=]?\s*([A-Z0-9/]{3,15})",
        r"\bDE\b\s*([A-Z0-9/]{3,15})",
        r"\bSENDER\b\s*[:=]?\s*([A-Z0-9/]{3,15})",
        r"\bCALLSIGN\b\s*[:=]?\s*([A-Z0-9/]{3,15})",
    ]
    upper = body.upper()
    for pattern in patterns:
        match = re.search(pattern, upper)
        if match:
            return _normalize_call(match.group(1))
    return ""


def _extract_sender_guess(body: str) -> str:
    upper = str(body or "").upper()
    if "FILE SUCCESSFULLY RECEIVED" in upper:
        tokens = [tok for tok in CALLSIGN_RE.findall(upper) if tok not in {"FILE", "SUCCESSFULLY", "RECEIVED", "FROM", "SENDER", "CALLSIGN"}]
        if tokens:
            return _normalize_call(tokens[0])
    return ""


def _validated_sender_for_file_event(body: str, active_remote: str) -> tuple[str, str, Tuple[str, ...]]:
    session_sender = _normalize_call(active_remote)
    line_sender = _extract_sender_from_fields(body)
    guessed_sender = _extract_sender_guess(body)
    candidates = tuple(dict.fromkeys(item for item in (session_sender, line_sender, guessed_sender) if item))
    if session_sender and line_sender and not _callsign_matches(session_sender, line_sender):
        return "", "sender_conflict", candidates
    if session_sender:
        return session_sender, "session", candidates
    if line_sender:
        return line_sender, "line", candidates
    if guessed_sender:
        return guessed_sender, "guess", candidates
    return "", "sender_unresolved", candidates


def _extract_filename(body: str) -> str:
    for regex in (FILENAME_RE, QUOTED_FILE_RE, TRAILING_FILE_RE):
        match = regex.search(body)
        if match:
            candidate = str(match.group(1) or "").strip().strip(".")
            if candidate:
                return candidate
    return ""


def parse_varac_transfer_events(text: str, *, log_path: str = "") -> List[VaracTransferEvent]:
    events: List[VaracTransferEvent] = []
    active_remote = ""
    for block in _split_events(text):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        match = EVENT_TS_RE.match(lines[0])
        if not match:
            continue
        body = "\n".join([match.group("body")] + lines[1:]).strip()
        upper = body.upper()
        connected = CONNECTED_RE.search(upper)
        if connected:
            active_remote = _normalize_call(connected.group(1))
            continue
        if DISCONNECT_RE.search(upper):
            active_remote = ""
            continue
        if "FILE SUCCESSFULLY RECEIVED" not in upper:
            continue
        sender, sender_source, sender_candidates = _validated_sender_for_file_event(body, active_remote)
        filename = _extract_filename(body)
        events.append(
            VaracTransferEvent(
                timestamp_utc=_parse_timestamp(match.group("stamp")),
                sender=sender,
                filename=filename,
                raw_line=block,
                log_path=log_path,
                sender_source=sender_source,
                sender_candidates=sender_candidates,
            )
        )
    return events


def _load_guard_state(settings) -> dict:
    raw = settings.get("varac_guard_state_v1", {}) if settings is not None else {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _save_guard_state(settings, state: dict) -> None:
    if settings is None:
        return
    try:
        settings.set("varac_guard_state_v1", state)
    except Exception as exc:
        log.debug("varac_guard: failed to persist state: %s", exc)


def _state_key(event: VaracTransferEvent) -> str:
    return "|".join(
        [
            event.log_path,
            str(int(event.timestamp_utc or 0.0)),
            event.sender,
            event.filename.lower(),
        ]
    )


def _event_to_state(event: VaracTransferEvent) -> dict:
    return {
        "timestamp_utc": float(event.timestamp_utc or 0.0),
        "sender": event.sender,
        "filename": event.filename,
        "raw_line": event.raw_line,
        "log_path": event.log_path,
        "sender_source": event.sender_source,
        "sender_candidates": list(event.sender_candidates),
    }


def _event_from_state(value: object) -> Optional[VaracTransferEvent]:
    if not isinstance(value, dict):
        return None
    try:
        candidates_raw = value.get("sender_candidates", [])
        if isinstance(candidates_raw, (list, tuple)):
            candidates = tuple(str(item or "") for item in candidates_raw if str(item or "").strip())
        else:
            candidates = tuple()
        return VaracTransferEvent(
            timestamp_utc=float(value.get("timestamp_utc", 0.0) or 0.0),
            sender=str(value.get("sender", "") or ""),
            filename=str(value.get("filename", "") or ""),
            raw_line=str(value.get("raw_line", "") or ""),
            log_path=str(value.get("log_path", "") or ""),
            sender_source=str(value.get("sender_source", "") or ""),
            sender_candidates=candidates,
        )
    except Exception:
        return None


def _candidate_files(incoming_dir: Path, filename: str) -> List[Path]:
    if not filename:
        return []
    candidates: List[Path] = []
    exact = incoming_dir / filename
    if exact.exists():
        candidates.append(exact)
    wanted = filename.lower()
    for child in incoming_dir.iterdir():
        if not child.is_file():
            continue
        if child.name.lower() == wanted:
            candidates.append(child)
    return candidates


def _latest_recent_file(incoming_dir: Path, *, since_ts: float, retry_seconds: int) -> Optional[Path]:
    newest: Optional[Path] = None
    newest_mtime = 0.0
    deadline = since_ts - max(5, int(retry_seconds))
    for child in incoming_dir.iterdir():
        if not child.is_file():
            continue
        try:
            st = child.stat()
        except Exception:
            continue
        mtime = float(st.st_mtime or 0.0)
        if mtime < deadline:
            continue
        if mtime > newest_mtime:
            newest = child
            newest_mtime = mtime
    return newest


def _resolve_incoming_dir(settings) -> Optional[Path]:
    raw = ""
    if settings is not None:
        raw = str(settings.get("varac_incoming_path", "") or "").strip()
        if not raw:
            message_paths = settings.get("message_paths", {}) or {}
            raw = str(message_paths.get("varac", "") or "").strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser()
    except Exception:
        return None
    if not path.exists() or not path.is_dir():
        return None
    return path


def _resolve_quarantine_dir(settings, incoming_dir: Path) -> Path:
    raw = str(settings.get("varac_guard_quarantine_dir", "") or "").strip() if settings is not None else ""
    if raw:
        try:
            path = Path(raw).expanduser()
            return path
        except Exception:
            pass
    return incoming_dir / "VGuard_Quarantine"


def _local_operator_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout_nets.db"


def _operator_history_trust_enabled(settings) -> bool:
    if settings is None:
        return False
    try:
        return bool(settings.get("varac_guard_allow_operator_trusted", True))
    except Exception:
        return False


def _bbs_allowed_trust_enabled(settings) -> bool:
    if settings is None:
        return False
    try:
        return bool(settings.get("varac_guard_allow_bbs_allowed_callsigns", True))
    except Exception:
        return False


def _trusted_operator_callsigns() -> List[str]:
    db_path = _local_operator_db_path()
    if not db_path.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        ensure_operator_checkins_schema(conn)
        cur = conn.execute("SELECT callsign FROM operator_checkins WHERE COALESCE(trusted, 0) != 0")
        return [str(row[0] or "").strip() for row in cur.fetchall() if str(row[0] or "").strip()]
    except Exception as exc:
        log.debug("varac_guard: failed to load trusted operator callsigns: %s", exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class _UnavailableOperatorTrust(set):
    """Explicit manual grants remain usable; unknown roster evidence is not denial."""


def _operator_allowed_callsigns(settings):
    if _operator_history_trust_enabled(settings):
        policy_text = settings.get("varac_guard_access_policy_v1", "") if settings is not None else ""
        if policy_text:
            from freqinout.core.bbs_access_policy import (
                normalize_access_policy, refresh_access_policy, load_access_families,
            )
            policy = normalize_access_policy(policy_text)
            if not policy["group_sources"]:
                return set(policy["manual_callsigns"])
            try:
                db_path = getattr(settings, "db_path", None)
                roster_path = Path(db_path).with_name("freqinout_nets.db") if db_path else _local_operator_db_path()
                live = refresh_access_policy(policy, load_access_families(roster_path))
                # Newly trusted members require reviewed refresh; revoked trust
                # and removed memberships cease authorizing on the next check.
                trusted = set(policy["resolved_group_callsigns"]) & set(live["resolved_group_callsigns"])
            except Exception:
                return _UnavailableOperatorTrust(policy["manual_callsigns"])
            allowed = set(policy["manual_callsigns"]) | trusted
        else:
            allowed = _trusted_operator_callsigns()
        return allowed
    return ()


def _sender_allow_reason(sender: str, settings, *, operator_callsigns=None) -> str:
    if not sender:
        return ""
    if _bbs_allowed_trust_enabled(settings):
        allowed = parse_callsign_list(settings.get("varac_bbs_allowed_callsigns", "") if settings is not None else "")
        if _callsign_set_matches(sender, allowed):
            return "authorized_bbs_allowed_callsign"
    if _operator_history_trust_enabled(settings):
        allowed = _operator_allowed_callsigns(settings) if operator_callsigns is None else operator_callsigns
        if _callsign_set_matches(sender, allowed):
            return "authorized_operator_history_trusted"
    return ""


def _deny_reason_for_event(event: VaracTransferEvent) -> str:
    source = str(event.sender_source or "").strip()
    if source in {"sender_conflict", "sender_unresolved"}:
        return source
    return "unauthorized_sender"


def _is_fio_generated_helper_file(filename: object) -> bool:
    clean = Path(str(filename or "").strip()).name
    upper = clean.upper()
    return any(upper.startswith(prefix.upper()) for prefix in FIO_HELPER_FILE_PREFIXES)


def evaluate_varac_guard_event(
    event: VaracTransferEvent,
    *,
    settings,
    state: Optional[dict] = None,
    now_utc: Optional[float] = None,
    retry_seconds: int = 120,
    operator_callsigns=None,
) -> tuple[VaracGuardDecision, Optional[VaracFileActionResult], bool]:
    incoming_dir = _resolve_incoming_dir(settings)
    if incoming_dir is None:
        return (
            VaracGuardDecision(action="skip", reason="incoming_folder_missing", sender=event.sender, filename=event.filename, log_path=event.log_path),
            None,
            False,
        )

    sender = _normalize_call(event.sender)
    if operator_callsigns is None:
        operator_callsigns = _operator_allowed_callsigns(settings)
    allow_reason = _sender_allow_reason(sender, settings, operator_callsigns=operator_callsigns)
    if allow_reason:
        return (
            VaracGuardDecision(action="allow", reason=allow_reason, sender=sender, filename=event.filename, log_path=event.log_path),
            None,
            True,
        )
    if isinstance(operator_callsigns, _UnavailableOperatorTrust):
        # Failed trust lookup must not delete/quarantine a potentially authorized
        # sender. Retain the event for the next normal worker scan.
        return (
            VaracGuardDecision(action="pending", reason="operator_trust_unavailable", sender=sender,
                               filename=event.filename, log_path=event.log_path),
            None, False,
        )

    mode = str(settings.get("varac_guard_mode", "Log only") or "Log only").strip().lower() if settings is not None else "log only"
    state = state or {}
    filename = str(event.filename or "").strip()
    deny_reason = _deny_reason_for_event(event)
    if _is_fio_generated_helper_file(filename):
        return (
            VaracGuardDecision(action="skip", reason="fio_helper_file", sender=sender, filename=filename, log_path=event.log_path),
            None,
            True,
        )

    candidates = _candidate_files(incoming_dir, filename)
    if not candidates and event.timestamp_utc > 0:
        fallback = _latest_recent_file(incoming_dir, since_ts=event.timestamp_utc, retry_seconds=retry_seconds)
        if fallback is not None:
            candidates = [fallback]

    if not candidates:
        age = 0.0
        if now_utc is not None and event.timestamp_utc > 0:
            age = max(0.0, float(now_utc) - float(event.timestamp_utc))
        if age < float(retry_seconds):
            return (
                VaracGuardDecision(action="pending", reason="waiting_for_delayed_file_write", sender=sender, filename=filename, log_path=event.log_path),
                None,
                False,
            )
        return (
            VaracGuardDecision(action="skip", reason="file_not_found", sender=sender, filename=filename, log_path=event.log_path),
            None,
            True,
        )

    src = candidates[0]
    if _is_fio_generated_helper_file(src.name):
        return (
            VaracGuardDecision(action="skip", reason="fio_helper_file", sender=sender, filename=src.name, source_path=str(src), log_path=event.log_path),
            None,
            True,
        )
    try:
        st = src.stat()
    except Exception:
        return (
            VaracGuardDecision(action="pending", reason="file_stat_unavailable", sender=sender, filename=filename, source_path=str(src), log_path=event.log_path),
            None,
            False,
        )

    # VarAC writes and moves files outside FIO's control. This mtime check is a
    # conservative heuristic to avoid acting on an older same-name file.
    if event.timestamp_utc > 0 and float(st.st_mtime or 0.0) < (float(event.timestamp_utc) - 30.0):
        return (
            VaracGuardDecision(action="skip", reason="preexisting_file", sender=sender, filename=filename, source_path=str(src), log_path=event.log_path),
            None,
            True,
        )

    if mode == "log only":
        return (
            VaracGuardDecision(action="log_only", reason=deny_reason, sender=sender, filename=filename, source_path=str(src), log_path=event.log_path),
            None,
            True,
        )

    if mode == "delete unauthorized files":
        result = delete_file(src)
        return (
            VaracGuardDecision(action="delete", reason=deny_reason, sender=sender, filename=filename, source_path=str(src), log_path=event.log_path),
            result,
            True,
        )

    if mode == "quarantine unauthorized files":
        quarantine_dir = _resolve_quarantine_dir(settings, incoming_dir)
        result = quarantine_file(src, quarantine_dir)
        return (
            VaracGuardDecision(action="quarantine", reason=deny_reason, sender=sender, filename=filename, source_path=str(src), destination_path=result.destination, log_path=event.log_path),
            result,
            True,
        )

    return (
        VaracGuardDecision(action="skip", reason="unknown_mode", sender=sender, filename=filename, source_path=str(src), log_path=event.log_path),
        None,
        True,
    )


def run_varac_guard(settings, *, retry_seconds: Optional[int] = None) -> VaracGuardRunResult:
    if not bool(settings.get("varac_guard_enabled", False) if settings is not None else False):
        return VaracGuardRunResult(0, 0, 0, 0, 0, 0, 0, 0, "BBS Access Guard disabled")

    log_paths = resolve_varac_traffic_log_paths(settings)
    if not log_paths:
        summary = "BBS Access Guard enabled, but no VarAC traffic log was found"
        try:
            settings.set("varac_guard_last_summary", summary)
        except Exception:
            pass
        return VaracGuardRunResult(0, 0, 0, 0, 0, 0, 0, 0, summary)

    retry_seconds = int(retry_seconds if retry_seconds is not None else settings.get("varac_guard_retry_seconds", 120) if settings is not None else 120)
    state = _load_guard_state(settings)
    seen = set(str(x) for x in state.get("processed_event_keys", []) if str(x).strip())
    processed_keys: List[str] = list(state.get("processed_event_keys", [])) if isinstance(state.get("processed_event_keys", []), list) else []
    log_cursor_map = {
        str(key): dict(value)
        for key, value in (state.get("log_cursors", {}) or {}).items()
        if isinstance(value, dict)
    } if isinstance(state.get("log_cursors", {}), dict) else {}
    pending_records = state.get("pending_events", [])
    pending_events: List[VaracTransferEvent] = []
    if isinstance(pending_records, list):
        for record in pending_records:
            event = _event_from_state(record)
            if event is not None:
                pending_events.append(event)
    now_utc = dt.datetime.now(dt.timezone.utc).timestamp()

    scanned = 0
    processed = 0
    allowed = 0
    unauthorized = 0
    deleted = 0
    quarantined = 0
    pending = 0
    skipped = 0
    recent_decisions: List[dict] = []
    events_by_key: dict[str, VaracTransferEvent] = {}
    for event in pending_events:
        key = _state_key(event)
        if key and key not in seen:
            events_by_key[key] = event

    for log_path in log_paths:
        cursor_key = _log_cursor_key(log_path)
        text, next_cursor, readable = _read_log_increment(log_path, log_cursor_map.get(cursor_key, {}))
        if readable:
            log_cursor_map[cursor_key] = next_cursor
        for event in parse_varac_transfer_events(text, log_path=str(log_path)):
            key = _state_key(event)
            if key and key not in seen:
                events_by_key[key] = event

    next_pending_events: List[dict] = []
    # One roster snapshot per worker scan, not one SQL read per received file.
    operator_callsigns = _operator_allowed_callsigns(settings) if events_by_key else ()
    for key, event in events_by_key.items():
        scanned += 1
        if key in seen:
            skipped += 1
            continue
        decision, action_result, should_mark = evaluate_varac_guard_event(
            event,
            settings=settings,
            state=state,
            now_utc=now_utc,
            retry_seconds=retry_seconds,
            operator_callsigns=operator_callsigns,
        )
        if decision.action == "allow":
            allowed += 1
            processed += 1
        elif decision.action == "pending":
            pending += 1
            next_pending_events.append(_event_to_state(event))
        elif decision.action == "skip":
            skipped += 1
        else:
            processed += 1
            unauthorized += 1
            if decision.action == "delete":
                deleted += 1
            elif decision.action == "quarantine":
                quarantined += 1
        if should_mark and decision.action != "pending":
            seen.add(key)
            processed_keys.append(key)
            if len(processed_keys) > 256:
                processed_keys = processed_keys[-256:]
        if decision.action in {"allow", "log_only", "delete", "quarantine"}:
            recent_decisions.append(
                {
                    "action": decision.action,
                    "reason": decision.reason,
                    "sender": decision.sender,
                    "filename": decision.filename,
                    "log_path": decision.log_path,
                }
            )
            if len(recent_decisions) > 20:
                recent_decisions = recent_decisions[-20:]
        if action_result is not None:
            log.debug("varac_guard: %s", action_result)

    state["processed_event_keys"] = processed_keys
    state["log_cursors"] = log_cursor_map
    state["pending_events"] = next_pending_events[-128:]
    state["last_run_utc"] = now_utc
    state["last_summary"] = {
        "scanned_events": scanned,
        "processed_events": processed,
        "allowed_events": allowed,
        "unauthorized_events": unauthorized,
        "deleted_files": deleted,
        "quarantined_files": quarantined,
        "pending_events": pending,
        "skipped_events": skipped,
    }
    state["last_decisions"] = recent_decisions
    _save_guard_state(settings, state)

    summary = (
        f"BBS Access Guard {str(settings.get('varac_guard_mode', 'Log only') or 'Log only')} | "
        f"scanned {scanned}, processed {processed}, allowed {allowed}, unauthorized {unauthorized}, "
        f"deleted {deleted}, quarantined {quarantined}, pending {pending}, skipped {skipped}"
    )
    try:
        settings.set("varac_guard_last_summary", summary)
    except Exception:
        pass
    return VaracGuardRunResult(
        scanned_events=scanned,
        processed_events=processed,
        allowed_events=allowed,
        unauthorized_events=unauthorized,
        deleted_files=deleted,
        quarantined_files=quarantined,
        pending_events=pending,
        skipped_events=skipped,
        summary=summary,
    )
