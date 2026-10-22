from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from freqinout.core.logger import log
from freqinout.core.operating_group_frequency import ensure_operating_group_frequency_keys
from freqinout.core.config_paths import get_config_dir
from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_runtime_write
from freqinout.core.system_timezone import detect_system_timezone_name
from freqinout.core.multi_radio_store import (
    CURRENT_MULTI_RIG_MIGRATION_VERSION,
    detect_existing_fio_usage,
    ensure_multi_rig_migration,
    ensure_multi_radio_settings_schema,
    get_multi_rig_migration_deferred,
    get_multi_rig_migration_version,
    is_multi_rig_migration_current,
    mirror_legacy_settings_into_runtime_active_device,
    set_multi_rig_migration_deferred,
    set_multi_rig_migration_version,
)

APP_NAME = "FreqInOut"
_DEFERRED_MIGRATION_LOGGED = False
_DEFERRED_MIGRATION_LOG_LOCK = threading.Lock()

# Each entry must be qualified as an additive, non-destructive transition before
# it is added here. Version 0 remains the explicit single-radio adoption gate.
_AUTOMATIC_INCREMENTAL_MULTI_RIG_MIGRATIONS = {(2, 3)}


class SettingsManager:
    """
    SQLite-backed settings store with a simple key/value table.
    Values are JSON-encoded to preserve existing data structures.
    """

    def __init__(self, *, runtime_worker: bool = False) -> None:
        # Prefer a user-writable config dir (works for both source and frozen builds)
        self.config_dir = get_config_dir() / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config_dir / "freqinout.db"
        # Backwards-compat: expose `_config_path`
        self._config_path = self.db_path

        self._conn: Optional[sqlite3.Connection] = None
        self._data: Dict[str, Any] = {}
        self._thread_id = threading.get_ident()
        self._last_timezone_sync_monotonic = 0.0
        self._runtime_worker = bool(runtime_worker)

        # Startup owns schema creation, migrations, legacy cleanup, and launch
        # bundle adoption. Recurring ingest workers need the same get/set API,
        # but must not repeat that initialization or request a WAL journal-mode
        # lock while the UI is reading the database.
        if self._runtime_worker:
            self._conn = connect_sqlite_runtime_write(self.db_path, timeout=0.5)
            self.reload()
            return

        self._init_db()
        legacy_config_imported = self._maybe_migrate_from_json()
        self.reload()
        existing_fio_usage = detect_existing_fio_usage(
            self._conn,
            self._data,
            legacy_config_exists=legacy_config_imported,
        )
        self._purge_legacy_autoquery_keys()
        migration_version = get_multi_rig_migration_version(self._conn)
        migration_deferred = get_multi_rig_migration_deferred(self._conn)
        automatic_incremental = (
            (migration_version, CURRENT_MULTI_RIG_MIGRATION_VERSION)
            in _AUTOMATIC_INCREMENTAL_MULTI_RIG_MIGRATIONS
            and not migration_deferred
        )
        if is_multi_rig_migration_current(self._conn):
            log.debug("SettingsManager: multi-rig migration marker is current.")
        elif existing_fio_usage and automatic_incremental:
            try:
                result = ensure_multi_rig_migration(
                    self._conn,
                    self._data,
                    target_version=CURRENT_MULTI_RIG_MIGRATION_VERSION,
                )
                self.reload()
                log.info(
                    "SettingsManager: applied qualified additive multi-rig migration v%s to v%s.",
                    result.from_version,
                    result.to_version,
                )
            except Exception:
                # Keep the older marker in place so runtime construction remains
                # fail-closed; startup must never claim a partial migration.
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                log.exception(
                    "SettingsManager: qualified additive multi-rig migration v%s to v%s failed safely.",
                    migration_version,
                    CURRENT_MULTI_RIG_MIGRATION_VERSION,
                )
        elif existing_fio_usage:
            global _DEFERRED_MIGRATION_LOGGED
            with _DEFERRED_MIGRATION_LOG_LOCK:
                log_first_deferred_migration = not _DEFERRED_MIGRATION_LOGGED
                _DEFERRED_MIGRATION_LOGGED = True
            message = "SettingsManager: existing FIO configuration detected; multi-rig migration remains deferred."
            if log_first_deferred_migration:
                log.info(message)
            else:
                log.debug(message)
        else:
            set_multi_rig_migration_deferred(self._conn, False)
            set_multi_rig_migration_version(
                self._conn,
                summary={
                    "fresh_install_blank_slate": True,
                    "created_device_profile_id": None,
                    "created_operating_profile_id": None,
                    "note": "Fresh multi-rig installs start with no radios; use Add Radio / Configure Automatically.",
                },
            )
            log.info(
                "SettingsManager: initialized fresh multi-rig blank slate; no default radio was created."
            )
            self.reload()
        self._ensure_launch_bundle_migration()
        self._sync_system_timezone(force=True)

    # ---------- internal I/O ---------- #

    def _init_db(self) -> None:
        self._conn = connect_sqlite(self.db_path)
        ensure_multi_radio_settings_schema(self._conn)

    def _maybe_migrate_from_json(self) -> bool:
        """
        If the kv table is empty and a legacy config.json exists, import it once.
        """
        cur = self._conn.execute("SELECT COUNT(*) FROM kv")
        count = cur.fetchone()[0]
        if count:
            return False
        legacy = self.config_dir / "config.json"
        if not legacy.exists():
            return False
        try:
            data = json.loads(legacy.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                return False
            self._bulk_write(data)
            log.info("SettingsManager: migrated legacy config.json into %s", self.db_path)
            return detect_existing_fio_usage(self._conn, data, legacy_config_exists=False)
        except Exception as e:
            log.error("SettingsManager: migration from config.json failed: %s", e)
            return False

    def _ensure_launch_bundle_migration(self) -> None:
        """Run the additive launch-ownership migration from the startup migration owner."""
        if not is_multi_rig_migration_current(self._conn):
            log.debug("SettingsManager: launch bundle migration waits for multi-rig ownership migration.")
            return
        try:
            from freqinout.core.launch_bundle_store import LaunchBundleStore

            result = LaunchBundleStore(self.db_path).migrate_legacy(self._data)
            state = str(result.get("state", "") or "")
            if state == "confirmed" and not bool(result.get("already_applied", False)):
                log.info(
                    "SettingsManager: migrated Launch Control to radio %s (%s item(s)); legacy keys retained read-only.",
                    result.get("target_radio_profile_id"),
                    result.get("item_count", 0),
                )
            elif state == "deferred":
                log.debug("SettingsManager: launch bundle migration deferred until a radio exists.")
        except Exception as exc:
            # The source keys remain intact and provide a read-only fallback.
            log.error("SettingsManager: launch bundle migration failed safely: %s", exc)

    def _bulk_write(self, data: Dict[str, Any]) -> None:
        payload = [(k, json.dumps(v)) for k, v in data.items()]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", payload
            )

    def _purge_legacy_autoquery_keys(self) -> None:
        """
        Remove deprecated JS8 auto-query keys from the settings table.
        """
        if self._data.get("autoquery_keys_purged_v1"):
            return
        keys = ("js8_auto_query_msg_id", "js8_auto_query_grids")
        try:
            with self._conn:
                self._conn.executemany("DELETE FROM kv WHERE key=?", [(k,) for k in keys])
                self._conn.execute(
                    "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
                    ("autoquery_keys_purged_v1", json.dumps(True)),
                )
            for k in keys:
                self._data.pop(k, None)
            self._data["autoquery_keys_purged_v1"] = True
        except Exception as e:
            log.error("SettingsManager: failed to purge legacy autoquery keys: %s", e)

    def reload(self) -> None:
        """Reload settings from SQLite into the in-memory dict."""
        self._assert_thread_affinity()
        cur = self._conn.execute("SELECT key, value FROM kv")
        loaded: Dict[str, Any] = {}
        for key, val in cur.fetchall():
            try:
                loaded[key] = json.loads(val)
            except Exception:
                loaded[key] = val
        groups = loaded.get("operating_groups")
        if isinstance(groups, list) and all(isinstance(row, dict) for row in groups):
            loaded["operating_groups"] = ensure_operating_group_frequency_keys(groups)
        self._data = loaded
        from freqinout.core.station_bbs_state import load_station_bbs_catalog
        self._station_bbs_catalog = load_station_bbs_catalog(self._conn)

    # ---------- public persistence API ---------- #

    def save(self) -> None:
        """Compatibility no-op: data is written immediately on set."""
        return

    def write(self) -> None:
        """Compatibility no-op: data is written immediately on set."""
        return

    # ---------- public data API ---------- #

    def _assert_thread_affinity(self) -> None:
        current_thread_id = threading.get_ident()
        if current_thread_id == self._thread_id:
            return
        raise sqlite3.ProgrammingError(
            "SettingsManager used from a different thread than it was created on "
            f"(created={self._thread_id}, current={current_thread_id}). "
            "Create a new SettingsManager in the worker thread instead of sharing this instance."
        )

    def _sync_system_timezone(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_timezone_sync_monotonic) < 30.0:
            return
        self._last_timezone_sync_monotonic = now

        current = str(self._data.get("timezone") or "").strip() or "UTC"
        detected = detect_system_timezone_name(current)
        if detected == current:
            return

        self._data["timezone"] = detected
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
                    ("timezone", json.dumps(detected)),
                )
                try:
                    mirror_legacy_settings_into_runtime_active_device(
                        self._conn,
                        self._data,
                        keys_changed={"timezone"},
                    )
                except Exception as mirror_exc:
                    log.error(
                        "SettingsManager: failed to mirror timezone into multi-radio store: %s",
                        mirror_exc,
                    )
            log.info("SettingsManager: synced timezone to %s (was %s)", detected, current)
        except Exception as e:
            log.error("SettingsManager: failed to sync timezone %s: %s", detected, e)
            raise

    def get(self, key: str, default: Any = None) -> Any:
        self._assert_thread_affinity()
        from freqinout.core.station_bbs_state import BBS_CATALOG_SETTINGS_KEYS, project_station_bbs_settings
        catalog = getattr(self, '_station_bbs_catalog', None)
        if catalog is not None and key in BBS_CATALOG_SETTINGS_KEYS:
            return project_station_bbs_settings({}, catalog).get(key, default)
        if key == "timezone" and not self._runtime_worker:
            self._sync_system_timezone()
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """
        Set a key and immediately persist to SQLite.
        """
        self._assert_thread_affinity()
        if key == "operating_groups" and isinstance(value, list) and all(isinstance(row, dict) for row in value):
            value = ensure_operating_group_frequency_keys(value)
        candidate = {**self._data, key: value}
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
                    (key, json.dumps(value)),
                )
                mirror_legacy_settings_into_runtime_active_device(
                    self._conn, candidate, keys_changed={key},
                )
            self._data[key] = value
        except Exception as e:
            log.error("SettingsManager: failed to write key %s: %s", key, e)
            raise

    def set_many(self, values: Dict[str, Any], *, save: bool = True) -> None:
        """
        Batch update multiple keys. Saves once by default to avoid repeated writes.
        """
        self._assert_thread_affinity()
        values = dict(values)
        groups = values.get("operating_groups")
        if isinstance(groups, list) and all(isinstance(row, dict) for row in groups):
            values["operating_groups"] = ensure_operating_group_frequency_keys(groups)
        candidate = {**self._data, **values}
        try:
            payload = [(k, json.dumps(v)) for k, v in values.items()]
            with self._conn:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", payload
                )
                mirror_legacy_settings_into_runtime_active_device(
                    self._conn, candidate, keys_changed=set(values),
                )
            self._data.update(values)
        except Exception as e:
            log.error("SettingsManager: failed to batch write: %s", e)
            raise

    def all(self) -> Dict[str, Any]:
        """
        Return the cached effective settings view. Use set/set_many to update
        persisted operator values; canonical BBS metadata is a read projection.
        """
        self._assert_thread_affinity()
        from freqinout.core.station_bbs_state import project_station_bbs_settings
        return project_station_bbs_settings(self._data, getattr(self, '_station_bbs_catalog', None))

    def publish_station_bbs_catalog(self, snapshot) -> None:
        """Accept detached worker-published metadata without UI-thread I/O."""
        from copy import deepcopy
        self._assert_thread_affinity()
        self._station_bbs_catalog = deepcopy(snapshot)

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
