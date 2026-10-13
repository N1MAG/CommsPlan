"""Explicit, cold-profile backup and recovery for the guided CommsPlan upgrade.

This tool never installs software, converts a database, removes FIO, or starts
radio services. Installation and the existing informed station-upgrade flow
remain separate steps. Recovery writes only to a new destination.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import sys

from freqinout.core.product_identity import LOCK_FILENAME

SUPPORTED_SOURCES = {"1.2.8", "2.0.4"}
EXCLUDED_ROOTS = {"backups", "cache", "logs", "downloads", LOCK_FILENAME, ".commsplan-journal-recovery.json"}
FORMAT = "commsplan-profile-backup-v1"
SIDECAR_POLICY = "omit-validated-nondurable-sidecars-v1"
DATABASE_NAMES = ("freqinout.db", "freqinout_nets.db")


class MigrationError(RuntimeError):
    pass


def _plain_path(path: Path) -> Path:
    """Require an explicit physical path; never silently switch profile aliases."""
    path = path.expanduser().absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise MigrationError(f"Linked path needs explicit review; select its physical location: {component}")
    return path.resolve()


def _migration_marker(database: Path) -> int:
    # Do not call the runtime getter: it creates/updates schema as a side effect.
    try:
        with sqlite3.connect(database.as_uri()+"?mode=ro&immutable=1", uri=True) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'").fetchone():
                return 0
            row = conn.execute("SELECT value FROM kv WHERE key=?", ("multi_rig_migration_version",)).fetchone()
            marker = int(json.loads(row[0])) if row else 0
            if marker < 0:
                raise ValueError("negative marker")
            return marker
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise MigrationError("Cannot determine the selected profile's multi-rig migration state.") from exc


def source_version(installation: Path) -> str:
    """Read historical source metadata without executing untrusted code."""
    file = installation / "freqinout" / "version.py"
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
            ):
                value = ast.literal_eval(node.value)
                if isinstance(value, str) and value in SUPPORTED_SOURCES:
                    return value
    except (OSError, ValueError, SyntaxError) as exc:
        raise MigrationError("Select a source installation with readable version metadata.") from exc
    raise MigrationError("Only source versions 1.2.8 and 2.0.4 are supported inputs for this preparation helper.")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nondurable_sidecar(path: Path) -> bool:
    """Exclude sidecars only for a recognized, already checkpointed SQLite DB."""
    suffix = next((s for s in ("-wal", "-shm", "-journal") if path.name.endswith(s)), None)
    if suffix is None:
        return False
    database = path.with_name(path.name[:-len(suffix)])
    if database.is_symlink() or not database.is_file():
        return False
    with database.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            return False
    _database_summary(database)  # Refuse pending data before omitting anything.
    return True


def _files(root: Path, *, profile: bool, include_sidecars: bool = False) -> dict[str, str]:
    found = {}
    def visit(folder: Path):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root)
            if profile and relative.parts[0] in EXCLUDED_ROOTS:
                continue
            if path.is_symlink():
                raise MigrationError(f"Linked profile data needs explicit review: {relative}")
            if profile and not include_sidecars and _nondurable_sidecar(path):
                continue
            if path.is_dir():
                visit(path)
            elif path.is_file():
                found[relative.as_posix()] = _hash(path)
            else:
                raise MigrationError(f"Unsupported profile entry: {relative}")
    visit(root)
    return found


def _directories(root: Path, *, profile: bool) -> list[str]:
    """Inventory physical directories separately, including empty trees."""
    found = []
    def visit(folder):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root)
            if profile and relative.parts[0] in EXCLUDED_ROOTS:
                continue
            if path.is_symlink():
                raise MigrationError(f"Linked profile data needs explicit review: {relative}")
            if path.is_dir():
                found.append(relative.as_posix())
                visit(path)
            elif not path.is_file():
                raise MigrationError(f"Unsupported profile entry: {relative}")
    visit(root)
    return sorted(found)


def _journal_inventory(path: Path) -> list[dict]:
    result = []
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink():
            result.append({"file": sidecar.name, "kind": suffix[1:], "state": "linked", "bytes": None})
        elif sidecar.exists():
            result.append({"file": sidecar.name, "kind": suffix[1:],
                           "state": "regular" if sidecar.is_file() else "irregular",
                           "bytes": sidecar.stat().st_size})
        else:
            result.append({"file": sidecar.name, "kind": suffix[1:], "state": "absent", "bytes": 0})
    return result


def _sidecar_snapshot(profile: Path, *, validate: bool = True) -> list[dict]:
    """Bind excluded residue to a source snapshot; never ignore orphan entries."""
    result = []
    def visit(folder):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(profile)
            if relative.parts[0] in EXCLUDED_ROOTS:
                continue
            suffix = next((part for part in ("-wal", "-shm", "-journal") if path.name.endswith(part)), None)
            if suffix:
                info = path.lstat()
                state = "linked" if path.is_symlink() else "regular" if path.is_file() else "irregular"
                item = {"path": relative.as_posix(), "kind": suffix[1:], "state": state,
                        "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                        "device": info.st_dev, "inode": info.st_ino}
                if state == "regular":
                    item["sha256"] = _hash(path)
                if validate:
                    if state != "regular":
                        raise MigrationError(f"Linked or irregular SQLite sidecar requires review: {relative}")
                    if not _nondurable_sidecar(path):
                        raise MigrationError(f"Orphan or unrecognized SQLite sidecar requires review: {relative}. Do not delete it.")
                result.append(item)
            elif path.is_symlink():
                if validate:
                    raise MigrationError(f"Linked profile data needs explicit review: {relative}")
            elif path.is_dir():
                visit(path)
    visit(profile)
    return result


def _database_summary(path: Path) -> dict[str, int]:
    path = _plain_path(path)
    sidecars = _journal_inventory(path)
    unsafe = [item for item in sidecars if item["state"] in {"linked", "irregular"}
              or (item["kind"] != "shm" and item["bytes"])]
    if unsafe:
        details = ", ".join(f'{item["file"]} ({item["state"]}, {item["bytes"]} bytes)' for item in sidecars if item["state"] != "absent")
        raise MigrationError(f"Database has pending journal data or unsafe sidecars: {path.name}: {details}. "
                             "Nonempty WAL/journal data may still be needed even after shutdown. "
                             "Do not delete these files. Run install-commsplan.sh profile journals --source PROFILE_ROOT for a read-only inventory.")
    try:
        # SHM is a transient index, not database content. Only absent/empty WAL
        # and rollback journals are allowed; immutable inspection ignores SHM.
        # No persistent journal data is pending. Immutable mode prevents SQLite creating
        # or changing SHM/journal files while inspecting the cold source.
        with sqlite3.connect(path.as_uri()+"?mode=ro&immutable=1", uri=True) as conn:
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise MigrationError(f"Database integrity check failed: {path.name}")
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            return {name: conn.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
                    for name in sorted(tables)}
    except sqlite3.Error as exc:
        raise MigrationError(f"Cannot validate database: {path.name}") from exc


def inspect_station(installation: Path, profile: Path) -> dict:
    installation, profile = _plain_path(installation), _plain_path(profile)
    version = source_version(installation)
    report = _inspect_profile(profile)
    return dict(report, source_metadata_sha256=_hash(installation / "freqinout/version.py"),
                source_qualification="Version metadata only; historical installation provenance still needs qualification.",
                source_version=version, installation=str(installation))


def _inspect_profile(profile: Path) -> dict:
    profile = _plain_path(profile)
    if not profile.is_dir():
        raise MigrationError("The selected profile does not exist. No empty profile will be created.")
    sidecars = _sidecar_snapshot(profile)
    databases = {}
    for name in DATABASE_NAMES:
        path = profile / "config" / name
        if not path.is_file() or path.is_symlink():
            raise MigrationError(f"Selected profile is missing a regular config/{name} file.")
        databases[name] = _database_summary(path)
    marker = _migration_marker(profile / "config" / "freqinout.db")
    return {"migration_marker": marker, "profile": str(profile), "sqlite_sidecars": sidecars,
            "upgrade_path": "single-rig-upgrade-required" if marker == 0 else "retain-multi-rig-station",
            "databases": databases, "stage": "inspected",
            "instructions": "Close FIO and companion apps, then create a verified backup before installation."}


@contextmanager
def _locked_profile(profile: Path):
    try:
        from PySide6.QtCore import QLockFile
    except ImportError as exc:
        raise MigrationError("Run this step with the installed application Python (PySide6 required for the shared lock).") from exc
    lock = QLockFile(str(profile / LOCK_FILENAME))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise MigrationError("FIO/CommsPlan is running or the profile lock is unavailable. Close it before retrying.")
    try:
        yield
    finally:
        lock.unlock()


def create_backup(installation: Path, profile: Path, destination: Path) -> Path:
    return _create_backup(profile, destination, lambda: inspect_station(installation, profile))


def _create_backup(profile: Path, destination: Path, inspect) -> Path:
    """Shared cold-copy engine; caller supplies read-only installation inspection."""
    profile, destination = _plain_path(profile), _plain_path(destination)
    if destination == profile or profile in destination.parents:
        raise MigrationError("Choose a backup destination outside the selected profile.")
    if not destination.is_dir():
        raise MigrationError("Choose an existing backup destination directory.")
    with _locked_profile(profile):
        report = inspect()
        before = _files(profile, profile=True)
        directories = _directories(profile, profile=True)
        needed = sum((profile / name).stat().st_size for name in before) + 64*1024*1024
        if shutil.disk_usage(destination).free < needed:
            raise MigrationError("Not enough free space for a verified station backup.")
        backup = Path(tempfile.mkdtemp(prefix="CommsPlan-backup-", dir=destination))
        data = backup / "profile"
        data.mkdir()
        try:
            for name in directories:
                (data / name).mkdir(parents=True, exist_ok=True)
            for name in before:
                src, target = profile / name, data / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target, follow_symlinks=False)
            if (_files(profile, profile=True) != before or _files(data, profile=False) != before
                    or _directories(profile, profile=True) != directories
                    or _directories(data, profile=False) != directories):
                raise MigrationError("Profile changed while backing up; the backup is incomplete. Close all writers and retry.")
            if inspect() != report:
                raise MigrationError("Installation evidence changed while backing up; preserve the incomplete copy and retry.")
            for name in DATABASE_NAMES:
                if _database_summary(data / "config" / name) != report["databases"][name]:
                    raise MigrationError("Backup database contents do not match the selected station.")
            report.update({"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                           "stage": "backed-up", "files": before, "directories": directories,
                           "excluded_profile_roots": sorted(EXCLUDED_ROOTS),
                           "sqlite_sidecar_policy": SIDECAR_POLICY,
                           "external_data_policy": "External companion data is untouched; this backup contains only selected profile files.",
                           "keyring_policy": "Existing OS account/keyring reused; secrets are not exported."})
            (backup / "manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
            verify_backup(backup)
        except Exception:
            # Preserve incomplete files for diagnosis; do not call them verified.
            (backup / "INCOMPLETE.txt").write_text("Backup did not complete. Do not install from this result; retry with the source closed.\n")
            raise
        return backup


def verify_backup(backup: Path) -> dict:
    backup = _plain_path(backup)
    manifest = backup / "manifest.json"
    if manifest.is_symlink() or (backup / "INCOMPLETE.txt").exists():
        raise MigrationError("Backup is incomplete or has linked metadata.")
    try:
        report = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise MigrationError("Backup manifest is missing or invalid.") from exc
    if not isinstance(report, dict) or report.get("format") != FORMAT or report.get("stage") != "backed-up":
        raise MigrationError("Unsupported backup format or incomplete stage.")
    databases = report.get("databases")
    if not isinstance(databases, dict) or set(databases) != set(DATABASE_NAMES):
        raise MigrationError("Backup database inventory must contain exactly the two station databases.")
    files = report.get("files")
    if not isinstance(files, dict) or not files:
        raise MigrationError("Backup contains no verified file inventory.")
    data = backup / "profile"
    if not data.is_dir() or data.is_symlink():
        raise MigrationError("Backup profile is missing or linked.")
    policy = report.get("sqlite_sidecar_policy")
    if policy not in (None, SIDECAR_POLICY):
        raise MigrationError("Unsupported backup sidecar inventory policy.")
    if policy == SIDECAR_POLICY and _sidecar_snapshot(data):
        raise MigrationError("Backup must not contain SQLite sidecars under its inventory policy.")
    if _files(data, profile=False) != files:
        raise MigrationError("Backup files changed or are missing; do not proceed.")
    if "directories" in report and _directories(data, profile=False) != report["directories"]:
        raise MigrationError("Backup directory inventory changed; do not proceed.")
    for name in DATABASE_NAMES:
        summary = _database_summary(data / "config" / name)
        if summary != report.get("databases", {}).get(name):
            raise MigrationError("Backup database verification does not match manifest.")
    return report


def restore_copy(backup: Path, destination: Path, *, guard_filename: str | None = None) -> Path:
    """Recover to a NEW directory; never overwrite a station or installation."""
    if guard_filename is not None and (not guard_filename or guard_filename in {".", ".."} or Path(guard_filename).name != guard_filename):
        raise MigrationError("Copy guard must be a single filename.")
    report = verify_backup(backup)
    if guard_filename in report["files"]:
        raise MigrationError("Backup conflicts with the copy guard.")
    destination = _plain_path(destination)
    parent_identity = destination.parent.stat()
    if destination.exists() or destination.is_symlink():
        raise MigrationError("Recovery destination must not exist. Existing station data will not be overwritten.")
    destination.mkdir(parents=False, mode=0o700)
    if guard_filename:
        try:
            (destination / guard_filename).write_text("Profile copy is incomplete. Do not launch or activate it.\n")
        except OSError as exc:
            # Only remove the empty directory we just created; never delete data.
            try:
                destination.rmdir()
            except OSError:
                pass
            raise MigrationError("Cannot guard the new profile; copy did not begin.") from exc
    try:
        current_parent = destination.parent.stat()
        if destination.is_symlink() or (parent_identity.st_dev, parent_identity.st_ino) != (current_parent.st_dev, current_parent.st_ino):
            raise MigrationError("Recovery parent changed during preparation.")
        _plain_path(destination)
        # Copy children so copytree cannot replace the private root permissions.
        for entry in (backup.resolve() / "profile").iterdir():
            target = destination / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, symlinks=True)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)
        copied = _files(destination, profile=False)
        if guard_filename:
            copied.pop(guard_filename, None)
        if "directories" in report and _directories(destination, profile=False) != report["directories"]:
            raise MigrationError("Recovered directory tree did not match the backup.")
        if copied != report["files"]:
            raise MigrationError("Recovered copy did not match the backup. Do not launch it.")
        for name in DATABASE_NAMES:
            _database_summary(destination / "config" / name)
    except Exception:
        # A partial restore must remain visible and never overwrite a valid copy.
        raise MigrationError(f"Recovery incomplete at {destination}; keep it for review and retry into a new directory.")
    return destination


def main(argv: list[str] | None = None) -> int:
    session_commands = {"begin", "backup-session", "installed", "launch-instructions", "verify-station", "finish", "resume"}
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"profile-plan", "profile-copy", "profile-journals", "profile-recover"}:
        from freqinout.core.profile_transition import main as profile_main
        return profile_main(arguments)
    if arguments and arguments[0] in session_commands:
        from freqinout.core.brand_migration_session import main as session_main
        return session_main(arguments)
    if arguments and arguments[0] in {"inspect-native", "backup-native", "verify-native-backup", "--report-file"}:
        from freqinout.core.brand_migration_native import main as native_main
        return native_main(arguments)
    parser = argparse.ArgumentParser(description="CommsPlan migration preparation and safe recovery. No software installation or database conversion is performed.", epilog="Guided source commands: begin, backup-session, installed, launch-instructions, verify-station, finish, resume. Native preparation: inspect-native, backup-native, verify-native-backup. Use COMMAND --help for details.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "backup"):
        sub = commands.add_parser(command)
        sub.add_argument("--installation", type=Path, required=True)
        sub.add_argument("--profile", type=Path, required=True,
                         help="Existing profile ROOT containing config/, not the config directory itself")
        if command == "backup":
            sub.add_argument("--destination", type=Path, required=True)
    verify = commands.add_parser("verify-backup")
    verify.add_argument("backup", type=Path)
    restore = commands.add_parser("restore-copy")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        if args.command == "inspect":
            result = inspect_station(args.installation, args.profile)
        elif args.command == "backup":
            path = create_backup(args.installation, args.profile, args.destination)
            result = {"stage": "backed-up", "backup": str(path),
                      "next": "Keep the old application/package for rollback. Install CommsPlan using the platform guide; then verify your station before removing old launchers."}
        elif args.command == "verify-backup":
            report = verify_backup(args.backup)
            result = {"stage": "backed-up", "profile": report["profile"], "source_version": report["source_version"], "verified": True}
        else:
            result = {"recovered_copy": str(restore_copy(args.backup, args.destination)),
                      "next": "Use this copy only with the matching historical version after reviewing the recovery instructions."}
        print(json.dumps(result, indent=2))
        return 0
    except (MigrationError, OSError) as exc:
        print(f"Migration preparation stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
