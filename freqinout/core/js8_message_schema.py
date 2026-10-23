from __future__ import annotations

"""Idempotent schema assurance for the local JS8 message cache."""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    existing = _columns(conn, table)
    for name, declaration in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def ensure_js8_message_cache_schema(conn: sqlite3.Connection) -> None:
    """Ensure both legacy and multi-radio JS8 cache fields exist.

    This must run before message-projection triggers are installed.  Historic
    single-radio rows receive stable, non-destructive source identities based
    on their existing primary keys.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_messages (
            id INTEGER PRIMARY KEY,
            from_call TEXT,
            to_call TEXT,
            msg_type TEXT,
            utc_str TEXT,
            utc_ts REAL,
            raw_text TEXT,
            decoded_text TEXT,
            state TEXT,
            read_ts REAL,
            flag_state INTEGER DEFAULT 0,
            source_key TEXT,
            source_id INTEGER,
            source_radio_id TEXT,
            js8_instance_id TEXT,
            source_path TEXT
        )
        """
    )
    _add_missing_columns(
        conn,
        "js8_messages",
        (
            ("from_call", "TEXT"),
            ("to_call", "TEXT"),
            ("msg_type", "TEXT"),
            ("utc_str", "TEXT"),
            ("utc_ts", "REAL"),
            ("raw_text", "TEXT"),
            ("decoded_text", "TEXT"),
            ("state", "TEXT"),
            ("read_ts", "REAL"),
            ("flag_state", "INTEGER DEFAULT 0"),
            ("source_key", "TEXT"),
            ("source_id", "INTEGER"),
            ("source_radio_id", "TEXT"),
            ("js8_instance_id", "TEXT"),
            ("source_path", "TEXT"),
            ("rx_freq_hz", "REAL"),
            ("rx_band", "TEXT"),
            ("rx_snr", "REAL"),
            ("rx_sender", "TEXT"),
            ("receipt_kind", "TEXT"),
            ("received_ts", "REAL"),
        ),
    )
    conn.execute("UPDATE js8_messages SET source_key='' WHERE source_key IS NULL")
    conn.execute("UPDATE js8_messages SET source_id=id WHERE source_id IS NULL")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_inbox_state (
            id INTEGER PRIMARY KEY,
            state TEXT,
            last_seen REAL,
            read_ts REAL,
            last_ingested_id INTEGER,
            source_key TEXT,
            source_id INTEGER
        )
        """
    )
    _add_missing_columns(
        conn,
        "js8_inbox_state",
        (
            ("read_ts", "REAL"),
            ("last_ingested_id", "INTEGER"),
            ("source_key", "TEXT"),
            ("source_id", "INTEGER"),
        ),
    )
    conn.execute("UPDATE js8_inbox_state SET source_key='' WHERE source_key IS NULL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_ingest_checkpoint (
            source_key TEXT PRIMARY KEY,
            last_source_id INTEGER NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS js8_bad_records (
            source TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            raw_preview TEXT,
            first_seen_ts REAL,
            last_seen_ts REAL,
            count INTEGER DEFAULT 1,
            PRIMARY KEY (source, source_id, reason)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_js8_messages_source_native "
        "ON js8_messages(source_key, source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_js8_messages_utc_ts "
        "ON js8_messages(utc_ts DESC, from_call)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_js8_messages_projection "
        "ON js8_messages(utc_ts DESC, source_id DESC, id DESC)"
    )
