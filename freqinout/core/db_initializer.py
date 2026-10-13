"""
Ensure core SQLite tables exist before the UI starts.
This avoids runtime errors such as "no such table: js8_links".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Set

from freqinout.core.checkins_db import ensure_operator_checkins_schema
from freqinout.core.commstat_artifacts import ensure_commstat_artifact_tables
from freqinout.core.logger import log
from freqinout.core.config_paths import get_config_dir
from freqinout.core.group_utils import normalize_group_name
from freqinout.core.js8_message_schema import ensure_js8_message_cache_schema
from freqinout.core.message_projection_store import ensure_message_projection_schema
from freqinout.core.message_projection_queue import ensure_source_dirty_triggers
from freqinout.core.multi_radio_store import ensure_multi_radio_settings_schema
from freqinout.core.operator_activity import ensure_js8_callsign_stats
from freqinout.core.observation_store import ensure_observation_schema
from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.resource_catalog_migration import cutover_resource_catalog_to_canonical
from freqinout.core.shortwave_store import ensure_shortwave_schema
from freqinout.core.sqlite_utils import connect_sqlite
from freqinout.core.varac_ingest import ensure_varac_local_tables
from freqinout.core.varac_bbs_library_store import (
    ensure_bbs_library_schema,
    import_legacy_station_bbs_profiles,
)

def _config_dir() -> Path:
    """Resolve the active profile config directory at call time."""
    return get_config_dir() / "config"


def _ensure_settings_db() -> None:
    """
    Ensure settings DB (freqinout.db) has the compatibility kv table and
    Wave 1 multi-rig settings schema.
    """
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    db_path = config_dir / "freqinout.db"
    conn = connect_sqlite(db_path)
    try:
        ensure_multi_radio_settings_schema(conn)
        conn.commit()
        ensure_bbs_library_schema(conn)
        conn.row_factory = sqlite3.Row
        profile_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM device_profiles ORDER BY runtime_primary DESC, runtime_active DESC, display_order, id"
            ).fetchall()
        ]
        import_legacy_station_bbs_profiles(conn, profile_rows)
        conn.commit()
    finally:
        conn.close()


def _ensure_operator_checkins(conn: sqlite3.Connection) -> None:
    """
    Ensure operator_checkins has the unified schema and one-time data repairs
    before any UI path touches it.
    """
    ensure_operator_checkins_schema(conn, repair_data=True)


def _ensure_local_operator_tables(conn: sqlite3.Connection) -> None:
    """
    Ensure local operator and local NCS check-in tables exist.
    """
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS local_operator_checkins (
            callsign TEXT PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            name TEXT,
            city TEXT,
            state TEXT,
            category TEXT,
            first_seen_utc TEXT,
            last_seen_utc TEXT,
            checkin_count INTEGER DEFAULT 0,
            notes TEXT,
            sitrep_status TEXT DEFAULT 'GREEN',
            updated_utc TEXT
        )
        """
    )
    _ensure_columns(
        conn,
        "local_operator_checkins",
        {
            "callsign": "TEXT",
            "first_name": "TEXT",
            "last_name": "TEXT",
            "name": "TEXT",
            "city": "TEXT",
            "state": "TEXT",
            "category": "TEXT",
            "first_seen_utc": "TEXT",
            "last_seen_utc": "TEXT",
            "checkin_count": "INTEGER DEFAULT 0",
            "notes": "TEXT",
            "sitrep_status": "TEXT DEFAULT 'GREEN'",
            "updated_utc": "TEXT",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ops_last_seen ON local_operator_checkins(last_seen_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ops_category ON local_operator_checkins(category)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ops_status ON local_operator_checkins(sitrep_status)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS local_ncs_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkin_utc TEXT NOT NULL,
            net_name TEXT,
            channels TEXT,
            callsign TEXT NOT NULL,
            first_name TEXT,
            last_name TEXT,
            name TEXT,
            city TEXT,
            state TEXT,
            category TEXT,
            sitrep_status TEXT DEFAULT 'GREEN',
            notes TEXT,
            updated_utc TEXT
        )
        """
    )
    _ensure_columns(
        conn,
        "local_ncs_checkins",
        {
            "checkin_utc": "TEXT",
            "net_name": "TEXT",
            "channels": "TEXT",
            "callsign": "TEXT",
            "first_name": "TEXT",
            "last_name": "TEXT",
            "name": "TEXT",
            "city": "TEXT",
            "state": "TEXT",
            "category": "TEXT",
            "sitrep_status": "TEXT DEFAULT 'GREEN'",
            "notes": "TEXT",
            "updated_utc": "TEXT",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ncs_checkins_ts ON local_ncs_checkins(checkin_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_ncs_checkins_callsign ON local_ncs_checkins(callsign)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS local_operator_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT,
            source_kind TEXT,
            source_channel TEXT,
            net_session_id TEXT,
            callsign TEXT,
            operator_id TEXT,
            from_name TEXT,
            city TEXT,
            county TEXT,
            state TEXT,
            grid TEXT,
            lat REAL,
            lon REAL,
            location_source TEXT,
            location_confidence TEXT,
            status TEXT,
            topics_json TEXT,
            topic_evidence_json TEXT,
            subject TEXT,
            body TEXT,
            confirmed_state TEXT,
            followup_state TEXT,
            exercise_flag INTEGER DEFAULT 0,
            source_radio_id INTEGER,
            source_app TEXT,
            raw_reference TEXT,
            created_by TEXT,
            updated_by TEXT
        )
        """
    )
    _ensure_columns(
        conn,
        "local_operator_reports",
        {
            "created_utc": "TEXT",
            "updated_utc": "TEXT",
            "source_kind": "TEXT",
            "source_channel": "TEXT",
            "net_session_id": "TEXT",
            "callsign": "TEXT",
            "operator_id": "TEXT",
            "from_name": "TEXT",
            "city": "TEXT",
            "county": "TEXT",
            "state": "TEXT",
            "grid": "TEXT",
            "lat": "REAL",
            "lon": "REAL",
            "location_source": "TEXT",
            "location_confidence": "TEXT",
            "status": "TEXT",
            "topics_json": "TEXT",
            "topic_evidence_json": "TEXT",
            "subject": "TEXT",
            "body": "TEXT",
            "confirmed_state": "TEXT",
            "followup_state": "TEXT",
            "exercise_flag": "INTEGER DEFAULT 0",
            "source_radio_id": "INTEGER",
            "source_app": "TEXT",
            "raw_reference": "TEXT",
            "created_by": "TEXT",
            "updated_by": "TEXT",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_created ON local_operator_reports(created_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_callsign ON local_operator_reports(callsign)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_state ON local_operator_reports(state)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_grid ON local_operator_reports(grid)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_local_reports_status ON local_operator_reports(status)")


def _ensure_js8_links(conn: sqlite3.Connection) -> None:
    """
    Ensure js8_links exists with the expected columns.
    """
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_links (
            ts REAL,
            origin TEXT,
            destination TEXT,
            snr REAL,
            band TEXT,
            freq_hz REAL,
            source_id TEXT,
            app_instance_id TEXT,
            source_radio_id TEXT,
            is_relay INTEGER DEFAULT 0,
            relay_via TEXT,
            is_spotter INTEGER DEFAULT 0,
            last_seen_utc TEXT
        )
        """
    )
    cur.execute("PRAGMA table_info(js8_links)")
    cols = {row[1] for row in cur.fetchall()}
    for name in ("source_id", "app_instance_id", "source_radio_id", "last_seen_utc"):
        if name not in cols:
            cur.execute(f"ALTER TABLE js8_links ADD COLUMN {name} TEXT")
    if "direction_known" not in cols:
        cur.execute("ALTER TABLE js8_links ADD COLUMN direction_known INTEGER NOT NULL DEFAULT 0")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_ts ON js8_links(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_origin_ts ON js8_links(origin, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_destination_ts ON js8_links(destination, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_band ON js8_links(band)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_source ON js8_links(source_id, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_app_instance ON js8_links(app_instance_id, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_radio ON js8_links(source_radio_id, ts)")
    ensure_js8_callsign_stats(conn, rebuild_if_empty=True)


def _ensure_controlfreq_support_indexes(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    tables = {
        str(row[0])
        for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if row and row[0]
    }
    if "js8_messages" in tables:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_js8_messages_utc_ts ON js8_messages(utc_ts DESC, from_call)"
        )
    if "spotter_traffic" in tables:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_spotter_traffic_utc_ts ON spotter_traffic(utc_ts DESC, from_call, id DESC)"
        )
    if "fldigi_checkins" in tables:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_fldigi_checkins_last_seen_ts ON fldigi_checkins(last_seen_ts DESC, callsign)"
        )


def _ensure_js8_expect_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_msg_auth_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT,
            callsign TEXT,
            label TEXT,
            key_text TEXT,
            key_scope TEXT DEFAULT 'signing',
            enabled INTEGER DEFAULT 1,
            notes TEXT,
            created_ts REAL,
            updated_ts REAL
        )
        """
    )
    _ensure_columns(
        conn,
        "js8_msg_auth_keys",
        {
            "group_name": "TEXT",
            "callsign": "TEXT",
            "label": "TEXT",
            "key_text": "TEXT",
            "key_scope": "TEXT DEFAULT 'signing'",
            "enabled": "INTEGER DEFAULT 1",
            "notes": "TEXT",
            "created_ts": "REAL",
            "updated_ts": "REAL",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_msg_auth_keys_scope ON js8_msg_auth_keys(group_name, callsign)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_js8_msg_auth_keys_purpose ON js8_msg_auth_keys(key_scope, group_name, callsign)"
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_expect_allow_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            allowed_callsigns_json TEXT DEFAULT '[]',
            allowed_groups_json TEXT DEFAULT '[]',
            allow_trusted_operators INTEGER DEFAULT 0,
            trusted_operator_groups_json TEXT DEFAULT '[]',
            blocked_callsigns_json TEXT DEFAULT '[]',
            source_scope TEXT DEFAULT 'all',
            source_radio_ids_json TEXT DEFAULT '[]',
            enabled INTEGER DEFAULT 1,
            import_source TEXT,
            notes TEXT,
            created_ts REAL,
            updated_ts REAL
        )
        """
    )
    _ensure_columns(
        conn,
        "js8_expect_allow_policies",
        {
            "name": "TEXT",
            "allowed_callsigns_json": "TEXT DEFAULT '[]'",
            "allowed_groups_json": "TEXT DEFAULT '[]'",
            "allow_trusted_operators": "INTEGER DEFAULT 0",
            "trusted_operator_groups_json": "TEXT DEFAULT '[]'",
            "blocked_callsigns_json": "TEXT DEFAULT '[]'",
            "source_scope": "TEXT DEFAULT 'all'",
            "source_radio_ids_json": "TEXT DEFAULT '[]'",
            "enabled": "INTEGER DEFAULT 1",
            "import_source": "TEXT",
            "notes": "TEXT",
            "created_ts": "REAL",
            "updated_ts": "REAL",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_expect_allow_policies_name ON js8_expect_allow_policies(name)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_expect_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_radio_id TEXT,
            source_scope TEXT DEFAULT 'all',
            js8_instance_id TEXT,
            allow_policy_id INTEGER,
            expect_key TEXT,
            response_text TEXT,
            msg_auth_sign_enabled INTEGER DEFAULT 0,
            msg_auth_sign_callsign TEXT,
            msg_auth_include_datecode INTEGER DEFAULT 0,
            msg_auth_datecode TEXT,
            allowed_callsigns_json TEXT DEFAULT '[]',
            allowed_groups_json TEXT DEFAULT '[]',
            allow_any INTEGER DEFAULT 0,
            allow_trusted_operators INTEGER DEFAULT 0,
            trusted_operator_groups_json TEXT DEFAULT '[]',
            blocked_callsigns_json TEXT DEFAULT '[]',
            max_replies INTEGER DEFAULT 1,
            cooldown_seconds INTEGER DEFAULT 0,
            tx_speed TEXT,
            auto_tx_schedule TEXT,
            auto_reply_enabled INTEGER DEFAULT 0,
            unattended_auto_reply_enabled INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            import_source TEXT,
            created_ts REAL,
            updated_ts REAL
        )
        """
    )
    _ensure_columns(
        conn,
        "js8_expect_entries",
        {
            "source_radio_id": "TEXT",
            "source_scope": "TEXT DEFAULT 'all'",
            "js8_instance_id": "TEXT",
            "allow_policy_id": "INTEGER",
            "expect_key": "TEXT",
            "response_text": "TEXT",
            "msg_auth_sign_enabled": "INTEGER DEFAULT 0",
            "msg_auth_sign_callsign": "TEXT",
            "msg_auth_include_datecode": "INTEGER DEFAULT 0",
            "msg_auth_datecode": "TEXT",
            "allowed_callsigns_json": "TEXT DEFAULT '[]'",
            "allowed_groups_json": "TEXT DEFAULT '[]'",
            "allow_any": "INTEGER DEFAULT 0",
            "allow_trusted_operators": "INTEGER DEFAULT 0",
            "trusted_operator_groups_json": "TEXT DEFAULT '[]'",
            "blocked_callsigns_json": "TEXT DEFAULT '[]'",
            "max_replies": "INTEGER DEFAULT 1",
            "cooldown_seconds": "INTEGER DEFAULT 0",
            "tx_speed": "TEXT",
            "auto_tx_schedule": "TEXT",
            "auto_reply_enabled": "INTEGER DEFAULT 0",
            "unattended_auto_reply_enabled": "INTEGER DEFAULT 0",
            "enabled": "INTEGER DEFAULT 1",
            "import_source": "TEXT",
            "created_ts": "REAL",
            "updated_ts": "REAL",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_expect_entries_key ON js8_expect_entries(expect_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_js8_expect_entries_policy ON js8_expect_entries(allow_policy_id)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_js8_expect_entries_source ON js8_expect_entries(source_scope, source_radio_id, js8_instance_id)"
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_expect_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            expect_entry_id INTEGER,
            expect_key TEXT,
            source_radio_id TEXT,
            source_js8_instance_id TEXT,
            requesting_callsign TEXT,
            target_group TEXT,
            decision TEXT,
            reason TEXT,
            reply_radio_id TEXT,
            reply_js8_instance_id TEXT,
            created_ts REAL
        )
        """
    )
    _ensure_columns(
        conn,
        "js8_expect_audit",
        {
            "event_id": "TEXT",
            "expect_entry_id": "INTEGER",
            "expect_key": "TEXT",
            "source_radio_id": "TEXT",
            "source_js8_instance_id": "TEXT",
            "requesting_callsign": "TEXT",
            "target_group": "TEXT",
            "decision": "TEXT",
            "reason": "TEXT",
            "reply_radio_id": "TEXT",
            "reply_js8_instance_id": "TEXT",
            "created_ts": "REAL",
        },
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_js8_expect_audit_created_call ON js8_expect_audit(created_ts DESC, requesting_callsign)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_expect_dispatch_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            expect_entry_id INTEGER,
            expect_key TEXT,
            source_radio_id TEXT,
            source_js8_instance_id TEXT,
            requesting_callsign TEXT,
            target_group TEXT,
            decision TEXT,
            reason TEXT,
            reply_radio_id TEXT,
            reply_js8_instance_id TEXT,
            transmitted_text TEXT,
            created_ts REAL
        )
        """
    )
    _ensure_columns(
        conn,
        "js8_expect_dispatch_audit",
        {
            "event_id": "TEXT",
            "expect_entry_id": "INTEGER",
            "expect_key": "TEXT",
            "source_radio_id": "TEXT",
            "source_js8_instance_id": "TEXT",
            "requesting_callsign": "TEXT",
            "target_group": "TEXT",
            "decision": "TEXT",
            "reason": "TEXT",
            "reply_radio_id": "TEXT",
            "reply_js8_instance_id": "TEXT",
            "transmitted_text": "TEXT",
            "created_ts": "REAL",
        },
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_js8_expect_dispatch_audit_created ON js8_expect_dispatch_audit(created_ts DESC, expect_key)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_expect_management_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expect_entry_id INTEGER,
            action TEXT NOT NULL,
            expect_key TEXT,
            source_radio_id TEXT,
            source_scope TEXT,
            js8_instance_id TEXT,
            enabled INTEGER,
            auto_reply_enabled INTEGER,
            import_source TEXT,
            detail_json TEXT,
            created_ts REAL NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "js8_expect_management_audit",
        {
            "expect_entry_id": "INTEGER",
            "action": "TEXT",
            "expect_key": "TEXT",
            "source_radio_id": "TEXT",
            "source_scope": "TEXT",
            "js8_instance_id": "TEXT",
            "enabled": "INTEGER",
            "auto_reply_enabled": "INTEGER",
            "import_source": "TEXT",
            "detail_json": "TEXT",
            "created_ts": "REAL",
        },
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_js8_expect_management_audit_recent
        ON js8_expect_management_audit(created_ts DESC, expect_entry_id)
        """
    )
    _ensure_fio_spotter_tables(conn)
    _ensure_flamp_dynamic_tables(conn)


def _ensure_fio_spotter_tables(conn: sqlite3.Connection) -> None:
    """Create the additive station-owned watch service schema."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fio_spotter_watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            watch_kind TEXT NOT NULL,
            pattern TEXT NOT NULL,
            criteria_json TEXT NOT NULL DEFAULT '{}',
            match_mode TEXT NOT NULL DEFAULT 'contains',
            priority TEXT NOT NULL DEFAULT 'watch',
            source_families_json TEXT NOT NULL DEFAULT '[]',
            source_radio_ids_json TEXT NOT NULL DEFAULT '[]',
            notification_mode TEXT NOT NULL DEFAULT 'in-app',
            expires_ts REAL NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_match_ts REAL NOT NULL DEFAULT 0,
            match_count INTEGER NOT NULL DEFAULT 0,
            health TEXT NOT NULL DEFAULT 'ready',
            import_source TEXT,
            notes TEXT,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "fio_spotter_watches",
        {
            "name": "TEXT NOT NULL DEFAULT ''",
            "watch_kind": "TEXT NOT NULL DEFAULT 'keyword'",
            "pattern": "TEXT NOT NULL DEFAULT ''",
            "criteria_json": "TEXT NOT NULL DEFAULT '{}'",
            "match_mode": "TEXT NOT NULL DEFAULT 'contains'",
            "priority": "TEXT NOT NULL DEFAULT 'watch'",
            "source_families_json": "TEXT NOT NULL DEFAULT '[]'",
            "source_radio_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "notification_mode": "TEXT NOT NULL DEFAULT 'in-app'",
            "expires_ts": "REAL NOT NULL DEFAULT 0",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "last_match_ts": "REAL NOT NULL DEFAULT 0",
            "match_count": "INTEGER NOT NULL DEFAULT 0",
            "health": "TEXT NOT NULL DEFAULT 'ready'",
            "import_source": "TEXT",
            "notes": "TEXT",
            "created_ts": "REAL NOT NULL DEFAULT 0",
            "updated_ts": "REAL NOT NULL DEFAULT 0",
        },
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fio_spotter_watches_enabled_kind "
        "ON fio_spotter_watches(enabled, watch_kind, priority, updated_ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fio_spotter_watches_expiry "
        "ON fio_spotter_watches(expires_ts, enabled)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fio_spotter_watch_matches (
            watch_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            matched_ts REAL NOT NULL,
            PRIMARY KEY (watch_id, message_id),
            FOREIGN KEY (watch_id) REFERENCES fio_spotter_watches(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fio_spotter_watch_matches_recent "
        "ON fio_spotter_watch_matches(matched_ts DESC, watch_id)"
    )


def _ensure_flamp_dynamic_tables(conn: sqlite3.Connection) -> None:
    """Create the durable, source-scoped state used by dynamic FLAMP Expect.

    These tables are additive and intentionally do not alter or delete any
    existing traffic/message rows.  They live beside the Expect tables because
    both the transfer projection and request claims are part of the FIO Spotter
    runtime database.
    """
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS flamp_transfer_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            q_id TEXT NOT NULL,
            source_radio_id TEXT NOT NULL DEFAULT '',
            source_js8_instance_id TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            source_mtime_ns INTEGER NOT NULL DEFAULT 0,
            source_size_bytes INTEGER NOT NULL DEFAULT 0,
            source_sha256 TEXT NOT NULL DEFAULT '',
            transfer_filename TEXT NOT NULL DEFAULT '',
            expected_file_size INTEGER,
            completion_path TEXT NOT NULL DEFAULT '',
            evidence_kind TEXT NOT NULL DEFAULT 'relay_snapshot',
            parser_version INTEGER NOT NULL DEFAULT 0,
            total_blocks INTEGER,
            available_blocks_json TEXT NOT NULL DEFAULT '[]',
            missing_blocks_json TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'unavailable',
            parser_confidence REAL NOT NULL DEFAULT 0,
            observed_ts REAL NOT NULL DEFAULT 0,
            validated_scan_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0,
            UNIQUE(q_id, source_radio_id, source_js8_instance_id)
        )
        """
    )
    _ensure_columns(
        conn,
        "flamp_transfer_state",
        {
            "q_id": "TEXT NOT NULL DEFAULT ''",
            "source_radio_id": "TEXT NOT NULL DEFAULT ''",
            "source_js8_instance_id": "TEXT NOT NULL DEFAULT ''",
            "source_path": "TEXT NOT NULL DEFAULT ''",
            "source_mtime_ns": "INTEGER NOT NULL DEFAULT 0",
            "source_size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "source_sha256": "TEXT NOT NULL DEFAULT ''",
            "transfer_filename": "TEXT NOT NULL DEFAULT ''",
            "expected_file_size": "INTEGER",
            "completion_path": "TEXT NOT NULL DEFAULT ''",
            "evidence_kind": "TEXT NOT NULL DEFAULT 'relay_snapshot'",
            "parser_version": "INTEGER NOT NULL DEFAULT 0",
            "total_blocks": "INTEGER",
            "available_blocks_json": "TEXT NOT NULL DEFAULT '[]'",
            "missing_blocks_json": "TEXT NOT NULL DEFAULT '[]'",
            "state": "TEXT NOT NULL DEFAULT 'unavailable'",
            "parser_confidence": "REAL NOT NULL DEFAULT 0",
            "observed_ts": "REAL NOT NULL DEFAULT 0",
            "validated_scan_ts": "REAL NOT NULL DEFAULT 0",
            "updated_ts": "REAL NOT NULL DEFAULT 0",
        },
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_flamp_transfer_state_source ON flamp_transfer_state(q_id, source_radio_id, source_js8_instance_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_flamp_transfer_state_lookup ON flamp_transfer_state(q_id, state, source_radio_id, source_js8_instance_id)"
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS flamp_transfer_state_scans (
            source_radio_id TEXT NOT NULL DEFAULT '',
            source_js8_instance_id TEXT NOT NULL DEFAULT '',
            relay_dir TEXT NOT NULL DEFAULT '',
            receive_dir TEXT NOT NULL DEFAULT '',
            scan_success INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            error_text TEXT NOT NULL DEFAULT '',
            scanned_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(source_radio_id, source_js8_instance_id)
        )
        """
    )
    _ensure_columns(
        conn,
        "flamp_transfer_state_scans",
        {
            "source_radio_id": "TEXT NOT NULL DEFAULT ''",
            "source_js8_instance_id": "TEXT NOT NULL DEFAULT ''",
            "relay_dir": "TEXT NOT NULL DEFAULT ''",
            "receive_dir": "TEXT NOT NULL DEFAULT ''",
            "scan_success": "INTEGER NOT NULL DEFAULT 0",
            "file_count": "INTEGER NOT NULL DEFAULT 0",
            "error_text": "TEXT NOT NULL DEFAULT ''",
            "scanned_ts": "REAL NOT NULL DEFAULT 0",
        },
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_expect_request_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            expect_entry_id INTEGER NOT NULL DEFAULT 0,
            q_id TEXT NOT NULL DEFAULT '',
            source_radio_id TEXT NOT NULL DEFAULT '',
            source_js8_instance_id TEXT NOT NULL DEFAULT '',
            requesting_callsign TEXT NOT NULL DEFAULT '',
            target_group TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'claimed',
            reply_text TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 1,
            claimed_ts REAL NOT NULL DEFAULT 0,
            sent_ts REAL,
            next_retry_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0
        )
        """
    )
    _ensure_columns(
        conn,
        "js8_expect_request_claims",
        {
            "event_key": "TEXT NOT NULL DEFAULT ''",
            "expect_entry_id": "INTEGER NOT NULL DEFAULT 0",
            "q_id": "TEXT NOT NULL DEFAULT ''",
            "source_radio_id": "TEXT NOT NULL DEFAULT ''",
            "source_js8_instance_id": "TEXT NOT NULL DEFAULT ''",
            "requesting_callsign": "TEXT NOT NULL DEFAULT ''",
            "target_group": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'claimed'",
            "reply_text": "TEXT NOT NULL DEFAULT ''",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "attempts": "INTEGER NOT NULL DEFAULT 1",
            "claimed_ts": "REAL NOT NULL DEFAULT 0",
            "sent_ts": "REAL",
            "next_retry_ts": "REAL NOT NULL DEFAULT 0",
            "updated_ts": "REAL NOT NULL DEFAULT 0",
        },
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_js8_expect_claims_policy ON js8_expect_request_claims(expect_entry_id, q_id, source_radio_id, source_js8_instance_id, requesting_callsign, status)"
    )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    missing = [(name, col_type) for name, col_type in columns.items() if name not in existing]
    if not missing:
        return
    cur.execute("SAVEPOINT fio_ensure_columns")
    try:
        for name, col_type in missing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT fio_ensure_columns")
        cur.execute("RELEASE SAVEPOINT fio_ensure_columns")
        raise
    cur.execute("RELEASE SAVEPOINT fio_ensure_columns")


def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,))
    return cur.fetchone() is not None


def _repair_group_column(conn: sqlite3.Connection, table: str, column: str, *, preserve_all: bool = False) -> int:
    if not _table_exists(conn, table) or column not in _table_columns(conn, table):
        return 0
    cur = conn.cursor()
    # This is a compatibility repair, not a startup projection.  Older builds
    # loaded every row from several traffic tables into Python on every launch,
    # even after all values were canonical.  On production histories that made
    # database initialization take more than a minute.  Let SQLite identify
    # only values which normalization can actually change.
    cur.execute(
        f"""SELECT rowid, {column} FROM {table}
             WHERE COALESCE({column}, '') <> ''
               AND (
                    {column} <> TRIM({column})
                    OR {column} <> UPPER({column})
                    OR SUBSTR(TRIM({column}), 1, 1) = '@'
               )"""
    )
    changed = 0
    for rowid, value in cur.fetchall():
        original = str(value or "").strip()
        if preserve_all and original == "__ALL__":
            continue
        normalized = normalize_group_name(original)
        if normalized != original:
            cur.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (normalized, rowid))
            changed += 1
    return changed


def _repair_sitrep_commstat_groups(conn: sqlite3.Connection) -> None:
    changed = 0
    for table, column, preserve_all in (
        ("sitrep_source_events", "report_group", False),
        ("sitrep_events", "report_group", False),
        ("sitrep_latest_by_callsign", "latest_report_group", False),
        ("commstat_artifacts", "report_group", False),
    ):
        changed += _repair_group_column(conn, table, column, preserve_all=preserve_all)
    # Rebuilding the rollup is expensive and is only necessary when the source
    # repair changed at least one identity.  Recomputing it unconditionally was
    # the dominant startup cost on mature production databases.
    if changed and _table_exists(conn, "sitrep_latest_by_callsign") and _table_exists(conn, "sitrep_state_rollup"):
        try:
            from freqinout.core.sitrep_fusion import _refresh_state_rollups

            _refresh_state_rollups(conn)
        except Exception as e:
            log.debug("DB init: SitRep state rollup repair refresh failed: %s", e)
    if changed:
        log.info("DB init: normalized %d SitRep/CommStat group value(s).", changed)


def _ensure_prop_contact_events(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS prop_contact_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            origin_callsign TEXT,
            origin_grid6 TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_callsign TEXT,
            target_grid6 TEXT,
            band TEXT NOT NULL,
            mode TEXT,
            freq_hz REAL,
            distance_km REAL,
            outcome TEXT NOT NULL,
            source TEXT NOT NULL,
            source_ref TEXT,
            source_key TEXT,
            app_instance_id TEXT,
            source_radio_id TEXT,
            inserted_utc TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "prop_contact_events",
        {
            "event_key": "TEXT",
            "ts_utc": "TEXT",
            "origin_callsign": "TEXT",
            "origin_grid6": "TEXT",
            "target_type": "TEXT",
            "target_id": "TEXT",
            "target_callsign": "TEXT",
            "target_grid6": "TEXT",
            "band": "TEXT",
            "mode": "TEXT",
            "freq_hz": "REAL",
            "distance_km": "REAL",
            "outcome": "TEXT",
            "source": "TEXT",
            "source_ref": "TEXT",
            "source_key": "TEXT",
            "app_instance_id": "TEXT",
            "source_radio_id": "TEXT",
            "inserted_utc": "TEXT",
        },
    )
    cols = _table_columns(conn, "prop_contact_events")
    if "id" in cols and "event_key" in cols:
        # Safety: backfill missing keys in older/dev DBs before enforcing uniqueness.
        cur.execute(
            """
            UPDATE prop_contact_events
            SET event_key = 'legacy:' || id
            WHERE event_key IS NULL OR TRIM(event_key) = ''
            """
        )
    # Safety repairs must be idempotent *and* cheap on a mature database.  The
    # old unconditional UPDATEs rewrote every propagation row on every launch,
    # while the unconditional GROUP BY/DELETE scanned the whole table even
    # after uniqueness had already been enforced.  Repair only malformed rows,
    # and perform the legacy de-duplication only once while creating the unique
    # index that prevents future duplicates.
    for column in (
        "target_type",
        "outcome",
        "source",
        "band",
        "origin_grid6",
        "target_grid6",
    ):
        cur.execute(
            f"""UPDATE prop_contact_events
                   SET {column} = UPPER(TRIM({column}))
                 WHERE {column} IS NOT NULL
                   AND {column} <> UPPER(TRIM({column}))"""
        )
    unique_index_exists = bool(
        cur.execute(
            """SELECT 1 FROM sqlite_master
                 WHERE type='index' AND name='idx_prop_contact_events_event_key'
                 LIMIT 1"""
        ).fetchone()
    )
    if not unique_index_exists:
        cur.execute(
            """
            DELETE FROM prop_contact_events
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM prop_contact_events
                GROUP BY event_key
            )
            """
        )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_prop_contact_events_event_key ON prop_contact_events(event_key)"
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prop_contact_events_lookup
        ON prop_contact_events(origin_grid6, target_type, target_id, band, ts_utc)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prop_contact_events_pooled_lookup
        ON prop_contact_events(origin_grid6, target_type, band, ts_utc DESC)
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_contact_events_source ON prop_contact_events(source, ts_utc)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prop_contact_events_inserted ON prop_contact_events(inserted_utc)")


def _ensure_prop_ingest_checkpoint(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS prop_ingest_checkpoint (
            source TEXT PRIMARY KEY,
            last_ts_utc TEXT,
            last_source_ref TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "prop_ingest_checkpoint",
        {
            "source": "TEXT",
            "last_ts_utc": "TEXT",
            "last_source_ref": "TEXT",
            "updated_utc": "TEXT",
        },
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_ingest_checkpoint_updated ON prop_ingest_checkpoint(updated_utc)"
    )


def _ensure_prop_outcome_stats(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS prop_outcome_stats (
            key_hash TEXT PRIMARY KEY,
            origin_grid6 TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            band TEXT NOT NULL,
            mode TEXT,
            month INTEGER NOT NULL,
            utc_hour_bucket INTEGER NOT NULL,
            distance_bucket TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            weighted_attempt REAL NOT NULL DEFAULT 0,
            weighted_success REAL NOT NULL DEFAULT 0,
            last_event_utc TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "prop_outcome_stats",
        {
            "key_hash": "TEXT",
            "origin_grid6": "TEXT",
            "target_type": "TEXT",
            "target_id": "TEXT",
            "band": "TEXT",
            "mode": "TEXT",
            "month": "INTEGER",
            "utc_hour_bucket": "INTEGER",
            "distance_bucket": "TEXT",
            "attempt_count": "INTEGER DEFAULT 0",
            "success_count": "INTEGER DEFAULT 0",
            "weighted_attempt": "REAL DEFAULT 0",
            "weighted_success": "REAL DEFAULT 0",
            "last_event_utc": "TEXT",
            "updated_utc": "TEXT",
        },
    )
    # As above, repair only malformed legacy rows.  Unconditional normalization
    # made this table a full write transaction during every cold start.
    cur.execute(
        """UPDATE prop_outcome_stats
              SET month = MIN(12, MAX(1, CAST(month AS INTEGER)))
            WHERE month IS NOT NULL
              AND (typeof(month) <> 'integer' OR month < 1 OR month > 12)"""
    )
    cur.execute(
        """
        UPDATE prop_outcome_stats
        SET utc_hour_bucket = MIN(23, MAX(0, CAST(utc_hour_bucket AS INTEGER)))
        WHERE utc_hour_bucket IS NOT NULL
          AND (typeof(utc_hour_bucket) <> 'integer' OR utc_hour_bucket < 0 OR utc_hour_bucket > 23)
        """
    )
    for column in ("attempt_count", "success_count"):
        cur.execute(
            f"""UPDATE prop_outcome_stats
                   SET {column} = MAX(0, CAST({column} AS INTEGER))
                 WHERE typeof({column}) <> 'integer' OR {column} < 0"""
        )
    for column in ("weighted_attempt", "weighted_success"):
        cur.execute(
            f"""UPDATE prop_outcome_stats
                   SET {column} = MAX(0, {column})
                 WHERE {column} < 0"""
        )
    for column in ("band", "origin_grid6", "distance_bucket", "target_type"):
        cur.execute(
            f"""UPDATE prop_outcome_stats
                   SET {column} = UPPER(TRIM({column}))
                 WHERE {column} IS NOT NULL
                   AND {column} <> UPPER(TRIM({column}))"""
        )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prop_outcome_stats_lookup
        ON prop_outcome_stats(origin_grid6, target_type, target_id, band, month, utc_hour_bucket)
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_outcome_stats_updated ON prop_outcome_stats(updated_utc)"
    )


def _ensure_sitrep_ingest_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_ingest_checkpoint (
            source_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_db_path TEXT,
            last_id INTEGER NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL,
            last_error TEXT
        )
        """
    )
    _ensure_columns(
        conn,
        "sitrep_ingest_checkpoint",
        {
            "source_key": "TEXT",
            "source": "TEXT",
            "source_table": "TEXT",
            "source_db_path": "TEXT",
            "last_id": "INTEGER DEFAULT 0",
            "updated_ts": "REAL",
            "last_error": "TEXT",
        },
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_source_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_db_path TEXT,
            source_id INTEGER NOT NULL,
            subtype TEXT NOT NULL,
            from_call TEXT,
            target TEXT,
            report_group TEXT,
            grid TEXT,
            scope TEXT,
            transport_mode TEXT,
            reach_mode TEXT,
            origin_path TEXT,
            remarks_text TEXT,
            brevity_code TEXT,
            brevity_summary TEXT,
            state_code TEXT,
            state_confidence TEXT,
            geo_confidence TEXT,
            status_payload TEXT,
            raw_payload TEXT,
            event_ts REAL,
            event_ts_utc TEXT,
            ingested_ts REAL NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "sitrep_source_events",
        {
            "source": "TEXT",
            "source_table": "TEXT",
            "source_db_path": "TEXT",
            "source_id": "INTEGER",
            "subtype": "TEXT",
            "from_call": "TEXT",
            "target": "TEXT",
            "report_group": "TEXT",
            "grid": "TEXT",
            "scope": "TEXT",
            "transport_mode": "TEXT",
            "reach_mode": "TEXT",
            "origin_path": "TEXT",
            "remarks_text": "TEXT",
            "brevity_code": "TEXT",
            "brevity_summary": "TEXT",
            "state_code": "TEXT",
            "state_confidence": "TEXT",
            "geo_confidence": "TEXT",
            "status_payload": "TEXT",
            "raw_payload": "TEXT",
            "event_ts": "REAL",
            "event_ts_utc": "TEXT",
            "ingested_ts": "REAL",
        },
    )
    # Safety: remove duplicates before enforcing uniqueness in upgraded DBs.
    cur.execute(
        """
        DELETE FROM sitrep_source_events
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM sitrep_source_events
            GROUP BY source, source_table, source_db_path, source_id
        )
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sitrep_source_events_source_id
            ON sitrep_source_events(source, source_table, source_db_path, source_id)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_source_events_recent
            ON sitrep_source_events(source, event_ts DESC, id DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_source_events_call_recent
            ON sitrep_source_events(from_call, event_ts DESC, id DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_checkpoint_updated
            ON sitrep_ingest_checkpoint(updated_ts)
        """
    )
    ensure_commstat_artifact_tables(conn)


def _ensure_sitrep_fusion_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_fusion_checkpoint (
            pipeline_key TEXT PRIMARY KEY,
            last_source_event_id INTEGER NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "sitrep_fusion_checkpoint",
        {
            "pipeline_key": "TEXT",
            "last_source_event_id": "INTEGER DEFAULT 0",
            "updated_ts": "REAL",
        },
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_key TEXT NOT NULL UNIQUE,
            event_ts REAL,
            event_ts_utc TEXT,
            from_call TEXT NOT NULL,
            target TEXT,
            report_group TEXT,
            grid TEXT,
            state_code TEXT,
            state_confidence TEXT,
            geo_confidence TEXT,
            scope TEXT,
            overall_status TEXT NOT NULL DEFAULT 'not_reported',
            power TEXT NOT NULL DEFAULT 'not_reported',
            water TEXT NOT NULL DEFAULT 'not_reported',
            medical TEXT NOT NULL DEFAULT 'not_reported',
            communications TEXT NOT NULL DEFAULT 'not_reported',
            internet TEXT NOT NULL DEFAULT 'not_reported',
            travel TEXT NOT NULL DEFAULT 'not_reported',
            food TEXT NOT NULL DEFAULT 'not_reported',
            fuel TEXT NOT NULL DEFAULT 'not_reported',
            crime TEXT NOT NULL DEFAULT 'not_reported',
            civil_unrest TEXT NOT NULL DEFAULT 'not_reported',
            political TEXT NOT NULL DEFAULT 'not_reported',
            subtype TEXT,
            transport_mode TEXT,
            remarks_text TEXT,
            brevity_code TEXT,
            brevity_summary TEXT,
            source_first TEXT,
            source_last TEXT,
            sources_json TEXT,
            source_count INTEGER DEFAULT 1,
            source_refs_json TEXT,
            raw_payload_json TEXT,
            inserted_ts REAL NOT NULL,
            updated_ts REAL NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "sitrep_events",
        {
            "report_key": "TEXT",
            "event_ts": "REAL",
            "event_ts_utc": "TEXT",
            "from_call": "TEXT",
            "target": "TEXT",
            "report_group": "TEXT",
            "grid": "TEXT",
            "state_code": "TEXT",
            "state_confidence": "TEXT",
            "geo_confidence": "TEXT",
            "scope": "TEXT",
            "overall_status": "TEXT DEFAULT 'not_reported'",
            "power": "TEXT DEFAULT 'not_reported'",
            "water": "TEXT DEFAULT 'not_reported'",
            "medical": "TEXT DEFAULT 'not_reported'",
            "communications": "TEXT DEFAULT 'not_reported'",
            "internet": "TEXT DEFAULT 'not_reported'",
            "travel": "TEXT DEFAULT 'not_reported'",
            "food": "TEXT DEFAULT 'not_reported'",
            "fuel": "TEXT DEFAULT 'not_reported'",
            "crime": "TEXT DEFAULT 'not_reported'",
            "civil_unrest": "TEXT DEFAULT 'not_reported'",
            "political": "TEXT DEFAULT 'not_reported'",
            "subtype": "TEXT",
            "transport_mode": "TEXT",
            "remarks_text": "TEXT",
            "brevity_code": "TEXT",
            "brevity_summary": "TEXT",
            "source_first": "TEXT",
            "source_last": "TEXT",
            "sources_json": "TEXT",
            "source_count": "INTEGER DEFAULT 1",
            "source_refs_json": "TEXT",
            "raw_payload_json": "TEXT",
            "inserted_ts": "REAL",
            "updated_ts": "REAL",
        },
    )
    # Safety: collapse duplicate report keys before enforcing uniqueness.
    cur.execute(
        """
        DELETE FROM sitrep_events
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM sitrep_events
            GROUP BY report_key
        )
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sitrep_events_report_key
            ON sitrep_events(report_key)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_events_recent
            ON sitrep_events(event_ts DESC, id DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_events_call_recent
            ON sitrep_events(from_call, event_ts DESC, id DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_events_source_last
            ON sitrep_events(source_last, event_ts DESC, id DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_latest_by_callsign (
            callsign TEXT PRIMARY KEY,
            latest_event_id INTEGER NOT NULL,
            latest_event_ts REAL,
            latest_event_ts_utc TEXT,
            latest_subtype TEXT,
            latest_target TEXT,
            latest_report_group TEXT,
            latest_grid TEXT,
            latest_state_code TEXT,
            latest_state_confidence TEXT,
            latest_geo_confidence TEXT,
            latest_transport_mode TEXT,
            latest_remarks_text TEXT,
            latest_brevity_code TEXT,
            latest_brevity_summary TEXT,
            effective_status TEXT NOT NULL DEFAULT 'not_reported',
            scope TEXT,
            overall_status TEXT NOT NULL DEFAULT 'not_reported',
            power TEXT NOT NULL DEFAULT 'not_reported',
            water TEXT NOT NULL DEFAULT 'not_reported',
            medical TEXT NOT NULL DEFAULT 'not_reported',
            communications TEXT NOT NULL DEFAULT 'not_reported',
            internet TEXT NOT NULL DEFAULT 'not_reported',
            travel TEXT NOT NULL DEFAULT 'not_reported',
            food TEXT NOT NULL DEFAULT 'not_reported',
            fuel TEXT NOT NULL DEFAULT 'not_reported',
            crime TEXT NOT NULL DEFAULT 'not_reported',
            civil_unrest TEXT NOT NULL DEFAULT 'not_reported',
            political TEXT NOT NULL DEFAULT 'not_reported',
            source_summary_json TEXT,
            updated_ts REAL NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "sitrep_latest_by_callsign",
        {
            "callsign": "TEXT",
            "latest_event_id": "INTEGER",
            "latest_event_ts": "REAL",
            "latest_event_ts_utc": "TEXT",
            "latest_subtype": "TEXT",
            "latest_target": "TEXT",
            "latest_report_group": "TEXT",
            "latest_grid": "TEXT",
            "latest_state_code": "TEXT",
            "latest_state_confidence": "TEXT",
            "latest_geo_confidence": "TEXT",
            "latest_transport_mode": "TEXT",
            "latest_remarks_text": "TEXT",
            "latest_brevity_code": "TEXT",
            "latest_brevity_summary": "TEXT",
            "effective_status": "TEXT DEFAULT 'not_reported'",
            "scope": "TEXT",
            "overall_status": "TEXT DEFAULT 'not_reported'",
            "power": "TEXT DEFAULT 'not_reported'",
            "water": "TEXT DEFAULT 'not_reported'",
            "medical": "TEXT DEFAULT 'not_reported'",
            "communications": "TEXT DEFAULT 'not_reported'",
            "internet": "TEXT DEFAULT 'not_reported'",
            "travel": "TEXT DEFAULT 'not_reported'",
            "food": "TEXT DEFAULT 'not_reported'",
            "fuel": "TEXT DEFAULT 'not_reported'",
            "crime": "TEXT DEFAULT 'not_reported'",
            "civil_unrest": "TEXT DEFAULT 'not_reported'",
            "political": "TEXT DEFAULT 'not_reported'",
            "source_summary_json": "TEXT",
            "updated_ts": "REAL",
        },
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_latest_effective
            ON sitrep_latest_by_callsign(effective_status, latest_event_ts DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sitrep_state_rollup (
            report_group TEXT NOT NULL,
            state_code TEXT NOT NULL,
            callsign_count INTEGER NOT NULL DEFAULT 0,
            red_count INTEGER NOT NULL DEFAULT 0,
            yellow_count INTEGER NOT NULL DEFAULT 0,
            green_count INTEGER NOT NULL DEFAULT 0,
            unknown_count INTEGER NOT NULL DEFAULT 0,
            js8_count INTEGER NOT NULL DEFAULT 0,
            internet_count INTEGER NOT NULL DEFAULT 0,
            mixed_transport_count INTEGER NOT NULL DEFAULT 0,
            latest_event_ts REAL,
            updated_ts REAL NOT NULL,
            PRIMARY KEY (report_group, state_code)
        )
        """
    )
    _ensure_columns(
        conn,
        "sitrep_state_rollup",
        {
            "report_group": "TEXT",
            "state_code": "TEXT",
            "callsign_count": "INTEGER DEFAULT 0",
            "red_count": "INTEGER DEFAULT 0",
            "yellow_count": "INTEGER DEFAULT 0",
            "green_count": "INTEGER DEFAULT 0",
            "unknown_count": "INTEGER DEFAULT 0",
            "js8_count": "INTEGER DEFAULT 0",
            "internet_count": "INTEGER DEFAULT 0",
            "mixed_transport_count": "INTEGER DEFAULT 0",
            "latest_event_ts": "REAL",
            "updated_ts": "REAL",
        },
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_state_rollup_group
            ON sitrep_state_rollup(report_group, latest_event_ts DESC, state_code)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sitrep_fusion_checkpoint_updated
            ON sitrep_fusion_checkpoint(updated_ts)
        """
    )


def _ensure_propagation_outcome_tables(conn: sqlite3.Connection) -> None:
    _ensure_prop_contact_events(conn)
    _ensure_prop_ingest_checkpoint(conn)
    _ensure_prop_outcome_stats(conn)


def _ensure_nets_db() -> None:
    """
    Ensure nets DB (freqinout_nets.db) has required tables.
    """
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    db_path = config_dir / "freqinout_nets.db"
    conn = connect_sqlite(db_path)
    try:
        cur = conn.cursor()
        # Daily / Net schedules
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_schedule_tab (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_utc TEXT NOT NULL,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                vfo TEXT,
                frequency TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                group_name TEXT,
                auto_tune INTEGER DEFAULT 0,
                target_scope TEXT NOT NULL DEFAULT 'station',
                target_device_profile_id INTEGER,
                target_operating_profile_id INTEGER
            )
            """
        )
        _ensure_columns(
            conn,
            "daily_schedule_tab",
            {
                "day_utc": "TEXT",
                "band": "TEXT",
                "mode": "TEXT",
                "vfo": "TEXT",
                "frequency": "TEXT",
                "start_utc": "TEXT",
                "end_utc": "TEXT",
                "group_name": "TEXT",
                "auto_tune": "INTEGER DEFAULT 0",
                "target_scope": "TEXT NOT NULL DEFAULT 'station'",
                "target_device_profile_id": "INTEGER",
                "target_operating_profile_id": "INTEGER",
            },
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS net_schedule_tab (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_utc TEXT NOT NULL,
                recurrence TEXT DEFAULT 'Weekly',
                biweekly_offset_weeks INTEGER DEFAULT 0,
                month_weeks TEXT,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                vfo TEXT,
                frequency TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                early_checkin INTEGER NOT NULL,
                auto_tune INTEGER DEFAULT 0,
                primary_js8call_group TEXT,
                comment TEXT,
                net_name TEXT,
                group_name TEXT,
                fldigi_mode TEXT,
                fldigi_offset TEXT,
                resource_id INTEGER,
                net_session_key TEXT,
                accepted_session_version_hash TEXT,
                accepted_resource_version_hash TEXT,
                accepted_snapshot_json TEXT,
                target_scope TEXT NOT NULL DEFAULT 'station',
                target_device_profile_id INTEGER,
                target_operating_profile_id INTEGER
            )
            """
        )
        _ensure_columns(
            conn,
            "net_schedule_tab",
            {
                "day_utc": "TEXT",
                "recurrence": "TEXT",
                "biweekly_offset_weeks": "INTEGER DEFAULT 0",
                "month_weeks": "TEXT",
                "band": "TEXT",
                "mode": "TEXT",
                "vfo": "TEXT",
                "frequency": "TEXT",
                "start_utc": "TEXT",
                "end_utc": "TEXT",
                "early_checkin": "INTEGER DEFAULT 0",
                "auto_tune": "INTEGER DEFAULT 0",
                "primary_js8call_group": "TEXT",
                "comment": "TEXT",
                "net_name": "TEXT",
                "group_name": "TEXT",
                "fldigi_mode": "TEXT",
                "fldigi_offset": "TEXT",
                "resource_id": "INTEGER",
                "net_session_key": "TEXT",
                "accepted_session_version_hash": "TEXT",
                "accepted_resource_version_hash": "TEXT",
                "accepted_snapshot_json": "TEXT",
                "target_scope": "TEXT NOT NULL DEFAULT 'station'",
                "target_device_profile_id": "INTEGER",
                "target_operating_profile_id": "INTEGER",
            },
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS net_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_utc TEXT NOT NULL,
                recurrence TEXT DEFAULT 'Weekly',
                biweekly_offset_weeks INTEGER DEFAULT 0,
                month_weeks TEXT,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                frequency TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                early_checkin INTEGER NOT NULL,
                auto_tune INTEGER DEFAULT 0,
                primary_js8call_group TEXT,
                comment TEXT,
                net_name TEXT,
                group_name TEXT,
                fldigi_mode TEXT,
                fldigi_offset TEXT,
                net_session_key TEXT,
                accepted_session_version_hash TEXT,
                accepted_resource_version_hash TEXT,
                accepted_snapshot_json TEXT,
                target_scope TEXT NOT NULL DEFAULT 'station',
                target_device_profile_id INTEGER,
                target_operating_profile_id INTEGER
            )
            """
        )
        _ensure_columns(
            conn,
            "net_schedule",
            {
                "day_utc": "TEXT",
                "recurrence": "TEXT",
                "biweekly_offset_weeks": "INTEGER DEFAULT 0",
                "month_weeks": "TEXT",
                "band": "TEXT",
                "mode": "TEXT",
                "frequency": "TEXT",
                "start_utc": "TEXT",
                "end_utc": "TEXT",
                "early_checkin": "INTEGER DEFAULT 0",
                "auto_tune": "INTEGER DEFAULT 0",
                "primary_js8call_group": "TEXT",
                "comment": "TEXT",
                "net_name": "TEXT",
                "group_name": "TEXT",
                "fldigi_mode": "TEXT",
                "fldigi_offset": "TEXT",
                "net_session_key": "TEXT",
                "accepted_session_version_hash": "TEXT",
                "accepted_resource_version_hash": "TEXT",
                "accepted_snapshot_json": "TEXT",
                "target_scope": "TEXT NOT NULL DEFAULT 'station'",
                "target_device_profile_id": "INTEGER",
                "target_operating_profile_id": "INTEGER",
            },
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS net_resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_set TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT,
                readonly INTEGER DEFAULT 1,
                day_utc TEXT NOT NULL,
                recurrence TEXT DEFAULT 'Weekly',
                biweekly_offset_weeks INTEGER DEFAULT 0,
                month_weeks TEXT,
                group_name TEXT,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                frequency TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                early_checkin INTEGER NOT NULL,
                primary_js8call_group TEXT,
                coverage TEXT,
                comment TEXT,
                net_name TEXT,
                fldigi_mode TEXT,
                fldigi_offset TEXT,
                updated_utc TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "net_resources",
            {
                "resource_set": "TEXT",
                "source_type": "TEXT",
                "source_ref": "TEXT",
                "readonly": "INTEGER DEFAULT 1",
                "day_utc": "TEXT",
                "recurrence": "TEXT",
                "biweekly_offset_weeks": "INTEGER DEFAULT 0",
                "month_weeks": "TEXT",
                "group_name": "TEXT",
                "band": "TEXT",
                "mode": "TEXT",
                "frequency": "TEXT",
                "start_utc": "TEXT",
                "end_utc": "TEXT",
                "early_checkin": "INTEGER DEFAULT 0",
                "primary_js8call_group": "TEXT",
                "coverage": "TEXT",
                "comment": "TEXT",
                "net_name": "TEXT",
                "fldigi_mode": "TEXT",
                "fldigi_offset": "TEXT",
                "updated_utc": "TEXT",
            },
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS message_viewer_paths (
                origin TEXT,
                path TEXT UNIQUE
            )
            """
        )
        # FLDigi check-ins (used for map tooltip mode flags)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fldigi_checkins (
                callsign TEXT PRIMARY KEY,
                last_seen_ts REAL
            )
            """
        )

        # Auto-query backlog for JS8 (MSG IDs / GRID requests)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS autoquery_backlog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT NOT NULL,
                msg_id TEXT,
                kind TEXT NOT NULL,           -- 'MSG' or 'GRID'
                status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING / RETRIEVED / FAILED
                attempts INTEGER DEFAULT 0,
                last_attempt_ts REAL,
                created_ts REAL
            )
            """
        )
        _ensure_columns(
            conn,
            "autoquery_backlog",
            {
                "callsign": "TEXT",
                "msg_id": "TEXT",
                "kind": "TEXT",
                "status": "TEXT",
                "attempts": "INTEGER DEFAULT 0",
                "last_attempt_ts": "REAL",
                "created_ts": "REAL",
            },
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_autoquery_callsign ON autoquery_backlog(callsign)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_autoquery_status ON autoquery_backlog(status)")

        # Peer HF schedule (imported from other operators)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS peer_hf_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_callsign TEXT NOT NULL,
                day_utc TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                frequency TEXT NOT NULL,
                meta_json TEXT,
                imported_at TEXT,
                source_type TEXT DEFAULT 'IMPORTED',
                updated_at TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "peer_hf_schedule",
            {
                "owner_callsign": "TEXT",
                "day_utc": "TEXT",
                "start_utc": "TEXT",
                "end_utc": "TEXT",
                "band": "TEXT",
                "mode": "TEXT",
                "frequency": "TEXT",
                "meta_json": "TEXT",
                "imported_at": "TEXT",
                "source_type": "TEXT DEFAULT 'IMPORTED'",
                "updated_at": "TEXT",
            },
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_peer_hf_owner ON peer_hf_schedule(owner_callsign)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS peer_hf_schedule_inferred (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_callsign TEXT NOT NULL,
                day_utc TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                frequency TEXT NOT NULL,
                source TEXT,
                confidence REAL DEFAULT 0,
                sample_count INTEGER DEFAULT 0,
                weeks_seen INTEGER DEFAULT 0,
                weeks_observed INTEGER DEFAULT 0,
                last_seen_ts REAL,
                updated_ts REAL
            )
            """
        )
        _ensure_columns(
            conn,
            "peer_hf_schedule_inferred",
            {
                "owner_callsign": "TEXT",
                "day_utc": "TEXT",
                "start_utc": "TEXT",
                "end_utc": "TEXT",
                "band": "TEXT",
                "mode": "TEXT",
                "frequency": "TEXT",
                "source": "TEXT",
                "confidence": "REAL DEFAULT 0",
                "sample_count": "INTEGER DEFAULT 0",
                "weeks_seen": "INTEGER DEFAULT 0",
                "weeks_observed": "INTEGER DEFAULT 0",
                "last_seen_ts": "REAL",
                "updated_ts": "REAL",
            },
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_peer_hf_inferred_owner ON peer_hf_schedule_inferred(owner_callsign)"
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_peer_hf_inferred_owner_day
            ON peer_hf_schedule_inferred(owner_callsign, day_utc, start_utc, end_utc)
            """
        )
        # Imported schedules are authoritative; inferred rows are a fallback per callsign.
        cur.execute("DROP VIEW IF EXISTS peer_hf_schedule_effective")
        cur.execute(
            """
            CREATE VIEW IF NOT EXISTS peer_hf_schedule_effective AS
            SELECT
                owner_callsign,
                day_utc,
                start_utc,
                end_utc,
                band,
                mode,
                frequency,
                COALESCE(NULLIF(TRIM(source_type), ''), 'IMPORTED') AS source_type,
                NULL AS confidence,
                NULL AS sample_count,
                NULL AS weeks_seen,
                NULL AS weeks_observed,
                NULL AS last_seen_ts
            FROM peer_hf_schedule
            UNION ALL
            SELECT
                i.owner_callsign,
                i.day_utc,
                i.start_utc,
                i.end_utc,
                i.band,
                i.mode,
                i.frequency,
                'INFERRED' AS source_type,
                i.confidence,
                i.sample_count,
                i.weeks_seen,
                i.weeks_observed,
                i.last_seen_ts
            FROM peer_hf_schedule_inferred i
            WHERE NOT EXISTS (
                SELECT 1
                FROM peer_hf_schedule e
                WHERE UPPER(TRIM(e.owner_callsign)) = UPPER(TRIM(i.owner_callsign))
            )
            """
        )

        # Propagation outcomes (offline scoring support)
        with perf_span("startup.database.nets.propagation", min_ms=10.0):
            _ensure_propagation_outcome_tables(conn)
        with perf_span("startup.database.nets.sitrep_ingest", min_ms=10.0):
            _ensure_sitrep_ingest_tables(conn)
        with perf_span("startup.database.nets.sitrep_fusion", min_ms=10.0):
            _ensure_sitrep_fusion_tables(conn)

        # SOP profiles/actions/state
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sop_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                operating_group TEXT NOT NULL,
                secondary_group TEXT,
                frequency TEXT NOT NULL,
                sop_start_utc TEXT NOT NULL,
                priority INTEGER DEFAULT 100,
                active INTEGER DEFAULT 0,
                window_hours INTEGER DEFAULT 12,
                created_utc TEXT,
                updated_utc TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "sop_profiles",
            {
                "name": "TEXT",
                "operating_group": "TEXT",
                "secondary_group": "TEXT",
                "frequency": "TEXT",
                "sop_start_utc": "TEXT",
                "priority": "INTEGER DEFAULT 100",
                "active": "INTEGER DEFAULT 0",
                "window_hours": "INTEGER DEFAULT 12",
                "created_utc": "TEXT",
                "updated_utc": "TEXT",
            },
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_profiles_name ON sop_profiles(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sop_profiles_active ON sop_profiles(active)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sop_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                band TEXT,
                frequency TEXT,
                software TEXT NOT NULL,
                action_key TEXT NOT NULL,
                action_label TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                interval_hours INTEGER DEFAULT 3,
                interval_minutes INTEGER,
                description TEXT,
                contact_rule TEXT DEFAULT 'none',
                contact_target TEXT,
                sort_order INTEGER DEFAULT 0
            )
            """
        )
        _ensure_columns(
            conn,
            "sop_actions",
            {
                "profile_id": "INTEGER",
                "band": "TEXT",
                "frequency": "TEXT",
                "software": "TEXT",
                "action_key": "TEXT",
                "action_label": "TEXT",
                "enabled": "INTEGER DEFAULT 1",
                "interval_hours": "INTEGER DEFAULT 3",
                "interval_minutes": "INTEGER",
                "description": "TEXT",
                "contact_rule": "TEXT DEFAULT 'none'",
                "contact_target": "TEXT",
                "sort_order": "INTEGER DEFAULT 0",
            },
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sop_actions_profile ON sop_actions(profile_id)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sop_action_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                action_id INTEGER NOT NULL,
                last_completed_utc TEXT,
                updated_utc TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "sop_action_state",
            {
                "profile_id": "INTEGER",
                "action_id": "INTEGER",
                "last_completed_utc": "TEXT",
                "updated_utc": "TEXT",
            },
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_state_action ON sop_action_state(profile_id, action_id)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sop_schedule_layer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                day_utc TEXT NOT NULL,
                recurrence TEXT DEFAULT 'Weekly',
                biweekly_offset_weeks INTEGER DEFAULT 0,
                month_weeks TEXT,
                group_name TEXT,
                band TEXT,
                mode TEXT,
                vfo TEXT,
                frequency TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                updated_utc TEXT
            )
            """
        )
        _ensure_columns(
            conn,
            "sop_schedule_layer",
            {
                "profile_id": "INTEGER",
                "day_utc": "TEXT",
                "recurrence": "TEXT DEFAULT 'Weekly'",
                "biweekly_offset_weeks": "INTEGER DEFAULT 0",
                "month_weeks": "TEXT",
                "group_name": "TEXT",
                "band": "TEXT",
                "mode": "TEXT",
                "vfo": "TEXT",
                "frequency": "TEXT",
                "start_utc": "TEXT",
                "end_utc": "TEXT",
                "enabled": "INTEGER DEFAULT 1",
                "sort_order": "INTEGER DEFAULT 0",
                "updated_utc": "TEXT",
            },
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sop_layer_profile ON sop_schedule_layer(profile_id)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sop_layer_profile_day ON sop_schedule_layer(profile_id, day_utc, start_utc)"
        )

        # Local Nets are reminder-only station calendars.  These tables are
        # intentionally separate from every SchedulerEngine input table.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS local_net_schedules (
                local_net_schedule_key TEXT PRIMARY KEY NOT NULL,
                name TEXT NOT NULL,
                service TEXT NOT NULL,
                recurrence TEXT NOT NULL,
                local_start_time TEXT NOT NULL,
                timezone_name TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                weekdays_json TEXT NOT NULL DEFAULT '[]',
                month_weeks_json TEXT NOT NULL DEFAULT '[]',
                biweekly_anchor_date TEXT,
                one_time_local_date TEXT,
                effective_start_date TEXT,
                effective_end_date TEXT,
                exception_dates_json TEXT NOT NULL DEFAULT '[]',
                reminder_minutes INTEGER NOT NULL DEFAULT 15,
                net_entry_key TEXT,
                net_session_key TEXT,
                frequency_resource_key TEXT,
                operating_group_key TEXT,
                operating_group_name TEXT,
                sop_id INTEGER,
                participation_notes TEXT,
                accepted_session_version_hash TEXT,
                accepted_resource_version_hash TEXT,
                accepted_snapshot_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                next_occurrence_utc TEXT,
                created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS idx_local_net_schedules_next
               ON local_net_schedules(enabled, next_occurrence_utc, local_net_schedule_key)"""
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS idx_local_net_schedules_filter
               ON local_net_schedules(operating_group_key, service, enabled)"""
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS idx_local_net_schedules_session
               ON local_net_schedules(net_session_key)"""
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS local_net_occurrence_state (
                occurrence_key TEXT PRIMARY KEY NOT NULL,
                local_net_schedule_key TEXT NOT NULL,
                occurrence_start_utc TEXT NOT NULL,
                state TEXT NOT NULL,
                operator_note TEXT,
                updated_utc TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS idx_local_net_occurrence_schedule
               ON local_net_occurrence_state(local_net_schedule_key, occurrence_start_utc)"""
        )

        with perf_span("startup.database.nets.operator_checkins", min_ms=10.0):
            _ensure_operator_checkins(conn)
        with perf_span("startup.database.nets.local_operators", min_ms=10.0):
            _ensure_local_operator_tables(conn)
        with perf_span("startup.database.nets.js8_message_cache", min_ms=10.0):
            ensure_js8_message_cache_schema(conn)
        with perf_span("startup.database.nets.observations", min_ms=10.0):
            ensure_observation_schema(conn)
        with perf_span("startup.database.nets.message_projection", min_ms=10.0):
            ensure_message_projection_schema(conn)
        with perf_span("startup.database.nets.js8_links", min_ms=10.0):
            _ensure_js8_links(conn)
        with perf_span("startup.database.nets.varac", min_ms=10.0):
            ensure_varac_local_tables(conn)
        with perf_span("startup.database.nets.dirty_triggers", min_ms=10.0):
            ensure_source_dirty_triggers(conn)
        with perf_span("startup.database.nets.expect", min_ms=10.0):
            _ensure_js8_expect_tables(conn)
        with perf_span("startup.database.nets.ops_indexes", min_ms=10.0):
            _ensure_controlfreq_support_indexes(conn)
        with perf_span("startup.database.nets.sitrep_group_repair", min_ms=10.0):
            _repair_sitrep_commstat_groups(conn)

        conn.commit()
    finally:
        conn.close()


def ensure_all_tables() -> None:
    """
    Public entry point to ensure both DBs are initialized.
    """
    ensure_settings_tables()
    ensure_nets_tables()
    log.info("DB init: ensured core tables (settings and nets).")


def ensure_settings_tables() -> None:
    """
    Public entry point to ensure settings DB tables and migrations are applied.
    """
    _ensure_settings_db()
    log.info("DB init: ensured settings tables.")


def ensure_nets_tables() -> None:
    """
    Public entry point to ensure nets DB tables and migrations are applied.
    """
    with perf_span("startup.database.nets.schema", min_ms=10.0):
        _ensure_nets_db()
    config_dir = _config_dir()
    try:
        with perf_span("startup.database.nets.resource_catalog", min_ms=10.0):
            report = cutover_resource_catalog_to_canonical(
                config_dir / "freqinout_nets.db",
                config_dir / "freqinout.db",
            )
        log.info(
            "Resource catalog: %s (%d legacy row(s), %d review required).",
            report.authority_state,
            report.total_legacy_rows,
            report.review_required_count,
        )
    except Exception as exc:
        # Cutover is transactional and backup-first; failure leaves the legacy
        # schedule surfaces usable and Resources navigation stays hidden.
        log.error("Resource catalog cutover failed safely; legacy resources remain active: %s", exc)
        report = None
    if report is not None and report.authority_state == "canonical":
        nets_path = config_dir / "freqinout_nets.db"
        try:
            shortwave_conn = connect_sqlite(nets_path)
            try:
                with shortwave_conn:
                    with perf_span("startup.database.nets.shortwave", min_ms=10.0):
                        ensure_shortwave_schema(shortwave_conn)
            finally:
                shortwave_conn.close()
        except Exception as exc:
            # Shortwave is an additive, independently gated resource surface.
            # Its failure must not misreport or roll back a completed canonical
            # catalog cutover; the UI verifies schema availability before
            # exposing Shortwave.
            log.error("Shortwave schema initialization failed safely; Shortwave remains unavailable: %s", exc)
    log.info("DB init: ensured nets tables.")
