from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from freqinout.core.config_backup import ConfigBackupResult, create_config_backup
from freqinout.core.multi_radio_store import settings_db_path
from freqinout.core.receiver_software_stack import (
    RECEIVE_ONLY_EXECUTION_SCOPE,
    STANDARD_EXECUTION_SCOPE,
    execution_scope,
)
from freqinout.core.guided_radio_software_model import ExecutionScope
from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_readonly


LAUNCH_BUNDLE_SCHEMA_VERSION = 1
LAUNCH_BUNDLE_MIGRATION_KEY = "launch-bundles-v1"
LEGACY_APP_ORDER = ("FLRig", "FLDigi", "FLAmp", "FLMsg", "VarAC", "JS8Call", "JS8Spotter", "CommStat")
LEGACY_AUTOSTART_KEYS = {
    "FLRig": "autostart_flrig",
    "FLDigi": "autostart_fldigi",
    "FLAmp": "autostart_flamp",
    "FLMsg": "autostart_flmsg",
    "JS8Call": "autostart_js8call",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_launch_items(items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name", "") or "").strip()
        identity = str(raw.get("instance_key", name) or name).strip()
        if not name or not identity or identity.casefold() in seen:
            continue
        seen.add(identity.casefold())
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list):
            dependencies = []
        readiness = raw.get("readiness_policy", {})
        if not isinstance(readiness, Mapping):
            readiness = {}
        normalized_scope = execution_scope(raw)
        if normalized_scope not in {scope.value for scope in ExecutionScope}:
            normalized_scope = STANDARD_EXECUTION_SCOPE
        normalized_readiness = dict(readiness)
        # ``readiness_json`` is the existing durable extension seam.  Carry the
        # scope there as well as exposing it at the model boundary, so no
        # database migration is required for receiver-specific launch rows.
        normalized_readiness["execution_scope"] = normalized_scope
        out.append(
            {
                "name": name,
                "instance_key": identity,
                "enabled": _truthy(raw.get("enabled", True)),
                "startup": _truthy(raw.get("startup", False)),
                "monitor_health": _truthy(raw.get("monitor_health", raw.get("enabled", True))),
                "launch_path_override": str(raw.get("launch_path_override", "") or "").strip(),
                "launch_command_override": str(raw.get("launch_command_override", "") or "").strip(),
                "dependencies": [str(value).strip() for value in dependencies if str(value).strip()],
                "readiness_policy": normalized_readiness,
                "execution_scope": normalized_scope,
            }
        )
    return out


def legacy_launch_items(settings_values: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Normalize every legacy Launch Control source without importing Qt orchestration."""
    raw = normalize_launch_items(settings_values.get("launch_control_items", []))
    existing = {str(item["name"]): dict(item) for item in raw}
    ordered_names = [str(item["name"]) for item in raw]
    for name in LEGACY_APP_ORDER:
        if name not in existing:
            existing[name] = {
                "name": name,
                "instance_key": name,
                "enabled": True,
                "startup": _truthy(settings_values.get(LEGACY_AUTOSTART_KEYS.get(name, ""), False)),
                "monitor_health": True,
                "launch_path_override": "",
                "launch_command_override": "",
                "dependencies": [],
                "readiness_policy": {},
            }
            ordered_names.append(name)
        elif name in LEGACY_AUTOSTART_KEYS and "startup" not in (
            next((item for item in settings_values.get("launch_control_items", []) if isinstance(item, Mapping) and item.get("name") == name), {})
        ):
            existing[name]["startup"] = _truthy(settings_values.get(LEGACY_AUTOSTART_KEYS[name], False))
    for tool in settings_values.get("custom_tool_items", []) if isinstance(settings_values.get("custom_tool_items"), list) else []:
        if not isinstance(tool, Mapping):
            continue
        name = str(tool.get("name", "") or "").strip()
        command = str(tool.get("command", "") or "").strip()
        if not name or not command or name in existing:
            continue
        existing[name] = {
            "name": name,
            "instance_key": name,
            "enabled": True,
            "startup": False,
            "monitor_health": False,
            "launch_path_override": "",
            "launch_command_override": command,
            "dependencies": [],
            "readiness_policy": {},
        }
        ordered_names.append(name)
    return [existing[name] for name in ordered_names]


class LaunchBundleStore:
    """Radio-owned Launch Control persistence with a read-only legacy fallback."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else settings_db_path()
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open an initialized launch store without journal/schema writes."""

        conn = connect_sqlite_readonly(self.db_path, timeout=0.25)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS radio_launch_bundles (
                radio_profile_id INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL DEFAULT 1,
                launch_enabled INTEGER NOT NULL DEFAULT 0,
                migrated_from_legacy INTEGER NOT NULL DEFAULT 0,
                updated_utc TEXT NOT NULL,
                FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS radio_launch_bundle_items (
                radio_profile_id INTEGER NOT NULL,
                instance_key TEXT NOT NULL,
                app_name TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                launch_at_startup INTEGER NOT NULL DEFAULT 0,
                monitor_health INTEGER NOT NULL DEFAULT 1,
                command_override TEXT,
                path_override TEXT,
                dependencies_json TEXT NOT NULL DEFAULT '[]',
                readiness_json TEXT NOT NULL DEFAULT '{}',
                updated_utc TEXT NOT NULL,
                PRIMARY KEY(radio_profile_id, instance_key),
                FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS launch_bundle_migration_audit (
                migration_key TEXT PRIMARY KEY,
                occurred_utc TEXT NOT NULL,
                source_key TEXT NOT NULL,
                target_radio_profile_id INTEGER,
                target_reason TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL,
                backup_path TEXT,
                result_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        conn.commit()

    def get_bundle(self, radio_profile_id: int, legacy_items: Any = None) -> Dict[str, Any]:
        radio_id = int(radio_profile_id)
        with self._connect_readonly() as conn:
            header = conn.execute(
                "SELECT * FROM radio_launch_bundles WHERE radio_profile_id=?", (radio_id,)
            ).fetchone()
            if header is None:
                migration_confirmed = conn.execute(
                    """SELECT 1 FROM launch_bundle_migration_audit
                       WHERE migration_key=? AND state LIKE 'confirmed%'""",
                    (LAUNCH_BUNDLE_MIGRATION_KEY,),
                ).fetchone() is not None
                fallback = None if migration_confirmed else legacy_items
                return {
                    "radio_profile_id": radio_id,
                    "schema_version": LAUNCH_BUNDLE_SCHEMA_VERSION,
                    "launch_enabled": False,
                    "items": normalize_launch_items(fallback),
                    "legacy_fallback": fallback is not None,
                    "updated_utc": "",
                }
            rows = conn.execute(
                """SELECT * FROM radio_launch_bundle_items
                     WHERE radio_profile_id=? ORDER BY display_order ASC, instance_key ASC""",
                (radio_id,),
            ).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                try:
                    dependencies = json.loads(item.get("dependencies_json") or "[]")
                except Exception:
                    dependencies = []
                try:
                    readiness = json.loads(item.get("readiness_json") or "{}")
                except Exception:
                    readiness = {}
                items.append(
                    {
                        "name": str(item.get("app_name") or ""),
                        "instance_key": str(item.get("instance_key") or ""),
                        "enabled": bool(item.get("enabled")),
                        "startup": bool(item.get("launch_at_startup")),
                        "monitor_health": bool(item.get("monitor_health")),
                        "launch_command_override": str(item.get("command_override") or ""),
                        "launch_path_override": str(item.get("path_override") or ""),
                        "dependencies": dependencies if isinstance(dependencies, list) else [],
                        "readiness_policy": readiness if isinstance(readiness, dict) else {},
                        "execution_scope": execution_scope(
                            {"readiness_policy": readiness if isinstance(readiness, dict) else {}}
                        ),
                    }
                )
            return {
                "radio_profile_id": radio_id,
                "schema_version": int(header["schema_version"]),
                "launch_enabled": bool(header["launch_enabled"]),
                "items": items,
                "legacy_fallback": False,
                "updated_utc": str(header["updated_utc"] or ""),
            }

    def save_bundle(self, radio_profile_id: int, launch_enabled: bool, items: Any) -> Dict[str, Any]:
        radio_id = int(radio_profile_id)
        normalized = normalize_launch_items(items)
        updated = _utc_now()
        with self._connect() as conn:
            self._ensure_schema(conn)
            if conn.execute("SELECT 1 FROM device_profiles WHERE id=?", (radio_id,)).fetchone() is None:
                raise KeyError(f"Unknown radio profile id: {radio_id}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO radio_launch_bundles
                       (radio_profile_id, schema_version, launch_enabled, migrated_from_legacy, updated_utc)
                       VALUES (?, ?, ?, 0, ?)
                       ON CONFLICT(radio_profile_id) DO UPDATE SET
                         schema_version=excluded.schema_version,
                         launch_enabled=excluded.launch_enabled,
                         updated_utc=excluded.updated_utc""",
                    (radio_id, LAUNCH_BUNDLE_SCHEMA_VERSION, int(bool(launch_enabled)), updated),
                )
                conn.execute("DELETE FROM radio_launch_bundle_items WHERE radio_profile_id=?", (radio_id,))
                for order, item in enumerate(normalized):
                    conn.execute(
                        """INSERT INTO radio_launch_bundle_items
                           (radio_profile_id, instance_key, app_name, display_order, enabled,
                            launch_at_startup, monitor_health, command_override, path_override,
                            dependencies_json, readiness_json, updated_utc)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            radio_id,
                            item["instance_key"],
                            item["name"],
                            order,
                            int(item["enabled"]),
                            int(item["startup"]),
                            int(item["monitor_health"]),
                            item["launch_command_override"],
                            item["launch_path_override"],
                            json.dumps(item["dependencies"], sort_keys=True),
                            json.dumps(item["readiness_policy"], sort_keys=True),
                            updated,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_bundle(radio_id)

    def list_migration_audit(self) -> List[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM launch_bundle_migration_audit ORDER BY occurred_utc ASC"
            ).fetchall()]

    def migrate_legacy(
        self,
        settings_values: Mapping[str, Any],
        *,
        backup_factory: Optional[Callable[..., ConfigBackupResult]] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        raw_items = settings_values.get("launch_control_items", [])
        items = legacy_launch_items(settings_values)
        source_payload = {
            "launch_control_items": raw_items,
            "launch_control_enabled": settings_values.get("launch_control_enabled"),
            "autostart": {key: settings_values.get(key) for key in LEGACY_AUTOSTART_KEYS.values()},
            "custom_tool_items": settings_values.get("custom_tool_items", []),
        }
        source = json.dumps(source_payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            self._ensure_schema(conn)
            prior = conn.execute(
                "SELECT * FROM launch_bundle_migration_audit WHERE migration_key=?",
                (LAUNCH_BUNDLE_MIGRATION_KEY,),
            ).fetchone()
            if prior is not None:
                return {"state": str(prior["state"]), "already_applied": True, "target_radio_profile_id": prior["target_radio_profile_id"]}
            target, reason = self._select_migration_target(conn)
        summary = {
            "state": "deferred" if target is None else "ready",
            "already_applied": False,
            "target_radio_profile_id": target,
            "target_reason": reason,
            "item_count": len(items),
            "source_sha256": digest,
        }
        if dry_run:
            return summary
        if target is None:
            return summary
        backup_fn = backup_factory or create_config_backup
        if backup_factory is None:
            with self._connect() as checkpoint_conn:
                checkpoint = checkpoint_conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
                if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                    raise RuntimeError("Launch bundle migration could not checkpoint the settings database; no data was changed.")
        backup = backup_fn([self.db_path], reason="launch-bundle-v1")
        db_item = next((item for item in backup.items if Path(item.original_path) == self.db_path), None)
        if db_item is None or db_item.status != "backed_up":
            raise RuntimeError("Launch bundle migration backup did not complete; no data was changed.")
        occurred = _utc_now()
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM launch_bundle_migration_audit WHERE migration_key=?",
                    (LAUNCH_BUNDLE_MIGRATION_KEY,),
                ).fetchone():
                    conn.rollback()
                    return {**summary, "already_applied": True, "state": "confirmed"}
                existing_bundle = conn.execute(
                    "SELECT 1 FROM radio_launch_bundles WHERE radio_profile_id=?", (target,)
                ).fetchone()
                if existing_bundle is not None:
                    result = {**summary, "state": "confirmed_existing_bundle", "backup_path": backup.backup_dir}
                    conn.execute(
                        """INSERT INTO launch_bundle_migration_audit
                           (migration_key, occurred_utc, source_key, target_radio_profile_id, target_reason,
                            source_sha256, item_count, state, backup_path, result_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed_existing_bundle', ?, ?)""",
                        (LAUNCH_BUNDLE_MIGRATION_KEY, occurred, "launch_control_items", target, reason,
                         digest, len(items), backup.backup_dir, json.dumps(result, sort_keys=True)),
                    )
                    conn.commit()
                    return result
                conn.execute(
                    """INSERT INTO radio_launch_bundles
                       (radio_profile_id, schema_version, launch_enabled, migrated_from_legacy, updated_utc)
                       VALUES (?, ?, ?, 1, ?)""",
                    (target, LAUNCH_BUNDLE_SCHEMA_VERSION, int(_truthy(settings_values.get("launch_control_enabled", False))), occurred),
                )
                for order, item in enumerate(items):
                    conn.execute(
                        """INSERT INTO radio_launch_bundle_items
                           (radio_profile_id, instance_key, app_name, display_order, enabled,
                            launch_at_startup, monitor_health, command_override, path_override,
                            dependencies_json, readiness_json, updated_utc)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (target, item["instance_key"], item["name"], order, int(item["enabled"]),
                         int(item["startup"]), int(item["monitor_health"]), item["launch_command_override"],
                         item["launch_path_override"], json.dumps(item["dependencies"]),
                         json.dumps(item["readiness_policy"]), occurred),
                    )
                result = {**summary, "state": "confirmed", "backup_path": backup.backup_dir}
                conn.execute(
                    """INSERT INTO launch_bundle_migration_audit
                       (migration_key, occurred_utc, source_key, target_radio_profile_id, target_reason,
                        source_sha256, item_count, state, backup_path, result_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)""",
                    (LAUNCH_BUNDLE_MIGRATION_KEY, occurred, "launch_control_items", target, reason,
                     digest, len(items), backup.backup_dir, json.dumps(result, sort_keys=True)),
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _select_migration_target(conn: sqlite3.Connection) -> tuple[Optional[int], str]:
        row = conn.execute(
            "SELECT id FROM device_profiles WHERE runtime_primary=1 ORDER BY display_order, id LIMIT 1"
        ).fetchone()
        if row is not None:
            return int(row[0]), "runtime_primary"
        active = conn.execute(
            "SELECT id FROM device_profiles WHERE runtime_active=1 ORDER BY display_order, id"
        ).fetchall()
        if len(active) == 1:
            return int(active[0][0]), "sole_runtime_active"
        row = conn.execute(
            "SELECT id FROM device_profiles WHERE system_key='default_device' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is not None:
            return int(row[0]), "legacy_default"
        row = conn.execute("SELECT id FROM device_profiles ORDER BY enabled DESC, display_order, id LIMIT 1").fetchone()
        return (int(row[0]), "first_profile") if row is not None else (None, "no_radio")
