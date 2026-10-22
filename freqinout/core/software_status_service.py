from __future__ import annotations

import os
import posixpath
import re
import shlex
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import psutil

from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.core.logger import log
from freqinout.core.custom_rigctld import parse_custom_rigctld
from freqinout.radio_interface.js8_api_client import JS8ApiClient, JS8ApiEndpoint


PROGRAM_TOKENS: Dict[str, Sequence[str]] = {
    "FLRig": ("flrig", "flrig.exe"),
    "RigCtlD": ("rigctld", "rigctld.exe"),
    "FLDigi": ("fldigi", "fldigi.exe"),
    "FLMsg": ("flmsg", "flmsg.exe"),
    "FLAmp": ("flamp", "flamp.exe"),
    "VarAC": ("varac", "varac.exe"),
    "JS8Call": (
        "js8call",
        "js8call.exe",
        "JS8Call",
        "JS8Call.exe",
        "js8call-improved",
        "js8call-improved.exe",
        "js8call-subspace",
        "js8call-subspace.exe",
        "subspace",
        "subspace.exe",
    ),
    "JS8Spotter": ("js8spotter", "js8spotter.exe", "js8spotter.py"),
    "CommStat": ("commstat", "commstat.exe", "commstat.py"),
    # SDR++ is only added to a receiver-scoped launch bundle.  It has no
    # global settings path because a station can host multiple receiver apps.
    "SDR++": ("sdrpp", "sdrpp.exe", "sdr++.exe"),
}

PROGRAM_PATH_KEYS: Dict[str, str] = {
    "FLRig": "path_flrig",
    "FLDigi": "path_fldigi",
    "FLMsg": "path_flmsg",
    "FLAmp": "path_flamp",
    "JS8Call": "path_js8call",
    "JS8Spotter": "path_js8spotter",
    "CommStat": "path_commstat",
}

# Process names that commonly host a configured script/application rather than
# exposing the application name as the process name. Only these processes need
# the more expensive command-line inspection during a routine inventory pass.
PROCESS_WRAPPER_TOKENS = {
    "python",
    "python3",
    "pythonw",
    "python.exe",
    "python3.exe",
    "wine",
    "wine64",
    "wine-preloader",
    "wine64-preloader",
    "wine-preloader.exe",
    "wine64-preloader.exe",
    "proton",
    "mono",
    "bash",
    "sh",
    "zsh",
    "env",
}

_INACTIVE_PROCESS_STATUSES = frozenset(
    str(value or "").strip().casefold()
    for value in (
        getattr(psutil, "STATUS_ZOMBIE", "zombie"),
        getattr(psutil, "STATUS_DEAD", "dead"),
    )
    if str(value or "").strip()
)

STATUS_KEYS: Sequence[str] = (
    "JS8Call_API",
    "FLRig",
    "RigCtlD",
    "FLDigi",
    "FLMsg",
    "FLAmp",
    "VarAC",
    "JS8Spotter",
    "CommStat",
)

JS8_DEFAULT_HOST = "127.0.0.1"
JS8_DEFAULT_PORT = 2442
JS8_HEALTH_SLOW_MS = 2500.0
FLRIG_DEFAULT_HOST = "127.0.0.1"
FLRIG_DEFAULT_PORT = 12345
RIGCTLD_DEFAULT_HOST = "127.0.0.1"
RIGCTLD_DEFAULT_PORT = 4532
FLDIGI_DEFAULT_HOST = "127.0.0.1"
FLDIGI_DEFAULT_PORT = 7362


class SoftwareStatusService:
    """
    Shared process and API status probe used by UI surfaces.

    This class is intentionally side-effect free aside from local process/socket
    inspection so tabs can refresh status consistently.
    """

    # Shared caches across all service instances to avoid duplicated polling work.
    _shared_instance_evidence = {}
    _shared_proc_snapshot: List[str] = []
    _shared_proc_records: List[Dict[str, object]] = []
    _shared_proc_snapshot_ts: float = 0.0
    _shared_proc_lock = threading.Lock()
    _shared_js8_api_cache: Dict[tuple[str, int, bool], tuple[float, bool]] = {}
    _shared_js8_capability_cache: Dict[tuple[str, int], tuple[float, Dict[str, object]]] = {}
    _shared_js8_shadow_cache: Dict[tuple[str, int], tuple[float, Dict[str, object]]] = {}
    _shared_service_probe_cache: Dict[tuple[str, ...], tuple[float, bool]] = {}

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._proc_snapshot: List[str] = []
        self._proc_records: List[Dict[str, object]] = []
        self._proc_snapshot_ts: float = 0.0
        # Process inventory is station-wide and comparatively expensive on
        # some Linux /proc implementations. Endpoint reachability still has
        # its own shorter cache, while launch/manual refreshes explicitly force
        # a new inventory. A longer passive TTL prevents two-radio health
        # projections from continuously walking the same process table.
        self._snapshot_ttl_sec: float = 30.0
        self._js8_api_cache_key: tuple[str, int, bool] | None = None
        self._js8_api_cache_ok: bool = False
        self._js8_api_cache_ts: float = 0.0
        self._api_success_ttl_sec: float = 15.0
        self._api_failure_ttl_sec: float = 30.0
        self._js8_capability_success_ttl_sec: float = 60.0
        self._js8_capability_failure_ttl_sec: float = 120.0
        self._js8_shadow_cache_ttl_sec: float = 60.0
        self._service_probe_success_ttl_sec: float = 15.0
        self._service_probe_failure_ttl_sec: float = 30.0
        self._health = get_dependency_health_registry()

    def _settings_text(self, key: str, default: str = "") -> str:
        try:
            return str(self.settings.get(key, default) or "").strip()
        except Exception:
            return str(default or "").strip()

    def _settings_int(self, key: str, default: int) -> int:
        try:
            value = self.settings.get(key, default)
            return int(value if value not in (None, "") else default)
        except Exception:
            return int(default)

    def _resolved_fldigi_host(self, host_override: Optional[str] = None) -> str:
        override = str(host_override or "").strip()
        if override:
            return override
        host = self._settings_text("fldigi_host", "")
        if host:
            return host
        host = self._settings_text("flrig_host", FLDIGI_DEFAULT_HOST)
        return host or FLDIGI_DEFAULT_HOST

    @staticmethod
    def _format_endpoint(host: str, port: int) -> str:
        return f"{host}:{int(port)}"

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        host_norm = str(host or "").strip().lower()
        return host_norm in {"", "127.0.0.1", "localhost", "::1", "[::1]"}

    def _cached_service_probe(
        self,
        cache_key: tuple[str, ...],
        *,
        force: bool = False,
        probe: Callable[[], bool],
    ) -> bool:
        cls = type(self)
        now = time.monotonic()
        cached = cls._shared_service_probe_cache.get(cache_key)
        if not force and cached:
            cached_ts, cached_ok = cached
            ttl = self._service_probe_success_ttl_sec if cached_ok else self._service_probe_failure_ttl_sec
            if (now - float(cached_ts or 0.0)) < float(ttl):
                return bool(cached_ok)
        health_key = self._health_key(cache_key)
        allowed, _health = self._health.may_run(health_key, owner="SoftwareStatusService", force=force)
        if not allowed and cached:
            return bool(cached[1])
        if not allowed:
            return False
        started = time.monotonic()
        try:
            ok = bool(probe())
            elapsed_ms = (time.monotonic() - started) * 1000.0
        except Exception as exc:
            ok = False
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self._health.record_failure(
                health_key,
                owner="SoftwareStatusService",
                error=str(exc or "probe failed"),
                duration_ms=elapsed_ms,
            )
        else:
            if ok:
                self._health.record_success(health_key, owner="SoftwareStatusService", duration_ms=elapsed_ms)
            else:
                self._health.record_failure(
                    health_key,
                    owner="SoftwareStatusService",
                    error="unreachable",
                    duration_ms=elapsed_ms,
                )
        cls._shared_service_probe_cache[cache_key] = (now, ok)
        return ok

    @staticmethod
    def _health_key(cache_key: tuple[object, ...]) -> str:
        parts = [str(part or "").strip().lower() for part in cache_key]
        return ":".join(part for part in parts if part) or "software-status"

    def _health_snapshot_for(self, cache_key: tuple[object, ...]) -> Dict[str, object]:
        return self._health.snapshot(self._health_key(cache_key))

    @staticmethod
    def _basename_token(value: object) -> str:
        try:
            text = str(value or "").strip().replace("\\", "/")
            return text.rsplit("/", 1)[-1].strip().lower()
        except Exception:
            return ""

    @staticmethod
    def _matches_target_process_name(name: str, targets: set[str]) -> bool:
        """Recognize direct and version-qualified native process names.

        Linux reports the executable's native name, which may differ from the
        configured symlink used in argv (for example ``flamp-2.2.14`` launched
        through ``/usr/local/bin/flamp``).  Inspect only an exact target or a
        delimiter-qualified version/variant so unrelated processes still avoid
        the comparatively expensive exe/cmdline reads.
        """

        normalized_name = str(name or "").strip().casefold()
        if not normalized_name:
            return False
        name_stem = normalized_name[:-4] if normalized_name.endswith(".exe") else normalized_name
        for raw_target in targets:
            target = str(raw_target or "").strip().casefold()
            target_stem = target[:-4] if target.endswith(".exe") else target
            if name_stem == target_stem:
                return True
            if name_stem.startswith(target_stem) and len(name_stem) > len(target_stem):
                if name_stem[len(target_stem)] in {"-", "_", "."}:
                    return True
        return False

    @staticmethod
    def _process_status(proc: object) -> str:
        """Return a best-effort psutil status without treating denial as absence."""

        try:
            info = getattr(proc, "info", {})
            if isinstance(info, Mapping):
                saved = str(info.get("status") or "").strip().casefold()
                if saved:
                    return saved
            getter = getattr(proc, "status", None)
            if callable(getter):
                return str(getter() or "").strip().casefold()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return getattr(psutil, "STATUS_DEAD", "dead")
        except (psutil.AccessDenied, OSError):
            # Unknown evidence remains fail-closed. Only positively identified
            # terminal processes are discarded.
            return ""
        return ""

    @staticmethod
    def _process_record_is_active(record: Mapping[str, object]) -> bool:
        status = str(record.get("status") or "").strip().casefold()
        return status not in _INACTIVE_PROCESS_STATUSES

    def _refresh_process_snapshot(
        self,
        *,
        force: bool = False,
        inspect_all: bool = False,
    ) -> None:
        cls = type(self)
        with cls._shared_proc_lock:
            now = time.monotonic()
            if not force and (now - float(cls._shared_proc_snapshot_ts or 0.0)) < self._snapshot_ttl_sec:
                self._proc_snapshot = cls._shared_proc_snapshot
                self._proc_records = cls._shared_proc_records
                self._proc_snapshot_ts = float(cls._shared_proc_snapshot_ts or now)
                return
            started = time.perf_counter()
            snap: List[str] = []
            records: List[Dict[str, object]] = []
            target_tokens = {
                token
                for program_name in PROGRAM_TOKENS
                for token in self._target_tokens(program_name)
                if token
            }
            # Reading exe/cmdline for every process is disproportionately slow on
            # some Linux systems (and can block on inaccessible/FUSE-backed proc
            # entries). Start with the cheap name inventory and inspect details
            # only for a direct match or a known wrapper process.
            for proc in psutil.process_iter(attrs=["name"]):
                try:
                    name = (proc.info.get("name") or "").strip().lower()
                    exe_path = ""
                    exe = ""
                    cmdline: Sequence[object] = ()
                    direct_match = self._matches_target_process_name(name, target_tokens)
                    # Multi-instance applications commonly share one binary
                    # and differ only by launch arguments (profile/config
                    # roots, rig name, or VarAC INI).  Inspect command lines
                    # for the small set of known direct matches as well as
                    # wrappers so status can attribute a process to one radio.
                    inspect_command = (
                        bool(inspect_all)
                        or direct_match
                        or name in PROCESS_WRAPPER_TOKENS
                        or name.startswith(("wine-", "wine64-", "proton-"))
                        or not name
                    )
                    process_status = ""
                    if direct_match or name in PROCESS_WRAPPER_TOKENS or not name:
                        process_status = self._process_status(proc)
                        if process_status in _INACTIVE_PROCESS_STATUSES:
                            continue
                    if direct_match:
                        try:
                            exe_path = str(proc.exe() or "").strip()
                        except Exception:
                            exe_path = ""
                        exe = self._basename_token(exe_path)
                    if inspect_command:
                        try:
                            cmdline = proc.cmdline() or ()
                        except Exception:
                            cmdline = ()
                    cwd = ""
                    if direct_match or any(self._basename_token(arg) in {"varac", "varac.exe"} for arg in cmdline):
                        try:
                            cwd = str(proc.cwd() or "")
                        except Exception:
                            pass
                    cmd_paths: List[str] = []
                    cmd_tokens: List[str] = []
                    normalized_cmdline = tuple(str(arg or "").strip() for arg in cmdline[:16])
                    for path in normalized_cmdline:
                        try:
                            token = self._basename_token(path)
                        except Exception:
                            path = ""
                            token = ""
                        if token:
                            cmd_paths.append(path)
                            cmd_tokens.append(token)
                    if not process_status and any(
                        self._matches_target_process_name(token, target_tokens)
                        for token in cmd_tokens
                    ):
                        process_status = self._process_status(proc)
                        if process_status in _INACTIVE_PROCESS_STATUSES:
                            continue
                    for token in (name, exe, *cmd_tokens):
                        if token:
                            snap.append(token)
                    records.append(
                        {
                            "pid": int(getattr(proc, "pid", 0) or 0),
                            "name": name,
                            "status": process_status,
                            "exe": exe,
                            "exe_path": exe_path,
                            "cmd_tokens": tuple(cmd_tokens),
                            "cmd_paths": tuple(cmd_paths),
                            "cmdline": normalized_cmdline,
                            "cwd": cwd,
                        }
                    )
                except Exception:
                    continue
            cls._shared_proc_snapshot = snap
            cls._shared_proc_records = records
            cls._shared_proc_snapshot_ts = now
            self._proc_snapshot = cls._shared_proc_snapshot
            self._proc_records = cls._shared_proc_records
            self._proc_snapshot_ts = now
        log.debug(
            "PROCESS_INVENTORY|refreshed|processes=%s|tokens=%s|duration_ms=%.1f",
            len(records),
            len(snap),
            (time.perf_counter() - started) * 1000.0,
        )

    def _configured_tokens(self, program_name: str) -> List[str]:
        key = PROGRAM_PATH_KEYS.get(program_name)
        if not key:
            return []
        try:
            path_txt = (self.settings.get(key, "") or "").strip()
        except Exception:
            path_txt = ""
        if not path_txt:
            return []
        out: List[str] = []
        try:
            p = Path(path_txt)
            name = self._basename_token(p.name)
            if name:
                out.append(name)
        except Exception:
            pass
        try:
            parts = shlex.split(path_txt, posix=os.name != "nt")
        except Exception:
            parts = []
        for part in parts[:6]:
            token = self._basename_token(part)
            if token:
                out.append(token)
        return list(dict.fromkeys(out))

    def _target_tokens(self, program_name: str) -> List[str]:
        defaults = [t.lower() for t in PROGRAM_TOKENS.get(program_name, ())]
        return list(dict.fromkeys(defaults + self._configured_tokens(program_name)))

    def program_is_running(self, program_name: str) -> bool:
        self._refresh_process_snapshot()
        targets = set(self._target_tokens(program_name))
        if not targets:
            targets = {program_name.strip().lower(), f"{program_name.strip().lower()}.exe"}
        return any(token in targets for token in self._proc_snapshot)

    def find_process_exe(self, program_name: str) -> Optional[str]:
        self._refresh_process_snapshot()
        targets = set(self._target_tokens(program_name))
        if not targets:
            targets = {program_name.strip().lower(), f"{program_name.strip().lower()}.exe"}
        for record in self._proc_records:
            try:
                cmd_tokens = tuple(str(token or "") for token in record.get("cmd_tokens", ()))
                cmd_paths = tuple(str(path or "") for path in record.get("cmd_paths", ()))
                for token, path in zip(cmd_tokens, cmd_paths):
                    if token in targets and path:
                        return path
                process_tokens = {str(record.get("name") or ""), str(record.get("exe") or "")}
                if not process_tokens.intersection(targets):
                    continue
                exe_path = str(record.get("exe_path") or "").strip()
                if exe_path:
                    return exe_path
                return None
            except Exception:
                continue
        return None

    def cached_program_is_running(self, program_name: str) -> bool:
        """Read the shared process snapshot without ever starting an inventory walk."""
        cls = type(self)
        self._proc_snapshot = cls._shared_proc_snapshot
        self._proc_records = cls._shared_proc_records
        self._proc_snapshot_ts = cls._shared_proc_snapshot_ts
        if not self._proc_snapshot and not self._proc_records:
            return False
        targets = set(self._target_tokens(program_name))
        if not targets:
            normalized = program_name.strip().lower()
            targets = {normalized, f"{normalized}.exe"}
        return any(token in targets for token in self._proc_snapshot)

    def cached_program_process_count(
        self,
        program_name: str,
        process_records: Sequence[Mapping[str, object]] | None = None,
    ) -> int:
        """Count cached process records carrying this program's family token."""

        cls = type(self)
        records = cls._shared_proc_records if process_records is None else process_records
        if process_records is None:
            self._proc_snapshot = cls._shared_proc_snapshot
            self._proc_records = cls._shared_proc_records
            self._proc_snapshot_ts = cls._shared_proc_snapshot_ts
        targets = set(self._target_tokens(program_name))
        if not targets:
            normalized = str(program_name or "").strip().casefold()
            targets = {normalized, f"{normalized}.exe"}
        count = 0
        for record in records:
            if not self._process_record_is_active(record):
                continue
            record_tokens = {
                str(record.get("name") or "").casefold(),
                str(record.get("exe") or "").casefold(),
                *(
                    str(value or "").casefold()
                    for value in record.get("cmd_tokens", ())
                ),
            }
            if any(
                self._matches_target_process_name(token, targets)
                for token in record_tokens
                if token
            ):
                count += 1
        return count

    @staticmethod
    def _normalized_process_argument(value: object) -> str:
        text = str(value or "").strip().strip('"').strip("'")
        if not text:
            return ""
        expanded = os.path.expanduser(os.path.expandvars(text))
        if re.match(r"^[a-zA-Z]:[\\/]", expanded):
            return expanded.replace("\\", "/").casefold()
        if expanded.startswith("/"):
            return str(Path(expanded).resolve(strict=False)).casefold()
        return expanded.casefold()

    @classmethod
    def _process_arguments_match(
        cls,
        actual: Sequence[object],
        expected: Sequence[object],
    ) -> bool:
        wanted = tuple(
            value
            for value in (cls._normalized_process_argument(item) for item in expected)
            if value
        )
        if not wanted:
            return True
        observed = tuple(cls._normalized_process_argument(item) for item in actual)
        if len(observed) < len(wanted):
            return False
        return any(
            observed[index : index + len(wanted)] == wanted
            for index in range(len(observed) - len(wanted) + 1)
        )

    @classmethod
    def _process_arguments_exclude_options(
        cls,
        actual: Sequence[object],
        excluded_options: Sequence[object],
    ) -> bool:
        """Return true when no excluded command-line option is present.

        An empty expected-argument sequence intentionally remains a wildcard
        for ordinary executable-only identities.  Native-default JS8Call is
        stricter: its lack of ``--rig-name`` is part of the identity, so the
        caller supplies that selector here as an excluded option.
        """

        excluded = {
            str(value or "").strip().casefold()
            for value in excluded_options
            if str(value or "").strip()
        }
        if not excluded:
            return True
        for value in actual:
            token = str(value or "").strip().casefold()
            option = token.split("=", 1)[0]
            if option in excluded:
                return False
        return True

    def cached_program_instance_running(
        self,
        program_name: str,
        configured_target: str,
        expected_arguments: Sequence[object] = (),
        process_records: Sequence[Mapping[str, object]] | None = None,
        *,
        excluded_options: Sequence[object] = (),
    ) -> bool:
        """Match one configured process identity from shared cached records.

        Executable-only matching remains available for genuinely distinct
        binaries.  When canonical launch arguments are supplied, the same
        executable running for another radio is not accepted.
        """
        target_text = str(configured_target or "").strip()
        if not target_text and not expected_arguments:
            return self.cached_program_is_running(program_name)
        try:
            parts = shlex.split(target_text, posix=os.name != "nt")
        except Exception:
            parts = [target_text]
        path_parts = [
            Path(os.path.expanduser(os.path.expandvars(value)))
            for value in parts[:4]
            if value and not value.startswith("-")
        ]
        target_paths = {str(path.resolve(strict=False)).casefold() for path in path_parts if path.is_absolute() or "/" in str(path) or "\\" in str(path)}
        if not target_paths and not expected_arguments:
            # A launch recipe may intentionally use a PATH-resolved command
            # (``sdrpp``) or a platform launcher (``open -a SDR++``).  In that
            # case there is no durable filesystem target to compare, so use
            # the cached, program-specific token set rather than forcing a
            # readiness timeout after a successful launch.
            return self.cached_program_is_running(program_name)
        targets = set(self._target_tokens(program_name))
        if not targets:
            normalized = program_name.strip().lower()
            targets = {normalized, f"{normalized}.exe"}
        cls = type(self)
        records = cls._shared_proc_records if process_records is None else process_records
        for record in records:
            if not self._process_record_is_active(record):
                continue
            record_tokens = {
                str(record.get("name") or ""),
                str(record.get("exe") or ""),
                *(str(value or "") for value in record.get("cmd_tokens", ())),
            }
            if not record_tokens.intersection(targets):
                continue
            candidates = [str(record.get("exe_path") or "")]
            candidates.extend(str(value or "") for value in record.get("cmd_paths", ()))
            target_matches = not target_paths
            for candidate in candidates:
                if not candidate:
                    continue
                resolved = str(Path(candidate).resolve(strict=False)).casefold()
                for target in target_paths:
                    if resolved == target or resolved.startswith(target.rstrip("/\\") + os.sep.casefold()):
                        target_matches = True
                        break
                if target_matches:
                    break
            if not target_matches:
                continue
            if not self._process_arguments_match(
                record.get("cmdline", ()),
                expected_arguments,
            ):
                continue
            if not self._process_arguments_exclude_options(
                record.get("cmdline", ()),
                excluded_options,
            ):
                continue
            return True
        return False

    @classmethod
    def cached_endpoint_identity_conflict(cls, host, port):
        with cls._shared_proc_lock:
            observed = cls._shared_instance_evidence.get((str(host).lower(),int(port)))
            return bool(observed and 0 <= time.monotonic()-observed[0] <= 15 and observed[1].get('identity_conflict'))

    def cached_varac_process_state(
        self, configured_target: str, ini_path: str = "", wine_prefix: str = "",
        process_records=None, checked_monotonic=None, max_age_s: float = 30.0,
    ) -> Dict[str, object]:
        """Attribute one VarAC instance without probing processes or files.

        Only a concrete drive_<letter> host path establishes a Wine mapping.
        A native Windows path in Wine without observable prefix evidence stays
        unknown; neither a wrapper PID nor a basename establishes ownership.
        """
        from freqinout.core.varac_launch_recipe import _wine_identity
        cls = type(self)
        with cls._shared_proc_lock:
            records = tuple(cls._shared_proc_records if process_records is None else process_records)
            stamp = float(cls._shared_proc_snapshot_ts if checked_monotonic is None else checked_monotonic)
        out = dict(running=None, checked_monotonic=stamp, reason="Could not verify", pids=())
        if not stamp or not 0 <= time.monotonic() - stamp <= max_age_s:
            out['reason'] = 'Process observation is stale or unavailable'
            return out
        def norm(value):
            return posixpath.normpath(str(value or '').strip().strip('"').replace('\\', '/')).casefold()
        target = str(configured_target or '').strip()
        if not target:
            return out
        if self._basename_token(target) not in {'varac', 'varac.exe'}:
            if posixpath.splitext(target)[1]:
                out['reason'] = 'Choose the VarAC executable for process attribution; launcher identity is unverified'
                return out
            target = target.rstrip('/\\') + '/VarAC.exe'
        target_mapping = _wine_identity(target) if target.startswith('/') else None
        prefix = wine_prefix or (target_mapping[0] if target_mapping else '')
        native_target = target_mapping[1] if target_mapping else target
        if os.name != 'nt' and re.match(r'^[a-zA-Z]:[\\/]', target) and not prefix:
            out['reason'] = 'Could not verify Wine prefix for the configured Windows path'
            return out
        ini = ini_path or target.replace('\\', '/').rsplit('/', 1)[0] + '/VarAC.ini'
        ini_mapping = _wine_identity(ini) if ini.startswith('/') else None
        native_ini = ini_mapping[1] if ini_mapping else ini
        default_ini = self._basename_token(ini) == 'varac.ini'
        unknown = False
        matched = []
        for record in records:
            if not self._process_record_is_active(record):
                continue
            tokens = (record.get('name',''), record.get('exe',''), *record.get('cmd_tokens', ()))
            args = tuple(record.get('cmdline', ()))
            paths = (record.get('exe_path',''), *record.get('cmd_paths', ()), *args)
            if not any(self._basename_token(token) in {'varac','varac.exe'} for token in (*tokens,*paths)):
                continue
            host_paths = [str(path) for path in paths if str(path).startswith('/')]
            mapped_prefixes = {norm(mapping[0]) for path in (*host_paths, str(record.get('cwd') or ''))
                               if path and (mapping := _wine_identity(path))}
            if record.get('wine_prefix'):
                mapped_prefixes.add(norm(record['wine_prefix']))
            exact_host = any(norm(path) == norm(target) for path in host_paths)
            if prefix and mapped_prefixes and norm(prefix) not in mapped_prefixes:
                continue
            native_matches = any(norm(path) == norm(native_target) for path in paths if path)
            if not exact_host and native_matches and prefix and norm(prefix) not in mapped_prefixes:
                unknown = True
                continue
            if not exact_host and not native_matches:
                if not host_paths or not args:
                    unknown = True
                continue
            if not args:
                unknown = True
                continue
            selectors = [str(arg) for arg in args if self._basename_token(arg).endswith('.ini')]
            if selectors:
                expected = {norm(ini), norm(native_ini)}
                if not any(norm(arg) in expected for arg in selectors):
                    continue
            elif not default_ini:
                continue
            pid = int(record.get('pid') or 0)
            if pid <= 0:
                unknown = True
            else:
                matched.append(pid)
        pids = tuple(sorted(set(matched)))
        out['pids'] = pids
        if len(pids) == 1 and not unknown:
            out.update(running=True, reason='Configured VarAC instance is running')
        elif len(pids) > 1 or unknown:
            out['reason'] = 'VarAC running — setup unverified'
        else:
            out.update(running=False, reason='Configured VarAC instance is not running')
        return out

    def cached_instance_process_state(
        self, program_name: str, configured_target: str,
        expected_arguments: Sequence[object] = (), *, max_age_s: float = 30.0,
        default_ini: str = "", excluded_options: Sequence[object] = (),
    ) -> Dict[str, object]:
        """Bounded attribution from the existing inventory; never walk processes.

        Unknown/ambiguous identity is distinct from a fresh observed absence.
        The original inventory timestamp must survive downstream publications.
        """
        cls = type(self)
        with cls._shared_proc_lock:
            stamp = float(cls._shared_proc_snapshot_ts or 0.0)
            records = tuple(cls._shared_proc_records)
        result: Dict[str, object] = {"running": None, "checked_monotonic": stamp}
        age = time.monotonic() - stamp
        if not configured_target or stamp <= 0 or not 0 <= age <= max_age_s:
            return result
        # This API takes a literal executable path, not a launch command.
        target = self._normalized_process_argument(configured_target)
        family = set(self._target_tokens(program_name))
        matches = []
        for record in records:
            if (expected_arguments or default_ini) and not record.get("cmdline"):
                continue
            ini_tokens = [str(value) for value in record.get("cmdline", ())
                          if str(value).lower().endswith(".ini")]
            if default_ini and ini_tokens and not self._process_arguments_match(ini_tokens, (default_ini,)):
                continue
            tokens = (record.get("name", ""), record.get("exe", ""), *record.get("cmd_tokens", ()))
            paths = (record.get("exe_path", ""), *record.get("cmd_paths", ()))
            if (self._process_record_is_active(record)
                    and any(self._matches_target_process_name(str(token), family) for token in tokens)
                    and any(self._normalized_process_argument(path) == target or
                            (target.endswith('.app') and self._normalized_process_argument(path).startswith(target + '/')) for path in paths if path)
                    and self._process_arguments_match(record.get("cmdline", ()), expected_arguments)
                    and self._process_arguments_exclude_options(record.get('cmdline', ()), excluded_options)):
                matches.append(record)
        pids = {record.get("pid") for record in matches}
        if len(pids) == 1 and None not in pids and 0 not in pids:
            result["running"] = True
        elif not matches:
            # A family process with unreadable attribution is not proof that
            # this radio's instance is stopped.
            family = self._target_tokens(program_name)
            unknown = any(
                self._process_record_is_active(record)
                and any(self._matches_target_process_name(str(token), set(family))
                    for token in (record.get("name", ""), record.get("exe", ""), *record.get("cmd_tokens", ())))
                and (not (record.get("exe_path") or record.get("cmd_paths"))
                     or ((expected_arguments or default_ini) and not record.get("cmdline")))
                for record in records
            )
            if not unknown:
                result["running"] = False
        return result

    def program_instance_running(
        self,
        program_name: str,
        configured_target: str,
        expected_arguments: Sequence[object] = (),
        *,
        excluded_options: Sequence[object] = (),
    ) -> bool:
        self._refresh_process_snapshot()
        return self.cached_program_instance_running(
            program_name,
            configured_target,
            expected_arguments,
            excluded_options=excluded_options,
        )

    def js8_api_reachable(
        self,
        *,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        allow_fallback: bool = True,
        force: bool = False,
    ) -> bool:
        if port_override is not None:
            port = int(port_override)
        else:
            try:
                port = int(self.settings.get("js8_port", 2442) or 2442)
            except Exception:
                port = 2442

        hosts: List[str] = []
        host_cfg = (host_override or "").strip()
        if not host_cfg:
            try:
                host_cfg = (self.settings.get("js8_host", "") or "").strip()
            except Exception:
                host_cfg = ""
        if host_cfg:
            hosts.append(host_cfg)
        cache_host = hosts[0].strip().lower() if hosts else ""
        cache_key = (cache_host, int(port), bool(allow_fallback))
        now = time.monotonic()
        cls = type(self)
        cache_entry = cls._shared_js8_api_cache.get(cache_key)
        if not force and cache_entry:
            cached_ts, cached_ok = cache_entry
            ttl = self._api_success_ttl_sec if cached_ok else self._api_failure_ttl_sec
            if (now - float(cached_ts or 0.0)) < float(ttl):
                self._js8_api_cache_key = cache_key
                self._js8_api_cache_ok = bool(cached_ok)
                self._js8_api_cache_ts = float(cached_ts or now)
                return bool(cached_ok)
        health_key = self._health_key(("JS8CALL", cache_host or "loopback", int(port), bool(allow_fallback)))
        allowed, _health = self._health.may_run(health_key, owner="SoftwareStatusService", force=force)
        if not allowed and cache_entry:
            return bool(cache_entry[1])
        if not allowed:
            return False

        primary_host = hosts[0] if hosts else None
        if not hosts:
            hosts.extend(["127.0.0.1", "localhost", "::1"])
        started = time.monotonic()
        reachable = False
        for host in hosts:
            try:
                with socket.create_connection((host, port), timeout=0.35):
                    reachable = True
                    break
            except Exception:
                continue

        if not reachable and allow_fallback:
            # Optional fallback probe through existing control client.
            try:
                from freqinout.radio_interface.js8_status import JS8ControlClient

                client = JS8ControlClient(host=primary_host, port=port)
                reachable = client.get_frequency() is not None
            except Exception:
                reachable = False

        self._js8_api_cache_key = cache_key
        self._js8_api_cache_ok = bool(reachable)
        self._js8_api_cache_ts = time.monotonic()
        cls._shared_js8_api_cache[cache_key] = (self._js8_api_cache_ts, self._js8_api_cache_ok)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if reachable:
            self._health.record_success(health_key, owner="SoftwareStatusService", duration_ms=elapsed_ms,
                                        slow_ms=JS8_HEALTH_SLOW_MS)
        else:
            self._health.record_failure(
                health_key,
                owner="SoftwareStatusService",
                error="unreachable",
                duration_ms=elapsed_ms,
            )
        return bool(reachable)

    def js8_api_capability_status(
        self,
        *,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        process_running: Optional[bool] = None,
        force: bool = False,
    ) -> Dict[str, object]:
        host = (host_override or "").strip() or self._settings_text("js8_host", JS8_DEFAULT_HOST) or JS8_DEFAULT_HOST
        port = int(port_override) if port_override is not None else self._settings_int("js8_port", JS8_DEFAULT_PORT)
        endpoint = JS8ApiEndpoint(host, port).normalized()
        cache_key = endpoint.key
        health_key = self._health_key(("JS8CALL", endpoint.host.lower(), int(endpoint.port), "capability"))
        running = bool(process_running) if process_running is not None else self.program_is_running("JS8Call")
        now = time.monotonic()
        cached = type(self)._shared_js8_capability_cache.get(cache_key)
        if not running and self._is_loopback_host(endpoint.host):
            status = self._js8_capability_offline_status(
                endpoint,
                last_error="JS8Call is not running",
            )
            type(self)._shared_js8_capability_cache.pop(cache_key, None)
            return status
        if not force and cached:
            cached_ts, cached_status = cached
            connected = bool(cached_status.get("connected"))
            ttl = self._js8_capability_success_ttl_sec if connected else self._js8_capability_failure_ttl_sec
            if (now - float(cached_ts or 0.0)) < ttl:
                return dict(cached_status)
        allowed, _health = self._health.may_run(health_key, owner="SoftwareStatusService", force=force)
        if not allowed and cached:
            return dict(cached[1])
        if not allowed:
            return self._js8_capability_offline_status(
                endpoint,
                last_error="JS8Call API capability check is waiting for cooldown",
            )
        status, elapsed_ms = self._probe_js8_capability(endpoint, timeout_s=0.4)
        metadata = {
            "capability_mode": status.get("mode", "offline"),
            "version": status.get("version", ""),
            "endpoint": status.get("endpoint", ""),
            "action": self._js8_capability_action(status, running=running),
        }
        if bool(status.get("connected")):
            self._health.record_success(
                health_key,
                owner="SoftwareStatusService",
                duration_ms=elapsed_ms,
                slow_ms=JS8_HEALTH_SLOW_MS,
                metadata=metadata,
            )
        elif running:
            self._health.record_failure(
                health_key,
                owner="SoftwareStatusService",
                error=str(status.get("last_error") or "JS8Call TCP API not reachable"),
                duration_ms=elapsed_ms,
                metadata=metadata,
            )
        type(self)._shared_js8_capability_cache[cache_key] = (time.monotonic(), dict(status))
        return status

    def _probe_js8_capability(
        self,
        endpoint: JS8ApiEndpoint,
        *,
        timeout_s: float = 0.4,
        client: Optional[JS8ApiClient] = None,
        stop_client: bool = True,
    ) -> tuple[Dict[str, object], float]:
        started = time.monotonic()
        status = self._js8_capability_offline_status(endpoint)
        probe_client = client or JS8ApiClient(endpoint, timeout_s=float(timeout_s), auto_reconnect=False)
        try:
            if probe_client.start():
                snapshot = probe_client.probe_capabilities(timeout_s=float(timeout_s))
                status.update(
                    {
                        "connected": bool(snapshot.connected),
                        "mode": str(snapshot.mode or "offline"),
                        "version": str(snapshot.version or ""),
                        "supported": dict(snapshot.supported),
                        "errors": dict(snapshot.errors),
                        "last_error": probe_client.last_error,
                    }
                )
            else:
                status["last_error"] = probe_client.last_error or "JS8Call TCP API not reachable"
        except Exception as exc:
            status["last_error"] = str(exc or "JS8Call capability probe failed")
        finally:
            if client is None or stop_client:
                try:
                    probe_client.stop()
                except Exception:
                    pass
        return status, (time.monotonic() - started) * 1000.0

    def _js8_shadow_busy_state(self, ptt_active: Optional[bool], queue_depth: Optional[int]) -> Optional[bool]:
        if ptt_active is True:
            return True
        if isinstance(queue_depth, int) and queue_depth > 0:
            return True
        if ptt_active is False and queue_depth == 0:
            return False
        if ptt_active is None and queue_depth is None:
            return None
        return None

    @staticmethod
    def _js8_shadow_to_int(value: object) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except Exception:
            try:
                return int(str(value).strip())
            except Exception:
                return None

    @staticmethod
    def _js8_shadow_to_bool(value: object) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if value in (None, ""):
            return None
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "active", "ptt"}:
            return True
        if text in {"0", "false", "no", "off", "inactive", "idle"}:
            return False
        return None

    @staticmethod
    def _js8_shadow_error_text(value: object, *, limit: int = 512) -> str:
        try:
            return str(value or "")[: max(0, int(limit))]
        except Exception:
            return ""

    def _js8_shadow_native_status(
        self,
        *,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        force: bool = False,
        timeout_s: float = 0.4,
    ) -> Dict[str, object]:
        host = (host_override or "").strip() or self._settings_text("js8_host", JS8_DEFAULT_HOST) or JS8_DEFAULT_HOST
        port = int(port_override) if port_override is not None else self._settings_int("js8_port", JS8_DEFAULT_PORT)
        endpoint = JS8ApiEndpoint(host, port).normalized()
        cache_key = endpoint.key
        if self._is_loopback_host(endpoint.host) and not self.program_is_running("JS8Call"):
            type(self)._shared_js8_shadow_cache.pop(cache_key, None)
            offline = self._js8_capability_offline_status(endpoint, last_error="JS8Call is not running")
            offline.update(
                {
                    "busy": None,
                    "frequency_hz": None,
                    "offset_hz": None,
                    "ptt_active": None,
                    "queue_depth": None,
                    "checked_ts": time.time(),
                    "elapsed_ms": 0.0,
                }
            )
            return offline

        now = time.monotonic()
        cached = type(self)._shared_js8_shadow_cache.get(cache_key)
        if not force and cached:
            cached_ts, cached_status = cached
            if (now - float(cached_ts or 0.0)) < self._js8_shadow_cache_ttl_sec:
                return dict(cached_status)

        started_at = time.monotonic()
        capability_cache = type(self)._shared_js8_capability_cache.get(cache_key)
        capability: Dict[str, object] | None = None
        client: Optional[JS8ApiClient] = None
        if not force and capability_cache:
            capability_ts, capability_status = capability_cache
            connected = bool(capability_status.get("connected"))
            ttl = self._js8_capability_success_ttl_sec if connected else self._js8_capability_failure_ttl_sec
            if (now - float(capability_ts or 0.0)) < ttl:
                capability = dict(capability_status)
        if capability is None:
            health_key = self._health_key(("JS8CALL", endpoint.host.lower(), int(endpoint.port), "capability"))
            allowed, _health = self._health.may_run(health_key, owner="SoftwareStatusService", force=force)
            if not allowed and capability_cache:
                capability = dict(capability_cache[1])
            elif not allowed:
                capability = self._js8_capability_offline_status(
                    endpoint,
                    last_error="JS8Call API capability check is waiting for cooldown",
                )
            else:
                client = JS8ApiClient(endpoint, timeout_s=float(timeout_s), auto_reconnect=False)
                capability, capability_elapsed_ms = self._probe_js8_capability(
                    endpoint,
                    timeout_s=timeout_s,
                    client=client,
                    stop_client=False,
                )
                metadata = {
                    "capability_mode": capability.get("mode", "offline"),
                    "version": capability.get("version", ""),
                    "endpoint": capability.get("endpoint", ""),
                    "action": self._js8_capability_action(capability, running=True),
                }
                if bool(capability.get("connected")):
                    self._health.record_success(
                        health_key,
                        owner="SoftwareStatusService",
                        duration_ms=capability_elapsed_ms,
                        slow_ms=JS8_HEALTH_SLOW_MS,
                        metadata=metadata,
                    )
                else:
                    self._health.record_failure(
                        health_key,
                        owner="SoftwareStatusService",
                        error=str(capability.get("last_error") or "JS8Call TCP API not reachable"),
                        duration_ms=capability_elapsed_ms,
                        metadata=metadata,
                    )
                type(self)._shared_js8_capability_cache[cache_key] = (time.monotonic(), dict(capability))
        supported = dict(capability.get("supported") or {})
        errors: Dict[str, str] = dict(capability.get("errors") or {})
        native: Dict[str, object] = {
            "connected": bool(capability.get("connected")),
            "mode": str(capability.get("mode") or "offline"),
            "version": str(capability.get("version") or ""),
            "endpoint": self._format_endpoint(endpoint.host, endpoint.port),
            "supported": supported,
            "errors": errors,
            "last_error": str(capability.get("last_error") or ""),
            "frequency_hz": None,
            "offset_hz": None,
            "ptt_active": None,
            "queue_depth": None,
            "busy": None,
            "checked_ts": time.time(),
            "elapsed_ms": 0.0,
        }

        if native["connected"]:
            try:
                if client is None:
                    client = JS8ApiClient(endpoint, timeout_s=float(timeout_s), auto_reconnect=False)
                    client_started = client.start()
                else:
                    client_started = True
                if client_started:
                    if client is None:
                        raise RuntimeError("JS8 diagnostic client unavailable")
                    if supported.get("RIG.GET_FREQ"):
                        try:
                            response = client.request("RIG.GET_FREQ", expect_types=("RIG.FREQ", "STATION.STATUS"), timeout_s=timeout_s)
                            freq_value = response.params.get("DIAL")
                            if freq_value in (None, ""):
                                freq_value = response.params.get("FREQ")
                            offset_value = response.params.get("OFFSET")
                            if offset_value in (None, ""):
                                offset_value = response.params.get("OFFSET_HZ")
                            native["frequency_hz"] = self._js8_shadow_to_int(freq_value)
                            native["offset_hz"] = self._js8_shadow_to_int(offset_value)
                        except Exception as exc:
                            errors["RIG.GET_FREQ"] = self._js8_shadow_error_text(exc, limit=512)
                    if supported.get("RIG.GET_PTT"):
                        try:
                            response = client.request("RIG.GET_PTT", expect_types=("RIG.PTT_STATUS",), timeout_s=timeout_s)
                            ptt_value = response.params.get("PTT")
                            if ptt_value in (None, ""):
                                ptt_value = response.params.get("PTT_ACTIVE")
                            if ptt_value in (None, ""):
                                ptt_value = response.value
                            native["ptt_active"] = self._js8_shadow_to_bool(ptt_value)
                        except Exception as exc:
                            errors["RIG.GET_PTT"] = self._js8_shadow_error_text(exc, limit=512)
                    if supported.get("TX.GET_QUEUE_DEPTH"):
                        try:
                            response = client.request("TX.GET_QUEUE_DEPTH", expect_types=("TX.QUEUE_DEPTH",), timeout_s=timeout_s)
                            depth_value = response.params.get("DEPTH")
                            if depth_value in (None, ""):
                                depth_value = response.params.get("QUEUE_DEPTH")
                            if depth_value in (None, ""):
                                depth_value = response.value
                            native["queue_depth"] = self._js8_shadow_to_int(depth_value)
                        except Exception as exc:
                            errors["TX.GET_QUEUE_DEPTH"] = self._js8_shadow_error_text(exc, limit=512)
            finally:
                if client is not None:
                    client.stop()

        native["busy"] = self._js8_shadow_busy_state(native.get("ptt_active"), native.get("queue_depth"))
        native["elapsed_ms"] = round((time.monotonic() - started_at) * 1000.0, 1)
        if not (self._is_loopback_host(endpoint.host) and not bool(native.get("connected"))):
            type(self)._shared_js8_shadow_cache[cache_key] = (now, dict(native))
        return native

    def js8_shadow_comparison_status(
        self,
        *,
        legacy_readings: Optional[Mapping[str, object]] = None,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        force: bool = False,
        timeout_s: float = 0.4,
    ) -> Dict[str, object]:
        native = self._js8_shadow_native_status(
            port_override=port_override,
            host_override=host_override,
            force=force,
            timeout_s=timeout_s,
        )

        def _coerce_int(value: object) -> Optional[int]:
            return self._js8_shadow_to_int(value)

        def _coerce_bool(value: object) -> Optional[bool]:
            return self._js8_shadow_to_bool(value)

        legacy: Dict[str, object] = {
            "busy": None,
            "frequency_hz": None,
            "offset_hz": None,
        }
        if isinstance(legacy_readings, Mapping):
            legacy["busy"] = _coerce_bool(legacy_readings.get("busy"))
            legacy["frequency_hz"] = _coerce_int(legacy_readings.get("frequency_hz"))
            legacy["offset_hz"] = _coerce_int(legacy_readings.get("offset_hz"))

        comparisons: Dict[str, Dict[str, object]] = {}
        differences: Dict[str, Dict[str, object]] = {}

        legacy_busy = legacy.get("busy")
        native_busy = native.get("busy")
        if legacy_busy is not None or native_busy is not None:
            match = None
            if legacy_busy is not None and native_busy is not None:
                match = bool(bool(legacy_busy) == bool(native_busy))
            comparisons["busy"] = {"legacy": legacy_busy, "native": native_busy, "match": match}
            if legacy_busy is not None and native_busy is not None and bool(legacy_busy) != bool(native_busy):
                differences["busy"] = {"legacy": legacy_busy, "native": native_busy}

        legacy_freq = legacy.get("frequency_hz")
        native_freq = native.get("frequency_hz")
        if legacy_freq is not None or native_freq is not None:
            match = None
            if legacy_freq is not None and native_freq is not None:
                match = bool(int(legacy_freq) == int(native_freq))
            comparisons["frequency_hz"] = {"legacy": legacy_freq, "native": native_freq, "match": match}
            if legacy_freq is not None and native_freq is not None and int(legacy_freq) != int(native_freq):
                differences["frequency_hz"] = {"legacy": legacy_freq, "native": native_freq}

        legacy_offset = legacy.get("offset_hz")
        native_offset = native.get("offset_hz")
        if legacy_offset is not None or native_offset is not None:
            match = None
            if legacy_offset is not None and native_offset is not None:
                match = bool(int(legacy_offset) == int(native_offset))
            comparisons["offset_hz"] = {"legacy": legacy_offset, "native": native_offset, "match": match}
            if legacy_offset is not None and native_offset is not None and int(legacy_offset) != int(native_offset):
                differences["offset_hz"] = {"legacy": legacy_offset, "native": native_offset}

        result: Dict[str, object] = {
            "connected": bool(native.get("connected")),
            "mode": str(native.get("mode", "offline") or "offline"),
            "version": str(native.get("version", "") or ""),
            "endpoint": str(native.get("endpoint") or self._format_endpoint(host_override or self._settings_text("js8_host", JS8_DEFAULT_HOST) or JS8_DEFAULT_HOST, int(port_override) if port_override is not None else self._settings_int("js8_port", JS8_DEFAULT_PORT))),
            "legacy": legacy,
            "native": native,
            "comparisons": comparisons,
            "differences": differences,
            "errors": dict(native.get("errors") or {}),
            "last_error": str(native.get("last_error") or ""),
            "checked_ts": float(native.get("checked_ts") or time.time()),
            "elapsed_ms": float(native.get("elapsed_ms") or 0.0),
        }

        if differences:
            log.info(
                "JS8_SHADOW|endpoint=%s|mode=%s|version=%s|legacy_busy=%s|native_busy=%s|legacy_freq=%s|native_freq=%s|legacy_offset=%s|native_offset=%s|native_ptt=%s|native_queue_depth=%s|diffs=%s",
                result["endpoint"],
                result["mode"],
                result["version"],
                legacy.get("busy"),
                native.get("busy"),
                legacy.get("frequency_hz"),
                native.get("frequency_hz"),
                legacy.get("offset_hz"),
                native.get("offset_hz"),
                native.get("ptt_active"),
                native.get("queue_depth"),
                ",".join(f"{key}:{value['legacy']}->{value['native']}" for key, value in differences.items()),
            )
        else:
            log.debug(
                "JS8_SHADOW|endpoint=%s|mode=%s|version=%s|legacy_busy=%s|native_busy=%s|legacy_freq=%s|native_freq=%s|legacy_offset=%s|native_offset=%s|native_ptt=%s|native_queue_depth=%s|elapsed_ms=%.1f",
                result["endpoint"],
                result["mode"],
                result["version"],
                legacy.get("busy"),
                native.get("busy"),
                legacy.get("frequency_hz"),
                native.get("frequency_hz"),
                legacy.get("offset_hz"),
                native.get("offset_hz"),
                native.get("ptt_active"),
                native.get("queue_depth"),
                float(result["elapsed_ms"]),
            )
        return result

    def _js8_capability_offline_status(
        self,
        endpoint: JS8ApiEndpoint,
        *,
        last_error: str = "",
    ) -> Dict[str, object]:
        normalized = endpoint.normalized()
        return {
            "connected": False,
            "mode": "offline",
            "version": "",
            "endpoint": self._format_endpoint(normalized.host, normalized.port),
            "supported": {},
            "errors": {},
            "last_error": str(last_error or ""),
        }

    @staticmethod
    def _js8_capability_action(status: Dict[str, object], *, running: bool) -> str:
        endpoint = str(status.get("endpoint", "") or "").strip()
        version = str(status.get("version", "") or "").strip()
        mode = str(status.get("mode", "offline") or "offline").strip()
        version_part = f" Version: {version}." if version else ""
        if mode == "api_full":
            return f"JS8Call API is ready for native CommsPlan diagnostics at {endpoint}.{version_part}"
        if mode == "api_basic":
            return f"JS8Call API is reachable at {endpoint}; CommsPlan will use basic API features and keep fallbacks available.{version_part}"
        if mode == "file_fallback":
            return f"JS8Call is reachable at {endpoint}, but native API support is limited; CommsPlan will keep using log/database fallbacks.{version_part}"
        if running:
            return f"JS8Call appears to be running, but CommsPlan could not verify the TCP API at {endpoint}."
        return "JS8Call is not running; no JS8 API check is needed right now."

    def flrig_api_reachable(
        self,
        *,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        host = (host_override or "").strip() or self._settings_text("flrig_host", FLRIG_DEFAULT_HOST) or FLRIG_DEFAULT_HOST
        port = int(port_override) if port_override is not None else self._settings_int("flrig_port", FLRIG_DEFAULT_PORT)
        cache_key = ("FLRIG", host.strip().lower(), str(int(port)))

        def _probe() -> bool:
            from freqinout.radio_interface.rigctl_client import FLRigClient

            client = FLRigClient(host=host, port=port, timeout=0.35)
            return bool(client.is_available())

        return self._cached_service_probe(cache_key, force=force, probe=_probe)

    def fldigi_api_reachable(
        self,
        *,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        flrig_port_override: Optional[int] = None,
        flrig_host_override: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        host = self._resolved_fldigi_host(host_override)
        port = int(port_override) if port_override is not None else self._settings_int("fldigi_port", FLDIGI_DEFAULT_PORT)
        flrig_host = (flrig_host_override or "").strip() or self._settings_text("flrig_host", FLRIG_DEFAULT_HOST) or FLRIG_DEFAULT_HOST
        flrig_port = (
            int(flrig_port_override)
            if flrig_port_override is not None
            else self._settings_int("flrig_port", FLRIG_DEFAULT_PORT)
        )
        cache_key = (
            "FLDIGI",
            host.strip().lower(),
            str(int(port)),
            flrig_host.strip().lower(),
            str(int(flrig_port)),
        )

        def _probe() -> bool:
            from freqinout.radio_interface.rigctl_client import FLRigClient

            client = FLRigClient(
                host=flrig_host,
                port=flrig_port,
                fldigi_host=host,
                fldigi_port=port,
                timeout=0.35,
            )
            return bool(client.is_fldigi_available())

        return self._cached_service_probe(cache_key, force=force, probe=_probe)

    def rigctld_api_reachable(
        self,
        *,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        host = (host_override or "").strip() or self._settings_text("rig_host", RIGCTLD_DEFAULT_HOST) or RIGCTLD_DEFAULT_HOST
        port = int(port_override) if port_override is not None else self._settings_int("rig_port", RIGCTLD_DEFAULT_PORT)
        cache_key = ("RIGCTLD", host.strip().lower(), str(int(port)))

        def _probe() -> bool:
            from freqinout.radio_interface.rigctl_client import RigctldClient

            client = RigctldClient(host=host, port=port, timeout=0.35)
            return bool(client.is_available())

        return self._cached_service_probe(cache_key, force=force, probe=_probe)

    def tcp_endpoint_reachable(
        self,
        *,
        service_name: str,
        host: str,
        port: int,
        force: bool = False,
    ) -> bool:
        host_value = str(host or "").strip()
        port_value = int(port or 0)
        if not host_value or port_value <= 0:
            return False
        cache_key = (str(service_name or "TCP").strip().upper() or "TCP", host_value.lower(), str(port_value))

        def _probe() -> bool:
            with socket.create_connection((host_value, port_value), timeout=0.35):
                return True

        return self._cached_service_probe(cache_key, force=force, probe=_probe)

    def _endpoint_status(
        self,
        *,
        endpoint_label: str,
        host: str,
        port: int,
        reachable: bool,
        process_running: bool,
        health: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        endpoint = self._format_endpoint(host, port)
        degraded = bool((health or {}).get("degraded"))
        cooldown = float((health or {}).get("cooldown_remaining_sec") or 0.0)
        reason = str((health or {}).get("last_error") or "").strip()
        suffix = ""
        if degraded and cooldown > 0:
            suffix = f" CommsPlan is backing off for {cooldown:.0f}s to keep the UI responsive."
        elif degraded and reason:
            suffix = f" Last issue: {reason}."
        if reachable:
            return {
                "state": "ok",
                "tooltip": f"Configured {endpoint_label} reachable at {endpoint}{suffix}",
                "running": True,
                "reachable": True,
                "endpoint": endpoint,
                "degraded": degraded,
                "stale": bool(cooldown > 0),
                "health": health or {},
            }
        if process_running:
            return {
                "state": "warn",
                "tooltip": (
                    f"Process running, configured {endpoint_label} unreachable at {endpoint} "
                    f"(possible instance/port mismatch).{suffix}"
                ),
                "running": True,
                "reachable": False,
                "endpoint": endpoint,
                "degraded": degraded,
                "stale": bool(cooldown > 0),
                "health": health or {},
            }
        tooltip = (
            f"Not running at configured {endpoint_label} {endpoint}"
            if self._is_loopback_host(host)
            else f"Configured {endpoint_label} unreachable at {endpoint}"
        )
        if suffix:
            tooltip = f"{tooltip}.{suffix}"
        return {
            "state": "idle",
            "tooltip": tooltip,
            "running": False,
            "reachable": False,
            "endpoint": endpoint,
            "degraded": degraded,
            "stale": bool(cooldown > 0),
            "health": health or {},
        }

    def generic_endpoint_status(
        self,
        *,
        service_name: str,
        endpoint_label: str,
        host: str,
        port: int,
        force: bool = False,
    ) -> Dict[str, object]:
        host_value = str(host or "").strip()
        port_value = int(port or 0)
        if not host_value or port_value <= 0:
            return {
                "state": "idle",
                "tooltip": f"{endpoint_label} not configured",
                "running": False,
                "reachable": False,
                "endpoint": "",
            }
        reachable = self.tcp_endpoint_reachable(
            service_name=service_name,
            host=host_value,
            port=port_value,
            force=force,
        )
        return self._endpoint_status(
            endpoint_label=endpoint_label,
            host=host_value,
            port=port_value,
            reachable=reachable,
            process_running=False,
            health=self._health_snapshot_for(
                (str(service_name or "TCP").strip().upper() or "TCP", host_value.lower(), str(port_value))
            ),
        )

    def status_snapshot(
        self,
        *,
        force: bool = False,
        force_process_snapshot: bool = True,
        port_override: Optional[int] = None,
        host_override: Optional[str] = None,
        flrig_port_override: Optional[int] = None,
        flrig_host_override: Optional[str] = None,
        rigctld_port_override: Optional[int] = None,
        rigctld_host_override: Optional[str] = None,
        fldigi_port_override: Optional[int] = None,
        fldigi_host_override: Optional[str] = None,
        instance_identities: Optional[Mapping[str, Mapping[str, object]]] = None,
        verify_control_readback: bool = False,
        control_backend: str = "",
        control_probe_only: bool = False,
    ) -> Dict[str, Dict[str, object]]:
        identities = instance_identities if isinstance(instance_identities, Mapping) else {}
        process_observations = {}
        active_control_via = (
            str(control_backend or "").strip().upper()
            or self._settings_text("control_via", "FLRig").strip().upper()
        )
        exact_control_probe = bool(control_probe_only and verify_control_readback)

        def _running(program_name: str) -> bool:
            identity = identities.get(program_name, {})
            if isinstance(identity, Mapping):
                target = str(identity.get("target") or "").strip()
                arguments = identity.get("arguments", ())
                if not isinstance(arguments, (list, tuple)):
                    arguments = ()
                if target or arguments:
                    process_observations[program_name] = self.cached_instance_process_state(
                        program_name,target,arguments,
                        excluded_options=('-r','--rig-name') if program_name == 'JS8Call' and not arguments else ())
                    excluded_options: tuple[str, ...] = ()
                    if program_name == "JS8Call" and not arguments:
                        # The native default JS8Call namespace is selected by
                        # the absence of a rig-name option. A named sibling on
                        # another API port is not evidence that this identity
                        # is running.
                        excluded_options = ("-r", "--rig-name")
                    if excluded_options:
                        return self.program_instance_running(
                            program_name,
                            target,
                            arguments,
                            excluded_options=excluded_options,
                        )
                    return self.program_instance_running(
                        program_name,
                        target,
                        arguments,
                    )
            return self.program_is_running(program_name)

        if force and force_process_snapshot:
            self._refresh_process_snapshot(force=True)
        running_js8 = _running("JS8Call")
        js8_host = (host_override or "").strip() or self._settings_text("js8_host", JS8_DEFAULT_HOST) or JS8_DEFAULT_HOST
        js8_port = int(port_override) if port_override is not None else self._settings_int("js8_port", JS8_DEFAULT_PORT)
        js8_cache_host = str(js8_host or "").strip().lower()
        js8_health = self._health.snapshot(
            self._health_key(("JS8CALL", js8_cache_host or "loopback", int(js8_port), False))
        )
        js8_api_ok = (
            self.js8_api_reachable(
                port_override=port_override,
                host_override=host_override,
                allow_fallback=False,
                force=force,
            )
            if not exact_control_probe or active_control_via == "JS8CALL"
            else False
        )
        running_flrig = _running("FLRig")
        flrig_host = (flrig_host_override or "").strip() or self._settings_text("flrig_host", FLRIG_DEFAULT_HOST) or FLRIG_DEFAULT_HOST
        flrig_port = (
            int(flrig_port_override)
            if flrig_port_override is not None
            else self._settings_int("flrig_port", FLRIG_DEFAULT_PORT)
        )
        flrig_key = ("FLRIG", flrig_host.strip().lower(), str(int(flrig_port)))
        flrig_api_ok = (
            self.flrig_api_reachable(
                port_override=flrig_port_override,
                host_override=flrig_host_override,
                force=force,
            )
            if not exact_control_probe or active_control_via == "FLRIG"
            else False
        )
        rigctld_active = active_control_via == "RIGCTLD" or rigctld_host_override is not None or rigctld_port_override is not None
        running_rigctld = _running("RigCtlD") if rigctld_active else False
        rigctld_host = (
            (rigctld_host_override or "").strip()
            or self._settings_text("rig_host", RIGCTLD_DEFAULT_HOST)
            or RIGCTLD_DEFAULT_HOST
        )
        rigctld_port = (
            int(rigctld_port_override)
            if rigctld_port_override is not None
            else self._settings_int("rig_port", RIGCTLD_DEFAULT_PORT)
        )
        rigctld_key = ("RIGCTLD", rigctld_host.strip().lower(), str(int(rigctld_port)))
        rigctld_api_ok = (
            self.rigctld_api_reachable(
                port_override=rigctld_port_override,
                host_override=rigctld_host_override,
                force=force,
            )
            if rigctld_active
            else False
        )
        running_fldigi = _running("FLDigi")
        fldigi_host = self._resolved_fldigi_host(fldigi_host_override)
        fldigi_port = (
            int(fldigi_port_override)
            if fldigi_port_override is not None
            else self._settings_int("fldigi_port", FLDIGI_DEFAULT_PORT)
        )
        fldigi_key = (
            "FLDIGI",
            fldigi_host.strip().lower(),
            str(int(fldigi_port)),
            flrig_host.strip().lower(),
            str(int(flrig_port)),
        )
        fldigi_api_ok = (
            self.fldigi_api_reachable(
                port_override=fldigi_port_override,
                host_override=fldigi_host_override,
                flrig_port_override=flrig_port_override,
                flrig_host_override=flrig_host_override,
                force=force,
            )
            if not exact_control_probe
            else False
        )

        out: Dict[str, Dict[str, object]] = {}
        out["JS8Call_API"] = self._endpoint_status(
            endpoint_label="TCP API",
            host=js8_host,
            port=js8_port,
            reachable=js8_api_ok,
            process_running=running_js8,
            health=js8_health,
        )

        js8_identity = identities.get('JS8Call') or {}
        local_expected = js8_identity.get('local_expected', self._is_loopback_host(js8_host))
        if isinstance(local_expected, str):
            local_expected = local_expected.lower() not in {'false','0'}
        process_state = process_observations.get('JS8Call', {}).get('running')
        if not js8_identity:
            # Family discovery cannot establish this endpoint's native profile.
            process_state = None
        conflict = bool(js8_api_ok and process_state is False and local_expected)
        evidence = out['JS8Call_API']
        evidence.update(process_running=process_state, local_expected=bool(local_expected),
                        identity_conflict=conflict, value='needs_review' if conflict else 'connected' if js8_api_ok else 'running' if process_state is True else 'unknown')
        if conflict:
            evidence.update(state='warn',tooltip='Status needs review: configured API is connected but the expected local profile was not observed.')
        elif js8_api_ok:
            evidence['tooltip'] = ('Started elsewhere · Connected' if not local_expected else 'Connected · local process/profile unverified' if process_state is None else 'Running · Connected') + f' · {self._format_endpoint(js8_host,js8_port)}'
        elif process_state is None and not running_js8:
            evidence.update(state='unknown',tooltip='Could not verify the configured process/profile; API is not connected.')
        with type(self)._shared_proc_lock:
            key = (str(js8_host).lower(),int(js8_port))
            type(self)._shared_instance_evidence[key] = (time.monotonic(),dict(evidence))
            if len(type(self)._shared_instance_evidence) > 64:
                oldest = min(type(self)._shared_instance_evidence,key=lambda entry:type(self)._shared_instance_evidence[entry][0])
                type(self)._shared_instance_evidence.pop(oldest,None)

        for key in STATUS_KEYS:
            if key == "JS8Call_API":
                continue
            if key == "FLRig":
                out[key] = self._endpoint_status(
                    endpoint_label="XML-RPC",
                    host=flrig_host,
                    port=flrig_port,
                    reachable=flrig_api_ok,
                    process_running=running_flrig,
                    health=self._health_snapshot_for(flrig_key),
                )
                continue
            if key == "RigCtlD":
                if not rigctld_active:
                    out[key] = {
                        "state": "idle",
                        "tooltip": "Inactive backend",
                        "running": False,
                        "reachable": False,
                    }
                else:
                    out[key] = self._endpoint_status(
                        endpoint_label="TCP",
                        host=rigctld_host,
                        port=rigctld_port,
                        reachable=rigctld_api_ok,
                        process_running=running_rigctld,
                        health=self._health_snapshot_for(rigctld_key),
                    )
                continue
            if key == "FLDigi":
                out[key] = self._endpoint_status(
                    endpoint_label="XML-RPC",
                    host=fldigi_host,
                    port=fldigi_port,
                    reachable=fldigi_api_ok,
                    process_running=running_fldigi,
                    health=self._health_snapshot_for(fldigi_key),
                )
                continue
            if key == 'VarAC' and identities.get('VarAC'):
                identity = identities['VarAC']
                target = identity.get('process_executable') or identity.get('target') or ''
                arguments = identity.get('arguments') or ()
                if self._basename_token(target) != 'varac.exe':
                    target = next((str(arg) for arg in arguments if self._basename_token(arg) == 'varac.exe'), target)
                observed = self.cached_varac_process_state(str(target),
                    str(identity.get('process_ini') or ''), str(identity.get('wine_prefix') or ''))
                running = observed['running']
                out[key] = {**observed, 'state':'ok' if running is True else 'unknown' if running is None else 'idle',
                            'tooltip': observed['reason'], 'process_running':running,
                            'value':'running' if running is True else 'unknown' if running is None else 'not_running'}
                continue
            running = _running(key)
            tooltip = "Running" if running else "Not running"
            if key == "VarAC" and running:
                exe = self.find_process_exe("VarAC")
                if exe:
                    tooltip = f"Running: {exe}"
            out[key] = {
                "state": "ok" if running else "idle",
                "tooltip": tooltip,
                "running": bool(running),
            }

        # Custom daemon names are labels, not executable names. Native
        # rigctld commands provide explicit per-radio process and endpoint
        # identity without interpreting arbitrary launcher scripts.
        for name, identity in identities.items():
            if name in out or not isinstance(identity, Mapping):
                continue
            command = str(identity.get("target") or "")
            extra_arguments = identity.get("arguments", ())
            if isinstance(extra_arguments, (list, tuple)) and extra_arguments:
                command += " " + shlex.join(tuple(str(value) for value in extra_arguments))
            daemon = parse_custom_rigctld(command)
            if daemon is None:
                continue
            from freqinout.radio_interface.rigctl_client import RigctldClient
            running = self.program_instance_running(
                "RigCtlD", daemon.executable, daemon.arguments,
            )
            frequency = RigctldClient(
                host=daemon.host, port=daemon.port, timeout=0.8,
            ).get_vfo_frequency()
            ready = isinstance(frequency, (int, float)) and frequency > 0
            out[name] = {
                "running": running,
                "reachable": bool(running and ready),
                "radio_readback_ready": bool(running and ready),
                "endpoint_occupied": bool(ready),
                "state": "ok" if running and ready else "warning" if running else "idle",
                "frequency_hz": frequency if ready else None,
                "tooltip": f"rigctld {daemon.host}:{daemon.port}: " + (
                    "radio frequency verified" if running and ready else
                    "process running, radio readback unavailable" if running else
                    "configured daemon not running"
                ),
            }

        if verify_control_readback:
            readback_key = {
                "FLRIG": "FLRig",
                "RIGCTLD": "RigCtlD",
                "HAMLIB": "RigCtlD",
                "JS8CALL": "JS8Call_API",
            }.get(active_control_via, "")
            frequency_hz: Optional[int] = None
            transceiver_name: Optional[str] = None
            try:
                if readback_key == "FLRig" and flrig_api_ok:
                    from freqinout.radio_interface.rigctl_client import FLRigClient

                    flrig_client = FLRigClient(
                        host=flrig_host,
                        port=flrig_port,
                        timeout=1.5,
                    )
                    transceiver_name, frequency_hz = flrig_client.get_launch_readback()
                elif readback_key == "RigCtlD" and rigctld_api_ok:
                    from freqinout.radio_interface.rigctl_client import RigctldClient

                    frequency_hz = RigctldClient(
                        host=rigctld_host,
                        port=rigctld_port,
                        timeout=0.8,
                    ).get_vfo_frequency()
                elif readback_key == "JS8Call_API" and js8_api_ok:
                    from freqinout.radio_interface.js8_status import JS8ControlClient

                    frequency_hz = JS8ControlClient(
                        host=js8_host,
                        port=js8_port,
                        settings=self.settings,
                    ).get_frequency()
            except Exception as exc:
                log.debug(
                    "Radio-control launch readback failed for %s: %s",
                    active_control_via or "unknown",
                    exc,
                )
                frequency_hz = None
            try:
                normalized_frequency_hz = int(frequency_hz) if frequency_hz is not None else None
            except (TypeError, ValueError):
                normalized_frequency_hz = None
            frequency_ready = bool(
                normalized_frequency_hz is not None and normalized_frequency_hz > 0
            )
            ready = bool(
                frequency_ready
                and (readback_key != "FLRig" or transceiver_name)
            )
            if readback_key and readback_key in out:
                row = out[readback_key]
                row["radio_readback_ready"] = ready
                row["frequency_hz"] = normalized_frequency_hz if ready else None
                row["control_backend"] = active_control_via.lower()
                if readback_key == "FLRig":
                    row["radio_online"] = bool(transceiver_name)
                    row["transceiver_name"] = transceiver_name or ""
                if bool(row.get("reachable")) and not ready:
                    reason = (
                        "FLRig XML-RPC is ready, but no online transceiver is reported."
                        if readback_key == "FLRig" and not transceiver_name
                        else (
                            f"FLRig reports {transceiver_name}, but radio frequency readback is unavailable."
                            if readback_key == "FLRig"
                            else "Radio frequency readback is unavailable."
                        )
                    )
                    row["tooltip"] = (
                        str(row.get("tooltip", "") or "").rstrip(". ")
                        + f". {reason}"
                    )
        return out
