from __future__ import annotations

import configparser
import io
import os
import platform as platform_module
import shutil
import sqlite3
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from freqinout.core.js8_storage import (
    js8_application_name,
    qt_data_root_candidates,
    rig_name_from_settings_path,
)


LOCALHOST = "127.0.0.1"

APP_DISPLAY_NAMES: Mapping[str, str] = {
    "flrig": "FLRig",
    "fldigi": "FLDigi",
    "flmsg": "FLMsg",
    "flamp": "FLAmp",
    "js8call": "JS8Call",
    "js8spotter": "JS8Spotter",
    "external_js8spotter": "External JS8Spotter",
    "commstat": "CommStat",
    "varac": "VarAC",
    "sdrpp": "SDR++",
}

DEFAULT_RADIO_INSTANCE_NAMES: Tuple[str, ...] = ("fio-a", "fio-b", "fio-c", "fio-d")

DEFAULT_PORT_PLAN: Mapping[str, Tuple[int, ...]] = {
    "rigctld": (4532, 4533, 4534, 4535),
    "flrig": (12345, 12346, 12347, 12348),
    "fldigi": (7362, 7363, 7364, 7365),
    "js8call": (2442, 2443, 2444, 2445),
    "js8call_udp": (2242, 2243, 2244, 2245),
    "wsjtx_udp": (2237, 2238, 2239, 2240),
}

JS8CALL_APP_NAMES: Tuple[str, ...] = (
    "JS8Call",
    "js8call",
    "JS8Call-improved",
    "js8call-improved",
    "Subspace Edition",
    "Subspace-Edition",
    "subspace",
    "JS8Call_Improved_Code/JS8Call-improved/build-codex-ptt-gate/JS8Call",
    "Subspace-Edition/build-trimode-baseline/JS8Call",
)

JS8CALL_COMMAND_NAMES: Tuple[str, ...] = (
    "JS8Call",
    "js8call",
    "JS8Call-improved",
    "js8call-improved",
    "js8call-subspace",
    "subspace",
)

# Settings files are normally only a few KiB.  Keep guided discovery from
# retaining a worker on an accidentally selected log, sparse file, or corrupt
# settings file after its UI budget has elapsed.
MAX_JS8CALL_SETTINGS_BYTES = 1024 * 1024
_JS8CALL_SETTINGS_READ_CHUNK_BYTES = 64 * 1024


class _JS8CallSettingsReadCancelled(Exception):
    """Internal sentinel used to abandon a bounded settings read."""


@dataclass(frozen=True)
class AppCandidate:
    app_id: str
    display_name: str
    path: str
    source: str
    confidence: str
    exists: bool
    executable: bool
    target_type: str
    notes: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PortAssignment:
    service: str
    host: str
    preferred_port: int
    assigned_port: int
    conflict: bool
    protocol: str = "tcp"
    conflict_checked: bool = True
    note: str = ""


@dataclass(frozen=True)
class RadioInstanceProposal:
    name: str
    instance_name: str
    index: int
    enabled_apps: Tuple[str, ...]
    ports: Tuple[PortAssignment, ...]
    varac_enabled: bool = False
    notes: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class JS8CallConfigProfile:
    name: str
    settings: Mapping[str, str]


@dataclass(frozen=True)
class JS8CallFileProfile:
    name: str
    ini_path: str
    save_dir: str
    tcp_server_port: str
    directed_path: str
    all_path: str
    confidence: str
    reason: str
    inbox_path: str = ""
    application_data_root: str = ""
    rig_name: str = ""
    storage_mode: str = "unverified"

    @property
    def family_label(self) -> str:
        return js8call_ini_family_label(self.ini_path)

    @property
    def operator_label(self) -> str:
        return js8call_file_profile_operator_label(self)


@dataclass(frozen=True)
class VarACLocalAsset:
    asset_id: str
    label: str
    path: str
    kind: str
    exists: bool
    confidence: str
    detail: str = ""


@dataclass(frozen=True)
class AutoconfigProposal:
    platform: str
    candidates: Tuple[AppCandidate, ...]
    radios: Tuple[RadioInstanceProposal, ...]
    warnings: Tuple[str, ...]
    missing_apps: Tuple[str, ...]
    js8_profiles: Tuple[JS8CallConfigProfile, ...] = field(default_factory=tuple)
    js8_file_profiles: Tuple[JS8CallFileProfile, ...] = field(default_factory=tuple)


def normalize_platform(value: Optional[str] = None) -> str:
    raw = (value or platform_module.system() or "").strip().lower()
    if raw in {"darwin", "mac", "macos", "osx"}:
        return "Darwin"
    if raw in {"windows", "win32", "win"}:
        return "Windows"
    if raw in {"linux", "gnu/linux"}:
        return "Linux"
    return value or platform_module.system() or "Unknown"


def default_app_search_paths(
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
) -> Mapping[str, Tuple[Path, ...]]:
    system = normalize_platform(platform)
    user_home = Path(home) if home is not None else Path.home()

    if system == "Darwin":
        base_dirs = (
            Path("/Applications"),
            Path("/Applications/RadioApps"),
            user_home / "Applications",
            user_home / "RadioTools" / "bin",
            user_home / "RadioTools" / "Programs",
        )
        return {
            "flrig": _mac_app_candidates(base_dirs, ("FLRig", "flrig")),
            "fldigi": _mac_app_candidates(base_dirs, ("FLDigi", "fldigi")),
            "flmsg": _mac_app_candidates(base_dirs, ("FLMsg", "flmsg")),
            "flamp": _mac_app_candidates(base_dirs, ("FLAmp", "flamp")),
            "js8call": _mac_app_candidates(base_dirs, JS8CALL_APP_NAMES)
            + (
                user_home / "RadioTools" / "Programs" / "js8_22" / "js8call",
            ),
            "js8spotter": _mac_app_candidates(base_dirs, ("JS8Spotter", "js8spotter")),
            "commstat": _mac_app_candidates(base_dirs, ("CommStat", "commstat")),
            "sdrpp": _mac_app_candidates(base_dirs, ("SDR++", "sdrpp")),
            "varac": _mac_app_candidates(base_dirs, ("VarAC", "varac"))
            + (
                user_home / "RadioTools" / "Programs" / "VarAC_files",
                user_home / ".wine" / "drive_c" / "VarAC",
                user_home / ".wine" / "drive_c" / "Program Files" / "VarAC",
                user_home / ".wine" / "drive_c" / "Program Files (x86)" / "VarAC",
            ),
        }

    if system == "Windows":
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        local_app_data = Path(os.environ.get("LOCALAPPDATA", str(user_home / "AppData" / "Local")))
        return {
            "flrig": (
                program_files / "flrig" / "flrig.exe",
                program_files_x86 / "flrig" / "flrig.exe",
                local_app_data / "flrig" / "flrig.exe",
            ),
            "fldigi": (
                program_files / "fldigi" / "fldigi.exe",
                program_files_x86 / "fldigi" / "fldigi.exe",
                local_app_data / "fldigi" / "fldigi.exe",
            ),
            "flmsg": (
                program_files / "flmsg" / "flmsg.exe",
                program_files_x86 / "flmsg" / "flmsg.exe",
                local_app_data / "flmsg" / "flmsg.exe",
            ),
            "flamp": (
                program_files / "flamp" / "flamp.exe",
                program_files_x86 / "flamp" / "flamp.exe",
                local_app_data / "flamp" / "flamp.exe",
            ),
            "js8call": (
                program_files / "JS8Call" / "js8call.exe",
                program_files_x86 / "JS8Call" / "js8call.exe",
                local_app_data / "JS8Call" / "js8call.exe",
                program_files / "JS8Call-improved" / "js8call.exe",
                program_files_x86 / "JS8Call-improved" / "js8call.exe",
                local_app_data / "JS8Call-improved" / "js8call.exe",
                program_files / "JS8Call Subspace" / "js8call.exe",
                program_files_x86 / "JS8Call Subspace" / "js8call.exe",
                local_app_data / "JS8Call Subspace" / "js8call.exe",
            ),
            "js8spotter": (
                program_files / "JS8Spotter" / "JS8Spotter.exe",
                program_files_x86 / "JS8Spotter" / "JS8Spotter.exe",
                local_app_data / "Programs" / "JS8Spotter" / "JS8Spotter.exe",
            ),
            "commstat": (
                program_files / "CommStat" / "CommStat.exe",
                program_files_x86 / "CommStat" / "CommStat.exe",
                local_app_data / "Programs" / "CommStat" / "CommStat.exe",
            ),
            "sdrpp": (
                program_files / "SDR++" / "sdrpp.exe",
                program_files_x86 / "SDR++" / "sdrpp.exe",
                local_app_data / "SDR++" / "sdrpp.exe",
            ),
            "varac": (
                program_files / "VarAC",
                program_files_x86 / "VarAC",
                local_app_data / "VarAC",
                user_home / "RadioTools" / "Programs" / "VarAC_files",
                user_home / "AppData" / "Local" / "VarAC",
            ),
        }

    return {
        "flrig": (Path("/usr/bin/flrig"), Path("/usr/local/bin/flrig"), Path("/opt/flrig/flrig")),
        "fldigi": (Path("/usr/bin/fldigi"), Path("/usr/local/bin/fldigi"), Path("/opt/fldigi/fldigi")),
        "flmsg": (Path("/usr/bin/flmsg"), Path("/usr/local/bin/flmsg"), Path("/opt/flmsg/flmsg")),
        "flamp": (Path("/usr/bin/flamp"), Path("/usr/local/bin/flamp"), Path("/opt/flamp/flamp")),
        "js8call": (
            Path("/usr/bin/js8call"),
            Path("/usr/local/bin/js8call"),
            Path("/usr/bin/JS8Call"),
            Path("/usr/local/bin/JS8Call"),
            user_home / ".local" / "bin" / "JS8Call",
            Path("/opt/js8call/js8call"),
            Path("/usr/bin/js8call-improved"),
            Path("/usr/local/bin/js8call-improved"),
            Path("/opt/js8call-improved/js8call"),
            Path("/opt/JS8Call-improved/bin/JS8Call"),
            Path("/usr/bin/js8call-subspace"),
            Path("/usr/local/bin/js8call-subspace"),
            Path("/opt/js8call-subspace/js8call"),
            Path("/opt/JS8Call Subspace/JS8Call"),
        ),
        "js8spotter": (
            Path("/usr/bin/js8spotter"),
            Path("/usr/local/bin/js8spotter"),
            user_home / ".local" / "bin" / "js8spotter",
            user_home / "bin" / "js8spotter",
        ),
        "commstat": (
            Path("/usr/bin/commstat"),
            Path("/usr/local/bin/commstat"),
            user_home / ".local" / "bin" / "commstat",
            user_home / "bin" / "commstat",
        ),
        "sdrpp": (
            Path("/usr/bin/sdrpp"),
            Path("/usr/local/bin/sdrpp"),
            Path("/opt/sdrpp/sdrpp"),
            user_home / ".local" / "bin" / "sdrpp",
        ),
        "varac": (
            user_home / "RadioTools" / "Programs" / "VarAC_files",
            user_home / ".wine" / "drive_c" / "VarAC",
            user_home / ".wine" / "drive_c" / "Program Files" / "VarAC",
            user_home / ".wine" / "drive_c" / "Program Files (x86)" / "VarAC",
            user_home / ".varac",
        ),
    }


def app_search_paths_with_radio_apps_base(
    base_folder: str | Path | None = None,
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
) -> Mapping[str, Tuple[Path, ...]]:
    """Return app search paths with an optional operator-selected app folder first."""

    defaults = {
        app_id: tuple(paths)
        for app_id, paths in default_app_search_paths(platform=platform, home=home).items()
    }
    raw = str(base_folder or "").strip()
    if not raw:
        return defaults
    root = Path(os.path.expandvars(os.path.expanduser(raw)))
    system = normalize_platform(platform)
    if system == "Darwin":
        additions = {
            "flrig": _mac_app_candidates((root,), ("FLRig", "flrig")),
            "fldigi": _mac_app_candidates((root,), ("FLDigi", "fldigi")),
            "flmsg": _mac_app_candidates((root,), ("FLMsg", "flmsg")),
            "flamp": _mac_app_candidates((root,), ("FLAmp", "flamp")),
            "js8call": _mac_app_candidates((root,), JS8CALL_APP_NAMES) + (root / "js8_22" / "js8call",),
            "js8spotter": _mac_app_candidates((root,), ("JS8Spotter", "js8spotter")),
            "commstat": _mac_app_candidates((root,), ("CommStat", "commstat")),
            "sdrpp": _mac_app_candidates((root,), ("SDR++", "sdrpp")),
            "varac": _mac_app_candidates((root,), ("VarAC", "varac")) + (root / "VarAC_files", root / "VarAC"),
        }
    elif system == "Windows":
        additions = {
            "flrig": (root / "flrig" / "flrig.exe", root / "FLRig" / "flrig.exe"),
            "fldigi": (root / "fldigi" / "fldigi.exe", root / "FLDigi" / "fldigi.exe"),
            "flmsg": (root / "flmsg" / "flmsg.exe", root / "FLMsg" / "flmsg.exe"),
            "flamp": (root / "flamp" / "flamp.exe", root / "FLAmp" / "flamp.exe"),
            "js8call": (
                root / "JS8Call" / "js8call.exe",
                root / "JS8Call-improved" / "js8call.exe",
                root / "JS8Call Subspace" / "js8call.exe",
            ),
            "js8spotter": (root / "JS8Spotter" / "JS8Spotter.exe",),
            "commstat": (root / "CommStat" / "CommStat.exe",),
            "sdrpp": (root / "SDR++" / "sdrpp.exe", root / "sdrpp.exe"),
            "varac": (root / "VarAC_files", root / "VarAC", root / "VarAC" / "VarAC.exe"),
        }
    else:
        additions = {
            "flrig": (root / "flrig", root / "FLRig" / "flrig"),
            "fldigi": (root / "fldigi", root / "FLDigi" / "fldigi"),
            "flmsg": (root / "flmsg", root / "FLMsg" / "flmsg"),
            "flamp": (root / "flamp", root / "FLAmp" / "flamp"),
            "js8call": (
                root / "js8call",
                root / "JS8Call",
                root / "JS8Call" / "js8call",
                root / "JS8Call" / "JS8Call",
                root / "JS8Call-improved" / "js8call",
                root / "JS8Call-improved" / "JS8Call",
                root / "JS8Call Subspace" / "js8call",
                root / "JS8Call Subspace" / "JS8Call",
                root / "js8call-subspace",
            ),
            "js8spotter": (root / "js8spotter", root / "JS8Spotter" / "js8spotter"),
            "commstat": (root / "commstat", root / "CommStat" / "commstat"),
            "sdrpp": (root / "sdrpp", root / "SDR++" / "sdrpp"),
            "varac": (root / "VarAC_files", root / "VarAC"),
        }
    merged: Dict[str, Tuple[Path, ...]] = {}
    for app_id, paths in defaults.items():
        merged[app_id] = tuple(_unique_paths(tuple(additions.get(app_id, ())) + tuple(paths)))
    return merged


def find_app_candidates(
    *,
    apps: Sequence[str] = ("flrig", "fldigi", "flmsg", "flamp", "js8call", "js8spotter", "commstat", "varac"),
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    extra_paths: Sequence[Path] = (),
    app_search_paths: Optional[Mapping[str, Sequence[Path]]] = None,
) -> Tuple[AppCandidate, ...]:
    system = normalize_platform(platform)
    search_paths = app_search_paths if app_search_paths is not None else default_app_search_paths(platform=system, home=home)
    candidates = []
    for app_id in apps:
        normalized_app = app_id.strip().lower()
        seen_paths = set()
        # A saved exact identity is stronger evidence than a well-known
        # fallback.  Keep both bounded and shallow: this helper never walks a
        # directory tree or scans existing application profiles.
        scoped_extra_paths = [
            path for path in tuple(extra_paths or ())[:128]
            if _path_name_matches_app(normalized_app, Path(path))
        ]
        saved_keys = {
            os.path.normcase(os.path.normpath(str(Path(os.path.expandvars(os.path.expanduser(str(path)))))))
            for path in scoped_extra_paths
        }
        raw_paths = scoped_extra_paths + list(tuple(search_paths.get(normalized_app, ()))[:128])
        for raw in _unique_paths(raw_paths):
            raw_key = os.path.normcase(os.path.normpath(str(raw)))
            source = "saved" if raw_key in saved_keys else "known_path"
            candidate = _candidate_from_path(normalized_app, raw, system=system, source=source)
            key = os.path.normcase(os.path.normpath(candidate.path))
            if key in seen_paths:
                continue
            seen_paths.add(key)
            if candidate.exists:
                candidates.append(candidate)

        for command in _command_names(normalized_app):
            resolved = shutil.which(command)
            if not resolved:
                continue
            candidate = _candidate_from_path(normalized_app, Path(resolved), system=system, source="path")
            key = os.path.normcase(os.path.normpath(candidate.path))
            if key in seen_paths:
                continue
            candidates.append(candidate)
            break

    return tuple(candidates)


def is_local_tcp_port_listening(host: str, port: int, timeout: float = 0.15) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def assign_default_ports(
    *,
    radio_count: int = 3,
    host: str = LOCALHOST,
    busy_checker: Callable[[str, int], bool] = is_local_tcp_port_listening,
) -> Tuple[Tuple[PortAssignment, ...], ...]:
    assignments = []
    used_ports = set()
    for index in range(max(0, int(radio_count))):
        radio_ports = []
        for service, defaults in DEFAULT_PORT_PLAN.items():
            protocol = _service_protocol(service)
            preferred = _default_port_for_index(defaults, index)
            assigned = preferred
            conflict_checked = protocol == "tcp"
            conflict = bool((conflict_checked and busy_checker(host, assigned)) or assigned in used_ports)
            if conflict:
                assigned = _first_available_port(preferred + 10, host, used_ports, busy_checker)
            used_ports.add(assigned)
            radio_ports.append(
                PortAssignment(
                    service=service,
                    host=host,
                    preferred_port=preferred,
                    assigned_port=assigned,
                    conflict=conflict,
                    protocol=protocol,
                    conflict_checked=conflict_checked,
                    note=_port_note(service, preferred, assigned, conflict, conflict_checked=conflict_checked),
                )
            )
        assignments.append(tuple(radio_ports))
    return tuple(assignments)


def build_radio_instance_proposals(
    *,
    radio_count: int = 3,
    enabled_apps: Sequence[str] = ("flrig", "fldigi", "js8call"),
    include_varac: bool = False,
    host: str = LOCALHOST,
    busy_checker: Callable[[str, int], bool] = is_local_tcp_port_listening,
) -> Tuple[RadioInstanceProposal, ...]:
    port_sets = assign_default_ports(radio_count=radio_count, host=host, busy_checker=busy_checker)
    radios = []
    clean_enabled_apps = tuple(app.strip().lower() for app in enabled_apps if str(app).strip())
    for index, ports in enumerate(port_sets):
        instance_name = _instance_name(index)
        radios.append(
            RadioInstanceProposal(
                name=f"Radio {chr(ord('A') + index)}",
                instance_name=instance_name,
                index=index,
                enabled_apps=clean_enabled_apps,
                ports=ports,
                varac_enabled=include_varac,
                notes=(f"CommsPlan-managed instance {instance_name}.",),
            )
        )
    return tuple(radios)


def read_js8call_multisettings(
    ini_path: Path,
    *,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Tuple[JS8CallConfigProfile, ...]:
    """Read one normal-sized JS8Call settings file without blocking cancellation.

    This is used by the guided-discovery worker, whose UI timeout cannot kill a
    Python parser already running.  Bound both the bytes read and the parser's
    input so a malformed or misidentified file cannot keep that worker busy.
    """

    path = Path(ini_path)
    contents = _read_bounded_js8call_settings(path, cancelled=cancelled)
    if contents is None:
        return ()

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    profiles = []
    try:
        parser.read_file(_cancel_aware_js8call_lines(contents, cancelled))
    except _JS8CallSettingsReadCancelled:
        return ()
    except configparser.Error:
        return _read_js8call_qsettings_text(contents, cancelled=cancelled)

    root_settings = _selected_js8_settings(parser.defaults())
    if parser.has_section("Configuration"):
        root_settings.update(_selected_js8_settings(dict(parser.items("Configuration"))))
    if root_settings:
        profiles.append(JS8CallConfigProfile(name="Default", settings=root_settings))

    for section in parser.sections():
        normalized = section.strip()
        if not normalized.lower().startswith("multisettings/"):
            continue
        name = normalized.split("/", 1)[1].strip() or normalized
        settings = _selected_js8_settings(dict(parser.items(section)))
        if settings:
            profiles.append(JS8CallConfigProfile(name=name, settings=settings))
    if _js8call_settings_cancelled(cancelled):
        return ()
    qsettings_profiles = _read_js8call_qsettings_text(contents, cancelled=cancelled)
    if not profiles:
        return qsettings_profiles
    merged = {profile.name.strip().casefold(): profile for profile in profiles}
    ordered = list(profiles)
    for qprofile in qsettings_profiles:
        key = qprofile.name.strip().casefold()
        existing = merged.get(key)
        if existing is None:
            merged[key] = qprofile
            ordered.append(qprofile)
            continue
        combined = dict(existing.settings)
        combined.update({k: v for k, v in qprofile.settings.items() if str(v).strip()})
        replacement = JS8CallConfigProfile(name=existing.name, settings=combined)
        merged[key] = replacement
        for index, current in enumerate(ordered):
            if current.name.strip().casefold() == key:
                ordered[index] = replacement
                break
    return tuple(ordered)


def _read_bounded_js8call_settings(
    path: Path,
    *,
    cancelled: Optional[Callable[[], bool]],
) -> Optional[str]:
    if _js8call_settings_cancelled(cancelled) or not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_JS8CALL_SETTINGS_BYTES:
            return None
        chunks = []
        total = 0
        with path.open("rb") as stream:
            while True:
                if _js8call_settings_cancelled(cancelled):
                    return None
                chunk = stream.read(
                    min(
                        _JS8CALL_SETTINGS_READ_CHUNK_BYTES,
                        MAX_JS8CALL_SETTINGS_BYTES + 1 - total,
                    )
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_JS8CALL_SETTINGS_BYTES:
                    return None
    except OSError:
        return None
    if _js8call_settings_cancelled(cancelled):
        return None
    return b"".join(chunks).decode("utf-8", errors="ignore")


def _js8call_settings_cancelled(cancelled: Optional[Callable[[], bool]]) -> bool:
    if cancelled is None:
        return False
    try:
        return bool(cancelled())
    except Exception:
        return True


def _cancel_aware_js8call_lines(
    contents: str,
    cancelled: Optional[Callable[[], bool]],
) -> Iterable[str]:
    for line in io.StringIO(contents):
        if _js8call_settings_cancelled(cancelled):
            raise _JS8CallSettingsReadCancelled()
        yield line


def _read_js8call_qsettings_text(
    contents: str,
    *,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Tuple[JS8CallConfigProfile, ...]:
    lines = contents.splitlines()

    grouped: Dict[str, Dict[str, str]] = {}
    current_section = ""
    for raw_line in lines:
        if _js8call_settings_cancelled(cancelled):
            return ()
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        raw_key, value = line.split("=", 1)
        raw_key = raw_key.strip()
        if not raw_key:
            continue
        group = current_section
        key = raw_key
        if "\\" in raw_key:
            group, key = _split_js8_qsettings_key(current_section, raw_key)
        if not group:
            continue
        grouped.setdefault(group, {})[key] = value.strip()

    profiles = []
    root_settings = _selected_js8_settings(grouped.get("Configuration", {}))
    if root_settings:
        profiles.append(JS8CallConfigProfile(name="Default", settings=root_settings))
    for group, settings_map in grouped.items():
        normalized = group.strip()
        if not normalized.lower().startswith("multisettings/"):
            continue
        name = normalized.split("/", 1)[1].strip() or normalized
        settings = _selected_js8_settings(settings_map)
        if settings:
            profiles.append(JS8CallConfigProfile(name=name, settings=settings))
    return tuple(profiles)


def _split_js8_qsettings_key(current_section: str, raw_key: str) -> tuple[str, str]:
    prefix, key = raw_key.rsplit("\\", 1)
    clean_prefix = prefix.strip()
    clean_key = key.strip()
    current = str(current_section or "").strip()
    current_lower = current.lower()
    prefix_lower = clean_prefix.lower()
    if current_lower.startswith("multisettings/") and prefix_lower == "configuration":
        return current, clean_key
    marker = "\\configuration"
    if prefix_lower.startswith("multisettings/") and prefix_lower.endswith(marker):
        return clean_prefix[: -len(marker)], clean_key
    return clean_prefix, clean_key


def default_js8call_ini_paths(
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
) -> Tuple[Path, ...]:
    system = normalize_platform(platform)
    user_home = Path(home) if home is not None else Path.home()
    if system == "Darwin":
        preferences_dir = user_home / "Library" / "Preferences"
        return _unique_paths(
            (
                preferences_dir / "JS8Call.ini",
                *_js8call_named_ini_files(preferences_dir),
                user_home / "Library" / "Application Support" / "JS8Call" / "JS8Call.ini",
            )
        )
    if system == "Windows":
        env_paths = []
        for env_key in ("LOCALAPPDATA", "APPDATA"):
            raw = str(os.environ.get(env_key, "") or "").strip()
            if raw:
                for directory in (Path(raw), Path(raw) / "JS8Call"):
                    env_paths.extend(_js8call_ini_name_candidates(directory))
                    env_paths.extend(_js8call_named_ini_files(directory))
        home_root = user_home / "AppData" / "Local"
        for directory in (home_root, home_root / "JS8Call"):
            env_paths.extend(_js8call_ini_name_candidates(directory))
            env_paths.extend(_js8call_named_ini_files(directory))
        return _unique_paths(env_paths)
    linux_directories = (
        user_home / ".config",
        user_home / ".config" / "JS8Call",
        user_home / ".local" / "share" / "JS8Call",
        user_home / ".var" / "app" / "org.js8call.JS8Call" / "config",
    )
    linux_paths = []
    for directory in linux_directories:
        linux_paths.extend(_js8call_ini_name_candidates(directory))
        linux_paths.extend(_js8call_named_ini_files(directory))
    return _unique_paths(linux_paths)


def _js8call_ini_name_candidates(directory: Path) -> Tuple[Path, ...]:
    return (
        directory / "JS8Call.ini",
        directory / "JS8Call-improved.ini",
        directory / "JS8Call Improved.ini",
        directory / "JS8Call Subspace.ini",
        directory / "Subspace.ini",
        directory / "Subspace-Edition.ini",
    )


def _js8call_named_ini_files(directory: Path) -> Tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    try:
        children = tuple(directory.iterdir())
    except OSError:
        return ()
    matches = []
    for child in children:
        name = child.name
        if not child.is_file():
            continue
        if not name.lower().endswith(".ini"):
            continue
        lowered = name.lower()
        if lowered == "js8call.ini":
            continue
        if (
            lowered.startswith("js8call - ")
            or lowered.startswith("js8call-")
            or lowered.startswith("js8call-improved")
            or lowered.startswith("js8call improved")
            or lowered.startswith("js8call subspace")
            or lowered.startswith("subspace")
        ):
            matches.append(child)
    return tuple(sorted(matches, key=lambda path: path.name.casefold()))


def js8call_ini_family_label(path: object) -> str:
    name = Path(str(path or "")).name.strip()
    lowered = name.casefold()
    if "subspace" in lowered:
        return "JS8Call Subspace"
    if "improved" in lowered:
        return "JS8Call-improved"
    if lowered.startswith("js8call - "):
        instance = name.rsplit(".", 1)[0].split(" - ", 1)[1].strip()
        return f"JS8Call {instance}" if instance else "JS8Call"
    if lowered.startswith("js8call-"):
        instance = name.rsplit(".", 1)[0].split("-", 1)[1].strip()
        return f"JS8Call {instance}" if instance else "JS8Call"
    return "JS8Call"


def js8call_file_profile_operator_label(profile: JS8CallFileProfile) -> str:
    family = profile.family_label
    name = str(profile.name or "").strip()
    port = str(profile.tcp_server_port or "").strip()
    pieces = [family]
    if name and name.casefold() != "default":
        pieces.append(name)
    if port:
        pieces.append(f"API {port}")
    return " | ".join(pieces)


def discover_js8call_file_profiles(
    *,
    ini_path: Optional[Path] = None,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Tuple[JS8CallFileProfile, ...]:
    ini_paths = (Path(ini_path),) if ini_path is not None else default_js8call_ini_paths(platform=platform, home=home)
    discovered = []
    for candidate_ini in _unique_paths(ini_paths):
        if _js8call_settings_cancelled(cancelled):
            break
        profiles = read_js8call_multisettings(candidate_ini, cancelled=cancelled)
        if _js8call_settings_cancelled(cancelled):
            break
        if not profiles:
            continue
        rig_name = rig_name_from_settings_path(candidate_ini)
        application_name = js8_application_name(rig_name)
        root_candidates = qt_data_root_candidates(
            application_name=application_name,
            platform=platform,
            home=home,
        )
        evidence_roots = tuple(
            root
            for root in root_candidates
            if any((root / filename).is_file() for filename in ("ALL.TXT", "DIRECTED.TXT", "inbox.db3"))
        )
        for profile in profiles:
            if _js8call_settings_cancelled(cancelled):
                break
            save_dir_text = str(profile.settings.get("SaveDir", "") or "").strip()
            tcp_server_port = str(profile.settings.get("TCPServerPort", "") or "").strip()
            save_dir = Path(os.path.expandvars(os.path.expanduser(save_dir_text))) if save_dir_text else None
            data_root = evidence_roots[0] if len(evidence_roots) == 1 else None
            directed = data_root / "DIRECTED.TXT" if data_root is not None else None
            all_txt = data_root / "ALL.TXT" if data_root is not None else None
            inbox = data_root / "inbox.db3" if data_root is not None else None
            confidence = "verified" if data_root is not None else "not_found"
            reason = (
                f"Found JS8Call message storage for application '{application_name}'. SaveDir is separate."
                if data_root is not None
                else f"No bounded message-storage evidence found for application '{application_name}'. SaveDir is not a message-log location."
            )
            discovered.append(
                JS8CallFileProfile(
                    name=profile.name,
                    ini_path=str(candidate_ini),
                    save_dir=str(save_dir) if save_dir is not None else "",
                    tcp_server_port=tcp_server_port,
                    directed_path=str(directed) if directed is not None and directed.is_file() else "",
                    all_path=str(all_txt) if all_txt is not None and all_txt.is_file() else "",
                    confidence=confidence,
                    reason=reason,
                    inbox_path=str(inbox) if inbox is not None and inbox.is_file() else "",
                    application_data_root=str(data_root) if data_root is not None else "",
                    rig_name=rig_name,
                    # A matching bounded path proves the file location, but an
                    # INI filename alone does not prove which variant owns it.
                    storage_mode="unverified",
                )
            )
        if _js8call_settings_cancelled(cancelled):
            break
    return tuple(discovered)


def select_js8call_file_profile(
    profiles: Sequence[JS8CallFileProfile],
    *,
    tcp_port: str = "",
    profile_name: str = "",
) -> Optional[JS8CallFileProfile]:
    usable = [profile for profile in profiles if profile.directed_path]
    if not usable:
        return None

    port_txt = str(tcp_port or "").strip()
    name_hint = _profile_name_match_key(profile_name)
    name_matches: List[JS8CallFileProfile] = []
    if name_hint:
        name_matches = [
            profile
            for profile in usable
            if _profile_name_match_key(profile.name) == name_hint
        ]
        if len(name_matches) == 1:
            return name_matches[0]
    if port_txt:
        port_matches = [profile for profile in usable if str(profile.tcp_server_port or "").strip() == port_txt]
        if len(port_matches) == 1:
            return port_matches[0]
        if name_hint:
            named_port_matches = [
                profile
                for profile in port_matches
                if _profile_name_match_key(profile.name) == name_hint
            ]
            if len(named_port_matches) == 1:
                return named_port_matches[0]
        if not port_matches and len(name_matches) == 1:
            return name_matches[0]
        return None

    if len(usable) == 1:
        return usable[0]
    return None


def discover_varac_local_assets(
    *,
    install_path: Optional[Path] = None,
    app_paths: Optional[Mapping[str, str]] = None,
) -> Tuple[VarACLocalAsset, ...]:
    paths = dict(app_paths or {})
    install_dir = _first_varac_path(
        install_path,
        paths.get("varac_install_path"),
        paths.get("varac"),
        paths.get("varac_path"),
    )
    db_path = _first_varac_path(paths.get("varac_db_path"), install_dir / "VarAC.db" if install_dir else None)
    ini_path = _first_varac_path(paths.get("varac_ini_path"), install_dir / "VarAC.ini" if install_dir else None)
    traffic_log = _first_varac_path(paths.get("varac_traffic_log"), install_dir / "VarAC_traffic.log" if install_dir else None)
    app_log = _first_varac_path(paths.get("varac_log"))
    if app_log is None and install_dir is not None:
        exact_app_log = install_dir / "VarAC.log"
        app_log = exact_app_log if exact_app_log.exists() else _latest_varac_app_log(install_dir)
    qso_log = _first_varac_path(paths.get("varac_qso_log"), install_dir / "VarAC_qso_log.adi" if install_dir else None)
    callsign_tags = _first_varac_path(
        paths.get("varac_callsign_tags_path"),
        install_dir / "VarAC_callsign_tags.conf" if install_dir else None,
    )
    alert_tags = _first_varac_path(
        paths.get("varac_alert_tags_path"),
        install_dir / "VarAC_alert_tags.conf" if install_dir else None,
    )
    templates = _first_varac_path(paths.get("varac_templates_path"), install_dir / "VarAC_templates.ini" if install_dir else None)
    bbs_dir = _first_varac_path(
        paths.get("varac_bbs_dir"),
        _varac_ini_existing_path(ini_path, "BBS", "BBSDirectory"),
        _varac_child_path(install_dir, "BBS"),
    )
    outbox_dir = _first_varac_path(
        paths.get("varac_outbox_dir"),
        _varac_child_path(install_dir, "OUTGOING", "Outgoing", "Outbox"),
    )
    incoming_dir = _first_varac_path(
        paths.get("varac_incoming_dir"),
        _varac_ini_existing_path(ini_path, "FILES", "IncomingFilesDir"),
        _varac_child_path(install_dir, "INCOMING", "Incoming"),
    )
    archive_dir = _first_varac_path(
        paths.get("varac_bbs_archive_dir"),
        _varac_child_path(bbs_dir, "Archive", "ARCHIVE") if bbs_dir else None,
    )

    assets = [
        _varac_path_asset("install", "VarAC folder", install_dir, "directory"),
        _varac_ini_asset(ini_path),
        _varac_db_asset(db_path),
        _varac_path_asset("traffic_log", "VarAC traffic log", traffic_log, "file"),
        _varac_path_asset("app_log", "VarAC app log", app_log, "file"),
        _varac_path_asset("qso_log", "VarAC QSO ADIF log", qso_log, "file"),
        _varac_path_asset("callsign_tags", "VarAC callsign tags", callsign_tags, "file"),
        _varac_path_asset("alert_tags", "VarAC alert tags", alert_tags, "file"),
        _varac_path_asset("templates", "VarAC templates", templates, "file"),
        _varac_path_asset("bbs", "VarAC BBS folder", bbs_dir, "directory"),
        _varac_path_asset("bbs_archive", "VarAC BBS archive folder", archive_dir, "directory"),
        _varac_path_asset("outbox", "VarAC outbox folder", outbox_dir, "directory"),
        _varac_path_asset("incoming", "VarAC incoming folder", incoming_dir, "directory"),
    ]
    return tuple(asset for asset in assets if asset.path or asset.asset_id in {"install", "db", "ini"})


def _first_varac_path(*values: object) -> Optional[Path]:
    for value in values:
        txt = str(value or "").strip()
        if txt:
            return Path(os.path.expandvars(os.path.expanduser(txt)))
    return None


def _varac_child_path(parent: Optional[Path], *names: str) -> Optional[Path]:
    if parent is None:
        return None
    for name in names:
        candidate = parent / name
        if candidate.exists():
            return candidate
    if parent.is_dir():
        by_key = {child.name.casefold(): child for child in parent.iterdir()}
        for name in names:
            match = by_key.get(str(name or "").casefold())
            if match is not None:
                return match
    return parent / names[0] if names else None


def _varac_ini_existing_path(ini_path: Optional[Path], section: str, option: str) -> Optional[Path]:
    if ini_path is None or not ini_path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(ini_path, encoding="utf-8")
        raw = parser.get(section, option, fallback="")
    except Exception:
        return None
    value = str(raw or "").strip()
    if not value:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.exists() else None


def _latest_varac_app_log(install_dir: Path) -> Optional[Path]:
    if not install_dir.is_dir():
        return None
    candidates = [
        path
        for path in install_dir.glob("VarAC_*.log")
        if path.is_file() and not path.name.lower().startswith("varac_traffic")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return candidates[0]


def _varac_path_asset(asset_id: str, label: str, path: Optional[Path], kind: str) -> VarACLocalAsset:
    exists = _varac_kind_exists(path, kind)
    confidence = "verified" if exists else "not_found"
    detail = f"Found {label}." if exists else f"{label} not found."
    return VarACLocalAsset(
        asset_id=asset_id,
        label=label,
        path=str(path or ""),
        kind=kind,
        exists=exists,
        confidence=confidence,
        detail=detail,
    )


def _varac_ini_asset(path: Optional[Path]) -> VarACLocalAsset:
    exists = bool(path and path.is_file())
    confidence = "verified" if exists else "not_found"
    detail = "VarAC.ini not found."
    if exists and path is not None:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
            rig_control = parser["RIG_CONTROL"] if parser.has_section("RIG_CONTROL") else {}
            flrig_control = parser["RIG_FLRIG_CONFIG"] if parser.has_section("RIG_FLRIG_CONFIG") else {}
            my_info = parser["MY_INFO"] if parser.has_section("MY_INFO") else {}
            call = str(my_info.get("Mycall", "") or "").strip()
            locator = str(my_info.get("MyLocator", "") or "").strip()
            freq_control = str(rig_control.get("RigFreqControlType", "") or "").strip()
            ptt_control = str(rig_control.get("RigPTTControlType", "") or "").strip()
            flrig_port = str(flrig_control.get("FlrigPort", "") or "").strip()
            parts = ["Found VarAC.ini"]
            if call or locator:
                parts.append("station " + " ".join(part for part in (call, locator) if part))
            if freq_control or ptt_control:
                control = " / ".join(part for part in (freq_control, ptt_control) if part)
                parts.append(f"control {control}")
            if flrig_port:
                parts.append(f"FLRig port {flrig_port}")
            detail = "; ".join(parts) + "."
        except Exception as exc:
            confidence = "partial"
            detail = f"Found VarAC.ini, but it could not be summarized: {exc}"
    return VarACLocalAsset(
        asset_id="ini",
        label="VarAC.ini",
        path=str(path or ""),
        kind="file",
        exists=exists,
        confidence=confidence,
        detail=detail,
    )


def _varac_db_asset(path: Optional[Path]) -> VarACLocalAsset:
    exists = bool(path and path.is_file())
    tables: Tuple[str, ...] = ()
    confidence = "not_found"
    detail = "VarAC database not found."
    if exists and path is not None:
        try:
            tables = _sqlite_table_names_readonly(path)
            core_tables = tuple(table for table in ("qso", "vmail", "broadcast", "datastream") if table in tables)
            confidence = "verified" if len(core_tables) == 4 else "partial"
            detail = (
                "Found VarAC.db with core message/activity tables: " + ", ".join(core_tables)
                if core_tables
                else "Found VarAC.db, but core message/activity tables were not detected."
            )
        except Exception as exc:
            confidence = "partial"
            detail = f"Found VarAC.db, but schema could not be inspected: {exc}"
    return VarACLocalAsset(
        asset_id="db",
        label="VarAC.db",
        path=str(path or ""),
        kind="sqlite",
        exists=exists,
        confidence=confidence,
        detail=detail,
    )


def _varac_kind_exists(path: Optional[Path], kind: str) -> bool:
    if path is None:
        return False
    if kind == "directory":
        return path.is_dir()
    return path.is_file()


def _sqlite_table_names_readonly(path: Path) -> Tuple[str, ...]:
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return tuple(str(row[0]) for row in rows if row and str(row[0] or "").strip())


def _profile_name_match_key(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def build_autoconfig_proposal(
    *,
    radio_count: int = 1,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    include_varac: bool = False,
    extra_app_paths: Sequence[Path] = (),
    app_search_paths: Optional[Mapping[str, Sequence[Path]]] = None,
    js8_ini_path: Optional[Path] = None,
    busy_checker: Callable[[str, int], bool] = is_local_tcp_port_listening,
) -> AutoconfigProposal:
    system = normalize_platform(platform)
    candidates = find_app_candidates(
        platform=system,
        home=home,
        extra_paths=extra_app_paths,
        app_search_paths=app_search_paths,
    )
    found_app_ids = {candidate.app_id for candidate in candidates if candidate.executable}
    desired_apps = ("flrig", "fldigi", "js8call")
    missing_apps = tuple(app_id for app_id in desired_apps if app_id not in found_app_ids)
    warnings = []
    if missing_apps:
        labels = ", ".join(APP_DISPLAY_NAMES.get(app_id, app_id) for app_id in missing_apps)
        warnings.append(f"CommsPlan could not find {labels}; the user can choose paths, search again, or skip those apps.")
    if not include_varac:
        warnings.append("VarAC is optional and remains disabled unless the operator enables it.")

    radios = build_radio_instance_proposals(
        radio_count=radio_count,
        include_varac=include_varac,
        busy_checker=busy_checker,
    )
    js8_profiles = read_js8call_multisettings(js8_ini_path) if js8_ini_path is not None else ()
    js8_file_profiles = discover_js8call_file_profiles(
        ini_path=js8_ini_path,
        platform=system,
        home=home,
    )
    return AutoconfigProposal(
        platform=system,
        candidates=candidates,
        radios=radios,
        warnings=tuple(warnings),
        missing_apps=missing_apps,
        js8_profiles=js8_profiles,
        js8_file_profiles=js8_file_profiles,
    )


def _mac_app_candidates(base_dirs: Iterable[Path], names: Sequence[str]) -> Tuple[Path, ...]:
    candidates = []
    for base in base_dirs:
        base_path = Path(base)
        for name in names:
            app_name = name if name.lower().endswith(".app") else f"{name}.app"
            candidates.append(base_path / app_name)
            candidates.extend(_versioned_macos_bundle_candidates(base_path, name))
    return tuple(_unique_paths(candidates))


def _candidate_from_path(app_id: str, path: Path, *, system: str, source: str) -> AppCandidate:
    target = Path(os.path.expandvars(os.path.expanduser(str(path))))
    target_type = _target_type(target)
    executable = _path_is_executable_target(app_id, target, system)
    confidence = "verified" if executable else "high" if target.exists() else "not_found"
    notes = ()
    if target_type == "app_bundle" and executable:
        notes = ("Found macOS app bundle.",)
    elif target.exists() and not executable:
        notes = ("Found path, but executable entry point was not verified.",)
    return AppCandidate(
        app_id=app_id,
        display_name=APP_DISPLAY_NAMES.get(app_id, app_id),
        path=str(target),
        source=source,
        confidence=confidence,
        exists=target.exists(),
        executable=executable,
        target_type=target_type,
        notes=notes,
    )


def _target_type(path: Path) -> str:
    if path.suffix.lower() == ".app":
        return "app_bundle"
    if path.suffix.lower() in {".exe", ".bat", ".cmd", ".sh"}:
        return "file"
    if path.is_file():
        return "file"
    if path.is_dir():
        return "directory"
    return "file" if path.name and "." in path.name else "directory"


def _path_is_executable_target(app_id: str, path: Path, system: str) -> bool:
    if not _path_name_matches_app(app_id, path):
        return False
    if path.suffix.lower() == ".app":
        return _macos_bundle_executable(app_id, path) is not None
    if path.is_file():
        return os.access(path, os.X_OK) or system == "Windows"
    if app_id == "varac" and path.is_dir():
        return (path / "VarAC.exe").exists() or (path / "VarAC.db").exists()
    return False


def _macos_bundle_executable(app_id: str, bundle: Path) -> Optional[Path]:
    for name in _command_names(app_id):
        stem = Path(name).stem
        for candidate_name in (stem, stem.lower(), stem.upper()):
            candidate = bundle / "Contents" / "MacOS" / candidate_name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _command_names(app_id: str) -> Tuple[str, ...]:
    if app_id == "js8call":
        return JS8CALL_COMMAND_NAMES
    if app_id == "js8spotter":
        return ("JS8Spotter", "js8spotter")
    if app_id == "commstat":
        return ("CommStat", "commstat")
    if app_id == "sdrpp":
        return ("SDR++", "sdrpp", "sdrpp.exe")
    if app_id == "varac":
        return ("VarAC", "varac", "VarAC.exe")
    return (APP_DISPLAY_NAMES.get(app_id, app_id), app_id)


def _default_port_for_index(defaults: Sequence[int], index: int) -> int:
    if index < len(defaults):
        return int(defaults[index])
    return int(defaults[-1]) + (index - len(defaults) + 1)


def _first_available_port(
    start_port: int,
    host: str,
    used_ports: set,
    busy_checker: Callable[[str, int], bool],
) -> int:
    candidate = int(start_port)
    while candidate < 65535:
        if candidate not in used_ports and not busy_checker(host, candidate):
            return candidate
        candidate += 1
    raise RuntimeError("No available localhost port could be proposed.")


def _port_note(service: str, preferred: int, assigned: int, conflict: bool, *, conflict_checked: bool = True) -> str:
    if not conflict_checked:
        return f"{service} uses default UDP port {preferred}; UDP conflict probing is deferred."
    if conflict:
        return f"{service} preferred port {preferred} is busy; propose {assigned}."
    return f"{service} uses default port {preferred}."


def _service_protocol(service: str) -> str:
    return "udp" if service.endswith("_udp") else "tcp"


def _versioned_macos_bundle_candidates(base_dir: Path, name: str) -> Tuple[Path, ...]:
    if not base_dir.is_dir():
        return ()
    stem = Path(name).stem.strip().lower()
    matches = []
    try:
        children = tuple(base_dir.iterdir())
    except OSError:
        return ()
    for child in children:
        child_name = child.name.lower()
        if child.suffix.lower() == ".app" and child_name.startswith(stem) and child.is_dir():
            matches.append(child)
    return tuple(matches)


def _path_name_matches_app(app_id: str, path: Path) -> bool:
    if app_id == "varac":
        lowered = path.name.lower()
        return lowered.startswith("varac") or (path / "VarAC.exe").exists() or (path / "VarAC.db").exists()
    allowed_names = {Path(name).stem.lower() for name in _command_names(app_id)}
    stem = path.stem.lower()
    return any(stem == allowed or stem.startswith(f"{allowed}-") for allowed in allowed_names)


def _instance_name(index: int) -> str:
    if index < len(DEFAULT_RADIO_INSTANCE_NAMES):
        return DEFAULT_RADIO_INSTANCE_NAMES[index]
    return f"fio-{index + 1}"


def _selected_js8_settings(settings: Mapping[str, str]) -> Dict[str, str]:
    interesting = {
        "Rig",
        "CATNetworkPort",
        "TCPEnabled",
        "TCPServer",
        "TCPServerPort",
        "TCPMaxConnections",
        "UDPEnabled",
        "UDPServerPort",
        "SaveDir",
        "MyCall",
        "MyGrid",
    }
    return {key: str(value) for key, value in settings.items() if key in interesting and str(value).strip()}


def _unique_paths(paths: Iterable[Path]) -> Tuple[Path, ...]:
    out = []
    seen = set()
    for raw in paths:
        txt = str(raw or "").strip()
        if not txt:
            continue
        expanded = Path(os.path.expandvars(os.path.expanduser(txt)))
        key = os.path.normcase(os.path.normpath(str(expanded)))
        if key in seen:
            continue
        seen.add(key)
        out.append(expanded)
    return tuple(out)
