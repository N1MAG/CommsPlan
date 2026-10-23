"""Local MeshCore preferences and bounded exact-identity canonical history."""
from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
import time

from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_readonly


def contact_identity(adapter_id: object, transport: object, node_id: object, public_key: object) -> tuple[str, str, str, str] | None:
    adapter, protocol, node, key = (str(value or "").strip() for value in (adapter_id, transport, node_id, public_key))
    if not adapter or not node or len(adapter) > 200 or len(node) > 200 or protocol.lower() != "meshcore":
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        return None
    return adapter, "meshcore", node, key.lower()


@dataclass(frozen=True)
class ContactHistoryRow:
    canonical_key: str
    received_utc: str
    summary: str


@dataclass(frozen=True)
class ContactSnapshot:
    identity: tuple[str, str, str, str] | None
    favorite: bool = False
    history: tuple[ContactHistoryRow, ...] = ()
    error: str = ""
    coverage: str = "History requires exact receive-time contact identity; older prefix-only receipts are excluded. Contact-list matching is not authentication."
    favorite_saved: bool | None = None


def set_contact_favorite(db_path, identity, *, favorite: bool) -> bool:
    checked = _checked_identity(identity)
    if checked is None:
        return False
    from freqinout.core.mesh.store import ensure_mesh_schema
    conn = connect_sqlite(db_path, timeout=0.5)
    try:
        with conn:
            ensure_mesh_schema(conn)
            if favorite:
                count = conn.execute("SELECT COUNT(*) FROM mesh_contact_favorites").fetchone()[0]
                exists = conn.execute("SELECT 1 FROM mesh_contact_favorites WHERE adapter_id=? AND transport=? AND node_id=? AND public_key=?", checked).fetchone()
                if count >= 2000 and exists is None:
                    return False
                conn.execute("INSERT OR IGNORE INTO mesh_contact_favorites VALUES (?,?,?,?)", checked)
            else:
                conn.execute("DELETE FROM mesh_contact_favorites WHERE adapter_id=? AND transport=? AND node_id=? AND public_key=?", checked)
        return True
    finally:
        conn.close()


def load_contact_snapshot(db_path, identity, *, cancelled=None) -> ContactSnapshot:
    checked = _checked_identity(identity)
    if checked is None:
        return ContactSnapshot(None, error="An exact MeshCore node and full public key are required.")
    conn = None
    favorite = False
    try:
        conn = connect_sqlite_readonly(db_path, timeout=0.5)
        deadline = time.monotonic() + 2.0
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline or (cancelled is not None and cancelled())), 1000)
        conn.execute("BEGIN")
        try:
            favorite = conn.execute("SELECT 1 FROM mesh_contact_favorites WHERE adapter_id=? AND transport=? AND node_id=? AND public_key=?", checked).fetchone() is not None
        except sqlite3.OperationalError:
            pass  # Pre-feature profiles can still expose truthful unknown history.
        rows = conn.execute("""
            SELECT DISTINCT p.canonical_key, p.received_utc, p.summary, p.received_ts
            FROM mesh_messages m
            JOIN message_external_refs r ON r.external_key=m.source_ref
                 AND r.external_kind='mesh_message' AND r.source_id=?
            JOIN message_projection p ON p.message_id=r.message_id
            JOIN observation_projection o ON o.source_ref=m.source_ref AND o.source_family=m.transport
            WHERE m.adapter_id=? AND m.transport=? AND m.contact_node_id=? AND m.public_key_or_hash=?
              AND p.source_family='meshcore' AND p.inbox_visible=1 AND p.deleted=0 AND p.archived=0
              AND EXISTS (SELECT 1 FROM json_each(o.provenance_json,'$.surfaces') WHERE value='inbox')
            ORDER BY p.received_ts DESC, p.canonical_key DESC LIMIT 50
        """, (f"mesh:meshcore:{checked[0]}", *checked)).fetchall()
        return ContactSnapshot(checked, favorite=favorite, history=tuple(
            ContactHistoryRow(str(row[0]), str(row[1] or ""), str(row[2] or "")[:400]) for row in rows
        ))
    except (sqlite3.Error, OSError):
        return ContactSnapshot(checked, favorite=favorite, error="Exact contact history is unavailable for this retained profile.")
    finally:
        if conn is not None:
            conn.close()


def _checked_identity(identity):
    if not isinstance(identity, (tuple, list)) or len(identity) != 4:
        return None
    return contact_identity(*identity)


def list_contact_favorites(db_path, adapter_id: str) -> tuple[tuple[str, str, str, str], ...]:
    """Read only operator preferences; discovery never assures a schema."""
    conn = None
    try:
        conn = connect_sqlite_readonly(db_path, timeout=0.5)
        rows = conn.execute("SELECT adapter_id,transport,node_id,public_key FROM mesh_contact_favorites WHERE adapter_id=? LIMIT 2000", (str(adapter_id),)).fetchall()
        return tuple(checked for row in rows if (checked := _checked_identity(tuple(row))) is not None)
    except (sqlite3.Error, OSError):
        return ()
    finally:
        if conn is not None:
            conn.close()
