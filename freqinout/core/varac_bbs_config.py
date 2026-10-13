from __future__ import annotations

import configparser
import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import List, Mapping, Optional


def normalize_callsign(value: object) -> str:
    clean = str(value or "").strip().upper()
    clean = re.sub(r"^[^A-Z0-9/]+|[^A-Z0-9/]+$", "", clean)
    return clean


def base_callsign(value: object) -> str:
    return normalize_callsign(value).split("/", 1)[0]


def callsigns_match(candidate: object, expected: object) -> bool:
    left = normalize_callsign(candidate)
    right = normalize_callsign(expected)
    if not left or not right:
        return False
    return left == right or base_callsign(left) == base_callsign(right)


def parse_varac_bool(value: object, default: bool = False) -> bool:
    txt = str(value or "").strip().upper()
    if txt in {"ON", "TRUE", "YES", "1"}:
        return True
    if txt in {"OFF", "FALSE", "NO", "0"}:
        return False
    return bool(default)


def parse_callsign_list(value: object) -> List[str]:
    parts: List[str] = []
    if isinstance(value, (list, tuple, set)):
        tokens = [str(token or "") for token in value]
    else:
        raw = str(value or "")
        tokens = raw.replace("\n", ",").replace(";", ",").split(",")
    for token in tokens:
        clean = normalize_callsign(token)
        if clean:
            parts.append(clean)
    seen: set[str] = set()
    ordered: List[str] = []
    for callsign in parts:
        if callsign in seen:
            continue
        seen.add(callsign)
        ordered.append(callsign)
    return ordered


def format_callsign_list(value: object) -> str:
    return ",".join(parse_callsign_list(value))


def locate_varac_ini_path(*candidates: object) -> str:
    checked: List[Path] = []
    for candidate in candidates:
        txt = str(candidate or "").strip()
        if not txt:
            continue
        path = Path(txt).expanduser()
        checked.append(path)
        if path.is_file():
            return str(path)
        if path.is_dir():
            for name in ("VarAC.ini", "varac.ini"):
                maybe = path / name
                checked.append(maybe)
                if maybe.is_file():
                    return str(maybe)
    return ""


def _resolve_section_name(parser: configparser.ConfigParser, wanted: str) -> str | None:
    target = str(wanted or "").strip().lower()
    if not target:
        return None
    for section_name in parser.sections():
        if section_name.lower() == target:
            return section_name
    return None


def _get_section_value(section: configparser.SectionProxy, key: str, default: object = "") -> object:
    target = str(key or "").strip().lower()
    if not target:
        return default
    for existing_key, value in section.items():
        if str(existing_key or "").strip().lower() == target:
            return value
    return default


def _infer_wine_prefix_from_path(value: object) -> Path:
    txt = str(value or "").strip()
    if txt:
        try:
            path = Path(txt).expanduser()
            parts = path.parts
            for index, part in enumerate(parts):
                if re.fullmatch(r"drive_[A-Za-z]", part or ""):
                    prefix_parts = parts[:index]
                    if prefix_parts:
                        return Path(*prefix_parts)
        except Exception:
            pass
    return Path(os.environ.get("WINEPREFIX", "~/.wine")).expanduser()


def varac_path_to_host_path(value: object, *, ini_path: object = "") -> str:
    """Translate a VarAC/Wine Windows path to the current host path when needed."""
    txt = str(value or "").strip()
    if not txt:
        return ""
    win_match = re.match(r"^([A-Za-z]):[\\/](.*)$", txt)
    if not win_match or os.name == "nt":
        return str(Path(txt).expanduser()) if txt.startswith("~") else txt

    drive = win_match.group(1).lower()
    rest_parts = PureWindowsPath(txt).parts[1:]
    wine_prefix = _infer_wine_prefix_from_path(ini_path)
    return str(wine_prefix / f"drive_{drive}" / Path(*rest_parts))


def load_varac_bbs_config(ini_path: object) -> Mapping[str, object]:
    resolved = locate_varac_ini_path(ini_path)
    if not resolved:
        raise FileNotFoundError("VarAC.ini not found")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    from freqinout.core.config_varac_managed import read_varac_ini_source
    source = read_varac_ini_source(Path(resolved))
    parser.read_string(source.raw_bytes.decode(source.encoding))
    section_name = _resolve_section_name(parser, "BBS")
    if section_name is None:
        raise KeyError("VarAC.ini does not contain a [BBS] section")

    section = parser[section_name]
    return {
        "ini_path": resolved,
        "enable_bbs": parse_varac_bool(_get_section_value(section, "EnableBBS", ""), False),
        "bbs_directory": str(_get_section_value(section, "BBSDirectory", "") or "").strip(),
        "limit_access": parse_varac_bool(_get_section_value(section, "LimitAccessToCallsigns", ""), False),
        "allowed_callsigns": parse_callsign_list(_get_section_value(section, "LimitAccessToCallsignsList", "")),
        "announce": parse_varac_bool(_get_section_value(section, "Announce", ""), False),
    }


def bbs_summary_text(values: Mapping[str, object]) -> str:
    enabled = parse_varac_bool(values.get("enable_bbs"), False)
    limited = parse_varac_bool(values.get("limit_access"), False)
    announce = parse_varac_bool(values.get("announce"), False)
    allowed = parse_callsign_list(values.get("allowed_callsigns", ""))
    return (
        f"BBS {'on' if enabled else 'off'}, "
        f"Access {'limited' if limited else 'open'}, "
        f"{len(allowed)} allowed, "
        f"Announce {'on' if announce else 'off'}"
    )


@dataclass(frozen=True)
class VaracIniSyncState:
    path: str
    size: int
    mtime_ns: int
    digest: str


def get_varac_ini_sync_state(ini_path: object) -> VaracIniSyncState:
    resolved = locate_varac_ini_path(ini_path)
    if not resolved:
        raise FileNotFoundError("VarAC.ini not found")
    path = Path(resolved)
    stat = path.stat()
    content = path.read_bytes()
    return VaracIniSyncState(
        path=str(path),
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        digest=sha256(content).hexdigest(),
    )


def varac_ini_sync_state_to_json(state: Optional[VaracIniSyncState]) -> str:
    if not state:
        return ""
    try:
        return json.dumps(
            {
                "path": state.path,
                "size": state.size,
                "mtime_ns": state.mtime_ns,
                "digest": state.digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:
        return ""


def varac_ini_sync_state_from_json(value: object) -> Optional[VaracIniSyncState]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    path = str(data.get("path", "") or "").strip()
    digest = str(data.get("digest", "") or "").strip()
    if not path or not digest:
        return None
    try:
        size = int(data.get("size", 0) or 0)
    except Exception:
        size = 0
    try:
        mtime_ns = int(data.get("mtime_ns", 0) or 0)
    except Exception:
        mtime_ns = 0
    return VaracIniSyncState(path=path, size=size, mtime_ns=mtime_ns, digest=digest)


def varac_ini_sync_state_matches(known: object, current: object) -> bool:
    known_state = known if isinstance(known, VaracIniSyncState) else varac_ini_sync_state_from_json(known)
    current_state = current if isinstance(current, VaracIniSyncState) else varac_ini_sync_state_from_json(current)
    if known_state is None or current_state is None:
        return False
    return (
        known_state.path == current_state.path
        and known_state.size == current_state.size
        and known_state.mtime_ns == current_state.mtime_ns
        and known_state.digest == current_state.digest
    )


def render_varac_bbs_config_bytes(raw: bytes, *, enable_bbs: bool, bbs_directory: str,
                                  limit_access: bool, allowed_callsigns: object, announce: bool) -> bytes:
    """Preserve unrelated keys, comments, case, encoding, and line endings."""
    from freqinout.core.config_varac_managed import (
        parse_varac_ini_bytes, render_varac_ini, VarACWriterCapability,
    )
    directory = str(bbs_directory or "").strip()
    if any(ch in directory for ch in "\r\n\x00"):
        raise ValueError("BBS directory must be a single-line path")
    calls = format_callsign_list(allowed_callsigns)
    if not re.fullmatch(r"[A-Z0-9/]*(?:,[A-Z0-9/]+)*", calls):
        raise ValueError("Allowed callsigns must be concrete callsigns, separated by commas")
    if limit_access and not calls:
        raise ValueError("Limited VarAC BBS access requires at least one allowed callsign")
    changes = {"BBS": {
        "EnableBBS": "ON" if enable_bbs else "OFF", "BBSDirectory": directory,
        "LimitAccessToCallsigns": "ON" if limit_access else "OFF",
        "LimitAccessToCallsignsList": calls, "Announce": "ON" if announce else "OFF",
    }}
    source = parse_varac_ini_bytes(Path("VarAC.ini"), raw)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(raw.decode(source.encoding))  # reject ambiguous duplicate keys/sections
    # The established five-key BBS adapter, not the versioned cluster writer.
    capability = VarACWriterCapability("bbs-adapter-v1", "any", "bbs", "bbs-v1", creatable_sections=("BBS",))
    sections = [name for name in source.values if name.casefold() == "bbs"]
    if len(sections) > 1 or len(re.findall(rb"(?im)^\s*\[bbs\]\s*(?:[;#].*)?$", raw)) > 1:
        raise ValueError("Duplicate BBS sections cannot be updated safely")
    return render_varac_ini(source, changes, capability=capability)


def write_varac_bbs_config(
    ini_path: object,
    *,
    enable_bbs: bool,
    bbs_directory: str,
    limit_access: bool,
    allowed_callsigns: object,
    announce: bool,
    expected_sync_state: object = None,
) -> VaracIniSyncState:
    resolved = locate_varac_ini_path(ini_path)
    if not resolved:
        raise FileNotFoundError("VarAC.ini not found")

    path = Path(resolved)
    current_state = get_varac_ini_sync_state(path)
    known_state = expected_sync_state if isinstance(expected_sync_state, VaracIniSyncState) else varac_ini_sync_state_from_json(expected_sync_state)
    if known_state is not None and not varac_ini_sync_state_matches(known_state, current_state):
        raise RuntimeError("VarAC.ini changed since it was loaded")

    from freqinout.core.config_backup import create_config_backup, restore_config_backup
    from freqinout.core.config_varac_managed import _stage_bytes
    raw = path.read_bytes()
    payload = render_varac_bbs_config_bytes(raw, enable_bbs=enable_bbs, bbs_directory=bbs_directory,
                                          limit_access=limit_access, allowed_callsigns=allowed_callsigns, announce=announce)
    if payload == raw:
        return current_state
    backup = create_config_backup((path,), reason="varac-bbs")
    if any(item.status != "backed_up" for item in backup.items):
        raise OSError("VarAC BBS backup failed; INI was not changed")
    staged = _stage_bytes(path, payload)
    os.chmod(staged, path.stat().st_mode & 0o777)
    promoted = False
    try:
        if not varac_ini_sync_state_matches(current_state, get_varac_ini_sync_state(path)):
            raise RuntimeError("VarAC.ini changed before update")
        os.replace(staged, path)
        promoted = True
        if path.read_bytes() != payload:
            raise OSError("VarAC BBS readback did not match")
    except Exception:
        if promoted and not restore_config_backup(backup).ok:
            raise RuntimeError(f"VarAC BBS rollback failed; recover from {backup.backup_dir}")
        raise
    finally:
        staged.unlink(missing_ok=True)
    return get_varac_ini_sync_state(path)
