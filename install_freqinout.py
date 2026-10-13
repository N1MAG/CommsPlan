
"""Prepare and verify a CommsPlan source installation before first launch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 13)
MIN_FREE_BYTES = 512 * 1024 * 1024
REQUIRED_PROJECT_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "freqinout/__init__.py",
    "freqinout/main.py",
    "freqinout/version.py",
)
INSTALL_RECEIPT = ".freqinout-install-verified.json"
BACKUP_RENAME_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0)


class InstallationError(RuntimeError):
    """A safe, operator-actionable installation failure."""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _venv_python(venv: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
    subprocess.check_call(command, env=env, cwd=cwd)


def _python_is_usable(python: Path) -> bool:
    if not python.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(python), "-c", "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _unique_sibling(path: Path, suffix: str) -> Path:
    candidate = path.with_name(f"{path.name}.{suffix}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{suffix}-{index}")
        index += 1
    return candidate


def _ensure_virtual_environment(root: Path) -> tuple[Path, Path | None]:
    venv = root / ".venv"
    python = _venv_python(venv)
    quarantined: Path | None = None
    if venv.exists() and not _python_is_usable(python):
        quarantined = _unique_sibling(venv, f"incomplete-{_timestamp()}")
        print(f"Incomplete virtual environment found. Preserving it as: {quarantined.name}")
        venv.rename(quarantined)
    if not venv.exists():
        print("Creating a clean virtual environment...")
        _run([sys.executable, "-m", "venv", str(venv)])
    python = _venv_python(venv)
    if not _python_is_usable(python):
        raise InstallationError(f"The virtual environment was created, but {python} is not usable.")
    return python, quarantined


def _ensure_pip(python: Path) -> None:
    check = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode == 0:
        return
    print("The virtual environment is missing pip. Repairing it...")
    _run([str(python), "-m", "ensurepip", "--upgrade"])
    repaired = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if repaired.returncode != 0:
        raise InstallationError(f"pip could not be repaired in {python.parent.parent}.")


def _default_profile_root() -> Path:
    configured = str(os.environ.get("FREQINOUT_CONFIG_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    receipt = Path(__file__).resolve().parent / ".freqinout-install-verified.json"
    if receipt.exists():
        try:
            root = json.loads(receipt.read_text()).get("profile_root")
            if not isinstance(root, str) or not Path(root).is_absolute():
                raise ValueError("profile_root must be an absolute path")
            return Path(root)
        except (OSError, ValueError, TypeError) as exc:
            raise InstallationError("Cannot read the remembered profile; set FREQINOUT_CONFIG_DIR explicitly.") from exc
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "FreqInOut"
    return Path.home() / ".freqinout"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        ctypes.windll.kernel32.CloseHandle(process)
        return True
    except Exception:
        return True


def _active_fio_lock(profile_root: Path) -> bool:
    lock_path = profile_root / "freqinout.lock"
    if not lock_path.exists():
        return False
    try:
        first_line = lock_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        pid = int(first_line.strip())
    except (OSError, ValueError, IndexError):
        raise InstallationError(
            f"The application lock file could not be validated: {lock_path}\n"
            "Close FIO/CommsPlan. If it is already closed, restart the computer before retrying."
        )
    return _pid_is_running(pid)


def _sqlite_quick_check(path: Path) -> None:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
            rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    except sqlite3.Error as exc:
        raise InstallationError(
            f"Database validation failed for {path}: {exc}\n"
            "The installer made no changes. Keep this database and contact CommsPlan support before upgrading."
        ) from exc
    if rows != ["ok"]:
        detail = "; ".join(rows[:5]) or "unknown integrity error"
        raise InstallationError(
            f"Database validation failed for {path}: {detail}\n"
            "The installer made no changes. Keep this database and contact CommsPlan support before upgrading."
        )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _hash_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _existing_station_database(profile_root: Path) -> Path | None:
    path = profile_root / "config" / "freqinout.db"
    return path if path.is_file() and path.stat().st_size > 0 else None


def _finalize_verified_profile_backup(temporary_dir: Path, final_dir: Path) -> Path:
    """Give a verified backup its preferred name without discarding valid data.

    Windows security/indexing software can briefly or persistently deny a
    same-directory rename even though FIO created and verified every file. The
    friendly name is presentation only: after bounded retries, retain the
    verified staging directory and let the installation receipt record its
    actual path. Other filesystem errors remain fatal.
    """

    last_error: PermissionError | None = None
    for attempt in range(len(BACKUP_RENAME_RETRY_DELAYS) + 1):
        try:
            temporary_dir.rename(final_dir)
            return final_dir
        except PermissionError as exc:
            last_error = exc
            if final_dir.is_dir() and not temporary_dir.exists():
                return final_dir
            if attempt < len(BACKUP_RENAME_RETRY_DELAYS):
                time.sleep(BACKUP_RENAME_RETRY_DELAYS[attempt])

    if (
        not temporary_dir.is_dir()
        or not (temporary_dir / "config").is_dir()
        or not (temporary_dir / "manifest.json").is_file()
    ):
        if last_error is not None:
            raise last_error
        raise InstallationError("The verified station backup is no longer available.")
    print(
        "Backup naming warning: the operating system kept the verified backup "
        "under its temporary folder name. Its contents and databases passed "
        "verification, so installation can continue."
    )
    return temporary_dir


def _create_verified_profile_backup(profile_root: Path) -> Path | None:
    if (profile_root / ".commsplan-copy-incomplete").exists():
        raise InstallationError("Profile copy is incomplete; use the original profile. Installation selection was not changed.")
    station_db = _existing_station_database(profile_root)
    if station_db is None:
        print(f"New installation detected. No existing FIO/CommsPlan station profile was found at {profile_root}.")
        return None
    if _active_fio_lock(profile_root):
        raise InstallationError(
            "FIO/CommsPlan is still running. Close FIO/CommsPlan and its companion radio applications, then rerun this installer."
        )

    config_dir = profile_root / "config"
    database_paths = tuple(sorted(config_dir.glob("*.db")))
    if not database_paths:
        raise InstallationError(f"An existing station was detected, but no databases were found in {config_dir}.")
    print(f"Existing station detected at: {profile_root}")
    print("Checking station databases before backup...")
    for path in database_paths:
        _sqlite_quick_check(path)

    config_bytes = sum(path.stat().st_size for path in config_dir.rglob("*") if path.is_file())
    backup_free_bytes = shutil.disk_usage(profile_root).free
    backup_required_bytes = config_bytes + (64 * 1024 * 1024)
    if backup_free_bytes < backup_required_bytes:
        raise InstallationError(
            "There is not enough free space to create and verify the station backup. "
            f"Required: {backup_required_bytes // (1024 * 1024)} MB; "
            f"available: {backup_free_bytes // (1024 * 1024)} MB at {profile_root}."
        )

    backup_root = profile_root / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    final_dir = backup_root / f"pre-install-{stamp}"
    index = 2
    while final_dir.exists():
        final_dir = backup_root / f"pre-install-{stamp}-{index}"
        index += 1
    temporary_dir = Path(tempfile.mkdtemp(prefix=".pre-install-", dir=backup_root))
    try:
        copied_config = temporary_dir / "config"
        shutil.copytree(config_dir, copied_config)
        source_hashes = _file_hashes(config_dir)
        backup_hashes = _file_hashes(copied_config)
        if source_hashes != backup_hashes:
            raise InstallationError("The station backup did not match the original files.")
        for path in sorted(copied_config.glob("*.db")):
            _sqlite_quick_check(path)
        manifest = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "verification_status": "verified",
            "profile_root": str(profile_root),
            "source_config": str(config_dir),
            "files": source_hashes,
            "database_quick_check": {path.name: "ok" for path in database_paths},
        }
        (temporary_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        final_dir = _finalize_verified_profile_backup(temporary_dir, final_dir)
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    print(f"Verified pre-install station backup: {final_dir}")
    return final_dir


def _project_version(root: Path) -> str:
    source = (root / "freqinout" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if not match:
        raise InstallationError("CommsPlan version metadata is missing or invalid.")
    return match.group(1)


def _validate_project(root: Path) -> str:
    missing = [relative for relative in REQUIRED_PROJECT_FILES if not (root / relative).is_file()]
    if missing:
        raise InstallationError("The CommsPlan release is incomplete. Missing: " + ", ".join(missing))
    free_bytes = shutil.disk_usage(root).free
    if free_bytes < MIN_FREE_BYTES:
        raise InstallationError(
            f"At least 512 MB of free disk space is required; only {free_bytes // (1024 * 1024)} MB is available."
        )
    return _project_version(root)


def _verify_runtime(python: Path, root: Path) -> None:
    print("Running an isolated installation check...")
    with tempfile.TemporaryDirectory(prefix="freqinout-install-check-") as profile:
        env = os.environ.copy()
        env["FREQINOUT_CONFIG_DIR"] = profile
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        script = (
            "from freqinout.core import db_initializer; "
            "from freqinout.core.radio_catalog import load_radio_catalog; "
            "from freqinout.version import __version__; "
            "import bleak, freqinout.main, meshcore, meshtastic, PySide6; "
            "db_initializer.ensure_all_tables(); "
            "assert load_radio_catalog().get('entries'); "
            "print(__version__)"
        )
        _run([str(python), "-c", script], env=env, cwd=root)
    for launcher in ("start-freqinout.sh", "start-freqinout.cmd", "start-commsplan.sh", "start-commsplan.cmd"):
        if not (root / launcher).is_file():
            raise InstallationError(f"Required launcher is missing: {launcher}")
    if os.name != "nt":
        for launcher in ("start-freqinout.sh", "start-multi-rig.sh", "start-commsplan.sh"):
            (root / launcher).chmod((root / launcher).stat().st_mode | 0o111)


def _write_install_receipt(root: Path, python: Path, version: str, backup: Path | None) -> None:
    receipt = {
        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": version,
        "python": str(python.resolve()),
        "requirements_sha256": _hash_file(root / "requirements.txt"),
        "profile_root": str(_default_profile_root()),
        "pre_install_backup": str(backup) if backup is not None else "",
    }
    destination = root / INSTALL_RECEIPT
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(destination)


def install() -> None:
    if not (MIN_PYTHON <= sys.version_info[:2] <= MAX_PYTHON):
        raise InstallationError(
            "CommsPlan requires Python 3.10 through 3.13; found "
            f"{sys.version_info.major}.{sys.version_info.minor}."
        )
    root = Path(__file__).resolve().parent
    version = _validate_project(root)
    print(f"Preparing CommsPlan {version} from: {root}")
    backup = _create_verified_profile_backup(_default_profile_root())
    python, quarantined = _ensure_virtual_environment(root)
    _ensure_pip(python)
    requirements = root / "requirements.txt"
    print("Installing CommsPlan requirements...")
    _run([str(python), "-m", "pip", "install", "-r", str(requirements)])
    _verify_runtime(python, root)
    _write_install_receipt(root, python, version, backup)
    if sys.platform.startswith("win"):
        run_hint = r".\start-commsplan.cmd"
    else:
        run_hint = "./start-commsplan.sh"
    if quarantined is not None:
        print(f"The unusable prior environment was retained at: {quarantined}")
    print(f"Installation verified. Run: {run_hint}")


def main() -> None:
    try:
        install()
    except (InstallationError, OSError, subprocess.SubprocessError) as exc:
        print("", file=sys.stderr)
        print("Installation failed. Do not launch CommsPlan.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
