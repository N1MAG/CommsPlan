"""Durable guided source-upgrade handoff; never runs installers or converts data.

All application launches, station-upgrade consent and launcher retirement remain
explicit operator actions. Native replacement qualification is a separate gate.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile

from freqinout.core import brand_migration as backup_tool
from freqinout.core.brand_migration import MigrationError, _plain_path, _hash

FORMAT = "commsplan-migration-session-v1"
STAGES = ("discovered", "backed-up", "installed", "station-verified", "finished")
CHECKS = ("station-identity", "radios-and-bindings", "companion-paths", "schedules-and-groups",
          "messages-and-attachments", "credentials", "old-launchers-retired")
RECORD = "migration.json"
EVIDENCE = "startup.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _literal(path: Path, name: str):
    try:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
    except (OSError, ValueError, SyntaxError) as exc:
        raise MigrationError(f"Unreadable application metadata: {path.name}") from exc
    raise MigrationError(f"Missing application metadata: {name}")


def source_identity(root: Path) -> dict:
    root = _plain_path(root)
    version = backup_tool.source_version(root)
    inventory = backup_tool._files(root / "freqinout", profile=False)
    inventory = {"freqinout/" + name: value for name, value in inventory.items() if "__pycache__" not in Path(name).parts and not name.endswith((".pyc", ".pyo"))}
    for name in ("requirements.txt", "start-freqinout.sh", "start-freqinout.cmd", ".freqinout-install-verified.json"):
        path = _plain_path(root / name)
        if path.is_file():
            inventory[name] = _hash(path)
    return {"root": str(root), "form": "source", "version": version,
            "runtime_sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()}


def target_identity(root: Path) -> dict:
    """Inspect candidate source without importing/executing it."""
    root = _plain_path(root)
    product = _literal(root / "freqinout/core/product_identity.py", "DISPLAY_NAME")
    version = _literal(root / "freqinout/version.py", "__version__")
    marker = _literal(root / "freqinout/core/multi_radio_store.py", "CURRENT_MULTI_RIG_MIGRATION_VERSION")
    if product != "CommsPlan" or not isinstance(version, str) or type(marker) is not int or marker < 1:
        raise MigrationError("Select a CommsPlan candidate with valid version and migration metadata.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?", version):
        raise MigrationError("Candidate version metadata is unsupported.")
    # Include all runtime source, QML and resource files; reject linked data.
    inventory = backup_tool._files(root / "freqinout", profile=False)
    # Interpreter caches are rebuildable and not candidate identity.
    inventory = {"freqinout/" + name: value for name, value in inventory.items() if "__pycache__" not in Path(name).parts and not name.endswith((".pyc", ".pyo"))}
    for name in ("requirements.txt", "start-commsplan.sh", "start-commsplan.cmd", "start-freqinout.sh", "start-freqinout.cmd"):
        path = _plain_path(root / name)
        if not path.is_file():
            raise MigrationError(f"Candidate is incomplete: {name}")
        inventory[name] = _hash(path)
    receipt_path = _plain_path(root / ".freqinout-install-verified.json")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError("Candidate has no valid source-install receipt; run its supported installer first.") from exc
    python = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if (not isinstance(receipt, dict) or receipt.get("version") != version
            or receipt.get("requirements_sha256") != inventory["requirements.txt"]
            or receipt.get("python") != str(python.resolve()) or not python.is_file()
            or not isinstance(receipt.get("profile_root"), str)):
        raise MigrationError("Candidate source-install receipt does not match its runtime/requirements.")
    inventory[receipt_path.name] = _hash(receipt_path)
    inventory["interpreter"] = _hash(python.resolve())
    return {"root": str(root), "form": "source", "product": product, "version": version,
            "required_marker": marker, "install_profile": str(_plain_path(Path(receipt["profile_root"]))),
            "runtime_sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()}


def _atomic_json(path: Path, data: dict) -> None:
    """Publish complete records only, retaining the previous record on failure."""
    fd, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(session: Path) -> dict:
    session = _plain_path(session)
    record = _plain_path(session / RECORD)
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError("Migration record is missing or invalid; preserve the session for review.") from exc
    required = {"format", "session_id", "profile", "source", "stage", "last_completed", "backup", "target", "launch_id", "checks", "created_at", "updated_at", "outcome", "launch_inventory_sha256"}
    if not isinstance(data, dict) or set(data) != required or data.get("format") != FORMAT:
        raise MigrationError("Unsupported migration record schema.")
    if data["last_completed"] not in STAGES or data["stage"] not in (*STAGES, "recovery-needed"):
        raise MigrationError("Unsupported migration stage.")
    if data["stage"] != "recovery-needed" and data["stage"] != data["last_completed"]:
        raise MigrationError("Migration stage does not match its durable checkpoint.")
    if not isinstance(data["source"], dict) or data["source"].get("form") != "source":
        raise MigrationError("Unsupported source identity.")
    if not isinstance(data["checks"], list) or not all(isinstance(check, str) for check in data["checks"]) or not set(data["checks"]).issubset(CHECKS):
        raise MigrationError("Invalid station checklist.")
    for key in ("session_id", "launch_id"):
        value = data[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise MigrationError("Invalid session identity.")
    if not isinstance(data["profile"], str) or not isinstance(data["source"].get("root"), str):
        raise MigrationError("Invalid station paths.")
    def identity(value, target=False):
        fields = {"root", "form", "version", "runtime_sha256"}
        if target:
            fields |= {"product", "required_marker", "install_profile"}
        return (isinstance(value, dict) and set(value) == fields
                and all(isinstance(value[k], str) for k in fields - {"required_marker"})
                and (not target or type(value["required_marker"]) is int and value["required_marker"] > 0))
    if not identity(data["source"]):
        raise MigrationError("Malformed historical source identity.")
    if data["target"] is not None and not identity(data["target"], target=True):
        raise MigrationError("Malformed candidate identity.")
    if data["backup"] is not None and (not isinstance(data["backup"], dict)
            or set(data["backup"]) != {"path", "manifest_sha256"}
            or not all(isinstance(v, str) for v in data["backup"].values())):
        raise MigrationError("Malformed backup identity.")
    if not all(isinstance(data[key], str) for key in ("created_at", "updated_at", "outcome")):
        raise MigrationError("Malformed migration record metadata.")
    digest = data["launch_inventory_sha256"]
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
        raise MigrationError("Malformed launch profile identity.")
    return data


@contextmanager
def _session_lock(session: Path):
    from PySide6.QtCore import QLockFile
    session = _plain_path(session)
    if not session.is_dir():
        raise MigrationError("Migration session does not exist.")
    lock = QLockFile(str(session / "session.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise MigrationError("Another migration step is using this session.")
    try:
        yield session
    finally:
        lock.unlock()


def _save(session: Path, data: dict, stage: str, outcome: str) -> dict:
    data = dict(data, stage=stage, updated_at=_now(), outcome=outcome)
    if stage != "recovery-needed":
        data["last_completed"] = stage
    _atomic_json(session / RECORD, data)
    return data


def _validate(session: Path, data: dict, *, unchanged_profile: bool = False) -> dict | None:
    profile = _plain_path(Path(data["profile"]))
    for name in backup_tool.DATABASE_NAMES:
        backup_tool._database_summary(profile / "config" / name)
    source = source_identity(Path(data["source"]["root"]))
    if source != data["source"]:
        raise MigrationError("Historical source changed or moved; retain it for recovery and start a reviewed session.")
    rank = STAGES.index(data["last_completed"])
    manifest = None
    if rank >= 1:
        if not isinstance(data["backup"], dict) or set(data["backup"]) != {"path", "manifest_sha256"}:
            raise MigrationError("Missing verified backup identity.")
        backup = _plain_path(Path(data["backup"]["path"]))
        if backup.parent != session:
            raise MigrationError("Backup moved outside the migration session.")
        if _hash(backup / "manifest.json") != data["backup"]["manifest_sha256"]:
            raise MigrationError("Backup manifest changed; preserve it for review.")
        manifest = backup_tool.verify_backup(backup)
        if manifest["profile"] != str(profile) or manifest["installation"] != source["root"] or manifest["source_version"] != source["version"]:
            raise MigrationError("Backup belongs to a different station or installation.")
        if unchanged_profile and backup_tool._files(profile, profile=True) != manifest["files"]:
            raise MigrationError("Station changed since backup. Revalidate before installation; do not replace the rollback backup.")
    else:
        backup_tool.inspect_station(Path(source["root"]), profile)
    if rank >= 2:
        if not isinstance(data["target"], dict) or not isinstance(data["target"].get("root"), str):
            raise MigrationError("Missing installed target identity.")
        if backup_tool._migration_marker(profile / "config/freqinout.db") > data["target"]["required_marker"]:
            raise MigrationError("Profile migration state is newer than this candidate; do not downgrade.")
        if target_identity(Path(data["target"]["root"])) != data["target"]:
            raise MigrationError("Installed candidate changed; startup evidence must be requalified.")
    return manifest


def begin_session(installation: Path, profile: Path, session: Path) -> dict:
    installation, profile, session = map(_plain_path, (installation, profile, session))
    for protected in (installation, profile):
        if session == protected or protected in session.parents or session in protected.parents:
            raise MigrationError("Keep the session outside installation and profile directories.")
    with backup_tool._locked_profile(profile):
        backup_tool.inspect_station(installation, profile)
        source = source_identity(installation)
        session.mkdir(mode=0o700, parents=False)  # Never reuse/overwrite another session.
        data = {"format": FORMAT, "session_id": secrets.token_hex(32), "profile": str(profile),
                "source": source, "stage": "discovered", "last_completed": "discovered", "backup": None,
                "target": None, "launch_inventory_sha256": None, "launch_id": secrets.token_hex(32), "checks": [],
                "created_at": _now(), "updated_at": _now(), "outcome": "Close all companion writers, then create the station backup."}
        _atomic_json(session / RECORD, data)
        return data


def backup_session(session: Path) -> dict:
    with _session_lock(session) as session:
        data = _read(session)
        try:
            with backup_tool._locked_profile(Path(data["profile"])):
                _validate(session, data)
            if data["last_completed"] != "discovered":
                return data  # Revalidation, never replace the existing rollback copy.
            # A kill after backup completion but before checkpoint publication must
            # adopt the verified result, not create/overwrite a second rollback copy.
            candidates = sorted(session.glob("CommsPlan-backup-*"))
            complete = [p for p in candidates if not (p / "INCOMPLETE.txt").exists() and (p / "manifest.json").is_file()]
            if len(complete) > 1:
                raise MigrationError("Multiple backup candidates require review; none was selected.")
            if complete:
                backup = complete[0]
                report = backup_tool.verify_backup(backup)
                if (report["profile"] != data["profile"] or report["installation"] != data["source"]["root"]
                        or report["source_version"] != data["source"]["version"]
                        or report["source_metadata_sha256"] != _hash(Path(data["source"]["root"]) / "freqinout/version.py")):
                    raise MigrationError("Interrupted backup belongs to another station.")
                with backup_tool._locked_profile(Path(data["profile"])):
                    if backup_tool._files(Path(data["profile"]), profile=True) != report["files"]:
                        raise MigrationError("Station changed after interrupted backup; review before proceeding.")
            else:
                backup = backup_tool.create_backup(Path(data["source"]["root"]), Path(data["profile"]), session)
            data["backup"] = {"path": str(backup), "manifest_sha256": _hash(backup / "manifest.json")}
            return _save(session, data, "backed-up", "Backup verified. Install candidate separately; retain this historical source for rollback.")
        except (MigrationError, OSError):
            _save(session, data, "recovery-needed", "Backup/preflight failed; preserve original files and retry the last completed stage.")
            raise


def record_installed(session: Path, target: Path) -> dict:
    with _session_lock(session) as session:
        data = _read(session)
        try:
            if data["last_completed"] not in ("backed-up", "installed"):
                raise MigrationError("Verify a backup before recording installation.")
            with backup_tool._locked_profile(Path(data["profile"])):
                _validate(session, data, unchanged_profile=data["last_completed"] == "backed-up")
                if data["last_completed"] == "installed":
                    _validate_prelaunch_profile(session, data)
                candidate = target_identity(target)
                if candidate["install_profile"] != data["profile"]:
                    raise MigrationError("Candidate installer used a different profile; review its installation receipt.")
                if tuple(map(int, candidate["version"].split("-")[0].split("+")[0].split("."))) < tuple(map(int, data["source"]["version"].split("."))):
                    raise MigrationError("Candidate is older than the historical source; downgrade is not a migration path.")
                if backup_tool._migration_marker(Path(data["profile"]) / "config/freqinout.db") > candidate["required_marker"]:
                    raise MigrationError("Profile migration state is newer than this candidate; do not downgrade.")
                old = Path(data["source"]["root"])
                new = Path(candidate["root"])
                if old == new or old in new.parents or new in old.parents:
                    raise MigrationError("Source preview requires a separate candidate installation; keep the historical source intact.")
                if data["target"] is not None and candidate != data["target"]:
                    raise MigrationError("This session is already bound to a different candidate.")
                for protected in (Path(data["profile"]), session):
                    if new == protected or protected in new.parents or new in protected.parents:
                        raise MigrationError("Candidate must be outside profile and session directories.")
                data["launch_inventory_sha256"] = _profile_digest(Path(data["profile"]))
                data["target"] = candidate
                return _save(session, data, "installed", "Candidate identified; station conversion is not yet verified. Request a verification launch next.")
        except (MigrationError, OSError):
            _save(session, data, "recovery-needed", "Installation handoff failed; no software was installed or removed by the helper.")
            raise


def _profile_digest(profile: Path) -> str:
    inventory = backup_tool._files(profile, profile=True)
    return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()


def _validate_prelaunch_profile(session: Path, data: dict) -> None:
    if _profile_digest(Path(data["profile"])) != data["launch_inventory_sha256"]:
        # A completed previous launch can legitimately have updated runtime
        # bookkeeping. Without matching usable-shell evidence, stop for recovery.
        _verify_station(session, data)


def request_launch(session: Path) -> dict:
    """Rotate the non-secret launch correlation ID; never launch here."""
    with _session_lock(session) as session:
        data = _read(session)
        if data["last_completed"] != "installed":
            raise MigrationError("Record the installed candidate before requesting a verification launch.")
        with backup_tool._locked_profile(Path(data["profile"])):
            _validate(session, data)
            _validate_prelaunch_profile(session, data)
            data["launch_inventory_sha256"] = _profile_digest(Path(data["profile"]))
        data["launch_id"] = secrets.token_hex(32)
        data["checks"] = []
        return _save(session, data, "installed", "Launch the selected candidate with the printed profile and migration-session argument. Review the existing upgrade dialog, then close the application before verification.")


def validate_startup_request(session: Path, profile: Path, application_root: Path, version: str) -> None:
    """Fail before database initialization when a migration launch is misbound."""
    with _session_lock(session) as session:
        data = _read(session)
        if data["stage"] != "installed" or data["last_completed"] != "installed":
            raise MigrationError("Session is not awaiting candidate startup.")
        with backup_tool._locked_profile(Path(data["profile"])):
            _validate(session, data)
            if _profile_digest(Path(data["profile"])) != data["launch_inventory_sha256"]:
                raise MigrationError("Station changed after launch was requested; review before any upgrade writes.")
        if str(_plain_path(profile)) != data["profile"]:
            raise MigrationError("Candidate startup selected a different profile.")
        candidate = target_identity(application_root)
        if candidate != data["target"] or version != candidate["version"]:
            raise MigrationError("Startup application does not match the selected candidate.")


def record_startup_evidence(session: Path, profile: Path, application_root: Path, version: str) -> None:
    """Called after the existing gate/shell; launch_id is correlation, not a secret or attestation."""
    with _session_lock(session) as session:
        data = _read(session)
        if data["last_completed"] != "installed" or data["stage"] != "installed":
            raise MigrationError("Session is not awaiting candidate startup.")
        if str(_plain_path(profile)) != data["profile"]:
            raise MigrationError("Candidate startup selected a different profile.")
        candidate = target_identity(application_root)
        if candidate != data["target"] or version != candidate["version"]:
            raise MigrationError("Startup application does not match the selected candidate.")
        # Avoid immutable mode against a running DB. A read transaction sees the
        # current marker including committed WAL data, without schema creation.
        database = _plain_path(profile / "config/freqinout.db")
        with sqlite3.connect(database.as_uri()+"?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='multi_rig_migration_version'").fetchone()
            try:
                marker = int(json.loads(row[0])) if row else 0
            except (ValueError, TypeError) as exc:
                raise MigrationError("Cannot verify station migration marker.") from exc
        if marker < candidate["required_marker"]:
            raise MigrationError("Existing station upgrade has not completed.")
        _atomic_json(session / EVIDENCE, {"format": FORMAT, "session_id": data["session_id"],
                     "launch_id": data["launch_id"], "profile": data["profile"], "target": candidate,
                     "migration_marker": marker, "usable_shell": True, "created_at": _now()})


def _rows(conn: sqlite3.Connection, table: str, columns: list[str]) -> Counter:
    quote = lambda value: '"' + value.replace('"', '""') + '"'
    rows = conn.execute("SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table))
    # Values stay in memory; only digests leave this function. Preserve duplicate
    # rows as well as values. New candidate columns are deliberately ignored.
    return Counter(hashlib.sha256(repr(tuple(row)).encode()).hexdigest() for row in rows)


def _protected_content(backup: Path, profile: Path) -> None:
    for name in backup_tool.DATABASE_NAMES:
        old = backup / "profile/config" / name
        new = profile / "config" / name
        backup_tool._database_summary(new)
        with sqlite3.connect(old.as_uri()+"?mode=ro&immutable=1", uri=True) as before, sqlite3.connect(new.as_uri()+"?mode=ro&immutable=1", uri=True) as after:
            tables = [row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                # Runtime kv contains migration receipts, timezone and startup
                # bookkeeping. The station checklist verifies operator settings;
                # station-owned relational/message tables remain protected below.
                if name == "freqinout.db" and table == "kv":
                    for key in ("callsign", "grid", "my_callsign", "my_grid"):
                        prior = before.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                        current = after.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                        if prior is not None and prior != current:
                            raise MigrationError("Station callsign/grid changed since backup; review before completion.")
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in before.execute("PRAGMA table_info(" + quoted + ")")]
                try:
                    if _rows(before, table, columns) - _rows(after, table, columns):
                        raise MigrationError(f"Protected station content changed: {name}/{table}. Preserve the backup; qualification/review is required.")
                except sqlite3.Error as exc:
                    raise MigrationError(f"Cannot verify retained station table: {name}/{table}") from exc
    manifest = backup_tool.verify_backup(backup)
    for name, digest in manifest["files"].items():
        # Non-config persistent files (e.g. attachments) cannot be silently lost.
        if Path(name).parts[0] != "config":
            path = _plain_path(profile / name)
            if not path.is_file() or _hash(path) != digest:
                raise MigrationError("Persistent profile file changed or is missing; review before completion.")


def _verify_station(session: Path, data: dict) -> None:
    _validate(session, data)
    try:
        evidence = json.loads(_plain_path(session / EVIDENCE).read_text())
    except (OSError, ValueError) as exc:
        raise MigrationError("Candidate startup evidence is missing; request a verification launch.") from exc
    if not isinstance(evidence, dict):
        raise MigrationError("Candidate startup evidence is invalid.")
    for key in ("session_id", "launch_id", "profile", "target"):
        if evidence.get(key) != data[key]:
            raise MigrationError("Candidate startup evidence is stale or belongs to another session.")
    if evidence.get("format") != FORMAT or evidence.get("usable_shell") is not True:
        raise MigrationError("Candidate did not report a usable application shell.")
    profile = Path(data["profile"])
    marker = backup_tool._migration_marker(profile / "config/freqinout.db")
    if marker < data["target"]["required_marker"] or marker != evidence.get("migration_marker"):
        raise MigrationError("Station migration state does not match verified candidate startup.")
    _protected_content(Path(data["backup"]["path"]), profile)


def verify_station(session: Path, checks: list[str]) -> dict:
    with _session_lock(session) as session:
        data = _read(session)
        try:
            if data["last_completed"] not in ("installed", "station-verified"):
                raise MigrationError("Record installation before station verification.")
            required = set(CHECKS) - {"old-launchers-retired"}
            if set(checks) != required:
                raise MigrationError("Explicitly confirm every station check after reviewing the candidate.")
            with backup_tool._locked_profile(Path(data["profile"])):
                _verify_station(session, data)
            data["checks"] = sorted(required)
            return _save(session, data, "station-verified", "Station checks passed. Manually retire only reviewed old launch entries, then finish. Backup and historical source remain retained.")
        except (MigrationError, OSError, sqlite3.Error):
            _save(session, data, "recovery-needed", "Station verification failed; retain backup. Retry verification or restore into a new profile using the historical source.")
            raise


def finish_session(session: Path, *, old_launchers_retired: bool) -> dict:
    with _session_lock(session) as session:
        data = _read(session)
        try:
            if data["last_completed"] not in ("station-verified", "finished") or not old_launchers_retired:
                raise MigrationError("Station verification and explicit old-launcher retirement are required before finishing.")
            if set(data["checks"]) - {"old-launchers-retired"} != set(CHECKS) - {"old-launchers-retired"}:
                raise MigrationError("Station checklist is incomplete.")
            with backup_tool._locked_profile(Path(data["profile"])):
                _verify_station(session, data)
            data["checks"] = list(CHECKS)
            return _save(session, data, "finished", "Station verified in CommsPlan; operator confirmed old launch entries retired. Historical source and verified profile backup retained for recovery.")
        except (MigrationError, OSError, sqlite3.Error):
            _save(session, data, "recovery-needed", "Completion checks failed. No application or profile was deleted.")
            raise


def resume_session(session: Path) -> dict:
    with _session_lock(session) as session:
        data = _read(session)
        try:
            with backup_tool._locked_profile(Path(data["profile"])):
                _validate(session, data, unchanged_profile=data["last_completed"] == "backed-up")
                if data["last_completed"] == "installed":
                    _validate_prelaunch_profile(session, data)
                if data["last_completed"] in ("station-verified", "finished"):
                    _verify_station(session, data)
            # A recovery record is not silently promoted. The explicit failed
            # operation must be retried; backup/installation/conversion never run.
            return data
        except (MigrationError, OSError, sqlite3.Error):
            _save(session, data, "recovery-needed", "Resume validation failed. Preserve session/backup; retry the failed step after review.")
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CommsPlan guided source migration; manual installation and existing upgrade consent remain required.")
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("begin")
    begin.add_argument("--installation", type=Path, required=True)
    begin.add_argument("--profile", type=Path, required=True)
    begin.add_argument("--session", type=Path, required=True, help="New session directory outside installation/profile")
    for name in ("backup-session", "installed", "launch-instructions", "verify-station", "finish", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("--session", type=Path, required=True)
        if name == "installed":
            sub.add_argument("--target", type=Path, required=True)
        if name == "verify-station":
            sub.add_argument("--confirm", action="append", choices=CHECKS[:-1], required=True)
        if name == "finish":
            sub.add_argument("--old-launchers-retired", action="store_true")
    args = parser.parse_args(argv)
    try:
        functions = {"begin": lambda: begin_session(args.installation, args.profile, args.session),
                     "backup-session": lambda: backup_session(args.session),
                     "installed": lambda: record_installed(args.session, args.target),
                     "launch-instructions": lambda: request_launch(args.session),
                     "verify-station": lambda: verify_station(args.session, args.confirm),
                     "finish": lambda: finish_session(args.session, old_launchers_retired=args.old_launchers_retired),
                     "resume": lambda: resume_session(args.session)}
        data = functions[args.command]()
        result = {key: data[key] for key in ("stage", "last_completed", "profile", "source", "target", "backup", "outcome")}
        if args.command == "launch-instructions":
            result["launch"] = {"environment": {"FREQINOUT_CONFIG_DIR": data["profile"]},
                                "application": str(Path(data["target"]["root"]) / ("start-commsplan.cmd" if os.name == "nt" else "start-commsplan.sh")),
                                "arguments": ["--migration-session", str(_plain_path(args.session))]}
        print(json.dumps(result, indent=2))
        return 0
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"Guided migration stopped: {exc}", file=sys.stderr)
        return 1
