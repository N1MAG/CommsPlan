from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.core.logger import log
from freqinout.core.nbems_compose import safe_varac_bbs_filename
from freqinout.core.sqlite_utils import (
    connect_sqlite,
    connect_sqlite_readonly,
    connect_sqlite_runtime_write,
    table_exists,
)
from freqinout.core.varac_bbs_library_store import (
    bbs_location_catalog_source_dir,
    bbs_library_db_path_from_settings,  # compatibility export; runtime paths are explicit below
    ensure_bbs_library_schema,
    list_bbs_locations,
    list_bbs_location_manifest_rows,
    location_has_bbs_catalog,
    log_bbs_library_sync_failure,
    sync_bbs_locations_from_folders,
)
from freqinout.core.varac_log_parser import parse_varac_event_timestamp_to_epoch
from freqinout.core.varac_bbs_config import normalize_callsign, parse_callsign_list
from freqinout.core.varac_guard import resolve_varac_traffic_log_paths

DEFAULT_ACCESS_CODE_ITERATIONS = 310_000
DEFAULT_FAILED_ATTEMPT_LIMIT = 3
DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS = 15 * 60
DEFAULT_COOLDOWN_SECONDS = 30 * 60
DEFAULT_IDLE_TIMEOUT_SECONDS = 60
DEFAULT_TRIGGER_MODE = "VarAC session commands"
DEFAULT_RETURN_MODE = "On disconnect"
DEFAULT_LOCATION_ID = "default"
DEFAULT_LOCATION_NAME = "Default"
DEFAULT_GLOBAL_CODE_POLICY = "Allow public locations"
DEFAULT_VIEW_MODE = "root"
MAX_PROCESSED_EVENT_KEYS = 256
MAX_MANIFEST_FILENAME_LENGTH = 180
MAX_DB_ROWS_PER_SCAN = 256
DEFAULT_FLAMP_QUEUE_HELPER_NAME = "BBS_QUEUE_LIST.txt"
DEFAULT_FLAMP_BLOCK_PREFIX = "BBS_BLOCK_LIST"
DEFAULT_FLAMP_FILE_PREFIX = "BBS"
DEFAULT_FLAMP_LISTING_MAX_AGE_DAYS = 14
MAX_FLAMP_RECEIVE_FILES_PER_SCAN = 5000
MAX_FLAMP_RELAY_FILES_PER_SCAN = 5000
FLAMP_TRANSFER_PARSER_VERSION = 2
DEFAULT_BBS_REFRESH_PAUSE_SECONDS = 10
# Kept as a compatibility constant for callers that imported the old setting.
# Visitor-facing helper text must not promise a fixed delay; asynchronous state
# uses ASYNC_REFRESH_NOTICE instead.
ASYNC_REFRESH_NOTICE = "Request sent—refresh when the updated listing is ready"
VAULT_ALIAS_HEALTH_KEY = "varac-bbs-vault-aliases"
VAULT_ALIAS_HEALTH_OWNER = "VarAC Managed BBS Library"

EVENT_TS_RE = re.compile(r"^(?P<stamp>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\s+-\s+(?P<body>.*)$")
EVENT_TS_SCAN_RE = re.compile(r"(?P<stamp>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\s+-\s+")
CHAT_SENDER_RE = re.compile(r"^\s*([A-Z0-9/]{3,15})>\s*(.*)$", re.IGNORECASE)
CONNECTED_RE = re.compile(r"\bCONNECTED\s+(?:TO|FROM)\s+([A-Z0-9/]{3,15})\b", re.IGNORECASE)
COMMAND_RE = re.compile(r"\bBBS\s+OPEN\s+([A-Z0-9][A-Z0-9_.:/+\-]{0,63})\b", re.IGNORECASE)
DISCONNECT_RE = re.compile(r"\bDISCONNECTED(?:\s+(?:FROM|BY|TO)\s+[A-Z0-9/+\-]+)?\b", re.IGNORECASE)
CALLSIGN_PATTERNS = (
    r"\bFROM\b\s*[:=]?\s*([A-Z0-9/]{3,15})",
    r"\bDE\b\s*([A-Z0-9/]{3,15})",
    r"\bSENDER\b\s*[:=]?\s*([A-Z0-9/]{3,15})",
    r"\bCALLSIGN\b\s*[:=]?\s*([A-Z0-9/]{3,15})",
)
CALLSIGN_RE = re.compile(r"\b([A-Z0-9/]{3,15})\b")
IGNORED_CALLSIGN_TOKENS = {
    "BBS",
    "OPEN",
    "DISCONNECTED",
    "CONNECTION",
    "CLOSED",
    "SESSION",
    "FROM",
    "DE",
    "CALLSIGN",
    "SENDER",
}
ROOT_CMD_RE = re.compile(r"^(ROOT|LOCK|EXIT|BACK)\s*$", re.IGNORECASE)
FLAMP_CMD_RE = re.compile(r"^FLAMP\s*$", re.IGNORECASE)
LIST_Q_RE = re.compile(r"^LIST\s+Q\s*$", re.IGNORECASE)
LIST_BLOCKS_RE = re.compile(r"^(?:LIST\s+(?:BLKS|BLOCKS)\s+|LIST\s+)([A-F0-9]{4})\s*$", re.IGNORECASE)
BLOCK_REQUEST_RE = re.compile(r"^(?:REQ\s+)?(?:BLK|BLKS|BLOCK|BLOCKS)\s+([0-9][0-9,\s]*?)\s*([A-F0-9]{4})\s*$", re.IGNORECASE)
INVALID_BLOCK_REQUEST_RE = re.compile(r"^(?:REQ\s+)?(?:BLK|BLKS|BLOCK|BLOCKS)\s+([A-F0-9]{4})\s*$", re.IGNORECASE)
# JS8Spotter operators commonly omit the space between the Q token and the
# four-character FLAMP identifier.  Keep the command otherwise exact so a
# conversational mention cannot trigger unattended transmission.
DYNAMIC_FLAMP_QUERY_RE = re.compile(r"^\s*E\?\s+Q\s*([0-9A-F]{4})\s*$", re.IGNORECASE)
ALIAS_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.:/+\-]{0,31}$")


@dataclass(frozen=True)
class VaultLocation:
    id: str
    name: str
    source_dir: str
    enabled: bool = True
    inherit_global_allowed_callsigns: bool = True
    allowed_callsigns: Tuple[str, ...] = ()
    access_code_hash: str = ""
    access_code_salt: str = ""
    access_code_iterations: int = DEFAULT_ACCESS_CODE_ITERATIONS
    access_code_plaintext: str = ""
    alias: str = ""
    description: str = ""
    list_in_root_menu: bool = True
    visibility_rule: str = "Public"
    open_rule: str = "Public"
    retention_policy: str = "Use global BBS archive policy"
    retention_days: int = 0


@dataclass(frozen=True)
class VaultRuntimeState:
    current_location_id: str = DEFAULT_LOCATION_ID
    current_session_callsign: str = ""
    current_session_qso_guid: str = ""
    current_view_mode: str = DEFAULT_VIEW_MODE
    current_view_label: str = DEFAULT_LOCATION_NAME
    previous_location_id: str = DEFAULT_LOCATION_ID
    previous_view_mode: str = DEFAULT_VIEW_MODE
    previous_view_label: str = DEFAULT_LOCATION_NAME
    current_overlay_file: str = ""
    processed_event_keys: Tuple[str, ...] = ()
    cooldowns: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    failed_attempts: Mapping[str, Sequence[float]] = field(default_factory=dict)
    last_publish_manifest_path: str = ""
    last_publish_ts: float = 0.0
    last_action: str = ""
    last_request_ts: float = 0.0
    last_error: str = ""
    unmanaged_live_files: Tuple[str, ...] = ()
    last_datastream_id: int = 0
    log_cursors: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class VaultPublishManifestEntry:
    source_name: str
    live_name: str
    size: int
    mtime_ns: int
    sha256: str = ""
    source_path: str = ""


@dataclass(frozen=True)
class VaultPublishResult:
    changed: bool
    published_count: int
    removed_count: int
    unmanaged_live_files: Tuple[str, ...]
    manifest_path: str
    ignored_directories: int = 0


@dataclass(frozen=True)
class BbsFileActionResult:
    success: bool
    action: str
    source_path: str
    target_path: str = ""
    detail: str = ""


@dataclass(frozen=True)
class VaultLogEvent:
    timestamp_utc: float
    kind: str
    sender: str
    body: str
    code_text: str = ""
    alias: str = ""
    queue_id: str = ""
    block_numbers: Tuple[int, ...] = ()
    raw_line: str = ""
    log_path: str = ""
    refresh_requested: bool = False


@dataclass(frozen=True)
class VaultDbEvent:
    row_id: int
    timestamp_utc: float
    qso_guid: str
    remote_callsign: str
    my_callsign: str
    entry_callsign: str
    entry_text: str
    kind: str
    alias: str = ""
    code_text: str = ""
    queue_id: str = ""
    block_numbers: Tuple[int, ...] = ()
    refresh_requested: bool = False


@dataclass(frozen=True)
class VaultDbScanResult:
    events: Tuple[VaultDbEvent, ...]
    max_scanned_row_id: int = 0


@dataclass(frozen=True)
class VaultActionResult:
    action: str
    success: bool
    summary: str
    runtime_state: VaultRuntimeState
    publish_result: Optional[VaultPublishResult] = None


@dataclass(frozen=True)
class VaracBbsVaultRunResult:
    enabled: bool
    scanned_events: int
    processed_events: int
    published: bool
    active_location_id: str
    current_session_callsign: str
    summary: str
    state_changed: bool = False
    publish_changed: bool = False
    unmanaged_live_files_changed: bool = False
    active_session: bool = False


@dataclass(frozen=True)
class _VirtualFile:
    name: str
    content: str


@dataclass(frozen=True)
class DynamicFlampQuery:
    q_id: str
    confidence: float = 1.0


def parse_dynamic_flamp_query(value: object) -> Optional[DynamicFlampQuery]:
    """Parse only the exact dynamic FLAMP query form.

    Destination/source framing is deliberately handled by the JS8 ingest
    adapters.  This helper accepts only the payload itself and therefore cannot
    accidentally turn a relayed or partial message into a query.
    """
    match = DYNAMIC_FLAMP_QUERY_RE.fullmatch(str(value or ""))
    if not match:
        return None
    return DynamicFlampQuery(q_id=match.group(1).upper(), confidence=1.0)


def _normalize_callsign(value: object) -> str:
    return normalize_callsign(value)


def _clean_location_name(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_location_alias(value: object, fallback_name: object = "") -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        raw = re.sub(r"[^A-Z0-9]+", "", str(fallback_name or "").strip().upper())
    raw = re.sub(r"[^A-Z0-9]+", "", raw)
    if raw in {"ROOT", "LOCK", "EXIT", "BACK", "LIST", "BLK", "REQ", "BBS", "OPEN"}:
        raw = f"{raw}1"
    return raw[:32]


def _normalize_access_code_text(value: object) -> str:
    clean = " ".join(str(value or "").strip().split())
    while len(clean) >= 2 and clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1].strip()
    return clean.upper()


def _location_id_from_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return cleaned or DEFAULT_LOCATION_ID


def _ensure_location_id(existing_ids: Iterable[str], preferred_name: str, raw_id: object = "") -> str:
    existing = {str(item or "").strip() for item in existing_ids if str(item or "").strip()}
    base = str(raw_id or "").strip() or _location_id_from_name(preferred_name)
    if base not in existing:
        return base
    counter = 2
    while f"{base}-{counter}" in existing:
        counter += 1
    return f"{base}-{counter}"


def _resolve_path(value: object) -> Optional[Path]:
    txt = str(value or "").strip()
    if not txt:
        return None
    try:
        win_match = re.match(r"^([A-Za-z]):[\\/](.*)$", txt)
        if win_match and os.name != "nt":
            drive = win_match.group(1).lower()
            rest = PureWindowsPath(txt).parts[1:]
            wine_prefix = Path(os.environ.get("WINEPREFIX", "~/.wine")).expanduser()
            return wine_prefix / f"drive_{drive}" / Path(*rest)
        return Path(txt).expanduser()
    except Exception:
        return None


def _json_safe_load(value: object, default):
    if isinstance(value, (dict, list)):
        return value
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except Exception:
        return default
    return parsed


def _state_key(event: VaultLogEvent) -> str:
    return "|".join(
        [
            event.log_path,
            str(int(event.timestamp_utc or 0.0)),
            event.kind,
            event.sender,
            event.alias.upper(),
            event.code_text.upper(),
            event.queue_id.upper(),
            ",".join(str(num) for num in event.block_numbers),
        ]
    )


def _log_cursor_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except Exception:
        return str(path)


def _read_log_increment(path: Path, cursor: Mapping[str, object], *, initial_max_bytes: int = 262144, overlap_bytes: int = 4096) -> Tuple[str, Dict[str, object], bool]:
    try:
        stat = path.stat()
    except Exception as exc:
        log.debug("varac_bbs_vault: failed to stat %s: %s", path, exc)
        return "", dict(cursor or {}), False

    size = int(getattr(stat, "st_size", 0) or 0)
    inode = int(getattr(stat, "st_ino", 0) or 0)
    device = int(getattr(stat, "st_dev", 0) or 0)
    previous_offset = int((cursor or {}).get("offset", 0) or 0)
    previous_inode = int((cursor or {}).get("inode", 0) or 0)
    previous_device = int((cursor or {}).get("device", 0) or 0)
    previous_mtime_ns = int((cursor or {}).get("mtime_ns", 0) or 0)
    current_mtime_ns = int(getattr(stat, "st_mtime_ns", 0) or 0)

    rotated = bool(previous_offset and (size < previous_offset or (previous_inode and inode and previous_inode != inode) or (previous_device and device and previous_device != device)))
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
        log.debug("varac_bbs_vault: failed to read %s: %s", path, exc)
        return "", dict(cursor or {}), False

    next_cursor = {
        "path": str(path),
        "offset": size,
        "size": size,
        "mtime_ns": current_mtime_ns,
        "inode": inode,
        "device": device,
    }
    return text, next_cursor, True


def _read_tail(path: Path, max_bytes: int = 262144) -> str:
    try:
        with path.open("rb") as handle:
            try:
                handle.seek(-max_bytes, os.SEEK_END)
            except OSError:
                handle.seek(0)
            return handle.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.debug("varac_bbs_vault: failed to tail %s: %s", path, exc)
        return ""


def _parse_timestamp(stamp: str) -> float:
    return parse_varac_event_timestamp_to_epoch(stamp, prefer_day_first=True)


def _parse_db_timestamp(value: object) -> float:
    txt = str(value or "").strip()
    if not txt:
        return 0.0
    txt = txt.replace("T", " ").replace("Z", "")
    if "." in txt:
        head, frac = txt.split(".", 1)
        digits = "".join(ch for ch in frac if ch.isdigit())
        if digits:
            txt = f"{head}.{digits[:6].ljust(6, '0')}"
        else:
            txt = head
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(txt, fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        except Exception:
            continue
    return 0.0


def _split_log_events(text: str) -> List[str]:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    matches = list(EVENT_TS_SCAN_RE.finditer(raw))
    if not matches:
        return []
    events: List[str] = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        segment = raw[match.start() : end].strip("\n")
        if segment:
            events.append(segment)
    return events


def _base_callsign(value: object) -> str:
    return _normalize_callsign(value).split("/", 1)[0]


def _callsign_matches(candidate: object, expected: object) -> bool:
    left = _normalize_callsign(candidate)
    right = _normalize_callsign(expected)
    if not left or not right:
        return False
    return left == right or _base_callsign(left) == _base_callsign(right)


def _local_callsign_set(values: Iterable[object]) -> set[str]:
    out: set[str] = set()
    for value in values:
        call = _normalize_callsign(value)
        if not call:
            continue
        out.add(call)
        base = _base_callsign(call)
        if base:
            out.add(base)
    return out


def _extract_sender(body: str) -> str:
    upper = str(body or "").upper()
    for pattern in CALLSIGN_PATTERNS:
        match = re.search(pattern, upper)
        if match:
            return _normalize_callsign(match.group(1))
    tokens = [tok for tok in CALLSIGN_RE.findall(upper) if tok not in IGNORED_CALLSIGN_TOKENS]
    if tokens:
        return _normalize_callsign(tokens[0])
    return ""


def _extract_log_message_text(body: str) -> str:
    payloads: List[str] = []
    for line in str(body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.search(r"(?<!<)\b[A-Z0-9/]{3,15}>\s*(.*)$", line, re.IGNORECASE)
        payloads.append(str(match.group(1) if match else line).strip())
    return " ".join(payloads).strip()


def _entry_includes_bbs_refresh(value: object) -> bool:
    return bool(re.search(r"(?i)(?:^|\s)<BLR>(?:\s|$)", str(value or "")))


def _parse_flamp_block_numbers(value: object) -> Tuple[int, ...]:
    return tuple(
        int(part.strip())
        for part in str(value or "").split(",")
        if part.strip().isdigit()
    )


def _normalize_bbs_command_text(value: object) -> str:
    text = _extract_log_message_text(str(value or ""))
    text = text.replace("Ø", "0").replace("ø", "0")
    upper = " ".join(text.split()).upper()
    upper = re.sub(r"^DE\s+[A-Z0-9/+\-]{3,15}\s*", "", upper).strip()
    if upper != "<BLR>":
        upper = re.sub(r"\s+<BLR>\s*$", "", upper).strip()
    while True:
        cleaned = re.sub(r"^<R[+\-]?\d+>\s*", "", upper).strip()
        cleaned = re.sub(r"^<S:[A-Z]>\s*", "", cleaned).strip()
        if cleaned == upper:
            break
        upper = cleaned
    return upper


def parse_vault_log_events(
    text: str,
    *,
    trigger_mode: str = DEFAULT_TRIGGER_MODE,
    log_path: str = "",
    alias_map: Optional[Mapping[str, str]] = None,
    local_callsigns: Optional[Iterable[object]] = None,
) -> List[VaultLogEvent]:
    events: List[VaultLogEvent] = []
    exact_mode = str(trigger_mode or DEFAULT_TRIGGER_MODE).strip().lower() == "exact code only"
    aliases = alias_map or {}
    local_set = _local_callsign_set(local_callsigns or ())
    active_remote = ""
    for block in _split_log_events(text):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        match = EVENT_TS_RE.match(lines[0])
        if not match:
            continue
        body = "\n".join([match.group("body")] + lines[1:]).strip()
        timestamp_utc = _parse_timestamp(match.group("stamp"))
        upper_body = body.upper()
        connected = CONNECTED_RE.search(upper_body)
        if connected:
            active_remote = _normalize_callsign(connected.group(1))
            continue
        disconnect_match = DISCONNECT_RE.search(upper_body)
        if disconnect_match:
            sender = active_remote or _extract_sender(body)
            events.append(
                VaultLogEvent(
                    timestamp_utc=timestamp_utc,
                    kind="disconnect",
                    sender=sender,
                    body=body,
                    raw_line=block,
                    log_path=log_path,
                )
            )
            active_remote = ""
            continue

        chat_match = CHAT_SENDER_RE.match(lines[0])
        sender = _normalize_callsign(chat_match.group(1)) if chat_match else _extract_sender(body)
        if sender and local_set and (_normalize_callsign(sender) in local_set or _base_callsign(sender) in local_set):
            continue
        if active_remote and sender and not _callsign_matches(sender, active_remote):
            continue
        if re.search(r"(?i)^[A-Z0-9/]{3,15}>\s*<BL:", lines[0].strip()):
            continue
        if re.search(r"(?i)^[A-Z0-9/]{3,15}>\s*<(?:BG|SF|SFRD|SFOK)[:>]", lines[0].strip()):
            continue

        message_text = _extract_log_message_text(body)
        command_text = _normalize_bbs_command_text(message_text)
        refresh_requested = _entry_includes_bbs_refresh(body)
        code_text = ""
        alias = ""
        kind = ""
        queue_id = ""
        block_numbers: Tuple[int, ...] = ()
        alias, code_text = _extract_alias_request(command_text, aliases)
        if alias:
            kind = "open_alias"
        else:
            command_match = COMMAND_RE.search(body)
            if command_match:
                code_text = str(command_match.group(1) or "").strip()
                kind = "unlock"
        if not kind and ROOT_CMD_RE.match(command_text):
            kind = "root_return"
        if not kind and refresh_requested and command_text in {"", "<BLR>"}:
            kind = "root_request"
        if not kind and FLAMP_CMD_RE.match(command_text):
            kind = "flamp_help"
            code_text = command_text
        if not kind and LIST_Q_RE.match(command_text):
            kind = "flamp_list_q"
            code_text = command_text
        if not kind:
            list_blocks_match = LIST_BLOCKS_RE.match(command_text)
            if list_blocks_match:
                kind = "flamp_list_blocks"
                queue_id = str(list_blocks_match.group(1) or "").upper()
                code_text = command_text
        if not kind:
            block_match = BLOCK_REQUEST_RE.match(command_text)
            if block_match:
                nums = _parse_flamp_block_numbers(block_match.group(1))
                kind = "flamp_block_request"
                queue_id = str(block_match.group(2) or "").upper()
                block_numbers = nums
                code_text = command_text
        if not kind:
            invalid_block_match = INVALID_BLOCK_REQUEST_RE.match(command_text)
            if invalid_block_match:
                kind = "flamp_invalid_block_request"
                queue_id = str(invalid_block_match.group(1) or "").upper()
                code_text = command_text
        if not kind and exact_mode:
            stripped = command_text
            alias, code_text = _extract_alias_request(stripped, aliases)
            if alias:
                kind = "open_alias"
            elif ROOT_CMD_RE.match(stripped):
                kind = "root_return"
            elif stripped:
                code_text = stripped
                kind = "unlock"
        elif not kind and DISCONNECT_RE.search(body):
            kind = "disconnect"
        if not kind:
            continue
        events.append(
            VaultLogEvent(
                timestamp_utc=_parse_timestamp(match.group("stamp")),
                kind=kind,
                sender=sender,
                body=body,
                code_text=code_text,
                alias=alias,
                queue_id=queue_id,
                block_numbers=block_numbers,
                raw_line=block,
                log_path=log_path,
                refresh_requested=refresh_requested,
            )
        )
    return events


def hash_access_code(code: str, *, salt: Optional[str] = None, iterations: int = DEFAULT_ACCESS_CODE_ITERATIONS) -> Dict[str, object]:
    clean = _normalize_access_code_text(code)
    if not clean:
        return {
            "access_code_hash": "",
            "access_code_salt": "",
            "access_code_iterations": int(iterations),
        }
    raw_salt = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", clean.encode("utf-8"), raw_salt, int(iterations))
    return {
        "access_code_hash": digest.hex(),
        "access_code_salt": raw_salt.hex(),
        "access_code_iterations": int(iterations),
    }


def verify_access_code(code: str, *, access_code_hash: str, access_code_salt: str, access_code_iterations: int) -> bool:
    clean = _normalize_access_code_text(code)
    if not clean or not access_code_hash or not access_code_salt:
        return False
    try:
        expected = bytes.fromhex(str(access_code_hash).strip())
        salt = bytes.fromhex(str(access_code_salt).strip())
        iterations = int(access_code_iterations or DEFAULT_ACCESS_CODE_ITERATIONS)
    except Exception:
        return False
    candidates = []
    for candidate in (clean, clean.upper(), clean.lower()):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        actual = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt, iterations)
        if hmac.compare_digest(actual, expected):
            return True
    return False


def load_vault_locations(value: object) -> List[VaultLocation]:
    parsed = _json_safe_load(value, [])
    if not isinstance(parsed, list):
        return []
    locations: List[VaultLocation] = []
    existing_ids: List[str] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        try:
            access_code_iterations = int(
                row.get("access_code_iterations", DEFAULT_ACCESS_CODE_ITERATIONS) or DEFAULT_ACCESS_CODE_ITERATIONS
            )
        except Exception:
            access_code_iterations = DEFAULT_ACCESS_CODE_ITERATIONS
        try:
            retention_days = max(0, int(row.get("retention_days", 0) or 0))
        except Exception:
            retention_days = 0
        name = _clean_location_name(row.get("name", ""))
        if not name:
            continue
        location_id = _ensure_location_id(existing_ids, name, row.get("id", ""))
        existing_ids.append(location_id)
        locations.append(
            VaultLocation(
                id=location_id,
                name=name,
                source_dir=str(row.get("source_dir", "") or "").strip(),
                enabled=bool(row.get("enabled", True)),
                inherit_global_allowed_callsigns=bool(row.get("inherit_global_allowed_callsigns", True)),
                allowed_callsigns=tuple(parse_callsign_list(row.get("allowed_callsigns", []))),
                access_code_hash=str(row.get("access_code_hash", "") or "").strip(),
                access_code_salt=str(row.get("access_code_salt", "") or "").strip(),
                access_code_iterations=access_code_iterations,
                access_code_plaintext=str(row.get("access_code_plaintext", "") or "").strip(),
                alias=normalize_location_alias(row.get("alias", ""), name),
                description=str(row.get("description", "") or "").strip(),
                list_in_root_menu=bool(row.get("list_in_root_menu", True)),
                visibility_rule=str(row.get("visibility_rule", "Public") or "Public").strip() or "Public",
                open_rule=str(row.get("open_rule", "Public") or "Public").strip() or "Public",
                retention_policy=str(
                    row.get("retention_policy", "Use global BBS archive policy")
                    or "Use global BBS archive policy"
                ).strip()
                or "Use global BBS archive policy",
                retention_days=retention_days,
            )
        )
    return locations


def vault_locations_to_data(locations: Sequence[VaultLocation]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for location in locations:
        out.append(
            {
                "id": location.id,
                "name": location.name,
                "source_dir": location.source_dir,
                "enabled": bool(location.enabled),
                "inherit_global_allowed_callsigns": bool(location.inherit_global_allowed_callsigns),
                "allowed_callsigns": list(location.allowed_callsigns),
                "access_code_hash": location.access_code_hash,
                "access_code_salt": location.access_code_salt,
                "access_code_iterations": int(location.access_code_iterations or DEFAULT_ACCESS_CODE_ITERATIONS),
                "access_code_plaintext": location.access_code_plaintext,
                "alias": location.alias,
                "description": location.description,
                "list_in_root_menu": bool(location.list_in_root_menu),
                "visibility_rule": location.visibility_rule,
                "open_rule": location.open_rule,
                "retention_policy": location.retention_policy,
                "retention_days": int(location.retention_days or 0),
            }
        )
    return out


def load_vault_runtime_state(value: object) -> VaultRuntimeState:
    parsed = _json_safe_load(value, {})
    if not isinstance(parsed, dict):
        return VaultRuntimeState()
    cooldowns = parsed.get("cooldowns", {})
    if not isinstance(cooldowns, dict):
        cooldowns = {}
    failed_attempts = parsed.get("failed_attempts", {})
    if not isinstance(failed_attempts, dict):
        failed_attempts = {}
    log_cursors = parsed.get("log_cursors", {})
    if not isinstance(log_cursors, dict):
        log_cursors = {}
    return VaultRuntimeState(
        current_location_id=str(parsed.get("current_location_id", DEFAULT_LOCATION_ID) or DEFAULT_LOCATION_ID).strip() or DEFAULT_LOCATION_ID,
        current_session_callsign=_normalize_callsign(parsed.get("current_session_callsign", "")),
        current_session_qso_guid=str(parsed.get("current_session_qso_guid", "") or "").strip(),
        current_view_mode=str(parsed.get("current_view_mode", DEFAULT_VIEW_MODE) or DEFAULT_VIEW_MODE).strip() or DEFAULT_VIEW_MODE,
        current_view_label=str(parsed.get("current_view_label", DEFAULT_LOCATION_NAME) or DEFAULT_LOCATION_NAME).strip() or DEFAULT_LOCATION_NAME,
        previous_location_id=str(parsed.get("previous_location_id", DEFAULT_LOCATION_ID) or DEFAULT_LOCATION_ID).strip() or DEFAULT_LOCATION_ID,
        previous_view_mode=str(parsed.get("previous_view_mode", DEFAULT_VIEW_MODE) or DEFAULT_VIEW_MODE).strip() or DEFAULT_VIEW_MODE,
        previous_view_label=str(parsed.get("previous_view_label", DEFAULT_LOCATION_NAME) or DEFAULT_LOCATION_NAME).strip() or DEFAULT_LOCATION_NAME,
        current_overlay_file=str(parsed.get("current_overlay_file", "") or "").strip(),
        processed_event_keys=tuple(
            str(item or "").strip()
            for item in parsed.get("processed_event_keys", [])
            if str(item or "").strip()
        ),
        cooldowns={str(key or "").strip(): dict(value) for key, value in cooldowns.items() if str(key or "").strip() and isinstance(value, dict)},
        failed_attempts={
            str(key or "").strip(): [float(ts) for ts in value if isinstance(ts, (int, float))]
            for key, value in failed_attempts.items()
            if str(key or "").strip() and isinstance(value, list)
        },
        last_publish_manifest_path=str(parsed.get("last_publish_manifest_path", "") or "").strip(),
        last_publish_ts=float(parsed.get("last_publish_ts", 0.0) or 0.0),
        last_action=str(parsed.get("last_action", "") or "").strip(),
        last_request_ts=float(parsed.get("last_request_ts", 0.0) or 0.0),
        last_error=str(parsed.get("last_error", "") or "").strip(),
        unmanaged_live_files=tuple(
            str(item or "").strip()
            for item in parsed.get("unmanaged_live_files", [])
            if str(item or "").strip()
        ),
        last_datastream_id=int(parsed.get("last_datastream_id", 0) or 0),
        log_cursors={
            str(key or "").strip(): dict(value)
            for key, value in log_cursors.items()
            if str(key or "").strip() and isinstance(value, dict)
        },
    )


def vault_runtime_state_to_data(state: VaultRuntimeState) -> Dict[str, object]:
    return {
        "current_location_id": state.current_location_id,
        "current_session_callsign": state.current_session_callsign,
        "current_session_qso_guid": state.current_session_qso_guid,
        "current_view_mode": state.current_view_mode,
        "current_view_label": state.current_view_label,
        "previous_location_id": state.previous_location_id,
        "previous_view_mode": state.previous_view_mode,
        "previous_view_label": state.previous_view_label,
        "current_overlay_file": state.current_overlay_file,
        "processed_event_keys": list(state.processed_event_keys),
        "cooldowns": {str(key): dict(value) for key, value in state.cooldowns.items()},
        "failed_attempts": {str(key): list(value) for key, value in state.failed_attempts.items()},
        "last_publish_manifest_path": state.last_publish_manifest_path,
        "last_publish_ts": float(state.last_publish_ts or 0.0),
        "last_action": state.last_action,
        "last_request_ts": float(state.last_request_ts or 0.0),
        "last_error": state.last_error,
        "unmanaged_live_files": list(state.unmanaged_live_files),
        "last_datastream_id": int(state.last_datastream_id or 0),
        "log_cursors": {str(key): dict(value) for key, value in state.log_cursors.items()},
    }


def compute_default_managed_root(live_bbs_dir: object) -> str:
    live_path = _resolve_path(live_bbs_dir)
    if live_path is None:
        return ""
    return str(live_path.parent / "FIO_BBS_Vault")


def build_varac_bbs_vault_activity_signature(settings) -> Tuple[object, ...]:
    """Return cheap, non-consuming evidence that a full vault run may be needed."""

    def stat_signature(path_value: object) -> Tuple[str, bool, int, int]:
        if path_value is None:
            return ("", False, 0, 0)
        path_text = str(path_value or "").strip()
        if not path_text:
            return ("", False, 0, 0)
        try:
            path = Path(path_text)
            stat = path.stat()
            return (str(path), True, int(stat.st_size or 0), int(stat.st_mtime_ns or 0))
        except Exception:
            return (path_text, False, 0, 0)

    if settings is None:
        return ("disabled",)
    live_bbs_dir = str(settings.get("varac_bbs_dir", "") or "").strip()
    managed_root = compute_default_managed_root(live_bbs_dir)
    locations = load_vault_locations(settings.get("varac_bbs_vault_locations_v1", []))
    catalog_path = _explicit_catalog_db_path(settings)
    if catalog_path:
        locations, station_meta = _station_catalog_vault_configuration(catalog_path, locations)
        managed_root = str(station_meta.get("station_managed_root") or managed_root)
    flamp_enabled = bool(settings.get("varac_bbs_vault_flamp_enabled", False))
    flamp_relay_dir = str(settings.get("varac_bbs_vault_flamp_relay_dir", "") or "").strip()
    config_keys = (
        "varac_bbs_vault_enabled",
        "varac_bbs_vault_default_location_id",
        "varac_bbs_vault_locations_v1",
        "varac_bbs_allowed_callsigns",
        "varac_bbs_limit_access_enabled",
        "varac_bbs_vault_flamp_enabled",
        "varac_bbs_vault_flamp_relay_dir",
        "varac_bbs_vault_flamp_listing_max_age_days",
    )
    config_payload = {key: settings.get(key, None) for key in config_keys}
    config_payload["station_root"] = managed_root
    config_payload["station_locations"] = [(row.id, row.source_dir, row.enabled) for row in locations]
    config_signature = json.dumps(config_payload, sort_keys=True, default=str)
    path_signatures: List[Tuple[str, bool, int, int]] = [
        stat_signature(live_bbs_dir),
        stat_signature(managed_root),
        stat_signature(_resolve_varac_db_path(settings)),
    ]
    for log_path in resolve_varac_traffic_log_paths(settings):
        path_signatures.append(stat_signature(log_path))
    for location in locations:
        path_signatures.append(stat_signature(location.source_dir))
    if flamp_enabled:
        path_signatures.append(stat_signature(flamp_relay_dir))
    return (config_signature, tuple(path_signatures))


def _managed_root_paths(managed_root: object) -> Dict[str, Path]:
    root = _resolve_path(managed_root)
    if root is None:
        raise ValueError("Managed root path is required")
    return {
        "root": root,
        "locations": root / "locations",
        "default": root / "locations" / DEFAULT_LOCATION_NAME,
        "runtime": root / "runtime",
        "manifests": root / "runtime" / "manifests",
        "tmp": root / "runtime" / "tmp",
        "logs": root / "runtime" / "logs",
    }


def initialize_managed_root(managed_root: object) -> Dict[str, str]:
    paths = _managed_root_paths(managed_root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {name: str(path) for name, path in paths.items()}


def import_live_bbs_to_default_location(live_bbs_dir: object, default_location_dir: object) -> int:
    live_dir = _resolve_path(live_bbs_dir)
    default_dir = _resolve_path(default_location_dir)
    if live_dir is None or default_dir is None:
        return 0
    if not live_dir.exists() or not live_dir.is_dir():
        return 0
    default_dir.mkdir(parents=True, exist_ok=True)
    imported = 0
    for child in sorted(live_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_file():
            continue
        dst = default_dir / child.name
        if dst.exists():
            stem = dst.stem
            suffix = dst.suffix
            counter = 2
            while dst.exists():
                dst = default_dir / f"{stem}-{counter}{suffix}"
                counter += 1
        shutil.copy2(child, dst)
        imported += 1
    return imported


def _safe_relative_file_path(src: Path, source_root: Optional[Path]) -> Path:
    if source_root is None:
        return Path(src.name)
    try:
        rel = src.relative_to(source_root)
    except Exception:
        rel = Path(src.name)
    if any(part in {"", ".", ".."} for part in rel.parts):
        return Path(src.name)
    return rel


def unique_bbs_archive_destination(
    src: object,
    *,
    archive_dir: object,
    source_root: object = None,
    archive_context: object = "live",
) -> Path:
    src_path = _resolve_path(src)
    archive_root = _resolve_path(archive_dir)
    if src_path is None:
        raise ValueError("Source file is required")
    if archive_root is None:
        raise ValueError("Archive directory is required")
    context = re.sub(r"[^A-Za-z0-9_.:/+\-]+", "_", str(archive_context or "live").strip()) or "live"
    root_path = _resolve_path(source_root) if source_root is not None else None
    rel = _safe_relative_file_path(src_path, root_path)
    dst = archive_root / context / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        return dst
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = dst.parent / f"{src_path.stem}_{stamp}{src_path.suffix}"
    attempt = 2
    while dst.exists():
        dst = dst.parent / f"{src_path.stem}_{stamp}_{attempt}{src_path.suffix}"
        attempt += 1
    return dst


def archive_bbs_file(
    file_path: object,
    *,
    archive_dir: object,
    source_root: object = None,
    archive_context: object = "live",
) -> BbsFileActionResult:
    src = _resolve_path(file_path)
    if src is None:
        return BbsFileActionResult(False, "archive", "", detail="Source file is required")
    if not src.exists():
        return BbsFileActionResult(False, "archive", str(src), detail="Source file does not exist")
    if not src.is_file():
        return BbsFileActionResult(False, "archive", str(src), detail="Source path is not a file")
    try:
        dst = unique_bbs_archive_destination(
            src,
            archive_dir=archive_dir,
            source_root=source_root,
            archive_context=archive_context,
        )
        shutil.move(str(src), str(dst))
        return BbsFileActionResult(True, "archive", str(src), str(dst), "Archived BBS file")
    except Exception as exc:
        return BbsFileActionResult(False, "archive", str(src), detail=str(exc))


def delete_bbs_file(file_path: object) -> BbsFileActionResult:
    src = _resolve_path(file_path)
    if src is None:
        return BbsFileActionResult(False, "delete", "", detail="Source file is required")
    if not src.exists():
        return BbsFileActionResult(False, "delete", str(src), detail="Source file does not exist")
    if not src.is_file():
        return BbsFileActionResult(False, "delete", str(src), detail="Source path is not a file")
    try:
        src.unlink()
        return BbsFileActionResult(True, "delete", str(src), detail="Deleted BBS file")
    except Exception as exc:
        return BbsFileActionResult(False, "delete", str(src), detail=str(exc))


def bbs_file_management_roots(settings) -> List[Dict[str, str]]:
    if settings is None:
        return []
    rows: List[Dict[str, str]] = []

    def _setting(key: str, default: object = "") -> object:
        try:
            return settings.get(key, default)
        except Exception:
            return default

    def _add(kind: str, label: str, path_value: object, archive_context: str) -> None:
        path_txt = str(path_value or "").strip()
        if not path_txt:
            return
        rows.append(
            {
                "kind": kind,
                "label": label,
                "path": path_txt,
                "archive_context": archive_context,
            }
        )

    message_paths = _setting("message_paths", {})
    if not isinstance(message_paths, Mapping):
        message_paths = {}
    _add("incoming", "VarAC Incoming", message_paths.get("varac", ""), "incoming")
    _add("outgoing", "VarAC Outgoing", _setting("varac_outbox_dir", ""), "outgoing")
    _add("live_bbs", "Live BBS", _setting("varac_bbs_dir", ""), "live")
    for location in load_vault_locations(_setting("varac_bbs_vault_locations_v1", [])):
        if not location.enabled:
            continue
        if not str(location.source_dir or "").strip():
            continue
        context_id = re.sub(r"[^A-Za-z0-9_.:/+\-]+", "_", str(location.id or location.name or "location"))
        _add("managed_location", f"Managed Location: {location.name}", location.source_dir, f"locations/{context_id}")
    _add("archive", "BBS Archive", _setting("varac_bbs_archive_dir", ""), "archive")
    return rows


def _manifest_path_for(managed_root: object, live_bbs_dir: object = "") -> Path:
    paths = _managed_root_paths(managed_root)
    live_path = _resolve_path(live_bbs_dir)
    if live_path is None:
        return paths["manifests"] / "current_publish_manifest.json"
    identity = hashlib.sha1(str(live_path).encode("utf-8")).hexdigest()[:12]
    return paths["manifests"] / f"current_publish_manifest-{identity}.json"


def _audit_log_path_for(managed_root: object) -> Path:
    paths = _managed_root_paths(managed_root)
    return paths["logs"] / "vault_audit.jsonl"


def read_publish_manifest(path: object) -> List[VaultPublishManifestEntry]:
    manifest_path = _resolve_path(path)
    if manifest_path is None or not manifest_path.exists():
        return []
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    entries: List[VaultPublishManifestEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        live_name = str(item.get("live_name", "") or "").strip()
        source_name = str(item.get("source_name", "") or "").strip()
        if not live_name or not source_name:
            continue
        try:
            size = int(item.get("size", 0) or 0)
        except Exception:
            size = 0
        try:
            mtime_ns = int(item.get("mtime_ns", 0) or 0)
        except Exception:
            mtime_ns = 0
        entries.append(
            VaultPublishManifestEntry(
                source_name=source_name,
                live_name=live_name,
                size=size,
                mtime_ns=mtime_ns,
                sha256=str(item.get("sha256", "") or "").strip(),
                source_path=str(item.get("source_path", "") or "").strip(),
            )
        )
    return entries


def write_publish_manifest(path: object, entries: Sequence[VaultPublishManifestEntry]) -> str:
    manifest_path = _resolve_path(path)
    if manifest_path is None:
        raise ValueError("Manifest path is required")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(entry) for entry in entries]
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return str(manifest_path)


def _publish_manifest_text(entries: Sequence[VaultPublishManifestEntry]) -> str:
    return json.dumps([asdict(entry) for entry in entries], indent=2, sort_keys=True)


def scan_location_files(source_dir: object) -> Tuple[List[Path], int]:
    source_path = _resolve_path(source_dir)
    if source_path is None or not source_path.exists() or not source_path.is_dir():
        return [], 0
    files: List[Path] = []
    ignored_dirs = 0
    for child in sorted(source_path.iterdir(), key=lambda item: item.name.lower()):
        if child.is_dir():
            ignored_dirs += 1
            continue
        if child.is_file():
            files.append(child)
    return files, ignored_dirs


def _location_folder_candidates(location: VaultLocation, managed_root: object) -> List[Path]:
    try:
        locations_root = _managed_root_paths(managed_root)["locations"]
    except Exception:
        return []
    names = [
        str(location.name or "").strip(),
        str(location.alias or "").strip(),
        str(location.id or "").strip(),
    ]
    candidates: List[Path] = []
    seen: set[str] = set()
    for name in names:
        if not name:
            continue
        candidate = locations_root / name
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _effective_location_source_dir(location: VaultLocation, managed_root: object) -> object:
    configured_files, _ignored = scan_location_files(location.source_dir)
    if configured_files:
        return location.source_dir
    for candidate in _location_folder_candidates(location, managed_root):
        candidate_files, _candidate_ignored = scan_location_files(candidate)
        if candidate_files:
            return candidate
    return location.source_dir


def _with_filesystem_location_fallbacks(
    locations: Sequence[VaultLocation],
    managed_root: object,
    *,
    default_location_id: str,
) -> List[VaultLocation]:
    merged = list(locations)
    try:
        locations_root = _managed_root_paths(managed_root)["locations"]
    except Exception:
        return merged
    if not locations_root.exists() or not locations_root.is_dir():
        return merged

    existing_ids = [location.id for location in merged]
    existing_aliases = {normalize_location_alias(location.alias, location.name) for location in merged}
    existing_aliases.add("ROOT")
    for child in sorted(locations_root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or child.name == DEFAULT_LOCATION_NAME:
            continue
        alias = normalize_location_alias("", child.name)
        if not alias or alias in existing_aliases:
            continue
        files, _ignored = scan_location_files(child)
        if not files:
            continue
        location_id = _ensure_location_id(existing_ids, child.name, f"fs-{_location_id_from_name(child.name)}")
        existing_ids.append(location_id)
        existing_aliases.add(alias)
        merged.append(
            VaultLocation(
                id=location_id,
                name=child.name,
                source_dir=str(child),
                enabled=True,
                inherit_global_allowed_callsigns=True,
                allowed_callsigns=(),
                access_code_hash="",
                access_code_salt="",
                access_code_iterations=DEFAULT_ACCESS_CODE_ITERATIONS,
                access_code_plaintext="",
                alias=alias,
                description=f"Open {child.name}",
                list_in_root_menu=True,
                visibility_rule="Public",
                open_rule="Public",
            )
        )
    return merged


def build_publish_manifest(source_dir: object, *, virtual_files: Sequence[_VirtualFile] = ()) -> Tuple[List[VaultPublishManifestEntry], int]:
    files, ignored_dirs = scan_location_files(source_dir)
    manifest: List[VaultPublishManifestEntry] = []
    used_names: set[str] = set()

    def _unique_name(raw_name: str) -> str:
        safe_name = safe_varac_bbs_filename(raw_name, max_len=MAX_MANIFEST_FILENAME_LENGTH)
        if safe_name not in used_names:
            used_names.add(safe_name)
            return safe_name
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        counter = 2
        candidate = f"{stem}-{counter}{suffix}"
        while candidate in used_names:
            counter += 1
            candidate = f"{stem}-{counter}{suffix}"
        used_names.add(candidate)
        return candidate

    for child in files:
        try:
            st = child.stat()
        except OSError:
            continue
        live_name = _unique_name(child.name)
        manifest.append(
            VaultPublishManifestEntry(
                source_name=child.name,
                live_name=live_name,
                size=int(st.st_size or 0),
                mtime_ns=int(st.st_mtime_ns or 0),
            )
        )
    for virtual in virtual_files:
        content = str(virtual.content or "")
        live_name = _unique_name(virtual.name)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest.append(
            VaultPublishManifestEntry(
                source_name=f"@virtual/{virtual.name}",
                live_name=live_name,
                size=len(content.encode("utf-8")),
                mtime_ns=0,
                sha256=digest,
            )
        )
    return manifest, ignored_dirs


def _hash_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _entry_map(entries: Sequence[VaultPublishManifestEntry]) -> Dict[str, VaultPublishManifestEntry]:
    return {entry.live_name: entry for entry in entries}


def _is_fio_bbs_generated_listing(name: object) -> bool:
    clean = Path(str(name or "").strip()).name.upper()
    if clean.startswith((
        "BBS MSG - ",
        "00 HOW TO USE -",
        "00 READ FIRST -",
        "00 NOTICE -",
        "01 COMMANDS -",
    )):
        return True
    if re.match(r"^\d{2} TYPE .+(?:\.TXT)?$", clean):
        return True
    return clean.startswith((DEFAULT_FLAMP_QUEUE_HELPER_NAME.upper(), f"{DEFAULT_FLAMP_BLOCK_PREFIX}_"))


def _source_path_for_entry(entry: VaultPublishManifestEntry, src_root: Optional[Path]) -> Optional[Path]:
    if str(entry.source_path or "").strip():
        return _resolve_path(entry.source_path)
    if src_root is None:
        return None
    return src_root / entry.source_name


def _entries_equal(a: VaultPublishManifestEntry, b: VaultPublishManifestEntry, *, src_root: Optional[Path] = None, dst_root: Optional[Path] = None) -> bool:
    if a.size != b.size or a.mtime_ns != b.mtime_ns or a.source_name != b.source_name or a.source_path != b.source_path:
        return False
    if a.sha256 and b.sha256:
        return a.sha256 == b.sha256
    if a.source_name.startswith("@virtual/") or b.source_name.startswith("@virtual/"):
        return bool(a.sha256 and b.sha256 and a.sha256 == b.sha256)
    if src_root is None or dst_root is None:
        return True
    try:
        source_path = _source_path_for_entry(a, src_root)
        if source_path is None:
            return False
        src_hash = _hash_file(source_path)
        dst_hash = _hash_file(dst_root / a.live_name)
    except Exception:
        return False
    return src_hash == dst_hash


def _db_backed_manifest_entries(
    location: VaultLocation,
    *,
    manifest_db_path: object = "",
    virtual_files: Sequence[_VirtualFile] = (),
) -> Optional[List[VaultPublishManifestEntry]]:
    # A caller that has a catalog identity must pass it explicitly.  Empty
    # paths intentionally select the legacy filesystem manifest, rather than
    # silently borrowing the process-wide FIO database.
    if not str(manifest_db_path or "").strip():
        return None
    db_path = _resolve_path(manifest_db_path)
    if db_path is None or not db_path.exists():
        return None
    try:
        with connect_sqlite(db_path) as conn:
            ensure_bbs_library_schema(conn)
            if not location_has_bbs_catalog(conn, location.id):
                return None
            configured_source = str(_effective_location_source_dir(location, "") or "").strip()
            catalog_source = bbs_location_catalog_source_dir(conn, location.id)
            if configured_source and catalog_source and str(Path(catalog_source).expanduser()) != str(Path(configured_source).expanduser()):
                return None
            rows = list_bbs_location_manifest_rows(conn, location.id)
    except Exception as exc:
        log_bbs_library_sync_failure(exc)
        return None

    entries: List[VaultPublishManifestEntry] = []
    used_names: set[str] = set()

    def _unique_name(raw_name: str) -> str:
        safe_name = safe_varac_bbs_filename(raw_name, max_len=MAX_MANIFEST_FILENAME_LENGTH)
        if safe_name not in used_names:
            used_names.add(safe_name)
            return safe_name
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        counter = 2
        candidate = f"{stem}-{counter}{suffix}"
        while candidate in used_names:
            counter += 1
            candidate = f"{stem}-{counter}{suffix}"
        used_names.add(candidate)
        return candidate

    for row in rows:
        source_path = _resolve_path(row.source_path)
        if source_path is None or not source_path.exists() or not source_path.is_file():
            continue
        entries.append(
            VaultPublishManifestEntry(
                source_name=f"@artifact/{row.artifact_id}",
                live_name=_unique_name(row.live_name or row.display_name or source_path.name),
                size=int(row.size or 0),
                mtime_ns=int(row.mtime_ns or 0),
                sha256=str(row.content_hash or ""),
                source_path=str(source_path),
            )
        )
    for virtual in virtual_files:
        content = str(virtual.content or "")
        entries.append(
            VaultPublishManifestEntry(
                source_name=f"@virtual/{virtual.name}",
                live_name=_unique_name(virtual.name),
                size=len(content.encode("utf-8")),
                mtime_ns=0,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
    return entries


def _publish_manifest_entries(
    entries: Sequence[VaultPublishManifestEntry],
    *,
    source_dir: object,
    live_bbs_dir: object,
    managed_root: object,
    virtual_files: Sequence[_VirtualFile] = (),
) -> VaultPublishResult:
    live_dir = _resolve_path(live_bbs_dir)
    if live_dir is None:
        raise ValueError("Live BBS directory is required")
    live_dir.mkdir(parents=True, exist_ok=True)
    src_root = _resolve_path(source_dir)
    manifest_path = _manifest_path_for(managed_root, live_bbs_dir)
    previous_manifest = read_publish_manifest(manifest_path)
    previous_map = _entry_map(previous_manifest)
    next_map = _entry_map(entries)
    virtual_map = {f"@virtual/{entry.name}": entry for entry in virtual_files}

    published = 0
    for entry in entries:
        dst = live_dir / entry.live_name
        previous = previous_map.get(entry.live_name)
        if previous is not None and dst.exists() and _entries_equal(entry, previous, src_root=src_root, dst_root=live_dir):
            continue
        tmp_name = f".fio-vault-{uuid.uuid4().hex}.tmp"
        tmp_path = live_dir / tmp_name
        if entry.source_name.startswith("@virtual/"):
            virtual = virtual_map.get(entry.source_name)
            if virtual is None:
                continue
            tmp_path.write_text(str(virtual.content or ""), encoding="utf-8")
        else:
            source_path = _source_path_for_entry(entry, src_root)
            if source_path is None:
                raise ValueError("Location source directory is required")
            shutil.copy2(source_path, tmp_path)
        os.replace(tmp_path, dst)
        published += 1

    removed = 0
    for live_name, previous in previous_map.items():
        if live_name in next_map:
            continue
        target = live_dir / live_name
        if target.exists():
            try:
                target.unlink()
                removed += 1
            except OSError:
                pass
    for child in sorted(live_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_file():
            continue
        if child.name in next_map:
            continue
        if _is_fio_bbs_generated_listing(child.name):
            try:
                child.unlink()
                removed += 1
            except OSError:
                pass

    next_manifest_text = _publish_manifest_text(entries)
    existing_manifest_text = ""
    if manifest_path.exists():
        try:
            existing_manifest_text = manifest_path.read_text(encoding="utf-8")
        except Exception:
            existing_manifest_text = ""
    if not manifest_path.exists() or existing_manifest_text != next_manifest_text:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(next_manifest_text, encoding="utf-8")
        # Keep the historical path as a diagnostics-only pointer to the most
        # recently materialized instance. Runtime reconciliation always uses
        # the radio-specific manifest above.
        compatibility_manifest = _manifest_path_for(managed_root)
        compatibility_manifest.write_text(next_manifest_text, encoding="utf-8")
    else:
        log.debug("VARAC_VAULT_MANIFEST|unchanged|entries=%s|write_skipped=true", len(entries))

    unmanaged: List[str] = []
    tracked = set(next_map.keys())
    for child in sorted(live_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_file():
            continue
        if child.name.startswith(".fio-vault-"):
            continue
        if child.name not in tracked:
            unmanaged.append(child.name)

    changed = bool(published or removed or list(entries) != list(previous_manifest))
    return VaultPublishResult(
        changed=changed,
        published_count=published,
        removed_count=removed,
        unmanaged_live_files=tuple(unmanaged),
        manifest_path=str(manifest_path),
        ignored_directories=0,
    )


def publish_location(
    location: VaultLocation,
    *,
    live_bbs_dir: object,
    managed_root: object,
    manifest_db_path: object = "",
) -> VaultPublishResult:
    source_dir = _effective_location_source_dir(location, managed_root)
    db_entries = _db_backed_manifest_entries(location, manifest_db_path=manifest_db_path)
    if db_entries is not None:
        entries = db_entries
        ignored_dirs = 0
    else:
        entries, ignored_dirs = build_publish_manifest(source_dir)
    result = _publish_manifest_entries(
        entries,
        source_dir=source_dir,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


def _append_audit_event(managed_root: object, payload: Mapping[str, object]) -> None:
    try:
        audit_path = _audit_log_path_for(managed_root)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")
    except Exception as exc:
        log.debug("varac_bbs_vault: failed to append audit event: %s", exc)


def _sender_globally_allowed(sender: str, *, global_allowed_callsigns: Sequence[str], limit_access_enabled: bool) -> bool:
    normalized_sender = _normalize_callsign(sender)
    if not normalized_sender:
        return False
    if not limit_access_enabled:
        return True
    global_allowed = {item for item in parse_callsign_list(global_allowed_callsigns)}
    return not global_allowed or normalized_sender in global_allowed


def _location_allowed_subset(sender: str, location: VaultLocation) -> bool:
    normalized_sender = _normalize_callsign(sender)
    if not normalized_sender:
        return False
    if location.inherit_global_allowed_callsigns:
        return True
    allowed = {item for item in parse_callsign_list(location.allowed_callsigns)}
    return not allowed or normalized_sender in allowed


def _location_sender_allowed(
    sender: str,
    *,
    location: VaultLocation,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
) -> bool:
    return _sender_globally_allowed(
        sender,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
    ) and _location_allowed_subset(sender, location)


def _location_requires_code(location: VaultLocation, *, default_location_id: str, global_code_policy: str) -> bool:
    if location.id == default_location_id:
        return False
    open_rule = str(location.open_rule or "Public").strip()
    if open_rule == "Public":
        return False
    if open_rule == "Access code required":
        return True
    policy = str(global_code_policy or DEFAULT_GLOBAL_CODE_POLICY).strip()
    if open_rule == "Allowed callsigns + access code":
        return True
    if policy == "Require for non-default locations":
        return True
    if policy == "Require for all restricted locations" and open_rule != "Public":
        return True
    return False


def _location_visible_in_root(
    location: VaultLocation,
    *,
    sender: str,
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    global_code_policy: str,
) -> bool:
    if not location.enabled or location.id == default_location_id or not location.list_in_root_menu:
        return False
    rule = str(location.visibility_rule or "Public").strip()
    if rule == "Hidden":
        return False
    if rule == "Public":
        return True
    if not _normalize_callsign(sender):
        return False
    if rule == "Allowed callsigns only":
        return _location_sender_allowed(
            sender,
            location=location,
            global_allowed_callsigns=global_allowed_callsigns,
            limit_access_enabled=limit_access_enabled,
        )
    return _sender_globally_allowed(
        sender,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
    )


def _location_by_id(locations: Sequence[VaultLocation], location_id: str) -> Optional[VaultLocation]:
    return next((loc for loc in locations if loc.id == location_id), None)


def _location_by_alias(locations: Sequence[VaultLocation], alias: str) -> Optional[VaultLocation]:
    target = normalize_location_alias(alias)
    return next((loc for loc in locations if normalize_location_alias(loc.alias, loc.name) == target), None)


def _build_alias_map(locations: Sequence[VaultLocation], *, default_location_id: str) -> Tuple[Dict[str, str], List[str]]:
    alias_map: Dict[str, str] = {}
    collisions: List[str] = []
    for location in locations:
        if location.id == default_location_id or not location.enabled:
            continue
        alias = normalize_location_alias(location.alias, location.name)
        if not alias:
            continue
        if alias in alias_map:
            collisions.append(alias)
            continue
        alias_map[alias] = location.id
    return alias_map, collisions


def _record_alias_health(alias_collisions: Sequence[str]) -> None:
    try:
        health = get_dependency_health_registry()
        unique_collisions = sorted(set(str(alias or "").strip() for alias in alias_collisions if str(alias or "").strip()))
        if unique_collisions:
            health.record_failure(
                VAULT_ALIAS_HEALTH_KEY,
                owner=VAULT_ALIAS_HEALTH_OWNER,
                error=f"alias collision: {', '.join(unique_collisions)}",
                duration_ms=0.0,
                cooldown_sec=0.0,
                metadata={
                    "action": "Two managed vault locations use the same open command alias",
                    "aliases": unique_collisions,
                },
            )
        else:
            health.record_success(
                VAULT_ALIAS_HEALTH_KEY,
                owner=VAULT_ALIAS_HEALTH_OWNER,
                duration_ms=0.0,
                metadata={"action": "Managed vault aliases are unique"},
            )
    except Exception as exc:
        log.debug("varac_bbs_vault: alias health update failed: %s", exc)


def _extract_alias_request(text: str, alias_map: Mapping[str, str]) -> Tuple[str, str]:
    if not alias_map:
        return "", ""
    parts = " ".join(str(text or "").upper().split()).split()
    if not parts:
        return "", ""
    starts: List[int] = [0]
    if parts[0] in {"OPEN", "MSG", "TYPE"}:
        starts.append(1)
    if parts[0] == "BBS":
        starts.append(1)
        if len(parts) > 1 and parts[1] in {"OPEN", "MSG"}:
            starts.append(2)
        if len(parts) > 2 and parts[1] == "MSG" and parts[2] == "-":
            starts.append(3)
    for start in starts:
        while start < len(parts) and parts[start] in {"-", "TYPE"}:
            start += 1
        if start >= len(parts):
            continue
        alias = normalize_location_alias(parts[start])
        if alias and alias in alias_map:
            return alias, _normalize_access_code_text(" ".join(parts[start + 1 :]).strip())
    return "", ""


def _logical_helper_label(value: object) -> str:
    """Return a visitor-facing helper label without its compatibility suffix."""

    text = " ".join(str(value or "").strip().split())
    if text.lower().endswith(".txt"):
        return text[:-4].rstrip()
    return text


def _menu_instruction_entry(text: str) -> _VirtualFile:
    # New VarAC menu instruction files intentionally omit ``.txt``. Historical
    # files remain recognized above so reconciliation can remove them when the
    # extensionless projection supersedes them.
    logical_label = _logical_helper_label(text)
    return _VirtualFile(name=logical_label, content=logical_label + "\n")


def _read_first_entry() -> _VirtualFile:
    text = "00 HOW TO USE - Type command then refresh BBS"
    return _menu_instruction_entry(text)


def _commands_heading_entry() -> _VirtualFile:
    return _menu_instruction_entry("01 COMMANDS - type one command below")


def _standard_helper_header_entries(*, include_commands: bool = True) -> List[_VirtualFile]:
    entries = [_read_first_entry()]
    if include_commands:
        entries.append(_commands_heading_entry())
    return entries


def _command_helper_entry(order: int, command: object, description: object) -> _VirtualFile:
    order_num = max(0, min(99, int(order)))
    command_txt = " ".join(str(command or "").strip().upper().split())
    description_txt = " ".join(str(description or "").strip().split())
    if description_txt:
        text = f"{order_num:02d} type {command_txt} - {description_txt}"
    else:
        text = f"{order_num:02d} type {command_txt}"
    return _menu_instruction_entry(text)


def _notice_entry(message: object) -> _VirtualFile:
    message_txt = " ".join(str(message or "").strip().split()) or "Notice"
    return _menu_instruction_entry(f"00 NOTICE - {message_txt}")


def _quick_refresh_notice_entry(command_text: object) -> _VirtualFile:
    command = str(command_text or "").strip().upper()
    if not command:
        command = "Command"
    return _notice_entry(f"{command} received. {ASYNC_REFRESH_NOTICE}")


def root_location_helper_filename_preview(location: VaultLocation, *, default_location_id: str, global_code_policy: str, order: int = 20) -> str:
    alias = normalize_location_alias(location.alias, location.name)
    name = str(location.name or alias or "location").strip()
    requires_code = _location_requires_code(
        location,
        default_location_id=default_location_id,
        global_code_policy=global_code_policy,
    )
    custom = str(location.description or "").strip()
    if custom.lower() in {f"open {name}".lower(), f"to open {name}".lower()}:
        custom = ""
    command = f"{alias} [CODE]" if requires_code else alias
    if requires_code:
        description = f"open {alias} with access code"
    else:
        description = f"open {name}"
    if custom:
        description = f"{description} - {custom}"
    # This is a logical visitor label, not the on-disk compatibility filename.
    return f"{max(0, min(99, int(order))):02d} type {command} - {description}"


def _root_location_helper_entry(location: VaultLocation, *, default_location_id: str, global_code_policy: str, order: int) -> _VirtualFile:
    text = root_location_helper_filename_preview(
        location,
        default_location_id=default_location_id,
        global_code_policy=global_code_policy,
        order=order,
    )
    return _menu_instruction_entry(text)


def _root_virtual_files(
    *,
    sender: str,
    locations: Sequence[VaultLocation],
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    global_code_policy: str,
    flamp_enabled: bool,
    include_enabled_fallback: bool = False,
) -> List[_VirtualFile]:
    command_entries: List[_VirtualFile] = []
    order = 20
    for location in locations:
        if not _location_visible_in_root(
            location,
            sender=sender,
            default_location_id=default_location_id,
            global_allowed_callsigns=global_allowed_callsigns,
            limit_access_enabled=limit_access_enabled,
            global_code_policy=global_code_policy,
        ):
            continue
        command_entries.append(
            _root_location_helper_entry(
                location,
                default_location_id=default_location_id,
                global_code_policy=global_code_policy,
                order=order,
            )
        )
        order += 1
    if not command_entries and include_enabled_fallback:
        for location in locations:
            if not location.enabled or location.id == default_location_id:
                continue
            if str(location.visibility_rule or "Public").strip() == "Hidden":
                continue
            command_entries.append(
                _root_location_helper_entry(
                    location,
                    default_location_id=default_location_id,
                    global_code_policy=global_code_policy,
                    order=order,
                )
            )
            order += 1
    if flamp_enabled:
        command_entries.append(_command_helper_entry(order, "FLAMP", "show Flamp block fill commands"))
    if not command_entries:
        return []
    return [*_standard_helper_header_entries(), *command_entries]


def _filesystem_location_virtual_files(managed_root: object) -> List[_VirtualFile]:
    try:
        locations_root = _managed_root_paths(managed_root)["locations"]
    except Exception:
        return []
    if not locations_root.exists() or not locations_root.is_dir():
        return []
    command_entries: List[_VirtualFile] = []
    seen_aliases = {"ROOT"}
    order = 20
    for child in sorted(locations_root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.name == DEFAULT_LOCATION_NAME:
            continue
        alias = normalize_location_alias("", child.name)
        if not alias or alias in seen_aliases:
            continue
        seen_aliases.add(alias)
        command_entries.append(_command_helper_entry(order, alias, f"open {child.name}"))
        order += 1
    if not command_entries:
        return []
    return [*_standard_helper_header_entries(), *command_entries]


def _location_virtual_files(*, include_root: bool = True) -> List[_VirtualFile]:
    command_entries: List[_VirtualFile] = []
    if include_root:
        command_entries.append(_command_helper_entry(10, "ROOT", "return to main menu"))
    if not command_entries:
        return []
    return [*_standard_helper_header_entries(), *command_entries]


def _location_access_prompt_virtual_files(location: VaultLocation, *, reason: str = "code_required") -> List[_VirtualFile]:
    alias = normalize_location_alias(location.alias, location.name)
    if reason == "incorrect_code":
        return [
            _read_first_entry(),
            _notice_entry(f"Incorrect code for {alias}"),
            _command_helper_entry(10, f"{alias} [CODE]", "try again with access code"),
            _command_helper_entry(11, "ROOT", "return to main menu"),
        ]
    elif reason == "callsign_restricted":
        return [
            _read_first_entry(),
            _notice_entry(f"{location.name} is restricted to allowed callsigns"),
            _command_helper_entry(10, "ROOT", "return to main menu"),
        ]
    elif reason == "cooldown":
        return [
            _read_first_entry(),
            _notice_entry(f"{location.name} access temporarily locked after failed attempts"),
            _command_helper_entry(10, "ROOT", "return to main menu"),
        ]
    else:
        return [
            _read_first_entry(),
            _notice_entry(f"{location.name} requires an access code"),
            _command_helper_entry(10, f"{alias} [CODE]", "open with access code"),
            _command_helper_entry(11, "ROOT", "return to main menu"),
        ]


def publish_location_access_prompt_view(
    location: VaultLocation,
    *,
    live_bbs_dir: object,
    managed_root: object,
    reason: str = "code_required",
    manifest_db_path: object = "",
) -> VaultPublishResult:
    virtual_files = _location_access_prompt_virtual_files(location, reason=reason)
    manifest, ignored_dirs = build_publish_manifest("", virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir="",
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


def publish_root_view(
    *,
    sender: str,
    locations: Sequence[VaultLocation],
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    global_code_policy: str,
    live_bbs_dir: object,
    managed_root: object,
    flamp_enabled: bool = False,
    include_enabled_fallback: bool = False,
    manifest_db_path: object = "",
) -> VaultPublishResult:
    default_location = _location_by_id(locations, default_location_id)
    if default_location is None:
        raise ValueError("Managed BBS Library default location is missing")
    virtual_files = _root_virtual_files(
        sender=sender,
        locations=locations,
        default_location_id=default_location_id,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
        global_code_policy=global_code_policy,
        flamp_enabled=flamp_enabled,
        include_enabled_fallback=include_enabled_fallback,
    )
    if not virtual_files and include_enabled_fallback:
        # Filesystem-only root discovery is intentionally disabled for normal
        # operation. Configured-location fallback still happens inside
        # _root_virtual_files; raw folders left on disk must not resurrect a
        # location after it is deleted from FIO Settings.
        virtual_files = []
    source_dir = _effective_location_source_dir(default_location, managed_root)
    db_entries = _db_backed_manifest_entries(default_location, manifest_db_path=manifest_db_path, virtual_files=virtual_files)
    if db_entries is not None:
        manifest = db_entries
        ignored_dirs = 0
    else:
        manifest, ignored_dirs = build_publish_manifest(source_dir, virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir=source_dir,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


def publish_location_view(
    location: VaultLocation,
    *,
    live_bbs_dir: object,
    managed_root: object,
    manifest_db_path: object = "",
) -> VaultPublishResult:
    virtual_files = _location_virtual_files(include_root=True)
    source_dir = _effective_location_source_dir(location, managed_root)
    db_entries = _db_backed_manifest_entries(location, manifest_db_path=manifest_db_path, virtual_files=virtual_files)
    if db_entries is not None:
        manifest = db_entries
        ignored_dirs = 0
    else:
        manifest, ignored_dirs = build_publish_manifest(source_dir, virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir=source_dir,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


class FlampRelayStore:
    PROG_RE = re.compile(r"<PROG.*?\{([A-F0-9]+)\}", re.IGNORECASE)
    FILE_RE = re.compile(
        r"<FILE\s+[^>]*>\{([A-F0-9]+)\}([^:\r\n]*):([^\r\n]+)$",
        re.IGNORECASE,
    )
    SIZE_RE = re.compile(r"<SIZE\s+[^>]*>\{([A-F0-9]+)\}(\d+)\s+(\d+)\s+(\d+)", re.IGNORECASE)
    BLOCK_RE = re.compile(r"\{([A-F0-9]+):(\d+)\}", re.IGNORECASE)
    VALID_Q_RE = re.compile(r"^[A-F0-9]{4}$", re.IGNORECASE)

    def __init__(self, relay_dir: object):
        self.relay_dir = _resolve_path(relay_dir)

    def relay_files(self) -> List[Path]:
        relay_dir = self.relay_dir
        if relay_dir is None or not relay_dir.exists() or not relay_dir.is_dir() or relay_dir.is_symlink():
            return []
        files: List[Path] = []
        allowed_suffixes = {".b2s", ".k2s", ".relay", ".txt", ".dat"}
        try:
            entries = os.scandir(relay_dir)
        except OSError:
            return []
        with entries:
            for entry in entries:
                if len(files) >= MAX_FLAMP_RELAY_FILES_PER_SCAN:
                    break
                try:
                    if (
                        entry.is_symlink()
                        or not entry.is_file(follow_symlinks=False)
                        or Path(entry.name).suffix.lower() not in allowed_suffixes
                    ):
                        continue
                except OSError:
                    continue
                files.append(Path(entry.path))
        return files

    def queue_index(self, *, max_age_days: Optional[int] = None) -> Dict[str, Path]:
        index: Dict[str, Path] = {}
        cutoff_ts = None
        if max_age_days is not None:
            try:
                cutoff_ts = time.time() - max(1, int(max_age_days)) * 86400
            except Exception:
                cutoff_ts = None
        for path in self.relay_files():
            name = path.name
            if "unassigned" in name.lower():
                continue
            if len(name) < 4:
                continue
            queue_id = name[:4].upper()
            if not self.VALID_Q_RE.match(queue_id):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if cutoff_ts is not None and stat.st_mtime < cutoff_ts:
                continue
            previous = index.get(queue_id)
            if previous is None or stat.st_mtime > previous.stat().st_mtime:
                index[queue_id] = path
        return index

    def parse_queue(self, queue_id: str) -> Optional[Dict[str, object]]:
        queue_id = str(queue_id or "").strip().upper()
        if not self.VALID_Q_RE.match(queue_id):
            return None
        path = self.queue_index().get(queue_id)
        if not path:
            return None
        return self.parse_file(path)

    def parse_file(self, file_path: Path) -> Optional[Dict[str, object]]:
        file_id = None
        transfer_file_id = ""
        size_file_id = ""
        transfer_filename = ""
        transfer_timestamp = ""
        total_blocks = None
        file_size = None
        block_len = None
        blocks: Dict[int, str] = {}
        block_file_ids: Dict[int, str] = {}
        header_lines: List[str] = []
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
                for raw_line in handle:
                    line = raw_line.rstrip("\r\n")
                    if len(header_lines) < 4 and line.startswith("<"):
                        header_lines.append(line)
                    if not line.strip():
                        continue
                    match_prog = self.PROG_RE.search(line)
                    if match_prog and not file_id:
                        file_id = match_prog.group(1).upper()
                    match_file = self.FILE_RE.search(line)
                    if match_file:
                        transfer_file_id = match_file.group(1).upper()
                        file_id = file_id or match_file.group(1).upper()
                        transfer_timestamp = match_file.group(2).strip()
                        transfer_filename = PureWindowsPath(match_file.group(3).strip()).name
                    match_size = self.SIZE_RE.search(line)
                    if match_size:
                        size_file_id = match_size.group(1).upper()
                        file_id = file_id or match_size.group(1).upper()
                        file_size = int(match_size.group(2))
                        total_blocks = int(match_size.group(3))
                        block_len = int(match_size.group(4))
                    match_block = self.BLOCK_RE.search(line)
                    if match_block:
                        fid, block_num = match_block.groups()
                        fid = fid.upper()
                        if not file_id:
                            file_id = fid
                        number = int(block_num)
                        blocks[number] = line
                        block_file_ids[number] = fid
        except Exception:
            return None
        if not blocks:
            return None
        return {
            "path": str(file_path),
            "name": file_path.name,
            "file_id": (file_id or file_path.name[:4]).upper(),
            "transfer_filename": transfer_filename,
            "transfer_timestamp": transfer_timestamp,
            "transfer_file_id": transfer_file_id,
            "size_file_id": size_file_id,
            "total_blocks": total_blocks,
            "file_size": file_size,
            "block_len": block_len,
            "blocks": blocks,
            "block_file_ids": block_file_ids,
            "header": header_lines,
        }

    def available_blocks_text(self, relay_info: Mapping[str, object]) -> Tuple[List[int], List[int]]:
        blocks = sorted(int(item) for item in dict(relay_info.get("blocks", {})).keys())
        total = int(relay_info.get("total_blocks") or (blocks[-1] if blocks else 0))
        missing = [num for num in range(1, total + 1) if num not in blocks]
        return blocks, missing

    def authoritative_transfer(self, queue_id: object) -> Optional[Dict[str, object]]:
        """Return validated transfer facts suitable for a dynamic Q reply.

        This path intentionally does not use ``available_blocks_text``: that
        compatibility helper has a highest-seen-block fallback which is useful
        for old BBS views but is not proof of a FLAMP transfer total.
        """
        q_id = str(queue_id or "").strip().upper()
        if not self.VALID_Q_RE.fullmatch(q_id):
            return None
        relay_info = self.parse_queue(q_id)
        if not relay_info:
            return None
        return self._authoritative_transfer_from_info(q_id, relay_info)

    def authoritative_file(self, file_path: Path, queue_id: object) -> Optional[Dict[str, object]]:
        """Parse one already-resolved relay file without rebuilding the directory index."""

        q_id = str(queue_id or "").strip().upper()
        if not self.VALID_Q_RE.fullmatch(q_id):
            return None
        relay_info = self.parse_file(file_path)
        if not relay_info:
            return None
        return self._authoritative_transfer_from_info(q_id, relay_info)

    def _authoritative_transfer_from_info(
        self, q_id: str, relay_info: Mapping[str, object]
    ) -> Dict[str, object]:
        file_id = str(relay_info.get("file_id") or "").strip().upper()
        total_raw = relay_info.get("total_blocks")
        try:
            total = int(total_raw) if total_raw is not None else 0
        except Exception:
            total = 0
        transfer_file_id = str(relay_info.get("transfer_file_id") or q_id).strip().upper()
        size_file_id = str(relay_info.get("size_file_id") or q_id).strip().upper()
        if file_id != q_id or transfer_file_id != q_id or size_file_id != q_id or total <= 0:
            return {
                "q_id": q_id,
                "path": str(relay_info.get("path") or ""),
                "total_blocks": None,
                "available_blocks": [],
                "missing_blocks": [],
                "state": "unavailable",
                "parser_confidence": 0.0,
            }
        blocks = relay_info.get("blocks") if isinstance(relay_info.get("blocks"), Mapping) else {}
        file_ids = relay_info.get("block_file_ids") if isinstance(relay_info.get("block_file_ids"), Mapping) else {}
        available = sorted({int(number) for number in blocks.keys() if 1 <= int(number) <= total})
        invalid_numbers = [int(number) for number in blocks.keys() if int(number) < 1 or int(number) > total]
        mismatched_ids = [number for number in available if str(file_ids.get(number) or q_id).upper() != q_id]
        if invalid_numbers or mismatched_ids:
            state = "unavailable"
            confidence = 0.0
            missing: List[int] = []
        else:
            missing = [number for number in range(1, total + 1) if number not in available]
            state = "complete" if not missing else "partial"
            confidence = 1.0
        return {
            "q_id": q_id,
            "path": str(relay_info.get("path") or ""),
            "total_blocks": total,
            "available_blocks": available,
            "missing_blocks": missing,
            "state": state,
            "parser_confidence": confidence,
            "transfer_filename": str(relay_info.get("transfer_filename") or ""),
            "transfer_timestamp": str(relay_info.get("transfer_timestamp") or ""),
            "expected_file_size": relay_info.get("file_size"),
        }

    @staticmethod
    def completed_receive_index(
        receive_dir: object, *, max_files: int = MAX_FLAMP_RECEIVE_FILES_PER_SCAN
    ) -> Dict[str, Tuple[Path, int]]:
        """Index regular FLAMP outputs at the root and one date-folder level.

        FLAMP auto-save uses one date directory below ``FLAMP/rx``.  Keeping the
        walk to that documented shape prevents an accidentally broad recursive
        scan when a user configures the wrong directory.
        """

        root = _resolve_path(receive_dir)
        if root is None or not root.exists() or not root.is_dir() or root.is_symlink():
            return {}
        bounded = max(1, min(MAX_FLAMP_RECEIVE_FILES_PER_SCAN, int(max_files or 1)))
        indexed: Dict[str, Tuple[Path, int]] = {}
        examined = 0
        child_directories: List[Path] = []

        def scan_directory(directory: Path, *, collect_children: bool = False) -> None:
            nonlocal examined
            try:
                entries = os.scandir(directory)
            except OSError:
                return
            with entries:
                for entry in entries:
                    if examined >= bounded:
                        return
                    examined += 1
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            candidate = Path(entry.path)
                            previous = indexed.get(entry.name)
                            if previous is None or int(stat.st_mtime_ns) > previous[1]:
                                indexed[entry.name] = (candidate, int(stat.st_mtime_ns))
                        elif collect_children and entry.is_dir(follow_symlinks=False):
                            child_directories.append(Path(entry.path))
                    except OSError:
                        continue

        scan_directory(root, collect_children=True)
        for child in child_directories:
            if examined >= bounded:
                break
            scan_directory(child)
        return indexed


def index_flamp_transfer_state(
    relay_dir: object,
    *,
    db_path: Path,
    source_radio_id: object = "",
    source_js8_instance_id: object = "",
    receive_dir: object = "",
    observed_ts: Optional[float] = None,
) -> int:
    """Persist validated, source-scoped FLAMP transfer state.

    This is an additive projection.  A missing source file becomes
    ``unavailable`` instead of raising or repeatedly attempting to parse it.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    radio_id = str(source_radio_id or "").strip()
    js8_id = str(source_js8_instance_id or "").strip()
    now = float(observed_ts if observed_ts is not None else time.time())
    store = FlampRelayStore(relay_dir)
    conn = connect_sqlite_runtime_write(path)
    try:
        relay_root = store.relay_dir
        if (
            relay_root is None
            or not relay_root.exists()
            or not relay_root.is_dir()
            or relay_root.is_symlink()
        ):
            conn.execute(
                """
                INSERT INTO flamp_transfer_state_scans
                    (source_radio_id, source_js8_instance_id, relay_dir, receive_dir,
                     scan_success, file_count, error_text, scanned_ts)
                VALUES (?, ?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(source_radio_id, source_js8_instance_id) DO UPDATE SET
                    relay_dir=excluded.relay_dir,
                    receive_dir=excluded.receive_dir,
                    scan_success=excluded.scan_success,
                    file_count=excluded.file_count,
                    error_text=excluded.error_text,
                    scanned_ts=excluded.scanned_ts
                """,
                (
                    radio_id,
                    js8_id,
                    str(relay_dir or ""),
                    str(receive_dir or ""),
                    "FLAMP relay folder is unavailable.",
                    now,
                ),
            )
            conn.commit()
            return 0
        receive_root = _resolve_path(receive_dir)
        if str(receive_dir or "").strip() and (
            receive_root is None
            or not receive_root.exists()
            or not receive_root.is_dir()
            or receive_root.is_symlink()
        ):
            raise FileNotFoundError("FLAMP completed receive folder is unavailable.")
        receive_index = store.completed_receive_index(receive_dir)
        existing_rows = conn.execute(
            """
            SELECT q_id, source_path, source_mtime_ns, source_size_bytes,
                   source_sha256, transfer_filename, expected_file_size,
                   completion_path, evidence_kind, parser_version,
                   total_blocks, available_blocks_json, missing_blocks_json,
                   state, parser_confidence, observed_ts
            FROM flamp_transfer_state
            WHERE source_radio_id=? AND source_js8_instance_id=?
            """,
            (radio_id, js8_id),
        ).fetchall()
        existing = {
            str(row[0] or "").upper(): {
                "source_path": str(row[1] or ""),
                "source_mtime_ns": int(row[2] or 0),
                "source_size_bytes": int(row[3] or 0),
                "source_sha256": str(row[4] or ""),
                "transfer_filename": str(row[5] or ""),
                "expected_file_size": int(row[6]) if row[6] is not None else None,
                "completion_path": str(row[7] or ""),
                "evidence_kind": str(row[8] or "relay_snapshot"),
                "parser_version": int(row[9] or 0),
                "total_blocks": int(row[10]) if row[10] is not None else None,
                "available_blocks_json": str(row[11] or "[]"),
                "missing_blocks_json": str(row[12] or "[]"),
                "state": str(row[13] or "unavailable"),
                "parser_confidence": float(row[14] or 0.0),
                "observed_ts": float(row[15] or 0.0),
            }
            for row in existing_rows
        }
        seen: set[str] = set()
        count = 0
        for q_id, relay_path in store.queue_index().items():
            seen.add(q_id)
            try:
                stat = relay_path.stat()
                source_mtime_ns = int(stat.st_mtime_ns)
                source_size_bytes = int(stat.st_size)
                source_mtime_ts = float(stat.st_mtime)
            except OSError:
                source_mtime_ns = 0
                source_size_bytes = 0
                source_mtime_ts = 0.0
            prior = existing.get(q_id)
            unchanged = bool(
                prior
                and source_mtime_ns > 0
                and int(prior.get("source_mtime_ns") or 0) == source_mtime_ns
                and int(prior.get("source_size_bytes") or 0) == source_size_bytes
                and str(prior.get("source_path") or "") == str(relay_path)
                and str(prior.get("source_sha256") or "")
                and int(prior.get("parser_version") or 0) >= FLAMP_TRANSFER_PARSER_VERSION
            )
            transfer_filename = str((prior or {}).get("transfer_filename") or "")
            completion = receive_index.get(transfer_filename) if transfer_filename else None
            completion_is_current = bool(
                completion and source_mtime_ns > 0 and int(completion[1]) >= source_mtime_ns
            )
            must_parse = not unchanged or bool(
                prior
                and str(prior.get("evidence_kind") or "") == "completed_receive"
                and not completion_is_current
            )
            if must_parse:
                facts = store.authoritative_file(relay_path, q_id) or {
                    "q_id": q_id,
                    "path": str(relay_path),
                    "transfer_filename": "",
                    "expected_file_size": None,
                    "total_blocks": None,
                    "available_blocks": [],
                    "missing_blocks": [],
                    "state": "unavailable",
                    "parser_confidence": 0.0,
                }
                transfer_filename = str(facts.get("transfer_filename") or "")
                completion = receive_index.get(transfer_filename) if transfer_filename else None
                completion_is_current = bool(
                    completion and source_mtime_ns > 0 and int(completion[1]) >= source_mtime_ns
                )
                try:
                    source_sha256 = hashlib.sha256(relay_path.read_bytes()).hexdigest()
                except OSError:
                    source_sha256 = ""
                fact_observed_ts = source_mtime_ts
            else:
                try:
                    available = [int(item) for item in json.loads(str(prior.get("available_blocks_json") or "[]"))]
                except Exception:
                    available = []
                try:
                    missing = [int(item) for item in json.loads(str(prior.get("missing_blocks_json") or "[]"))]
                except Exception:
                    missing = []
                facts = {
                    "q_id": q_id,
                    "path": str(relay_path),
                    "transfer_filename": transfer_filename,
                    "expected_file_size": prior.get("expected_file_size"),
                    "total_blocks": prior.get("total_blocks"),
                    "available_blocks": available,
                    "missing_blocks": missing,
                    "state": prior.get("state") or "unavailable",
                    "parser_confidence": prior.get("parser_confidence") or 0.0,
                }
                source_sha256 = str(prior.get("source_sha256") or "")
                fact_observed_ts = float(prior.get("observed_ts") or 0.0)

            completion_path = ""
            evidence_kind = "relay_snapshot"
            total_blocks = facts.get("total_blocks")
            if (
                completion_is_current
                and total_blocks is not None
                and int(total_blocks or 0) > 0
                and float(facts.get("parser_confidence") or 0.0) >= 1.0
            ):
                completion_path = str(completion[0])
                evidence_kind = "completed_receive"
                facts = dict(facts)
                facts["available_blocks"] = list(range(1, int(total_blocks) + 1))
                facts["missing_blocks"] = []
                facts["state"] = "complete"
                facts["parser_confidence"] = 1.0
                if str((prior or {}).get("evidence_kind") or "") != "completed_receive":
                    log.info(
                        "FLAMP transfer projection: q=%s source=%s/%s completed receive reconciled path=%s",
                        q_id,
                        radio_id,
                        js8_id,
                        completion_path,
                    )
            elif str((prior or {}).get("evidence_kind") or "") == "completed_receive":
                log.warning(
                    "FLAMP transfer projection: q=%s source=%s/%s completed receive evidence removed; relay snapshot restored",
                    q_id,
                    radio_id,
                    js8_id,
                )
            conn.execute(
                """
                INSERT INTO flamp_transfer_state
                    (q_id, source_radio_id, source_js8_instance_id, source_path,
                     source_mtime_ns, source_size_bytes, source_sha256,
                     transfer_filename, expected_file_size, completion_path,
                     evidence_kind, parser_version, total_blocks,
                     available_blocks_json, missing_blocks_json, state,
                     parser_confidence, observed_ts, validated_scan_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(q_id, source_radio_id, source_js8_instance_id) DO UPDATE SET
                    source_path=excluded.source_path,
                    source_mtime_ns=excluded.source_mtime_ns,
                    source_size_bytes=excluded.source_size_bytes,
                    source_sha256=excluded.source_sha256,
                    transfer_filename=excluded.transfer_filename,
                    expected_file_size=excluded.expected_file_size,
                    completion_path=excluded.completion_path,
                    evidence_kind=excluded.evidence_kind,
                    parser_version=excluded.parser_version,
                    total_blocks=excluded.total_blocks,
                    available_blocks_json=excluded.available_blocks_json,
                    missing_blocks_json=excluded.missing_blocks_json,
                    state=excluded.state,
                    parser_confidence=excluded.parser_confidence,
                    observed_ts=excluded.observed_ts,
                    validated_scan_ts=excluded.validated_scan_ts,
                    updated_ts=excluded.updated_ts
                """,
                (
                    q_id,
                    radio_id,
                    js8_id,
                    str(facts.get("path") or relay_path),
                    source_mtime_ns,
                    source_size_bytes,
                    source_sha256,
                    transfer_filename,
                    facts.get("expected_file_size"),
                    completion_path,
                    evidence_kind,
                    FLAMP_TRANSFER_PARSER_VERSION,
                    facts.get("total_blocks"),
                    json.dumps(list(facts.get("available_blocks") or []), separators=(",", ":")),
                    json.dumps(list(facts.get("missing_blocks") or []), separators=(",", ":")),
                    str(facts.get("state") or "unavailable"),
                    float(facts.get("parser_confidence") or 0.0),
                    fact_observed_ts,
                    now,
                    now,
                ),
            )
            count += 1
        # Do not leave a deleted/rotated source looking current.
        rows = conn.execute(
            "SELECT id, q_id FROM flamp_transfer_state WHERE source_radio_id=? AND source_js8_instance_id=?",
            (radio_id, js8_id),
        ).fetchall()
        for row in rows:
            if str(row[1] or "").upper() in seen:
                continue
            conn.execute(
                """
                UPDATE flamp_transfer_state
                SET state='unavailable', parser_confidence=0, available_blocks_json='[]',
                    missing_blocks_json='[]', total_blocks=NULL, source_mtime_ns=0,
                    source_size_bytes=0, source_sha256='', completion_path='',
                    evidence_kind='relay_snapshot', validated_scan_ts=?, updated_ts=?
                WHERE id=?
                """,
                (now, now, int(row[0])),
            )
        conn.execute(
            """
            INSERT INTO flamp_transfer_state_scans
                (source_radio_id, source_js8_instance_id, relay_dir, receive_dir,
                 scan_success, file_count, error_text, scanned_ts)
            VALUES (?, ?, ?, ?, 1, ?, '', ?)
            ON CONFLICT(source_radio_id, source_js8_instance_id) DO UPDATE SET
                relay_dir=excluded.relay_dir,
                receive_dir=excluded.receive_dir,
                scan_success=excluded.scan_success,
                file_count=excluded.file_count,
                error_text=excluded.error_text,
                scanned_ts=excluded.scanned_ts
            """,
            (radio_id, js8_id, str(relay_root), str(receive_dir or ""), count, now),
        )
        conn.commit()
        return count
    except Exception as exc:
        conn.rollback()
        try:
            conn.execute(
                """
                INSERT INTO flamp_transfer_state_scans
                    (source_radio_id, source_js8_instance_id, relay_dir, receive_dir,
                     scan_success, file_count, error_text, scanned_ts)
                VALUES (?, ?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(source_radio_id, source_js8_instance_id) DO UPDATE SET
                    relay_dir=excluded.relay_dir,
                    receive_dir=excluded.receive_dir,
                    scan_success=excluded.scan_success,
                    file_count=excluded.file_count,
                    error_text=excluded.error_text,
                    scanned_ts=excluded.scanned_ts
                """,
                (
                    radio_id,
                    js8_id,
                    str(relay_dir or ""),
                    str(receive_dir or ""),
                    str(exc)[:500],
                    now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()


def flamp_transfer_index_status(
    *,
    db_path: Path,
    source_radio_id: object = "",
    source_js8_instance_id: object = "",
) -> Optional[Dict[str, object]]:
    """Return the last source-scoped background projection result."""

    path = Path(db_path)
    if not path.exists():
        return None
    conn = connect_sqlite_readonly(path)
    try:
        if not table_exists(conn, "flamp_transfer_state_scans"):
            return None
        row = conn.execute(
            """
            SELECT relay_dir, receive_dir, scan_success, file_count, error_text, scanned_ts
            FROM flamp_transfer_state_scans
            WHERE source_radio_id=? AND source_js8_instance_id=?
            LIMIT 1
            """,
            (
                str(source_radio_id or "").strip(),
                str(source_js8_instance_id or "").strip(),
            ),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "relay_dir": str(row[0] or ""),
        "receive_dir": str(row[1] or ""),
        "scan_success": bool(row[2]),
        "file_count": int(row[3] or 0),
        "error_text": str(row[4] or ""),
        "scanned_ts": float(row[5] or 0.0),
    }


def list_flamp_transfer_index_statuses(
    *, db_path: Path, limit: int = 100
) -> List[Dict[str, object]]:
    """Bounded status rows for FIO Spotter administration."""

    path = Path(db_path)
    if not path.exists():
        return []
    conn = connect_sqlite_readonly(path)
    try:
        if not table_exists(conn, "flamp_transfer_state_scans"):
            return []
        rows = conn.execute(
            """
            SELECT source_radio_id, source_js8_instance_id, relay_dir, receive_dir,
                   scan_success, file_count, error_text, scanned_ts
            FROM flamp_transfer_state_scans
            ORDER BY scanned_ts DESC, source_radio_id, source_js8_instance_id
            LIMIT ?
            """,
            (max(1, min(500, int(limit or 100))),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "source_radio_id": str(row[0] or ""),
            "source_js8_instance_id": str(row[1] or ""),
            "relay_dir": str(row[2] or ""),
            "receive_dir": str(row[3] or ""),
            "scan_success": bool(row[4]),
            "file_count": int(row[5] or 0),
            "error_text": str(row[6] or ""),
            "scanned_ts": float(row[7] or 0.0),
        }
        for row in rows
    ]


def lookup_flamp_transfer_state(
    q_id: object,
    *,
    db_path: Path,
    source_radio_id: object = "",
    source_js8_instance_id: object = "",
) -> Optional[Dict[str, object]]:
    canonical = str(q_id or "").strip().upper()
    if not FlampRelayStore.VALID_Q_RE.fullmatch(canonical):
        return None
    conn = connect_sqlite_readonly(Path(db_path))
    try:
        if not table_exists(conn, "flamp_transfer_state"):
            return None
        row = conn.execute(
            """
            SELECT q_id, source_path, source_mtime_ns, source_size_bytes,
                   source_sha256, transfer_filename, expected_file_size,
                   completion_path, evidence_kind, total_blocks,
                   available_blocks_json, missing_blocks_json, state, parser_confidence,
                   observed_ts, validated_scan_ts, updated_ts
            FROM flamp_transfer_state
            WHERE q_id=? AND source_radio_id=? AND source_js8_instance_id=?
            LIMIT 1
            """,
            (canonical, str(source_radio_id or "").strip(), str(source_js8_instance_id or "").strip()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        available = [int(item) for item in json.loads(str(row[10] or "[]"))]
    except Exception:
        available = []
    try:
        missing = [int(item) for item in json.loads(str(row[11] or "[]"))]
    except Exception:
        missing = []
    return {
        "q_id": str(row[0] or "").upper(),
        "source_path": str(row[1] or ""),
        "source_mtime_ns": int(row[2] or 0),
        "source_size_bytes": int(row[3] or 0),
        "source_sha256": str(row[4] or ""),
        "transfer_filename": str(row[5] or ""),
        "expected_file_size": int(row[6]) if row[6] is not None else None,
        "completion_path": str(row[7] or ""),
        "evidence_kind": str(row[8] or "relay_snapshot"),
        "total_blocks": int(row[9]) if row[9] is not None else None,
        "available_blocks": available,
        "missing_blocks": missing,
        "state": str(row[12] or "unavailable"),
        "parser_confidence": float(row[13] or 0.0),
        "observed_ts": float(row[14] or 0.0),
        "validated_scan_ts": float(row[15] or 0.0),
        "updated_ts": float(row[16] or 0.0),
    }


def _flamp_queue_files(store: FlampRelayStore) -> List[_VirtualFile]:
    command_entries: List[_VirtualFile] = []
    order = 20
    for queue_id, path in sorted(store.queue_index().items()):
        command_entries.append(_command_helper_entry(order, f"LIST {queue_id}", f"blocks for {path.name}"))
        order += 1
    return [*_standard_helper_header_entries(), _command_helper_entry(10, "ROOT", "return to main menu"), *command_entries]


def _flamp_command_help_files(*, refresh_retry_command: object = "") -> List[_VirtualFile]:
    entries = [
        *_standard_helper_header_entries(),
        _command_helper_entry(10, "ROOT", "return to main menu"),
        _command_helper_entry(20, "LIST Q", "list available Flamp files"),
        _command_helper_entry(21, "LIST F277", "show blocks for queue F277"),
        _command_helper_entry(22, "BLKS 0,8,9 F277", "request blocks 0,8,9"),
    ]
    if refresh_retry_command:
        entries.insert(0, _quick_refresh_notice_entry(refresh_retry_command))
    return entries


def publish_flamp_command_help_view(
    *,
    base_source_dir: object,
    live_bbs_dir: object,
    managed_root: object,
    refresh_retry_command: object = "",
) -> VaultPublishResult:
    virtual_files = _flamp_command_help_files(refresh_retry_command=refresh_retry_command)
    manifest, ignored_dirs = build_publish_manifest("", virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir="",
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


def publish_flamp_queue_list_view(
    store: FlampRelayStore,
    *,
    base_source_dir: object,
    live_bbs_dir: object,
    managed_root: object,
    max_age_days: Optional[int] = None,
    refresh_retry_command: object = "",
) -> VaultPublishResult:
    queues = store.queue_index(max_age_days=max_age_days)
    body_lines = [f"{queue_id} {path.name}" for queue_id, path in sorted(queues.items())]
    queue_helpers: List[_VirtualFile] = []
    order = 20
    for queue_id, path in sorted(queues.items()):
        queue_helpers.append(_command_helper_entry(order, f"LIST {queue_id}", f"blocks for {path.name}"))
        order += 1
    virtual_files = [
        *_standard_helper_header_entries(),
        _command_helper_entry(10, "ROOT", "return to main menu"),
        _VirtualFile(name=DEFAULT_FLAMP_QUEUE_HELPER_NAME, content="\n".join(body_lines) + ("\n" if body_lines else "")),
        *queue_helpers,
    ]
    if refresh_retry_command:
        virtual_files.insert(0, _quick_refresh_notice_entry(refresh_retry_command))
    manifest, ignored_dirs = build_publish_manifest("", virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir="",
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


def publish_flamp_block_list_view(
    store: FlampRelayStore,
    queue_id: str,
    *,
    base_source_dir: object,
    live_bbs_dir: object,
    managed_root: object,
    refresh_retry_command: object = "",
) -> VaultPublishResult:
    relay_info = store.parse_queue(queue_id)
    if not relay_info:
        raise ValueError(f"FLAMP queue {queue_id} not found")
    requested_queue_id = str(queue_id or relay_info["file_id"]).strip().upper()
    blocks, missing = store.available_blocks_text(relay_info)
    lines = [
        f"QUEUE {requested_queue_id}",
        f"FILE {relay_info['name']}",
        "AVAILABLE " + (",".join(map(str, blocks)) if blocks else "NONE"),
        "MISSING " + (",".join(map(str, missing)) if missing else "NONE"),
    ]
    virtual_files = [
        *_standard_helper_header_entries(),
        _command_helper_entry(10, "ROOT", "return to main menu"),
        _command_helper_entry(20, f"BLKS 0,8,9 {requested_queue_id}", "request example blocks"),
        _VirtualFile(
            name=f"{DEFAULT_FLAMP_BLOCK_PREFIX}_{requested_queue_id}.txt",
            content="\n".join(lines) + "\n",
        ),
    ]
    if refresh_retry_command:
        virtual_files.insert(0, _quick_refresh_notice_entry(refresh_retry_command))
    manifest, ignored_dirs = build_publish_manifest("", virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir="",
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


def publish_flamp_notice_view(
    *,
    message: str,
    base_source_dir: object,
    live_bbs_dir: object,
    managed_root: object,
    refresh_retry_command: object = "",
) -> VaultPublishResult:
    virtual_files = [
        _read_first_entry(),
        _notice_entry(message),
        _command_helper_entry(10, "ROOT", "return to main menu"),
        _command_helper_entry(20, "LIST Q", "list available Flamp files"),
    ]
    if refresh_retry_command:
        virtual_files.insert(0, _quick_refresh_notice_entry(refresh_retry_command))
    manifest, ignored_dirs = build_publish_manifest("", virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir="",
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return VaultPublishResult(
        changed=result.changed,
        published_count=result.published_count,
        removed_count=result.removed_count,
        unmanaged_live_files=result.unmanaged_live_files,
        manifest_path=result.manifest_path,
        ignored_directories=ignored_dirs,
    )


def publish_flamp_block_overlay_view(
    store: FlampRelayStore,
    queue_id: str,
    block_numbers: Sequence[int],
    *,
    live_bbs_dir: object,
    managed_root: object,
    refresh_retry_command: object = "",
) -> Tuple[VaultPublishResult, str]:
    relay_info = store.parse_queue(queue_id)
    if not relay_info:
        raise ValueError(f"FLAMP queue {queue_id} not found")
    delivered: List[str] = []
    missing: List[str] = []
    combined_blocks: List[str] = []
    for block_num in block_numbers:
        if int(block_num) == 0:
            header = list(relay_info.get("header", []))
            if header:
                combined_blocks.extend(header)
                delivered.append("0")
            else:
                missing.append("0")
            continue
        payload = dict(relay_info["blocks"]).get(int(block_num))
        if not payload:
            missing.append(str(block_num))
            continue
        combined_blocks.append(str(payload))
        delivered.append(str(block_num))
    if not delivered:
        raise ValueError(f"Requested block(s) not present for queue {queue_id}: {', '.join(missing)}")
    overlay_name = f"{DEFAULT_FLAMP_FILE_PREFIX}_{relay_info['file_id']}_BLK_{'_'.join(delivered)}.txt"
    virtual_files = [
        *_standard_helper_header_entries(),
        _command_helper_entry(10, "ROOT", "return to main menu"),
        _VirtualFile(name=overlay_name, content="\n".join(combined_blocks) + "\n"),
    ]
    if refresh_retry_command:
        virtual_files.insert(0, _quick_refresh_notice_entry(refresh_retry_command))
    manifest, _ = build_publish_manifest("", virtual_files=virtual_files)
    result = _publish_manifest_entries(
        manifest,
        source_dir="",
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        virtual_files=virtual_files,
    )
    return result, overlay_name


def _prune_attempts(attempts: Sequence[float], *, now_ts: float, window_seconds: int) -> List[float]:
    lower = float(now_ts) - max(1, int(window_seconds))
    return [float(ts) for ts in attempts if float(ts) >= lower]


def _resolve_varac_db_path(settings) -> Optional[Path]:
    raw_db = str(settings.get("varac_db_path", "") or "").strip() if settings is not None else ""
    raw_install = str(settings.get("varac_path", "") or "").strip() if settings is not None else ""
    for raw in (raw_db, raw_install):
        if not raw:
            continue
        try:
            path = Path(raw)
            if path.is_dir():
                candidate = path / "VarAC.db"
                if candidate.exists():
                    return candidate
            elif path.is_file():
                return path
        except Exception:
            continue
    return None


def _load_db_events(varac_db_path: Path, *, last_datastream_id: int, alias_map: Mapping[str, str]) -> VaultDbScanResult:
    events: List[VaultDbEvent] = []
    max_scanned_row_id = int(last_datastream_id or 0)
    try:
        conn = sqlite3.connect(str(varac_db_path), timeout=1.5)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        log.debug("varac_bbs_vault: could not open VarAC.db %s: %s", varac_db_path, exc)
        return VaultDbScanResult(tuple(events), max_scanned_row_id)
    try:
        start_after_id = int(last_datastream_id or 0)
        if start_after_id <= 0:
            max_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM datastream").fetchone()[0]
            start_after_id = max(0, int(max_row or 0) - int(MAX_DB_ROWS_PER_SCAN))
        rows = conn.execute(
            """
            SELECT
                ds.id,
                COALESCE(ds.qso_guid, '') AS qso_guid,
                COALESCE(ds.callsign, '') AS entry_callsign,
                COALESCE(ds.entry, '') AS entry_text,
                COALESCE(ds.creation_time, '') AS creation_time,
                COALESCE(ds.datastream_entry_type_id, 0) AS entry_type_id,
                COALESCE(q.callsign, '') AS remote_callsign,
                COALESCE(q.my_callsign, '') AS my_callsign
            FROM datastream ds
            LEFT JOIN qso q ON q.guid = ds.qso_guid
            WHERE ds.id > ?
            ORDER BY ds.id ASC
            LIMIT ?
            """,
            (start_after_id, int(MAX_DB_ROWS_PER_SCAN)),
        ).fetchall()
    except Exception as exc:
        log.debug("varac_bbs_vault: could not query VarAC.db datastream: %s", exc)
        conn.close()
        return VaultDbScanResult(tuple(events), max_scanned_row_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for row in rows:
        row_id = int(row["id"] or 0)
        max_scanned_row_id = max(max_scanned_row_id, row_id)
        qso_guid = str(row["qso_guid"] or "").strip()
        remote_callsign = _normalize_callsign(row["remote_callsign"])
        my_callsign = _normalize_callsign(row["my_callsign"])
        entry_callsign = _normalize_callsign(row["entry_callsign"])
        entry_text = str(row["entry_text"] or "").strip()
        entry_type_id = int(row["entry_type_id"] or 0)
        if not qso_guid or not entry_text:
            continue
        timestamp_utc = _parse_db_timestamp(row["creation_time"])
        upper = _normalize_bbs_command_text(entry_text)
        refresh_requested = _entry_includes_bbs_refresh(entry_text)

        if not remote_callsign and entry_type_id == 1 and entry_callsign:
            remote_callsign = entry_callsign

        if DISCONNECT_RE.search(upper):
            events.append(
                VaultDbEvent(
                    row_id=row_id,
                    timestamp_utc=timestamp_utc,
                    qso_guid=qso_guid,
                    remote_callsign=remote_callsign,
                    my_callsign=my_callsign,
                    entry_callsign=entry_callsign,
                    entry_text=entry_text,
                    kind="disconnect",
                    refresh_requested=refresh_requested,
                )
            )
            continue

        if not remote_callsign:
            continue
        if entry_callsign and entry_callsign != remote_callsign:
            continue

        if upper == "<BLR>":
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "root_request",
                    refresh_requested=refresh_requested,
                )
            )
            continue
        if ROOT_CMD_RE.match(upper):
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "root_return",
                    refresh_requested=refresh_requested,
                )
            )
            continue
        if FLAMP_CMD_RE.match(upper):
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "flamp_help",
                    refresh_requested=refresh_requested,
                )
            )
            continue
        match = LIST_Q_RE.match(upper)
        if match:
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "flamp_list_q",
                    refresh_requested=refresh_requested,
                )
            )
            continue
        match = LIST_BLOCKS_RE.match(upper)
        if match:
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "flamp_list_blocks",
                    queue_id=str(match.group(1) or "").upper(),
                    refresh_requested=refresh_requested,
                )
            )
            continue
        match = BLOCK_REQUEST_RE.match(upper)
        if match:
            nums = _parse_flamp_block_numbers(match.group(1))
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "flamp_block_request",
                    queue_id=str(match.group(2) or "").upper(),
                    block_numbers=nums,
                    refresh_requested=refresh_requested,
                )
            )
            continue
        match = INVALID_BLOCK_REQUEST_RE.match(upper)
        if match:
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "flamp_invalid_block_request",
                    queue_id=str(match.group(1) or "").upper(),
                    refresh_requested=refresh_requested,
                )
            )
            continue
        alias, code_text = _extract_alias_request(upper, alias_map)
        if alias:
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "open_alias",
                    alias=alias,
                    code_text=code_text,
                    refresh_requested=refresh_requested,
                )
            )
            continue

        match = COMMAND_RE.search(upper)
        if match:
            events.append(
                VaultDbEvent(
                    row_id,
                    timestamp_utc,
                    qso_guid,
                    remote_callsign,
                    my_callsign,
                    entry_callsign,
                    entry_text,
                    "legacy_code_open",
                    code_text=str(match.group(1) or "").strip(),
                    refresh_requested=refresh_requested,
                )
            )
    return VaultDbScanResult(tuple(events), max_scanned_row_id)


def _summary_location_name(locations: Sequence[VaultLocation], location_id: str) -> str:
    location = _location_by_id(locations, location_id)
    return location.name if location is not None else location_id or DEFAULT_LOCATION_NAME


def _refresh_retry_command_for_event(event: object) -> str:
    if not bool(getattr(event, "refresh_requested", False)):
        return ""
    kind = str(getattr(event, "kind", "") or "").strip()
    queue_id = str(getattr(event, "queue_id", "") or "").strip().upper()
    if kind == "flamp_help":
        return "FLAMP"
    if kind == "flamp_list_q":
        return "LIST Q"
    if kind == "flamp_list_blocks" and queue_id:
        return f"LIST BLKS {queue_id}"
    if kind == "flamp_block_request" and queue_id:
        blocks = ",".join(str(num) for num in getattr(event, "block_numbers", ()) or ())
        return f"BLK {blocks} {queue_id}".strip()
    if kind == "root_return":
        return "ROOT"
    if kind == "open_alias":
        alias = str(getattr(event, "alias", "") or "").strip().upper()
        code_text = str(getattr(event, "code_text", "") or "").strip()
        return f"{alias} {code_text}".strip()
    code_text = str(getattr(event, "code_text", "") or "").strip()
    if code_text:
        return code_text
    return _normalize_bbs_command_text(str(getattr(event, "entry_text", "") or getattr(event, "body", "") or ""))


def _invalid_flamp_block_request_message(queue_id: object) -> str:
    qid = str(queue_id or "QNUM").strip().upper() or "QNUM"
    return f"Use LIST {qid} to show blocks, or BLKS 7,8 {qid} to request blocks"


def _persist_runtime_state(settings, state: VaultRuntimeState, summary: str) -> None:
    if settings is None:
        return
    try:
        state_data = vault_runtime_state_to_data(state)
        current_state = settings.get("varac_bbs_vault_runtime_state_v1", {})
        current_summary = str(settings.get("varac_bbs_vault_last_summary", "") or "")
        wrote = False
        if current_state != state_data:
            settings.set("varac_bbs_vault_runtime_state_v1", state_data)
            wrote = True
        if current_summary != summary:
            settings.set("varac_bbs_vault_last_summary", summary)
            wrote = True
        if not wrote:
            log.debug("VARAC_VAULT_STATE|unchanged|write_skipped=true")
    except Exception as exc:
        log.debug("varac_bbs_vault: failed to persist runtime state: %s", exc)


def _update_state(
    state: VaultRuntimeState,
    **updates,
) -> VaultRuntimeState:
    payload = vault_runtime_state_to_data(state)
    payload.update(updates)
    return VaultRuntimeState(**payload)


def _publish_root_action(
    *,
    sender: str,
    qso_guid: str,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    global_code_policy: str,
    runtime_state: VaultRuntimeState,
    now_ts: float,
    flamp_enabled: bool,
    reason: str,
    manifest_db_path: object = "",
) -> VaultActionResult:
    publish_result = publish_root_view(
        sender=sender,
        locations=locations,
        default_location_id=default_location_id,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
        global_code_policy=global_code_policy,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        flamp_enabled=flamp_enabled,
        include_enabled_fallback=True,
        manifest_db_path=manifest_db_path,
    )
    summary = f"Managed BBS Library published root menu for {sender or 'public'}."
    next_state = _update_state(
        runtime_state,
        current_location_id=default_location_id,
        current_session_callsign=_normalize_callsign(sender),
        current_session_qso_guid=str(qso_guid or "").strip(),
        current_view_mode="root",
        current_view_label=_summary_location_name(locations, default_location_id),
        current_overlay_file="",
        last_publish_manifest_path=publish_result.manifest_path,
        last_publish_ts=now_ts,
        last_action=summary,
        last_request_ts=now_ts,
        last_error="",
        unmanaged_live_files=list(publish_result.unmanaged_live_files),
    )
    _append_audit_event(
        managed_root,
        {
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "action": reason,
            "sender": _normalize_callsign(sender),
            "qso_guid": str(qso_guid or "").strip(),
            "location_id": default_location_id,
        },
    )
    return VaultActionResult(reason, True, summary, next_state, publish_result=publish_result)


def _flamp_queue_from_view_label(label: object) -> str:
    match = re.search(r"\bFLAMP\s+([A-Z0-9]{3,12})\b", str(label or "").upper())
    return str(match.group(1) or "").strip().upper() if match else ""


def _flamp_overlay_request_from_state(state: VaultRuntimeState) -> Tuple[str, Tuple[int, ...]]:
    queue_id = _flamp_queue_from_view_label(state.current_view_label)
    overlay_name = str(state.current_overlay_file or "").strip()
    file_match = re.match(
        rf"^{re.escape(DEFAULT_FLAMP_FILE_PREFIX)}_([A-Z0-9]{{3,12}})_BLK_([0-9_Øø]+)\.txt$",
        overlay_name,
        re.IGNORECASE,
    )
    if file_match and not queue_id:
        queue_id = str(file_match.group(1) or "").upper()
    block_text = str(file_match.group(2) if file_match else "").replace("Ø", "0").replace("ø", "0")
    block_numbers = tuple(int(part) for part in block_text.split("_") if part.isdigit())
    return queue_id, block_numbers


def _publish_flamp_refresh_action(
    *,
    sender: str,
    qso_guid: str,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    default_location_id: str,
    runtime_state: VaultRuntimeState,
    now_ts: float,
    flamp_relay_dir: str,
    flamp_listing_max_age_days: int,
    reason: str,
) -> Optional[VaultActionResult]:
    state = load_vault_runtime_state(vault_runtime_state_to_data(runtime_state))
    mode = str(state.current_view_mode or "").strip()
    if not mode.startswith("flamp") or not flamp_relay_dir:
        return None
    base_location = _location_by_id(locations, state.current_location_id) or _location_by_id(locations, default_location_id)
    base_source_dir = base_location.source_dir if base_location is not None else ""
    store = FlampRelayStore(flamp_relay_dir)
    try:
        overlay_file = ""
        if mode in {"flamp-help"}:
            publish_result = publish_flamp_command_help_view(
                base_source_dir=base_source_dir,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
            )
            label = "FLAMP commands"
            action_text = f"Managed BBS Library refreshed FLAMP command help for {sender or 'public'}."
        elif mode in {"flamp-list", "flamp-queue"}:
            publish_result = publish_flamp_queue_list_view(
                store,
                base_source_dir=base_source_dir,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                max_age_days=flamp_listing_max_age_days,
            )
            label = "FLAMP queue"
            action_text = f"Managed BBS Library refreshed FLAMP queue list for {sender or 'public'}."
        elif mode in {"flamp-block-list", "flamp-blocks"}:
            queue_id = _flamp_queue_from_view_label(state.current_view_label)
            if not queue_id:
                return None
            publish_result = publish_flamp_block_list_view(
                store,
                queue_id,
                base_source_dir=base_source_dir,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
            )
            label = f"FLAMP {queue_id} blocks"
            action_text = f"Managed BBS Library refreshed FLAMP block list {queue_id} for {sender or 'public'}."
        elif mode == "flamp-block-overlay":
            queue_id, block_numbers = _flamp_overlay_request_from_state(state)
            if not queue_id or not block_numbers:
                return None
            publish_result, overlay_file = publish_flamp_block_overlay_view(
                store,
                queue_id,
                block_numbers,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
            )
            label = f"FLAMP {queue_id}"
            action_text = f"Managed BBS Library refreshed FLAMP overlay {overlay_file} for {sender or 'public'}."
        else:
            return None
    except Exception as exc:
        notice_result = publish_flamp_notice_view(
            message=f"FLAMP view no longer available; type LIST Q for current queues",
            base_source_dir=base_source_dir,
            live_bbs_dir=live_bbs_dir,
            managed_root=managed_root,
        )
        next_state = _update_state(
            state,
            current_session_callsign=_normalize_callsign(sender),
            current_session_qso_guid=str(qso_guid or "").strip(),
            current_view_mode="flamp-notice",
            current_view_label="FLAMP notice",
            last_publish_manifest_path=notice_result.manifest_path,
            last_publish_ts=now_ts,
            last_action=f"Managed BBS Library refreshed FLAMP notice for {sender or 'public'}: {exc}",
            last_request_ts=now_ts,
            last_error=str(exc),
            unmanaged_live_files=list(notice_result.unmanaged_live_files),
        )
        return VaultActionResult(reason, True, next_state.last_action, next_state, publish_result=notice_result)

    next_state = _update_state(
        state,
        current_session_callsign=_normalize_callsign(sender),
        current_session_qso_guid=str(qso_guid or "").strip(),
        current_view_mode=mode,
        current_view_label=label,
        current_overlay_file=overlay_file,
        last_publish_manifest_path=publish_result.manifest_path,
        last_publish_ts=now_ts,
        last_action=action_text,
        last_request_ts=now_ts,
        last_error="",
        unmanaged_live_files=list(publish_result.unmanaged_live_files),
    )
    return VaultActionResult(reason, True, action_text, next_state, publish_result=publish_result)


def _publish_refresh_action(
    *,
    sender: str,
    qso_guid: str,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    global_code_policy: str,
    runtime_state: VaultRuntimeState,
    now_ts: float,
    flamp_enabled: bool,
    reason: str,
    manifest_db_path: object = "",
) -> VaultActionResult:
    state = load_vault_runtime_state(vault_runtime_state_to_data(runtime_state))
    sender_norm = _normalize_callsign(sender)
    qso_guid_text = str(qso_guid or "").strip()
    same_session_location_refresh = (
        state.current_view_mode in {"location", "access-prompt"}
        and (not state.current_session_qso_guid or not qso_guid_text or state.current_session_qso_guid == qso_guid_text)
        and (not state.current_session_callsign or state.current_session_callsign == sender_norm)
    )
    if not same_session_location_refresh:
        return _publish_root_action(
            sender=sender_norm,
            qso_guid=qso_guid_text,
            locations=locations,
            live_bbs_dir=live_bbs_dir,
            managed_root=managed_root,
            default_location_id=default_location_id,
            global_allowed_callsigns=global_allowed_callsigns,
            limit_access_enabled=limit_access_enabled,
            global_code_policy=global_code_policy,
            runtime_state=state,
            now_ts=now_ts,
            flamp_enabled=flamp_enabled,
            reason=reason,
            manifest_db_path=manifest_db_path,
        )
    current_location = _location_by_id(locations, state.current_location_id)
    if current_location is None or not current_location.enabled:
        return _publish_root_action(
            sender=sender_norm,
            qso_guid=qso_guid_text,
            locations=locations,
            live_bbs_dir=live_bbs_dir,
            managed_root=managed_root,
            default_location_id=default_location_id,
            global_allowed_callsigns=global_allowed_callsigns,
            limit_access_enabled=limit_access_enabled,
            global_code_policy=global_code_policy,
            runtime_state=state,
            now_ts=now_ts,
            flamp_enabled=flamp_enabled,
            reason=reason,
            manifest_db_path=manifest_db_path,
        )
    if state.current_view_mode == "access-prompt":
        prompt_reason = "code_required"
        if state.last_error == "invalid_code":
            prompt_reason = "incorrect_code"
        elif state.last_error in {"cooldown_active", "cooldown_applied"}:
            prompt_reason = "cooldown"
        elif state.last_error == "rejected_callsign":
            prompt_reason = "callsign_restricted"
        publish_result = publish_location_access_prompt_view(
            current_location,
            live_bbs_dir=live_bbs_dir,
            managed_root=managed_root,
            reason=prompt_reason,
            manifest_db_path=manifest_db_path,
        )
        summary = f"Managed BBS Library refreshed access prompt for {current_location.name}."
        next_state = _update_state(
            state,
            current_location_id=current_location.id,
            current_session_callsign=sender_norm,
            current_session_qso_guid=qso_guid_text,
            current_view_mode="access-prompt",
            current_view_label=f"{current_location.name} access",
            current_overlay_file="",
            last_publish_manifest_path=publish_result.manifest_path,
            last_publish_ts=now_ts,
            last_action=summary,
            last_request_ts=now_ts,
            last_error="",
            unmanaged_live_files=list(publish_result.unmanaged_live_files),
        )
        _append_audit_event(
            managed_root,
            {
                "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": "refresh_access_prompt",
                "sender": sender_norm,
                "qso_guid": qso_guid_text,
                "location_id": current_location.id,
            },
        )
        return VaultActionResult("refresh_access_prompt", True, summary, next_state, publish_result=publish_result)
    publish_result = publish_location_view(
        current_location,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        manifest_db_path=manifest_db_path,
    )
    summary = f"Managed BBS Library refreshed {current_location.name} for {sender_norm or 'public'}."
    next_state = _update_state(
        state,
        current_location_id=current_location.id,
        current_session_callsign=sender_norm,
        current_session_qso_guid=qso_guid_text,
        current_view_mode="location",
        current_view_label=current_location.name,
        current_overlay_file="",
        last_publish_manifest_path=publish_result.manifest_path,
        last_publish_ts=now_ts,
        last_action=summary,
        last_request_ts=now_ts,
        last_error="",
        unmanaged_live_files=list(publish_result.unmanaged_live_files),
    )
    _append_audit_event(
        managed_root,
        {
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "action": "refresh_current_view",
            "sender": sender_norm,
            "qso_guid": qso_guid_text,
            "location_id": current_location.id,
        },
    )
    return VaultActionResult("refresh_current_view", True, summary, next_state, publish_result=publish_result)


def apply_unlock_request(
    sender: str,
    code_text: str,
    *,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    runtime_state: VaultRuntimeState,
    now_ts: Optional[float] = None,
    failed_attempt_limit: int = DEFAULT_FAILED_ATTEMPT_LIMIT,
    failed_attempt_window_seconds: int = DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    manifest_db_path: object = "",
) -> VaultActionResult:
    return _apply_open_request(
        sender=sender,
        qso_guid=runtime_state.current_session_qso_guid,
        requested_location=None,
        alias_text="",
        code_text=code_text,
        locations=locations,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        default_location_id=default_location_id,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
        runtime_state=runtime_state,
        now_ts=now_ts,
        failed_attempt_limit=failed_attempt_limit,
        failed_attempt_window_seconds=failed_attempt_window_seconds,
        cooldown_seconds=cooldown_seconds,
        global_code_policy=DEFAULT_GLOBAL_CODE_POLICY,
        action_reason="legacy_code_open",
        manifest_db_path=manifest_db_path,
    )


def _access_prompt_result(
    *,
    state: VaultRuntimeState,
    location: VaultLocation,
    sender: str,
    qso_guid: str,
    alias_text: str,
    live_bbs_dir: object,
    managed_root: object,
    now_ts: float,
    summary: str,
    action: str,
    reason: str,
    cooldowns: Mapping[str, Mapping[str, float]],
    failed_attempts: Mapping[str, Sequence[float]],
    manifest_db_path: object = "",
) -> VaultActionResult:
    publish_result = publish_location_access_prompt_view(
        location,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        reason=reason,
        manifest_db_path=manifest_db_path,
    )
    next_state = _update_state(
        state,
        current_location_id=location.id,
        current_session_callsign=sender,
        current_session_qso_guid=str(qso_guid or "").strip(),
        current_view_mode="access-prompt",
        current_view_label=f"{location.name} access",
        cooldowns=cooldowns,
        failed_attempts=failed_attempts,
        current_overlay_file="",
        last_publish_manifest_path=publish_result.manifest_path,
        last_publish_ts=now_ts,
        last_action=summary,
        last_request_ts=now_ts,
        last_error=action,
        unmanaged_live_files=list(publish_result.unmanaged_live_files),
    )
    _append_audit_event(
        managed_root,
        {
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "action": action,
            "sender": sender,
            "location_id": location.id,
            "location_name": location.name,
            "alias": alias_text,
            "prompt_reason": reason,
        },
    )
    return VaultActionResult(action, False, summary, next_state, publish_result=publish_result)


def _apply_open_request(
    *,
    sender: str,
    qso_guid: str,
    requested_location: Optional[VaultLocation],
    alias_text: str,
    code_text: str,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    runtime_state: VaultRuntimeState,
    now_ts: Optional[float],
    failed_attempt_limit: int,
    failed_attempt_window_seconds: int,
    cooldown_seconds: int,
    global_code_policy: str,
    action_reason: str,
    manifest_db_path: object = "",
) -> VaultActionResult:
    now_ts = float(now_ts if now_ts is not None else time.time())
    sender = _normalize_callsign(sender)
    code_text = _normalize_access_code_text(code_text)
    state = load_vault_runtime_state(vault_runtime_state_to_data(runtime_state))
    if not sender:
        summary = "Managed BBS Library ignored request with no identifiable callsign."
        return VaultActionResult("ignored_no_sender", False, summary, state)

    if state.current_session_qso_guid and qso_guid and state.current_session_qso_guid != qso_guid and state.current_session_callsign:
        summary = f"Managed BBS Library session is locked to {state.current_session_callsign}."
        return VaultActionResult("session_locked", False, summary, _update_state(state, last_action=summary, last_error="session_locked"))

    cooldowns = {str(key): dict(value) for key, value in state.cooldowns.items()}
    current_cooldown = cooldowns.get(sender, {})
    until_ts = float(current_cooldown.get("until_ts", 0.0) or 0.0)
    if until_ts > now_ts:
        remaining = int(max(1.0, until_ts - now_ts))
        summary = f"Managed BBS Library cooldown active for {sender} ({remaining}s remaining)."
        if requested_location is not None:
            return _access_prompt_result(
                state=state,
                location=requested_location,
                sender=sender,
                qso_guid=qso_guid,
                alias_text=alias_text,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                now_ts=now_ts,
                summary=summary,
                action="cooldown_active",
                reason="cooldown",
                cooldowns=cooldowns,
                failed_attempts={str(key): list(value) for key, value in state.failed_attempts.items()},
                manifest_db_path=manifest_db_path,
            )
        return VaultActionResult("cooldown_active", False, summary, _update_state(state, last_action=summary, last_error="cooldown_active"))

    matched_location = requested_location
    matched_by_verified_code = False
    if matched_location is None:
        for location in locations:
            if not location.enabled or location.id == default_location_id:
                continue
            if not location.access_code_hash or not location.access_code_salt:
                continue
            if verify_access_code(
                code_text,
                access_code_hash=location.access_code_hash,
                access_code_salt=location.access_code_salt,
                access_code_iterations=location.access_code_iterations,
            ):
                matched_location = location
                matched_by_verified_code = True
                break

    failed_attempts = {str(key): list(value) for key, value in state.failed_attempts.items()}
    sender_attempts = _prune_attempts(
        failed_attempts.get(sender, []),
        now_ts=now_ts,
        window_seconds=failed_attempt_window_seconds,
    )

    if matched_location is None:
        sender_attempts.append(now_ts)
        failed_attempts[sender] = sender_attempts
        action = "invalid_code"
        summary = f"Managed BBS Library rejected access request from {sender}."
        if len(sender_attempts) >= max(1, int(failed_attempt_limit)):
            cooldowns[sender] = {
                "until_ts": now_ts + max(1, int(cooldown_seconds)),
                "reason": "failed_attempt_limit",
            }
            action = "cooldown_applied"
            summary = f"Managed BBS Library rejected access request from {sender}; cooldown applied."
        next_state = _update_state(
            state,
            cooldowns=cooldowns,
            failed_attempts=failed_attempts,
            last_action=summary,
            last_error=action,
        )
        _append_audit_event(
            managed_root,
            {
                "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": action,
                "sender": sender,
                "alias": alias_text,
            },
        )
        return VaultActionResult(action, False, summary, next_state)

    open_rule = str(matched_location.open_rule or "Public").strip()
    if open_rule != "Public" and not _location_sender_allowed(
        sender,
        location=matched_location,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
    ):
        summary = f"Managed BBS Library rejected {sender}; callsign is not allowed for {matched_location.name}."
        return _access_prompt_result(
            state=state,
            location=matched_location,
            sender=sender,
            qso_guid=qso_guid,
            alias_text=alias_text,
            live_bbs_dir=live_bbs_dir,
            managed_root=managed_root,
            now_ts=now_ts,
            summary=summary,
            action="rejected_callsign",
            reason="callsign_restricted",
            cooldowns=cooldowns,
            failed_attempts=failed_attempts,
            manifest_db_path=manifest_db_path,
        )

    if (
        _location_requires_code(matched_location, default_location_id=default_location_id, global_code_policy=global_code_policy)
        and not matched_by_verified_code
    ):
        if not verify_access_code(
            code_text,
            access_code_hash=matched_location.access_code_hash,
            access_code_salt=matched_location.access_code_salt,
            access_code_iterations=matched_location.access_code_iterations,
        ):
            sender_attempts.append(now_ts)
            failed_attempts[sender] = sender_attempts
            summary = f"Managed BBS Library rejected access code for {matched_location.name} from {sender}."
            action = "invalid_code" if code_text else "missing_code"
            if len(sender_attempts) >= max(1, int(failed_attempt_limit)):
                cooldowns[sender] = {
                    "until_ts": now_ts + max(1, int(cooldown_seconds)),
                    "reason": "failed_attempt_limit",
                }
                action = "cooldown_applied"
                summary = f"Managed BBS Library rejected access code for {matched_location.name} from {sender}; cooldown applied."
            return _access_prompt_result(
                state=state,
                location=matched_location,
                sender=sender,
                qso_guid=qso_guid,
                alias_text=alias_text,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                now_ts=now_ts,
                summary=summary,
                action=action,
                reason=(
                    "cooldown"
                    if action == "cooldown_applied"
                    else ("incorrect_code" if action == "invalid_code" else "code_required")
                ),
                cooldowns=cooldowns,
                failed_attempts=failed_attempts,
                manifest_db_path=manifest_db_path,
            )

    publish_result = publish_location_view(
        matched_location,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        manifest_db_path=manifest_db_path,
    )
    failed_attempts.pop(sender, None)
    cooldowns.pop(sender, None)
    summary = f"Managed BBS Library published {matched_location.name} for {sender}."
    next_state = _update_state(
        state,
        current_location_id=matched_location.id,
        current_session_callsign=sender,
        current_session_qso_guid=str(qso_guid or "").strip(),
        current_view_mode="location",
        current_view_label=matched_location.name,
        cooldowns=cooldowns,
        failed_attempts=failed_attempts,
        current_overlay_file="",
        last_publish_manifest_path=publish_result.manifest_path,
        last_publish_ts=now_ts,
        last_action=summary,
        last_request_ts=now_ts,
        last_error="",
        unmanaged_live_files=list(publish_result.unmanaged_live_files),
    )
    _append_audit_event(
        managed_root,
        {
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "action": action_reason,
            "sender": sender,
            "location_id": matched_location.id,
            "location_name": matched_location.name,
            "alias": alias_text,
        },
    )
    return VaultActionResult(action_reason, True, summary, next_state, publish_result=publish_result)


def reset_to_default_location(
    *,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    default_location_id: str,
    runtime_state: VaultRuntimeState,
    global_allowed_callsigns: Sequence[str] = (),
    limit_access_enabled: bool = False,
    global_code_policy: str = DEFAULT_GLOBAL_CODE_POLICY,
    flamp_enabled: bool = False,
    reason: str = "manual_reset",
    now_ts: Optional[float] = None,
    manifest_db_path: object = "",
) -> VaultActionResult:
    now_ts = float(now_ts if now_ts is not None else time.time())
    state = load_vault_runtime_state(vault_runtime_state_to_data(runtime_state))
    publish_result = publish_root_view(
        sender="",
        locations=locations,
        default_location_id=default_location_id,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
        global_code_policy=global_code_policy,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        flamp_enabled=flamp_enabled,
        include_enabled_fallback=True,
        manifest_db_path=manifest_db_path,
    )
    summary = f"Managed BBS Library returned to {DEFAULT_LOCATION_NAME}."
    next_state = _update_state(
        state,
        current_location_id=default_location_id,
        current_session_callsign="",
        current_session_qso_guid="",
        current_view_mode="root",
        current_view_label=_summary_location_name(locations, default_location_id),
        current_overlay_file="",
        last_publish_manifest_path=publish_result.manifest_path,
        last_publish_ts=now_ts,
        last_action=summary,
        last_request_ts=0.0,
        last_error="",
        unmanaged_live_files=list(publish_result.unmanaged_live_files),
    )
    _append_audit_event(
        managed_root,
        {
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "action": "returned_to_default",
            "reason": reason,
            "location_id": default_location_id,
        },
    )
    return VaultActionResult("returned_to_default", True, summary, next_state, publish_result=publish_result)


def _restore_previous_view(
    *,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    default_location_id: str,
    runtime_state: VaultRuntimeState,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    global_code_policy: str,
    flamp_enabled: bool,
    now_ts: Optional[float] = None,
    reason: str = "overlay_restore",
    manifest_db_path: object = "",
) -> VaultActionResult:
    now_ts = float(now_ts if now_ts is not None else time.time())
    if runtime_state.previous_view_mode == "location":
        location = _location_by_id(locations, runtime_state.previous_location_id)
        if location is None:
            return reset_to_default_location(
                locations=locations,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                default_location_id=default_location_id,
                runtime_state=runtime_state,
                global_allowed_callsigns=global_allowed_callsigns,
                limit_access_enabled=limit_access_enabled,
                global_code_policy=global_code_policy,
                flamp_enabled=flamp_enabled,
                reason=reason,
                now_ts=now_ts,
                manifest_db_path=manifest_db_path,
            )
        publish_result = publish_location_view(
            location,
            live_bbs_dir=live_bbs_dir,
            managed_root=managed_root,
            manifest_db_path=manifest_db_path,
        )
        summary = f"Managed BBS Library restored {location.name} after FLAMP overlay."
        next_state = _update_state(
            runtime_state,
            current_location_id=location.id,
            current_view_mode="location",
            current_view_label=location.name,
            current_overlay_file="",
            previous_location_id=default_location_id,
            previous_view_mode="root",
            previous_view_label=_summary_location_name(locations, default_location_id),
            last_publish_manifest_path=publish_result.manifest_path,
            last_publish_ts=now_ts,
            last_action=summary,
            last_request_ts=now_ts,
            last_error="",
            unmanaged_live_files=list(publish_result.unmanaged_live_files),
        )
        return VaultActionResult("overlay_restore", True, summary, next_state, publish_result=publish_result)
    return _publish_root_action(
        sender=runtime_state.current_session_callsign,
        qso_guid=runtime_state.current_session_qso_guid,
        locations=locations,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        default_location_id=default_location_id,
        global_allowed_callsigns=global_allowed_callsigns,
        limit_access_enabled=limit_access_enabled,
        global_code_policy=global_code_policy,
        runtime_state=runtime_state,
        now_ts=now_ts,
        flamp_enabled=flamp_enabled,
        reason=reason,
        manifest_db_path=manifest_db_path,
    )


def _reconcile_current_location(
    settings,
    *,
    locations: Sequence[VaultLocation],
    live_bbs_dir: object,
    managed_root: object,
    runtime_state: VaultRuntimeState,
    default_location_id: str,
    global_allowed_callsigns: Sequence[str],
    limit_access_enabled: bool,
    global_code_policy: str,
    flamp_enabled: bool,
    manifest_db_path: object = "",
) -> Tuple[VaultRuntimeState, bool]:
    try:
        if runtime_state.current_view_mode == "location":
            current_location = _location_by_id(locations, runtime_state.current_location_id)
            if current_location is None or not current_location.enabled:
                return runtime_state, False
            publish_result = publish_location_view(
                current_location,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                manifest_db_path=manifest_db_path,
            )
        elif runtime_state.current_view_mode == "access-prompt":
            current_location = _location_by_id(locations, runtime_state.current_location_id)
            if current_location is None or not current_location.enabled:
                return runtime_state, False
            prompt_reason = "code_required"
            if runtime_state.last_error == "invalid_code":
                prompt_reason = "incorrect_code"
            elif runtime_state.last_error in {"cooldown_active", "cooldown_applied"}:
                prompt_reason = "cooldown"
            elif runtime_state.last_error == "rejected_callsign":
                prompt_reason = "callsign_restricted"
            publish_result = publish_location_access_prompt_view(
                current_location,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                reason=prompt_reason,
                manifest_db_path=manifest_db_path,
            )
        elif runtime_state.current_view_mode == "flamp-block-overlay":
            overlay_name = str(runtime_state.current_overlay_file or "").strip()
            if overlay_name:
                live_dir = _resolve_path(live_bbs_dir)
                if live_dir is not None and not (live_dir / overlay_name).exists():
                    flamp_relay_dir = (
                        str(settings.get("varac_bbs_vault_flamp_relay_dir", "") or "").strip()
                        if settings is not None
                        else ""
                    )
                    queue_id, block_numbers = _flamp_overlay_request_from_state(runtime_state)
                    if flamp_enabled and flamp_relay_dir and queue_id and block_numbers:
                        store = FlampRelayStore(flamp_relay_dir)
                        publish_result, refreshed_overlay = publish_flamp_block_overlay_view(
                            store,
                            queue_id,
                            block_numbers,
                            live_bbs_dir=live_bbs_dir,
                            managed_root=managed_root,
                        )
                        next_state = _update_state(
                            runtime_state,
                            current_overlay_file=refreshed_overlay,
                            last_publish_manifest_path=publish_result.manifest_path,
                            last_publish_ts=time.time(),
                            last_action=f"Managed BBS Library restored FLAMP overlay {refreshed_overlay}.",
                            last_error="",
                            unmanaged_live_files=list(publish_result.unmanaged_live_files),
                        )
                        return next_state, bool(publish_result.changed)
                    next_state = _update_state(
                        runtime_state,
                        last_action=f"Managed BBS Library is keeping FLAMP overlay {overlay_name} active, but it could not be recreated.",
                        last_error="FLAMP overlay source unavailable",
                    )
                    return next_state, False
            return runtime_state, False
        elif runtime_state.current_view_mode.startswith("flamp"):
            return runtime_state, False
        else:
            publish_result = publish_root_view(
                sender=runtime_state.current_session_callsign,
                locations=locations,
                default_location_id=default_location_id,
                global_allowed_callsigns=global_allowed_callsigns,
                limit_access_enabled=limit_access_enabled,
                global_code_policy=global_code_policy,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                flamp_enabled=flamp_enabled,
                include_enabled_fallback=True,
                manifest_db_path=manifest_db_path,
            )
    except Exception as exc:
        summary = f"Managed BBS Library degraded: {exc}"
        next_state = _update_state(runtime_state, last_action=summary, last_error=str(exc))
        return next_state, False
    next_state = _update_state(
        runtime_state,
        last_publish_manifest_path=publish_result.manifest_path,
        last_publish_ts=time.time(),
        unmanaged_live_files=list(publish_result.unmanaged_live_files),
    )
    return next_state, bool(publish_result.changed)


def _summary_text(runtime_state: VaultRuntimeState) -> str:
    summary = f"Managed BBS Library {runtime_state.current_view_label or DEFAULT_LOCATION_NAME}"
    if runtime_state.current_session_callsign:
        summary += f" | Session {runtime_state.current_session_callsign}"
    if runtime_state.current_view_mode == "flamp-block-overlay":
        summary += " | FLAMP overlay"
    elif runtime_state.current_view_mode.startswith("flamp"):
        summary += " | FLAMP relay"
    if runtime_state.unmanaged_live_files:
        summary += f" | Unmanaged live files: {len(runtime_state.unmanaged_live_files)}"
    if runtime_state.last_error:
        summary += f" | {runtime_state.last_action or runtime_state.last_error}"
    elif runtime_state.last_action:
        summary += f" | {runtime_state.last_action}"
    return summary


def _station_catalog_vault_configuration(
    db_path: object,
    fallback_locations: Sequence[VaultLocation],
) -> tuple[List[VaultLocation], Dict[str, str]]:
    """Load station-owned location policy without scanning source folders."""

    resolved = _resolve_path(db_path)
    if resolved is None:
        return list(fallback_locations), {}
    try:
        with connect_sqlite(resolved) as conn:
            ensure_bbs_library_schema(conn)
            records = list_bbs_locations(conn, include_disabled=True)
            meta_rows = conn.execute(
                "SELECT key, value FROM bbs_library_meta WHERE key LIKE 'station_%' OR key='global_retention_days'"
            ).fetchall()
    except Exception as exc:
        log_bbs_library_sync_failure(exc)
        return list(fallback_locations), {}
    if not records:
        return list(fallback_locations), {str(key or ""): str(value or "") for key, value in meta_rows}
    location_data: List[Dict[str, object]] = []
    for record in records:
        payload = dict(record.metadata or {})
        payload.update(
            {
                "id": record.location_id,
                "name": record.name,
                "source_dir": record.source_dir,
                "enabled": record.enabled,
                "open_rule": record.access_rule,
                "visibility_rule": payload.get("visibility_rule", "Public"),
                "retention_policy": {
                    "manual": "Keep until manually removed",
                    "expire_after_days": "Archive this location by age",
                }.get(record.retention_mode, "Use global BBS archive policy"),
                "retention_days": record.retention_days,
            }
        )
        location_data.append(payload)
    return load_vault_locations(location_data), {
        str(key or ""): str(value or "") for key, value in meta_rows
    }


def _explicit_catalog_db_path(settings: object) -> object:
    """Return only a database identity carried by this runtime settings object."""

    current = settings
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attr in ("db_path", "_config_path"):
            value = getattr(current, attr, None)
            if value:
                return value
        current = getattr(current, "fallback_settings", None)
    return ""


def run_varac_bbs_vault(settings) -> VaracBbsVaultRunResult:
    enabled = bool(settings.get("varac_bbs_vault_enabled", False) if settings is not None else False)
    if not enabled:
        return VaracBbsVaultRunResult(False, 0, 0, False, DEFAULT_LOCATION_ID, "", "Managed BBS Library disabled")

    live_bbs_dir = str(settings.get("varac_bbs_dir", "") or "").strip() if settings is not None else ""
    managed_root = compute_default_managed_root(live_bbs_dir)
    trigger_mode = DEFAULT_TRIGGER_MODE
    return_mode = DEFAULT_RETURN_MODE
    failed_attempt_limit = int(settings.get("varac_bbs_vault_failed_attempt_limit", DEFAULT_FAILED_ATTEMPT_LIMIT) or DEFAULT_FAILED_ATTEMPT_LIMIT)
    failed_attempt_window_seconds = int(settings.get("varac_bbs_vault_failed_attempt_window_seconds", DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS) or DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS)
    cooldown_seconds = int(settings.get("varac_bbs_vault_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS) or DEFAULT_COOLDOWN_SECONDS)
    flamp_enabled = bool(settings.get("varac_bbs_vault_flamp_enabled", False) if settings is not None else False)
    flamp_relay_dir = str(settings.get("varac_bbs_vault_flamp_relay_dir", "") or "").strip() if settings is not None else ""
    flamp_listing_max_age_days = int(
        settings.get("varac_bbs_vault_flamp_listing_max_age_days", DEFAULT_FLAMP_LISTING_MAX_AGE_DAYS)
        or DEFAULT_FLAMP_LISTING_MAX_AGE_DAYS
    )
    legacy_locations = load_vault_locations(settings.get("varac_bbs_vault_locations_v1", []))
    # Lightweight/legacy callers without an explicit catalog identity remain
    # on the folder-backed contract.  They must never borrow the operator's
    # process-global station database by accident.
    manifest_db_path = _explicit_catalog_db_path(settings)
    locations, station_meta = _station_catalog_vault_configuration(manifest_db_path, legacy_locations)
    station_root = str(station_meta.get("station_managed_root") or "").strip()
    if station_root:
        managed_root = station_root
    default_location_id = str(
        station_meta.get("station_default_location_id")
        or settings.get("varac_bbs_vault_default_location_id", DEFAULT_LOCATION_ID)
        or DEFAULT_LOCATION_ID
    ).strip() or DEFAULT_LOCATION_ID
    global_code_policy = str(
        station_meta.get("station_global_code_policy") or DEFAULT_GLOBAL_CODE_POLICY
    ).strip() or DEFAULT_GLOBAL_CODE_POLICY
    runtime_state = load_vault_runtime_state(settings.get("varac_bbs_vault_runtime_state_v1", {}))
    initial_state_data = vault_runtime_state_to_data(runtime_state)
    initial_unmanaged_live_files = tuple(runtime_state.unmanaged_live_files)
    global_allowed = parse_callsign_list(
        station_meta.get("station_allowed_callsigns")
        or (settings.get("varac_bbs_allowed_callsigns", "") if settings is not None else "")
    )
    if "station_limit_access_enabled" in station_meta:
        limit_access_enabled = station_meta.get("station_limit_access_enabled") == "1"
    else:
        limit_access_enabled = bool(
            settings.get("varac_bbs_limit_access_enabled", False) if settings is not None else False
        )

    if not live_bbs_dir or not managed_root or not locations:
        summary = "Managed BBS Library needs setup before it can run."
        _persist_runtime_state(settings, runtime_state, summary)
        return VaracBbsVaultRunResult(True, 0, 0, False, runtime_state.current_location_id, runtime_state.current_session_callsign, summary)

    try:
        initialize_managed_root(managed_root)
    except Exception as exc:
        summary = f"Managed BBS Library initialization failed: {exc}"
        error_state = _update_state(runtime_state, last_action=summary, last_error=str(exc))
        _persist_runtime_state(settings, error_state, summary)
        return VaracBbsVaultRunResult(True, 0, 0, False, error_state.current_location_id, error_state.current_session_callsign, summary)

    try:
        sync_bbs_locations_from_folders(
            manifest_db_path,
            [
                {
                    "id": location.id,
                    "name": location.name,
                    "source_dir": _effective_location_source_dir(location, managed_root),
                    "enabled": location.enabled,
                    "alias": location.alias,
                    "visibility_rule": location.visibility_rule,
                    "open_rule": location.open_rule,
                    "retention_policy": location.retention_policy,
                    "retention_days": location.retention_days,
                }
                for location in locations
            ],
        )
    except Exception as exc:
        log_bbs_library_sync_failure(exc)

    alias_map, alias_collisions = _build_alias_map(locations, default_location_id=default_location_id)
    _record_alias_health(alias_collisions)
    if alias_collisions:
        collision_text = ", ".join(sorted(set(alias_collisions)))
        summary = f"Managed BBS Library alias collision ignored for: {collision_text}"
        log.warning("varac_bbs_vault: %s", summary)
        runtime_state = _update_state(runtime_state, last_action=summary, last_error="alias_collision")
    varac_db_path = _resolve_varac_db_path(settings)
    scanned = 0
    processed = 0
    published = False
    now_ts = time.time()

    if not runtime_state.last_publish_manifest_path:
        runtime_state = _update_state(
            runtime_state,
            last_publish_manifest_path=str(_manifest_path_for(managed_root, live_bbs_dir)),
        )

    configured_local_calls = [
        settings.get("operator_callsign", "") if settings is not None else "",
        settings.get("callsign", "") if settings is not None else "",
    ]
    log_paths = resolve_varac_traffic_log_paths(settings)
    seen_keys = set(runtime_state.processed_event_keys)
    log_cursor_map = {str(key): dict(value) for key, value in runtime_state.log_cursors.items()}
    log_events_for_run: List[VaultLogEvent] = []
    for log_path in log_paths:
        cursor_key = _log_cursor_key(log_path)
        text, next_cursor, readable = _read_log_increment(log_path, log_cursor_map.get(cursor_key, {}))
        if readable:
            log_cursor_map[cursor_key] = next_cursor
        for event in parse_vault_log_events(
            text,
            trigger_mode=trigger_mode,
            log_path=str(log_path),
            alias_map=alias_map,
            local_callsigns=configured_local_calls,
        ):
            if _state_key(event) in seen_keys:
                continue
            log_events_for_run.append(event)

    events: Tuple[VaultDbEvent, ...] = ()
    max_scanned_datastream_id = int(runtime_state.last_datastream_id or 0)
    if varac_db_path is not None and not log_paths:
        db_scan = _load_db_events(varac_db_path, last_datastream_id=runtime_state.last_datastream_id, alias_map=alias_map)
        events = db_scan.events
        max_scanned_datastream_id = max(max_scanned_datastream_id, int(db_scan.max_scanned_row_id or 0))
        if events:
            runtime_state = _update_state(runtime_state, last_error="traffic_log_unavailable")

    for event in events:
        scanned += 1
        runtime_state = _update_state(runtime_state, last_datastream_id=event.row_id)
        if event.kind == "disconnect":
            if runtime_state.current_session_qso_guid and event.qso_guid == runtime_state.current_session_qso_guid:
                if return_mode != "Manual operator reset only":
                    result = reset_to_default_location(
                        locations=locations,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        default_location_id=default_location_id,
                        runtime_state=runtime_state,
                        global_allowed_callsigns=global_allowed,
                        limit_access_enabled=limit_access_enabled,
                        global_code_policy=global_code_policy,
                        flamp_enabled=flamp_enabled,
                        reason="disconnect",
                        now_ts=event.timestamp_utc or now_ts,
                        manifest_db_path=manifest_db_path,
                    )
                    runtime_state = result.runtime_state
                    published = published or bool(result.publish_result and result.publish_result.changed)
                else:
                    runtime_state = _update_state(
                        runtime_state,
                        current_session_callsign="",
                        current_session_qso_guid="",
                        last_action="Managed BBS Library session disconnected.",
                    )
                processed += 1
            continue
        if runtime_state.current_session_qso_guid and runtime_state.current_session_qso_guid != event.qso_guid:
            runtime_state = _update_state(
                runtime_state,
                current_location_id=default_location_id,
                current_session_callsign="",
                current_session_qso_guid="",
                current_view_mode=DEFAULT_VIEW_MODE,
                current_view_label=_summary_location_name(locations, default_location_id),
                current_overlay_file="",
            )
        if event.kind == "root_request":
            result = None
            if flamp_enabled and flamp_relay_dir:
                result = _publish_flamp_refresh_action(
                    sender=event.remote_callsign,
                    qso_guid=event.qso_guid,
                    locations=locations,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    default_location_id=default_location_id,
                    runtime_state=runtime_state,
                    now_ts=event.timestamp_utc or now_ts,
                    flamp_relay_dir=flamp_relay_dir,
                    flamp_listing_max_age_days=flamp_listing_max_age_days,
                    reason="root_request",
                )
            if result is None:
                result = _publish_refresh_action(
                    sender=event.remote_callsign,
                    qso_guid=event.qso_guid,
                    locations=locations,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    default_location_id=default_location_id,
                    global_allowed_callsigns=global_allowed,
                    limit_access_enabled=limit_access_enabled,
                    global_code_policy=global_code_policy,
                    runtime_state=runtime_state,
                    now_ts=event.timestamp_utc or now_ts,
                    flamp_enabled=flamp_enabled,
                    reason="root_request",
                    manifest_db_path=manifest_db_path,
                )
            runtime_state = result.runtime_state
            published = published or bool(result.publish_result and result.publish_result.changed)
            processed += 1
            continue
        if event.kind == "root_return":
            result = _publish_root_action(
                sender=event.remote_callsign,
                qso_guid=event.qso_guid,
                locations=locations,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                default_location_id=default_location_id,
                global_allowed_callsigns=global_allowed,
                limit_access_enabled=limit_access_enabled,
                global_code_policy=global_code_policy,
                runtime_state=runtime_state,
                now_ts=event.timestamp_utc or now_ts,
                flamp_enabled=flamp_enabled,
                reason="root_return",
                manifest_db_path=manifest_db_path,
            )
            runtime_state = result.runtime_state
            published = published or bool(result.publish_result and result.publish_result.changed)
            processed += 1
            continue
        if event.kind == "legacy_code_open":
            result = _apply_open_request(
                sender=event.remote_callsign,
                qso_guid=event.qso_guid,
                requested_location=None,
                alias_text="",
                code_text=event.code_text,
                locations=locations,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                default_location_id=default_location_id,
                global_allowed_callsigns=global_allowed,
                limit_access_enabled=limit_access_enabled,
                runtime_state=runtime_state,
                now_ts=event.timestamp_utc or now_ts,
                failed_attempt_limit=failed_attempt_limit,
                failed_attempt_window_seconds=failed_attempt_window_seconds,
                cooldown_seconds=cooldown_seconds,
                global_code_policy=global_code_policy,
                action_reason="legacy_code_open",
                manifest_db_path=manifest_db_path,
            )
            runtime_state = result.runtime_state
            published = published or bool(result.publish_result and result.publish_result.changed)
            processed += 1
            continue
        if event.kind == "open_alias":
            requested = _location_by_alias(locations, event.alias)
            result = _apply_open_request(
                sender=event.remote_callsign,
                qso_guid=event.qso_guid,
                requested_location=requested,
                alias_text=event.alias,
                code_text=event.code_text,
                locations=locations,
                live_bbs_dir=live_bbs_dir,
                managed_root=managed_root,
                default_location_id=default_location_id,
                global_allowed_callsigns=global_allowed,
                limit_access_enabled=limit_access_enabled,
                runtime_state=runtime_state,
                now_ts=event.timestamp_utc or now_ts,
                failed_attempt_limit=failed_attempt_limit,
                failed_attempt_window_seconds=failed_attempt_window_seconds,
                cooldown_seconds=cooldown_seconds,
                global_code_policy=global_code_policy,
                action_reason="open_alias",
                manifest_db_path=manifest_db_path,
            )
            runtime_state = result.runtime_state
            published = published or bool(result.publish_result and result.publish_result.changed)
            processed += 1
            continue
        if event.kind == "flamp_help" and flamp_enabled and flamp_relay_dir:
            base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(locations, default_location_id)
            if base_location is not None:
                publish_result = publish_flamp_command_help_view(
                    base_source_dir=base_location.source_dir,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    refresh_retry_command=_refresh_retry_command_for_event(event),
                )
                runtime_state = _update_state(
                    runtime_state,
                    current_session_callsign=event.remote_callsign,
                    current_session_qso_guid=event.qso_guid,
                    current_view_mode="flamp-help",
                    current_view_label="FLAMP commands",
                    last_publish_manifest_path=publish_result.manifest_path,
                    last_publish_ts=event.timestamp_utc or now_ts,
                    last_action=f"Managed BBS Library published FLAMP command help for {event.remote_callsign}.",
                    last_request_ts=event.timestamp_utc or now_ts,
                    last_error="",
                    unmanaged_live_files=list(publish_result.unmanaged_live_files),
                )
                published = published or bool(publish_result.changed)
                processed += 1
            continue
        if event.kind == "flamp_list_q" and flamp_enabled and flamp_relay_dir:
            store = FlampRelayStore(flamp_relay_dir)
            base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(locations, default_location_id)
            if base_location is not None:
                publish_result = publish_flamp_queue_list_view(
                    store,
                    base_source_dir=base_location.source_dir,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    max_age_days=flamp_listing_max_age_days,
                    refresh_retry_command=_refresh_retry_command_for_event(event),
                )
                runtime_state = _update_state(
                    runtime_state,
                    current_session_callsign=event.remote_callsign,
                    current_session_qso_guid=event.qso_guid,
                    current_view_mode="flamp-list",
                    current_view_label=f"FLAMP {DEFAULT_LOCATION_NAME}",
                    last_publish_manifest_path=publish_result.manifest_path,
                    last_publish_ts=event.timestamp_utc or now_ts,
                    last_action=f"Managed BBS Library published FLAMP queue list for {event.remote_callsign}.",
                    last_request_ts=event.timestamp_utc or now_ts,
                    last_error="",
                    unmanaged_live_files=list(publish_result.unmanaged_live_files),
                )
                published = published or bool(publish_result.changed)
                processed += 1
            continue
        if event.kind == "flamp_list_blocks" and flamp_enabled and flamp_relay_dir:
            store = FlampRelayStore(flamp_relay_dir)
            base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(locations, default_location_id)
            if base_location is not None:
                try:
                    publish_result = publish_flamp_block_list_view(
                        store,
                        event.queue_id,
                        base_source_dir=base_location.source_dir,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        refresh_retry_command=_refresh_retry_command_for_event(event),
                    )
                    runtime_state = _update_state(
                        runtime_state,
                        current_session_callsign=event.remote_callsign,
                        current_session_qso_guid=event.qso_guid,
                        current_view_mode="flamp-block-list",
                        current_view_label=f"FLAMP {event.queue_id}",
                        last_publish_manifest_path=publish_result.manifest_path,
                        last_publish_ts=event.timestamp_utc or now_ts,
                        last_action=f"Managed BBS Library published FLAMP block list {event.queue_id} for {event.remote_callsign}.",
                        last_request_ts=event.timestamp_utc or now_ts,
                        last_error="",
                        unmanaged_live_files=list(publish_result.unmanaged_live_files),
                    )
                    published = published or bool(publish_result.changed)
                    processed += 1
                except Exception as exc:
                    notice_result = publish_flamp_notice_view(
                        message=f"FLAMP queue {event.queue_id} not available; type LIST Q for current queues",
                        base_source_dir=base_location.source_dir,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        refresh_retry_command=_refresh_retry_command_for_event(event),
                    )
                    runtime_state = _update_state(
                        runtime_state,
                        current_session_callsign=event.remote_callsign,
                        current_session_qso_guid=event.qso_guid,
                        current_view_mode="flamp-notice",
                        current_view_label=f"FLAMP {event.queue_id}",
                        last_publish_manifest_path=notice_result.manifest_path,
                        last_publish_ts=event.timestamp_utc or now_ts,
                        last_action=f"FLAMP queue {event.queue_id} not available for {event.remote_callsign}: {exc}",
                        last_request_ts=event.timestamp_utc or now_ts,
                        last_error=str(exc),
                        unmanaged_live_files=list(notice_result.unmanaged_live_files),
                    )
                    published = published or bool(notice_result.changed)
                    processed += 1
            continue
        if event.kind == "flamp_invalid_block_request" and flamp_enabled and flamp_relay_dir:
            base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(locations, default_location_id)
            if base_location is not None:
                notice_result = publish_flamp_notice_view(
                    message=_invalid_flamp_block_request_message(event.queue_id),
                    base_source_dir=base_location.source_dir,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    refresh_retry_command=_refresh_retry_command_for_event(event),
                )
                runtime_state = _update_state(
                    runtime_state,
                    current_session_callsign=event.remote_callsign,
                    current_session_qso_guid=event.qso_guid,
                    current_view_mode="flamp-notice",
                    current_view_label=f"FLAMP {event.queue_id}",
                    last_publish_manifest_path=notice_result.manifest_path,
                    last_publish_ts=event.timestamp_utc or now_ts,
                    last_action=f"FLAMP block request missing block numbers for {event.remote_callsign}.",
                    last_request_ts=event.timestamp_utc or now_ts,
                    last_error="flamp_invalid_block_request",
                    unmanaged_live_files=list(notice_result.unmanaged_live_files),
                )
                published = published or bool(notice_result.changed)
                processed += 1
            continue
        if event.kind == "flamp_block_request" and flamp_enabled and flamp_relay_dir:
            store = FlampRelayStore(flamp_relay_dir)
            try:
                publish_result, overlay_name = publish_flamp_block_overlay_view(
                    store,
                    event.queue_id,
                    event.block_numbers,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    refresh_retry_command=_refresh_retry_command_for_event(event),
                )
                runtime_state = _update_state(
                    runtime_state,
                    previous_location_id=runtime_state.current_location_id,
                    previous_view_mode=runtime_state.current_view_mode or DEFAULT_VIEW_MODE,
                    previous_view_label=runtime_state.current_view_label or DEFAULT_LOCATION_NAME,
                    current_session_callsign=event.remote_callsign,
                    current_session_qso_guid=event.qso_guid,
                    current_view_mode="flamp-block-overlay",
                    current_view_label=f"FLAMP {event.queue_id}",
                    current_overlay_file=overlay_name,
                    last_publish_manifest_path=publish_result.manifest_path,
                    last_publish_ts=event.timestamp_utc or now_ts,
                    last_action=f"Managed BBS Library published FLAMP overlay {overlay_name} for {event.remote_callsign}.",
                    last_request_ts=event.timestamp_utc or now_ts,
                    last_error="",
                    unmanaged_live_files=list(publish_result.unmanaged_live_files),
                )
                published = published or bool(publish_result.changed)
                processed += 1
            except Exception as exc:
                runtime_state = _update_state(runtime_state, last_action=f"FLAMP block request failed: {exc}", last_error=str(exc))
            continue

    if max_scanned_datastream_id > int(runtime_state.last_datastream_id or 0):
        runtime_state = _update_state(runtime_state, last_datastream_id=max_scanned_datastream_id)

    for event in log_events_for_run:
            scanned += 1
            key = _state_key(event)
            if key in seen_keys and not (event.kind == "open_alias" and bool(event.code_text)):
                continue
            seen_keys.add(key)
            if event.kind == "root_request":
                result = None
                if flamp_enabled and flamp_relay_dir:
                    result = _publish_flamp_refresh_action(
                        sender=event.sender,
                        qso_guid=runtime_state.current_session_qso_guid,
                        locations=locations,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        default_location_id=default_location_id,
                        runtime_state=runtime_state,
                        now_ts=event.timestamp_utc or now_ts,
                        flamp_relay_dir=flamp_relay_dir,
                        flamp_listing_max_age_days=flamp_listing_max_age_days,
                        reason="log_root_request",
                    )
                if result is None:
                    result = _publish_refresh_action(
                        sender=event.sender,
                        qso_guid=runtime_state.current_session_qso_guid,
                        locations=locations,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        default_location_id=default_location_id,
                        global_allowed_callsigns=global_allowed,
                        limit_access_enabled=limit_access_enabled,
                        global_code_policy=global_code_policy,
                        runtime_state=runtime_state,
                        now_ts=event.timestamp_utc or now_ts,
                        flamp_enabled=flamp_enabled,
                        reason="log_root_request",
                        manifest_db_path=manifest_db_path,
                    )
                runtime_state = result.runtime_state
                published = published or bool(result.publish_result and result.publish_result.changed)
                processed += 1
            elif event.kind == "unlock":
                result = _apply_open_request(
                    sender=event.sender,
                    qso_guid=runtime_state.current_session_qso_guid,
                    requested_location=None,
                    alias_text="",
                    code_text=event.code_text,
                    locations=locations,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    default_location_id=default_location_id,
                    global_allowed_callsigns=global_allowed,
                    limit_access_enabled=limit_access_enabled,
                    runtime_state=runtime_state,
                    now_ts=event.timestamp_utc or now_ts,
                    failed_attempt_limit=failed_attempt_limit,
                    failed_attempt_window_seconds=failed_attempt_window_seconds,
                    cooldown_seconds=cooldown_seconds,
                    global_code_policy=global_code_policy,
                    action_reason="legacy_code_open",
                    manifest_db_path=manifest_db_path,
                )
                runtime_state = result.runtime_state
                published = published or bool(result.publish_result and result.publish_result.changed)
                processed += 1
            elif event.kind == "open_alias":
                requested = _location_by_alias(locations, event.alias)
                result = _apply_open_request(
                    sender=event.sender,
                    qso_guid=runtime_state.current_session_qso_guid,
                    requested_location=requested,
                    alias_text=event.alias,
                    code_text=event.code_text,
                    locations=locations,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    default_location_id=default_location_id,
                    global_allowed_callsigns=global_allowed,
                    limit_access_enabled=limit_access_enabled,
                    runtime_state=runtime_state,
                    now_ts=event.timestamp_utc or now_ts,
                    failed_attempt_limit=failed_attempt_limit,
                    failed_attempt_window_seconds=failed_attempt_window_seconds,
                    cooldown_seconds=cooldown_seconds,
                    global_code_policy=global_code_policy,
                    action_reason="log_open_alias",
                    manifest_db_path=manifest_db_path,
                )
                runtime_state = result.runtime_state
                published = published or bool(result.publish_result and result.publish_result.changed)
                processed += 1
            elif event.kind == "root_return":
                result = _publish_root_action(
                    sender=event.sender,
                    qso_guid=runtime_state.current_session_qso_guid,
                    locations=locations,
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    default_location_id=default_location_id,
                    global_allowed_callsigns=global_allowed,
                    limit_access_enabled=limit_access_enabled,
                    global_code_policy=global_code_policy,
                    runtime_state=runtime_state,
                    now_ts=event.timestamp_utc or now_ts,
                    flamp_enabled=flamp_enabled,
                    reason="log_root_return",
                    manifest_db_path=manifest_db_path,
                )
                runtime_state = result.runtime_state
                published = published or bool(result.publish_result and result.publish_result.changed)
                processed += 1
            elif event.kind == "flamp_help" and flamp_enabled and flamp_relay_dir:
                base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(
                    locations, default_location_id
                )
                if base_location is not None:
                    publish_result = publish_flamp_command_help_view(
                        base_source_dir=base_location.source_dir,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        refresh_retry_command=_refresh_retry_command_for_event(event),
                    )
                    runtime_state = _update_state(
                        runtime_state,
                        current_session_callsign=event.sender,
                        current_session_qso_guid=runtime_state.current_session_qso_guid,
                        current_view_mode="flamp-help",
                        current_view_label="FLAMP commands",
                        last_publish_manifest_path=publish_result.manifest_path,
                        last_publish_ts=event.timestamp_utc or now_ts,
                        last_action=f"Managed BBS Library published FLAMP command help for {event.sender}.",
                        last_request_ts=event.timestamp_utc or now_ts,
                        last_error="",
                        unmanaged_live_files=list(publish_result.unmanaged_live_files),
                    )
                    published = published or bool(publish_result.changed)
                    processed += 1
            elif event.kind == "flamp_list_q" and flamp_enabled and flamp_relay_dir:
                store = FlampRelayStore(flamp_relay_dir)
                base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(
                    locations, default_location_id
                )
                if base_location is not None:
                    publish_result = publish_flamp_queue_list_view(
                        store,
                        base_source_dir=base_location.source_dir,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        max_age_days=flamp_listing_max_age_days,
                        refresh_retry_command=_refresh_retry_command_for_event(event),
                    )
                    runtime_state = _update_state(
                        runtime_state,
                        current_session_callsign=event.sender,
                        current_session_qso_guid=runtime_state.current_session_qso_guid,
                        current_view_mode="flamp-queue",
                        current_view_label="FLAMP queue",
                        last_publish_manifest_path=publish_result.manifest_path,
                        last_publish_ts=event.timestamp_utc or now_ts,
                        last_action=f"Managed BBS Library published FLAMP queue list for {event.sender}.",
                        last_request_ts=event.timestamp_utc or now_ts,
                        last_error="",
                        unmanaged_live_files=list(publish_result.unmanaged_live_files),
                    )
                    published = published or bool(publish_result.changed)
                    processed += 1
            elif event.kind == "flamp_list_blocks" and flamp_enabled and flamp_relay_dir:
                store = FlampRelayStore(flamp_relay_dir)
                base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(
                    locations, default_location_id
                )
                try:
                    publish_result = publish_flamp_block_list_view(
                        store,
                        event.queue_id,
                        base_source_dir=base_location.source_dir if base_location is not None else "",
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        refresh_retry_command=_refresh_retry_command_for_event(event),
                    )
                    runtime_state = _update_state(
                        runtime_state,
                        current_session_callsign=event.sender,
                        current_session_qso_guid=runtime_state.current_session_qso_guid,
                        current_view_mode="flamp-blocks",
                        current_view_label=f"FLAMP {event.queue_id} blocks",
                        last_publish_manifest_path=publish_result.manifest_path,
                        last_publish_ts=event.timestamp_utc or now_ts,
                        last_action=f"Managed BBS Library published FLAMP block list {event.queue_id} for {event.sender}.",
                        last_request_ts=event.timestamp_utc or now_ts,
                        last_error="",
                        unmanaged_live_files=list(publish_result.unmanaged_live_files),
                    )
                    published = published or bool(publish_result.changed)
                    processed += 1
                except Exception as exc:
                    notice_result = publish_flamp_notice_view(
                        message=f"FLAMP queue {event.queue_id} not available; type LIST Q for current queues",
                        base_source_dir=base_location.source_dir if base_location is not None else "",
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        refresh_retry_command=_refresh_retry_command_for_event(event),
                    )
                    runtime_state = _update_state(
                        runtime_state,
                        current_session_callsign=event.sender,
                        current_session_qso_guid=runtime_state.current_session_qso_guid,
                        current_view_mode="flamp-notice",
                        current_view_label=f"FLAMP {event.queue_id}",
                        last_publish_manifest_path=notice_result.manifest_path,
                        last_publish_ts=event.timestamp_utc or now_ts,
                        last_action=f"FLAMP queue {event.queue_id} not available for {event.sender}: {exc}",
                        last_request_ts=event.timestamp_utc or now_ts,
                        last_error=str(exc),
                        unmanaged_live_files=list(notice_result.unmanaged_live_files),
                    )
                    published = published or bool(notice_result.changed)
                    processed += 1
            elif event.kind == "flamp_invalid_block_request" and flamp_enabled and flamp_relay_dir:
                base_location = _location_by_id(locations, runtime_state.current_location_id) or _location_by_id(
                    locations, default_location_id
                )
                notice_result = publish_flamp_notice_view(
                    message=_invalid_flamp_block_request_message(event.queue_id),
                    base_source_dir=base_location.source_dir if base_location is not None else "",
                    live_bbs_dir=live_bbs_dir,
                    managed_root=managed_root,
                    refresh_retry_command=_refresh_retry_command_for_event(event),
                )
                runtime_state = _update_state(
                    runtime_state,
                    current_session_callsign=event.sender,
                    current_session_qso_guid=runtime_state.current_session_qso_guid,
                    current_view_mode="flamp-notice",
                    current_view_label=f"FLAMP {event.queue_id}",
                    last_publish_manifest_path=notice_result.manifest_path,
                    last_publish_ts=event.timestamp_utc or now_ts,
                    last_action=f"FLAMP block request missing block numbers for {event.sender}.",
                    last_request_ts=event.timestamp_utc or now_ts,
                    last_error="flamp_invalid_block_request",
                    unmanaged_live_files=list(notice_result.unmanaged_live_files),
                )
                published = published or bool(notice_result.changed)
                processed += 1
            elif event.kind == "flamp_block_request" and flamp_enabled and flamp_relay_dir:
                store = FlampRelayStore(flamp_relay_dir)
                try:
                    publish_result, overlay_name = publish_flamp_block_overlay_view(
                        store,
                        event.queue_id,
                        event.block_numbers,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        refresh_retry_command=_refresh_retry_command_for_event(event),
                    )
                    runtime_state = _update_state(
                        runtime_state,
                        previous_location_id=runtime_state.current_location_id,
                        previous_view_mode=runtime_state.current_view_mode or DEFAULT_VIEW_MODE,
                        previous_view_label=runtime_state.current_view_label or DEFAULT_LOCATION_NAME,
                        current_session_callsign=event.sender,
                        current_session_qso_guid=runtime_state.current_session_qso_guid,
                        current_view_mode="flamp-block-overlay",
                        current_view_label=f"FLAMP {event.queue_id}",
                        current_overlay_file=overlay_name,
                        last_publish_manifest_path=publish_result.manifest_path,
                        last_publish_ts=event.timestamp_utc or now_ts,
                        last_action=f"Managed BBS Library published FLAMP overlay {overlay_name} for {event.sender}.",
                        last_request_ts=event.timestamp_utc or now_ts,
                        last_error="",
                        unmanaged_live_files=list(publish_result.unmanaged_live_files),
                    )
                    published = published or bool(publish_result.changed)
                    processed += 1
                except Exception as exc:
                    runtime_state = _update_state(runtime_state, last_action=f"FLAMP block request failed: {exc}", last_error=str(exc))
            elif event.kind == "disconnect" and runtime_state.current_session_callsign:
                if return_mode != "Manual operator reset only":
                    result = reset_to_default_location(
                        locations=locations,
                        live_bbs_dir=live_bbs_dir,
                        managed_root=managed_root,
                        default_location_id=default_location_id,
                        runtime_state=runtime_state,
                        reason="disconnect",
                        now_ts=event.timestamp_utc or now_ts,
                        manifest_db_path=manifest_db_path,
                    )
                    runtime_state = result.runtime_state
                    published = published or bool(result.publish_result and result.publish_result.changed)
                processed += 1
    runtime_state = _update_state(runtime_state, processed_event_keys=list(tuple(list(seen_keys)[-MAX_PROCESSED_EVENT_KEYS:])))

    # Do not reset the BBS on idle. VarAC file transfers and repeated file
    # retrievals can take a long time; the active listing stays live until a
    # new command changes it or the session disconnects.
    runtime_state = _update_state(runtime_state, log_cursors=log_cursor_map)

    runtime_state, reconciled = _reconcile_current_location(
        settings,
        locations=locations,
        live_bbs_dir=live_bbs_dir,
        managed_root=managed_root,
        runtime_state=runtime_state,
        default_location_id=default_location_id,
        global_allowed_callsigns=global_allowed,
        limit_access_enabled=limit_access_enabled,
        global_code_policy=global_code_policy,
        flamp_enabled=flamp_enabled,
        manifest_db_path=manifest_db_path,
    )
    published = published or reconciled

    summary = _summary_text(runtime_state)
    _persist_runtime_state(settings, runtime_state, summary)
    return VaracBbsVaultRunResult(
        enabled=True,
        scanned_events=scanned,
        processed_events=processed,
        published=published,
        active_location_id=runtime_state.current_location_id,
        current_session_callsign=runtime_state.current_session_callsign,
        summary=summary,
        state_changed=vault_runtime_state_to_data(runtime_state) != initial_state_data,
        publish_changed=published,
        unmanaged_live_files_changed=tuple(runtime_state.unmanaged_live_files) != initial_unmanaged_live_files,
        active_session=bool(runtime_state.current_session_callsign),
    )
