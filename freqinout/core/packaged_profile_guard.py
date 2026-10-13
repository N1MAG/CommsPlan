"""Backup an existing station before a frozen release performs schema writes.

The caller must hold the ordinary station QLockFile. Source installations keep
using the installer; the frozen application shares its verified cold backup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from install_freqinout import InstallationError, _create_verified_profile_backup, _active_fio_lock

RECEIPT = ".commsplan-packaged-startup.json"
FORMAT = "commsplan-packaged-startup-v1"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_receipt(path: Path, record: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".packaged-startup-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_packaged_profile(profile_root: Path, version: str, *, held_lock_pid: int) -> dict:
    """Return recorded backup evidence, or refuse before station database writes.

    The receipt records the actual verified path, including Windows naming
    fallbacks. It is written only after successful capture. An interrupted
    receipt leaves the prior version's record in place and is safe to retry.
    Reopening the same version checks the manifest identity, without hashing
    the complete backup on every station startup. Full content validation is
    still required when restoring that backup.
    """
    root = profile_root.resolve()
    if (root / ".commsplan-copy-incomplete").exists():
        raise InstallationError("Profile copy is incomplete. Use the original station profile.")
    try:
        lock_pid = int((root / "freqinout.lock").read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError) as exc:
        raise InstallationError("The startup backup cannot verify the station lock.") from exc
    if held_lock_pid != os.getpid() or lock_pid != held_lock_pid or not _active_fio_lock(root):
        raise InstallationError("The startup backup does not own the station lock.")
    config = root / "config"
    if config.is_symlink():
        raise InstallationError("Station configuration is a symbolic link; review the backup procedure before upgrading.")
    databases = list(config.glob("*.db"))
    if not databases and any(config.glob("*.db-*")):
        raise InstallationError("Station journal files exist without their databases. Recover the complete profile before upgrading.")
    if databases:
        if not all((config / name).is_file() for name in ("freqinout.db", "freqinout_nets.db")):
            raise InstallationError("The existing station is missing a database. Recover the complete verified profile before upgrading.")
        for database in databases:
            if database.is_symlink():
                raise InstallationError("Station database is a symbolic link; review the backup procedure before upgrading.")
            with database.open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\x00":
                    raise InstallationError("The existing station has an empty or invalid database. Keep the profile and recover its verified backup before upgrading.")
    elif (config / "config.json").exists():
        raise InstallationError("Legacy JSON settings exist without the expected station databases; use the historical upgrade procedure first.")
    receipt = root / RECEIPT
    if receipt.is_symlink() or (root / "backups").is_symlink():
        raise InstallationError("Startup receipt or backup directory is a symbolic link; review the station backup location.")
    if receipt.exists():
        try:
            prior = json.loads(receipt.read_text(encoding="utf-8"))
            if not isinstance(prior, dict) or prior.get("format") != FORMAT or prior.get("profile_root") != str(root):
                raise ValueError("receipt identity does not match the station")
            if prior.get("version") == version:
                if prior.get("state") == "fresh-profile" and not databases:
                    return prior
                if prior.get("state") == "verified-backup" and not databases:
                    raise ValueError("previously captured station databases are now missing")
                if prior.get("state") == "fresh-profile":
                    # A formerly fresh station now has data. Capture it once
                    # rather than letting the initial fresh receipt skip backup.
                    return _capture_and_record(root, version, held_lock_pid)
                elif prior.get("state") != "verified-backup":
                    raise ValueError("unknown backup state")
                backup = Path(prior["backup"])
                if not backup.is_absolute() or backup.parent != root / "backups":
                    raise ValueError("backup path is outside the station backup directory")
                manifest = backup / "manifest.json"
                if backup.is_symlink() or manifest.is_symlink() or (backup / "config").is_symlink() or not (backup / "config").is_dir():
                    raise ValueError("verified backup is incomplete or linked elsewhere")
                if _digest(manifest) != prior["manifest_sha256"]:
                    raise ValueError("verified backup manifest changed")
                evidence = json.loads(manifest.read_text(encoding="utf-8"))
                if evidence.get("verification_status") != "verified" or evidence.get("profile_root") != str(root):
                    raise ValueError("backup manifest identity is invalid")
                files = evidence.get("files")
                if not isinstance(files, dict) or not files:
                    raise ValueError("backup manifest has no verified files")
                for relative in files:
                    path = Path(relative)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("backup manifest contains an unsafe path")
                    captured = backup / "config" / path
                    if not captured.is_file() or captured.is_symlink():
                        raise ValueError("a verified backup file is missing or linked elsewhere")
                return prior
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise InstallationError(f"Cannot verify the packaged startup receipt at {receipt}: {exc}. Keep the station and backups; contact CommsPlan support.") from exc
    return _capture_and_record(root, version, held_lock_pid)


def _capture_and_record(root: Path, version: str, held_lock_pid: int) -> dict:
    receipt = root / RECEIPT
    backup = _create_verified_profile_backup(root, held_lock_pid=held_lock_pid)
    record = {"format": FORMAT, "version": version, "profile_root": str(root),
              "state": "verified-backup" if backup else "fresh-profile",
              "backup": str(backup) if backup else None,
              "manifest_sha256": _digest(backup / "manifest.json") if backup else None}
    _atomic_receipt(receipt, record)
    return record
