from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from freqinout.core.mesh.channel_policy import (
    MeshChannelPolicy,
    default_policy_for_channel,
    message_allowed_surfaces,
    policy_for_message,
    policy_from_channel,
    policy_from_mapping,
)
from freqinout.core.mesh.models import (
    MeshAdapterEvent,
    MeshChannel,
    MeshHealthSnapshot,
    MeshMessage,
    MeshNode,
    MeshSendRequest,
    MeshSendResult,
    utc_now,
)
from freqinout.core.message_intelligence import normalize_topic_terms
from freqinout.core.mesh_intelligence import analyze_mesh_message
from freqinout.core.message_projection_queue import DirtyProjectionItem, enqueue_dirty_conn
from freqinout.core.observation_projection import Observation
from freqinout.core.observation_store import delete_observations_by_source_refs, upsert_observation
from freqinout.core.location_confidence import location_evidence_from_values, location_evidence_to_provenance
from freqinout.core.source_connection import source_connection_from_mesh_health
from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_readonly

SOCIAL_TOPIC = "Social"
ATTENTION_SEVERITIES = {"caution", "watch", "warning", "urgent", "emergency", "priority", "critical"}


def ensure_mesh_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mesh_messages (
            source_ref TEXT PRIMARY KEY,
            adapter_id TEXT NOT NULL,
            transport TEXT NOT NULL,
            message_id TEXT NOT NULL,
            from_node TEXT,
            to_node TEXT,
            channel TEXT,
            portnum TEXT,
            text TEXT NOT NULL,
            rx_utc TEXT,
            hop_count INTEGER,
            route_type TEXT,
            direct_receive INTEGER,
            via_node TEXT,
            path_hops_json TEXT NOT NULL DEFAULT '[]',
            snr REAL,
            rssi REAL,
            lat REAL,
            lon REAL,
            grid TEXT,
            topics_json TEXT NOT NULL DEFAULT '[]',
            severity TEXT NOT NULL DEFAULT 'info',
            raw_payload_json TEXT NOT NULL DEFAULT '{}',
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_messages_transport_rx ON mesh_messages(transport, rx_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_messages_from_rx ON mesh_messages(from_node, rx_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_messages_grid_rx ON mesh_messages(grid, rx_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_messages_channel_rx ON mesh_messages(channel, rx_utc)")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mesh_nodes (
            source_ref TEXT PRIMARY KEY,
            adapter_id TEXT NOT NULL,
            transport TEXT NOT NULL,
            node_id TEXT NOT NULL,
            public_key_or_hash TEXT NOT NULL DEFAULT '',
            long_name TEXT,
            short_name TEXT,
            callsign TEXT,
            role TEXT,
            last_heard_utc TEXT,
            hop_count INTEGER,
            route_type TEXT,
            direct_receive INTEGER,
            via_node TEXT,
            path_hops_json TEXT NOT NULL DEFAULT '[]',
            snr REAL,
            rssi REAL,
            battery_percent REAL,
            lat REAL,
            lon REAL,
            grid TEXT,
            raw_payload_json TEXT NOT NULL DEFAULT '{}',
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_nodes_transport_seen ON mesh_nodes(transport, last_heard_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_nodes_callsign ON mesh_nodes(callsign)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_nodes_grid ON mesh_nodes(grid)")
    _ensure_columns(
        conn,
        "mesh_messages",
        {
            "public_key_or_hash": "TEXT NOT NULL DEFAULT ''",
            "contact_node_id": "TEXT NOT NULL DEFAULT ''",
            "route_type": "TEXT",
            "direct_receive": "INTEGER",
            "via_node": "TEXT",
            "path_hops_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_contact_history ON "
                "mesh_messages(adapter_id,transport,contact_node_id,public_key_or_hash,rx_utc DESC)")
    cur.execute("""CREATE TABLE IF NOT EXISTS mesh_contact_favorites (
        adapter_id TEXT NOT NULL, transport TEXT NOT NULL,
        node_id TEXT NOT NULL, public_key TEXT NOT NULL,
        PRIMARY KEY(adapter_id,transport,node_id,public_key)
    )""")
    _ensure_columns(
        conn,
        "mesh_nodes",
        {
            "public_key_or_hash": "TEXT NOT NULL DEFAULT ''",
            "route_type": "TEXT",
            "direct_receive": "INTEGER",
            "via_node": "TEXT",
            "path_hops_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mesh_health (
            adapter_id TEXT PRIMARY KEY,
            transport TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            connected INTEGER NOT NULL DEFAULT 0,
            connection_type TEXT,
            device_name TEXT,
            firmware_version TEXT,
            battery_percent REAL,
            battery_voltage REAL,
            last_rx_utc TEXT,
            last_tx_utc TEXT,
            last_error TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            lifecycle_state TEXT NOT NULL DEFAULT '',
            required INTEGER NOT NULL DEFAULT 0,
            guidance TEXT NOT NULL DEFAULT '',
            updated_utc TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "mesh_health",
        {
            "lifecycle_state": "TEXT NOT NULL DEFAULT ''",
            "required": "INTEGER NOT NULL DEFAULT 0",
            "guidance": "TEXT NOT NULL DEFAULT ''",
        },
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_health_transport ON mesh_health(transport, connected)")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mesh_channel_policies (
            source_ref TEXT PRIMARY KEY,
            adapter_id TEXT NOT NULL,
            transport TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            channel_role TEXT NOT NULL DEFAULT 'unknown',
            channel_privacy TEXT NOT NULL DEFAULT 'unknown',
            mapped_groups_json TEXT NOT NULL DEFAULT '[]',
            retention_window TEXT NOT NULL DEFAULT '7d',
            inbox_enabled INTEGER NOT NULL DEFAULT 1,
            ops_enabled INTEGER NOT NULL DEFAULT 1,
            map_enabled INTEGER NOT NULL DEFAULT 1,
            topic_scan_enabled INTEGER NOT NULL DEFAULT 1,
            default_category TEXT NOT NULL DEFAULT 'auto',
            review_state TEXT NOT NULL DEFAULT 'pending',
            key_state TEXT NOT NULL DEFAULT 'not_required',
            key_hint TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'device',
            updated_utc TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "mesh_channel_policies",
        {
            "key_state": "TEXT NOT NULL DEFAULT 'not_required'",
            "key_hint": "TEXT NOT NULL DEFAULT ''",
            "default_category": "TEXT NOT NULL DEFAULT 'auto'",
        },
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mesh_channel_identity
        ON mesh_channel_policies(adapter_id, transport, channel_id)
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_channel_review ON mesh_channel_policies(transport, review_state)")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mesh_message_topic_overrides (
            source_ref TEXT PRIMARY KEY,
            topics_json TEXT NOT NULL DEFAULT '[]',
            operator_attention INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mesh_send_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            adapter_id TEXT NOT NULL,
            transport TEXT NOT NULL DEFAULT '',
            destination_kind TEXT NOT NULL,
            destination_id TEXT NOT NULL DEFAULT '',
            channel_id TEXT NOT NULL DEFAULT '',
            payload_sha256 TEXT NOT NULL,
            payload_bytes INTEGER NOT NULL,
            state TEXT NOT NULL,
            native_message_id TEXT NOT NULL DEFAULT '',
            retryable INTEGER NOT NULL DEFAULT 0,
            evidence TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            event_utc TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_send_audit_request ON mesh_send_audit(request_id, audit_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mesh_send_audit_adapter_time ON mesh_send_audit(adapter_id, event_utc)")


def append_mesh_send_audit(
    db_path: str | Path,
    request: MeshSendRequest,
    *,
    transport: str = "",
    result: MeshSendResult | None = None,
    state: str = "requested",
    detail: str = "",
    event_utc: str | None = None,
) -> int:
    """Append one immutable outbound evidence event without storing message text."""

    payload = str(request.text or "").encode("utf-8")
    final_state = str(result.state if result is not None else state or "requested").strip().lower()
    stamp = str(event_utc or utc_now().isoformat()).strip()
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        with conn:
            cur = conn.execute(
                """
                INSERT INTO mesh_send_audit (
                    request_id, adapter_id, transport, destination_kind,
                    destination_id, channel_id, payload_sha256, payload_bytes,
                    state, native_message_id, retryable, evidence, detail, event_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.adapter_id,
                    str((result.transport if result is not None else transport) or ""),
                    request.destination_kind,
                    request.destination_id,
                    request.channel_id,
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                    final_state,
                    str(result.native_message_id if result is not None else ""),
                    int(bool(result.retryable)) if result is not None else 0,
                    str(result.evidence if result is not None else ""),
                    str((result.detail if result is not None else detail) or "")[:1000],
                    stamp,
                ),
            )
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_mesh_send_audit(db_path: str | Path, *, request_id: str = "", limit: int = 100) -> list[dict[str, object]]:
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        if request_id:
            rows = conn.execute(
                "SELECT * FROM mesh_send_audit WHERE request_id=? ORDER BY audit_id ASC LIMIT ?",
                (str(request_id), max(1, int(limit))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mesh_send_audit ORDER BY audit_id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        columns = [str(item[1]) for item in conn.execute("PRAGMA table_info(mesh_send_audit)").fetchall()]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


def mesh_send_policy_issue(
    db_path: str | Path,
    request: MeshSendRequest,
    *,
    transport: str = "",
) -> str:
    """Return a fail-closed policy reason, or an empty string when send is allowed."""

    kind = str(request.destination_kind or "").strip().lower()
    if kind in {"channel", "broadcast"}:
        return _mesh_channel_send_policy_issue(db_path, request)
    if kind == "direct":
        if str(transport or "").strip().lower() == "meshtastic":
            channel_issue = _mesh_channel_send_policy_issue(db_path, request)
            if channel_issue:
                return channel_issue
        destination = str(request.destination_id or "").strip()
        nodes = list_mesh_nodes(db_path, adapter_id=request.adapter_id, limit=1000)
        known = any(
            destination
            in {
                str(node.get("node_id") or "").strip(),
                str(node.get("public_key_or_hash") or "").strip(),
            }
            for node in nodes
        )
        if not known:
            return "Refresh this device's known nodes and choose a listed direct destination."
        return ""
    return "Choose a channel or direct-node mesh destination."


def _mesh_channel_send_policy_issue(db_path: str | Path, request: MeshSendRequest) -> str:
    channel_id = str(request.channel_id or "").strip()
    policies = list_mesh_channel_policies(db_path, adapter_id=request.adapter_id)
    policy = next(
        (row for row in policies if str(row.channel_id or "").strip() == channel_id),
        None,
    )
    if policy is None:
        return "Refresh and review this device's channels before sending."
    if str(policy.review_state or "").strip().lower() != "accepted":
        return "Accept this channel's Local Mesh policy before sending."
    if policy.requires_key and not policy.key_available:
        return "This private channel does not have a confirmed device key."
    return ""


def mesh_source_ref(message: MeshMessage) -> str:
    transport = _clean(message.transport) or "mesh"
    adapter_id = _clean(message.adapter_id) or "adapter"
    message_id = _clean(message.message_id) or "message"
    return f"mesh:{transport}:{adapter_id}:{message_id}"


def mesh_node_source_ref(node: MeshNode) -> str:
    transport = _clean(node.transport) or "mesh"
    adapter_id = _clean(node.adapter_id) or "adapter"
    node_id = _clean(node.node_id) or "node"
    return f"mesh-node:{transport}:{adapter_id}:{node_id}"


def mesh_channel_policy_source_ref(policy: MeshChannelPolicy) -> str:
    transport = _clean(policy.transport) or "mesh"
    adapter_id = _clean(policy.adapter_id) or "adapter"
    channel_id = _clean(policy.channel_id) or "channel"
    return f"mesh-channel:{transport}:{adapter_id}:{channel_id}"


def upsert_mesh_message(db_path: str | Path, message: MeshMessage, *, updated_utc: str | None = None) -> str:
    stamp = str(updated_utc or utc_now().isoformat()).strip()
    source_ref = mesh_source_ref(message)
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO mesh_messages (
                    source_ref,
                    adapter_id,
                    transport,
                    message_id,
                    from_node,
                    to_node,
                    channel,
                    portnum,
                    text,
                    rx_utc,
                    hop_count,
                    route_type,
                    direct_receive,
                    via_node,
                    path_hops_json,
                    snr,
                    rssi,
                    lat,
                    lon,
                    grid,
                    topics_json,
                    severity,
                    raw_payload_json,
                    updated_utc,
                    public_key_or_hash,
                    contact_node_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_ref) DO UPDATE SET
                    adapter_id=excluded.adapter_id,
                    transport=excluded.transport,
                    message_id=excluded.message_id,
                    from_node=excluded.from_node,
                    to_node=excluded.to_node,
                    channel=excluded.channel,
                    portnum=excluded.portnum,
                    text=excluded.text,
                    rx_utc=excluded.rx_utc,
                    hop_count=excluded.hop_count,
                    route_type=excluded.route_type,
                    direct_receive=excluded.direct_receive,
                    via_node=excluded.via_node,
                    path_hops_json=excluded.path_hops_json,
                    snr=excluded.snr,
                    rssi=excluded.rssi,
                    lat=excluded.lat,
                    lon=excluded.lon,
                    grid=excluded.grid,
                    topics_json=excluded.topics_json,
                    severity=excluded.severity,
                    raw_payload_json=excluded.raw_payload_json,
                    updated_utc=excluded.updated_utc
                """,
                _mesh_message_values(source_ref, message, stamp),
            )
        return source_ref
    finally:
        conn.close()


def upsert_mesh_messages(db_path: str | Path, messages: Sequence[MeshMessage], *, updated_utc: str | None = None) -> int:
    count = 0
    for message in messages:
        upsert_mesh_message(db_path, message, updated_utc=updated_utc)
        count += 1
    return count


def upsert_mesh_node(db_path: str | Path, node: MeshNode, *, updated_utc: str | None = None) -> str:
    return _upsert_retained_mesh_node(db_path, node, updated_utc=updated_utc)[0]


def _merge_retained_mesh_node(node: MeshNode, previous: Mapping[str, Any]) -> MeshNode:
    """Preserve sparse contact updates without borrowing outgoing-route evidence."""
    old = _node_row_to_dict(previous)
    changes: dict[str, Any] = {}
    for name in ("public_key_or_hash", "long_name", "short_name", "callsign", "role", "grid"):
        changes[name] = getattr(node, name) or old.get(name) or ""
    for name in ("snr", "rssi", "battery_percent"):
        changes[name] = getattr(node, name) if getattr(node, name) is not None else old.get(name)
    # Coordinates form one advertised point, never a mixture of two updates.
    if node.lat is None or node.lon is None:
        changes.update(lat=old.get("lat"), lon=old.get("lon"))
    if node.last_heard is None and old.get("last_heard_utc"):
        try:
            changes["last_heard"] = datetime.fromisoformat(old["last_heard_utc"])
        except ValueError:
            pass
    # Only explicit outgoing-route updates clear the legacy receive fields.
    # Other sparse updates preserve genuine receive evidence.
    if "outgoing_route" not in node.raw:
        for name in ("hop_count", "direct_receive"):
            changes[name] = getattr(node, name) if getattr(node, name) is not None else old.get(name)
        for name in ("route_type", "via_node", "path_hops"):
            changes[name] = getattr(node, name) or old.get(name) or (() if name == "path_hops" else "")
    raw = dict(old.get("raw_payload") or {})
    raw.update(node.raw)
    changes["raw"] = raw
    return replace(node, **changes)


def _upsert_retained_mesh_node(db_path: str | Path, node: MeshNode, *, updated_utc: str | None = None) -> tuple[str, MeshNode]:
    stamp = str(updated_utc or utc_now().isoformat()).strip()
    source_ref = mesh_node_source_ref(node)
    conn = connect_sqlite(db_path, row_factory=sqlite3.Row)
    try:
        ensure_mesh_schema(conn)
        with conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM mesh_nodes WHERE source_ref=?", (source_ref,)).fetchone()
            if previous is not None:
                node = _merge_retained_mesh_node(node, previous)
            conn.execute(
                """
                INSERT INTO mesh_nodes (
                    source_ref,
                    adapter_id,
                    transport,
                    node_id,
                    public_key_or_hash,
                    long_name,
                    short_name,
                    callsign,
                    role,
                    last_heard_utc,
                    hop_count,
                    route_type,
                    direct_receive,
                    via_node,
                    path_hops_json,
                    snr,
                    rssi,
                    battery_percent,
                    lat,
                    lon,
                    grid,
                    raw_payload_json,
                    updated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_ref) DO UPDATE SET
                    adapter_id=excluded.adapter_id,
                    transport=excluded.transport,
                    node_id=excluded.node_id,
                    public_key_or_hash=CASE
                        WHEN excluded.public_key_or_hash IS NOT NULL AND excluded.public_key_or_hash <> '' THEN excluded.public_key_or_hash
                        ELSE mesh_nodes.public_key_or_hash
                    END,
                    long_name=CASE
                        WHEN excluded.long_name IS NOT NULL AND excluded.long_name <> '' THEN excluded.long_name
                        ELSE mesh_nodes.long_name
                    END,
                    short_name=CASE
                        WHEN excluded.short_name IS NOT NULL AND excluded.short_name <> '' THEN excluded.short_name
                        ELSE mesh_nodes.short_name
                    END,
                    callsign=CASE
                        WHEN excluded.callsign IS NOT NULL AND excluded.callsign <> '' THEN excluded.callsign
                        ELSE mesh_nodes.callsign
                    END,
                    role=CASE
                        WHEN excluded.role IS NOT NULL AND excluded.role <> '' THEN excluded.role
                        ELSE mesh_nodes.role
                    END,
                    last_heard_utc=excluded.last_heard_utc,
                    hop_count=excluded.hop_count,
                    route_type=excluded.route_type,
                    direct_receive=excluded.direct_receive,
                    via_node=excluded.via_node,
                    path_hops_json=excluded.path_hops_json,
                    snr=excluded.snr,
                    rssi=excluded.rssi,
                    battery_percent=excluded.battery_percent,
                    lat=COALESCE(excluded.lat, mesh_nodes.lat),
                    lon=COALESCE(excluded.lon, mesh_nodes.lon),
                    grid=CASE
                        WHEN excluded.grid IS NOT NULL AND excluded.grid <> '' THEN excluded.grid
                        ELSE mesh_nodes.grid
                    END,
                    raw_payload_json=excluded.raw_payload_json,
                    updated_utc=excluded.updated_utc
                """,
                _mesh_node_values(source_ref, node, stamp),
            )
        return source_ref, node
    finally:
        conn.close()


def upsert_mesh_nodes(db_path: str | Path, nodes: Sequence[MeshNode], *, updated_utc: str | None = None) -> int:
    count = 0
    for node in nodes:
        upsert_mesh_node(db_path, node, updated_utc=updated_utc)
        count += 1
    return count


def upsert_mesh_channel_policy(
    db_path: str | Path,
    policy: MeshChannelPolicy,
    *,
    updated_utc: str | None = None,
) -> str:
    stamp = str(updated_utc or utc_now().isoformat()).strip()
    source_ref = mesh_channel_policy_source_ref(policy)
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO mesh_channel_policies (
                    source_ref,
                    adapter_id,
                    transport,
                    channel_id,
                    channel_name,
                    channel_role,
                    channel_privacy,
                    mapped_groups_json,
                    retention_window,
                    inbox_enabled,
                    ops_enabled,
                    map_enabled,
                    topic_scan_enabled,
                    default_category,
                    review_state,
                    key_state,
                    key_hint,
                    source,
                    updated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_ref) DO UPDATE SET
                    adapter_id=excluded.adapter_id,
                    transport=excluded.transport,
                    channel_id=excluded.channel_id,
                    channel_name=excluded.channel_name,
                    channel_role=excluded.channel_role,
                    channel_privacy=excluded.channel_privacy,
                    mapped_groups_json=excluded.mapped_groups_json,
                    retention_window=excluded.retention_window,
                    inbox_enabled=excluded.inbox_enabled,
                    ops_enabled=excluded.ops_enabled,
                    map_enabled=excluded.map_enabled,
                    topic_scan_enabled=excluded.topic_scan_enabled,
                    default_category=excluded.default_category,
                    review_state=excluded.review_state,
                    key_state=excluded.key_state,
                    key_hint=excluded.key_hint,
                    source=excluded.source,
                    updated_utc=excluded.updated_utc
                """,
                _mesh_channel_policy_values(source_ref, policy, stamp),
            )
        return source_ref
    finally:
        conn.close()


def upsert_mesh_channel_policies(
    db_path: str | Path,
    policies: Sequence[MeshChannelPolicy],
    *,
    updated_utc: str | None = None,
) -> int:
    count = 0
    for policy in policies:
        upsert_mesh_channel_policy(db_path, policy, updated_utc=updated_utc)
        count += 1
    return count


def archive_mesh_channel_policy(db_path: str | Path, policy: MeshChannelPolicy) -> str:
    """Remove a feed from FIO surfaces while preserving an auditable policy row."""

    return upsert_mesh_channel_policy(
        db_path,
        replace(
            policy,
            inbox_enabled=False,
            ops_enabled=False,
            map_enabled=False,
            topic_scan_enabled=False,
            review_state="ignored",
            source="archived",
        ),
    )


def stage_mesh_channel_policies_from_channels(
    db_path: str | Path,
    channels: Sequence[MeshChannel],
    *,
    review_state: str = "pending",
) -> int:
    """Stage newly discovered device channels without overwriting reviewed choices."""
    if not channels:
        return 0
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
    finally:
        conn.close()

    existing = {
        (policy.adapter_id, policy.transport, policy.channel_id)
        for policy in list_mesh_channel_policies(db_path)
    }
    staged: list[MeshChannelPolicy] = []
    for channel in channels:
        policy = policy_from_channel(channel, review_state=review_state)
        key = (policy.adapter_id, policy.transport, policy.channel_id)
        if key in existing:
            continue
        existing.add(key)
        staged.append(policy)
    return upsert_mesh_channel_policies(db_path, staged) if staged else 0


def set_mesh_message_topic_override(
    db_path: str | Path,
    source_ref: str,
    topics: Sequence[object],
    *,
    operator_attention: bool = False,
    updated_utc: str | None = None,
) -> None:
    ref = _clean(source_ref)
    if not ref:
        return
    normalized_list: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        value = _clean(topic)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized_list.append(value)
    normalized_topics = tuple(normalized_list)
    stamp = str(updated_utc or utc_now().isoformat()).strip()
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO mesh_message_topic_overrides (
                    source_ref, topics_json, operator_attention, updated_utc
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_ref) DO UPDATE SET
                    topics_json=excluded.topics_json,
                    operator_attention=excluded.operator_attention,
                    updated_utc=excluded.updated_utc
                """,
                (ref, _json_dumps(normalized_topics), 1 if operator_attention else 0, stamp),
            )
    finally:
        conn.close()


def clear_mesh_message_topic_override(db_path: str | Path, source_ref: str) -> None:
    ref = _clean(source_ref)
    if not ref:
        return
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        with conn:
            conn.execute("DELETE FROM mesh_message_topic_overrides WHERE source_ref=?", (ref,))
    finally:
        conn.close()


def mesh_message_topic_override(db_path: str | Path, source_ref: str) -> tuple[tuple[str, ...], bool] | None:
    ref = _clean(source_ref)
    if not ref:
        return None
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        row = conn.execute(
            """
            SELECT topics_json, operator_attention
            FROM mesh_message_topic_overrides
            WHERE source_ref=?
            """,
            (ref,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return (_json_tuple(row[0]), bool(row[1]))


def list_mesh_channel_policies(
    db_path: str | Path,
    *,
    adapter_id: str = "",
    transport: str = "",
    review_state: str = "",
    read_only: bool = False,
) -> list[MeshChannelPolicy]:
    clauses: list[str] = []
    params: list[Any] = []
    if adapter_id:
        clauses.append("adapter_id=?")
        params.append(_clean(adapter_id))
    if transport:
        clauses.append("transport=?")
        params.append(_clean(transport))
    if review_state:
        clauses.append("review_state=?")
        params.append(_clean(review_state))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn = (connect_sqlite_readonly(db_path, timeout=0.25, row_factory=sqlite3.Row)
            if read_only else connect_sqlite(db_path, row_factory=sqlite3.Row))
    try:
        if not read_only:
            ensure_mesh_schema(conn)
        rows = conn.execute(
            f"""
            SELECT
                source_ref,
                adapter_id,
                transport,
                channel_id,
                channel_name,
                channel_role,
                channel_privacy,
                mapped_groups_json,
                retention_window,
                inbox_enabled,
                ops_enabled,
                map_enabled,
                topic_scan_enabled,
                default_category,
                review_state,
                key_state,
                key_hint,
                source,
                updated_utc
            FROM mesh_channel_policies
            {where}
            ORDER BY transport, adapter_id, channel_role, channel_name
            """,
            tuple(params),
        ).fetchall()
        return [_policy_from_row(row) for row in rows]
    finally:
        conn.close()


def upsert_mesh_health(db_path: str | Path, snapshot: MeshHealthSnapshot, *, updated_utc: str | None = None) -> str:
    stamp = str(updated_utc or utc_now().isoformat()).strip()
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO mesh_health (
                    adapter_id,
                    transport,
                    enabled,
                    connected,
                    connection_type,
                    device_name,
                    firmware_version,
                    battery_percent,
                    battery_voltage,
                    last_rx_utc,
                    last_tx_utc,
                    last_error,
                    warnings_json,
                    lifecycle_state,
                    required,
                    guidance,
                    updated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(adapter_id) DO UPDATE SET
                    transport=excluded.transport,
                    enabled=excluded.enabled,
                    connected=excluded.connected,
                    connection_type=excluded.connection_type,
                    device_name=excluded.device_name,
                    firmware_version=excluded.firmware_version,
                    battery_percent=excluded.battery_percent,
                    battery_voltage=excluded.battery_voltage,
                    last_rx_utc=excluded.last_rx_utc,
                    last_tx_utc=excluded.last_tx_utc,
                    last_error=excluded.last_error,
                    warnings_json=excluded.warnings_json,
                    lifecycle_state=excluded.lifecycle_state,
                    required=excluded.required,
                    guidance=excluded.guidance,
                    updated_utc=excluded.updated_utc
                """,
                (
                    _clean(snapshot.adapter_id),
                    _clean(snapshot.transport),
                    1 if snapshot.enabled else 0,
                    1 if snapshot.connected else 0,
                    _clean(snapshot.connection_type),
                    _clean(snapshot.device_name),
                    _clean(snapshot.firmware_version),
                    snapshot.battery_percent,
                    snapshot.battery_voltage,
                    snapshot.last_rx.isoformat() if snapshot.last_rx else "",
                    snapshot.last_tx.isoformat() if snapshot.last_tx else "",
                    _clean(snapshot.last_error),
                    _json_dumps(tuple(_clean(warning) for warning in snapshot.warnings if _clean(warning))),
                    _clean(snapshot.lifecycle_state),
                    1 if snapshot.required else 0,
                    _clean(snapshot.guidance),
                    stamp,
                ),
            )
        return _clean(snapshot.adapter_id)
    finally:
        conn.close()


def list_mesh_health(db_path: str | Path, *, transport: str = "", read_only: bool = False) -> list[dict[str, Any]]:
    """Return retained health rows without mutating the database.

    Schema ownership belongs to startup/migration and mesh-ingest writers.  This
    function is also used by presentation code, where an ordinary read must
    never run DDL or inspect table columns.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if transport:
        clauses.append("transport=?")
        params.append(_clean(transport))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    conn = (connect_sqlite_readonly(db_path, timeout=0.25, row_factory=sqlite3.Row)
            if read_only else connect_sqlite(db_path, row_factory=sqlite3.Row))
    try:
        rows = conn.execute(
            f"""
            SELECT
                adapter_id,
                transport,
                enabled,
                connected,
                connection_type,
                device_name,
                firmware_version,
                battery_percent,
                battery_voltage,
                last_rx_utc,
                last_tx_utc,
                last_error,
                warnings_json,
                lifecycle_state,
                required,
                guidance,
                updated_utc
            FROM mesh_health
            {where}
            ORDER BY updated_utc DESC, adapter_id
            """,
            params,
        ).fetchall()
        return [_health_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def list_mesh_source_connection_snapshots(db_path: str | Path, *, transport: str = "") -> list[dict[str, object]]:
    return [
        dict(row.get("source_connection") or {})
        for row in list_mesh_health(db_path, transport=transport)
        if isinstance(row.get("source_connection"), Mapping)
    ]


def store_mesh_event(db_path: str | Path, event: MeshAdapterEvent) -> str:
    if event.message is not None:
        return store_mesh_message_with_channel_policy(db_path, event.message)
    if event.node is not None:
        return project_mesh_node_to_observation(db_path, event.node).source_ref
    if event.health is not None:
        return upsert_mesh_health(db_path, event.health)
    return ""


class MeshEventStoreSink:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def __call__(self, event: MeshAdapterEvent) -> None:
        store_mesh_event(self.db_path, event)

    def stage_channels(self, channels: Sequence[MeshChannel]) -> int:
        return stage_mesh_channel_policies_from_channels(self.db_path, channels)

    def prune_retained_messages(self) -> int:
        return prune_mesh_messages_by_channel_policy(self.db_path)


def project_mesh_message_to_observation(db_path: str | Path, message: MeshMessage) -> Observation:
    source_ref = upsert_mesh_message(db_path, message)
    observation = observation_from_mesh_message(message, source_ref=source_ref)
    upsert_observation(db_path, observation)
    return observation


def store_mesh_message_with_channel_policy(
    db_path: str | Path,
    message: MeshMessage,
    *,
    policies: Sequence[MeshChannelPolicy] | None = None,
) -> str:
    source_ref = upsert_mesh_message(db_path, message)
    active_policies = tuple(
        policies
        if policies is not None
        else list_mesh_channel_policies(db_path, adapter_id=message.adapter_id, transport=message.transport)
    )
    if not any(policy.channel_role == "direct" for policy in active_policies) and _clean(message.channel).lower() in {
        "direct",
        "dm",
        "private-message",
    }:
        direct_policy = default_policy_for_channel(
            adapter_id=message.adapter_id,
            transport=message.transport,
            channel_id="direct",
            channel_name="Direct",
            channel_role="direct",
            channel_privacy="direct",
            source="default",
            review_state="accepted",
        )
        upsert_mesh_channel_policy(db_path, direct_policy)
        active_policies = (*active_policies, direct_policy)
    surfaces = message_allowed_surfaces(message, active_policies)
    if not surfaces:
        delete_observations_by_source_refs(db_path, [source_ref], source_family=message.transport)
        _enqueue_mesh_message_projection(db_path, message, source_ref=source_ref)
        return source_ref

    policy = policy_for_message(message, active_policies)
    observation = observation_from_mesh_message(message, source_ref=source_ref)
    provenance = dict(observation.provenance or {})
    provenance["surfaces"] = list(surfaces)
    if policy is not None:
        channel_label = policy.channel_name or policy.channel_id or message.channel
        groups = policy.mapped_groups or ((channel_label,) if channel_label else ())
        provenance["channel_policy"] = {
            "channel_id": policy.channel_id,
            "channel_name": policy.channel_name,
            "channel_role": policy.channel_role,
            "channel_privacy": policy.channel_privacy,
            "inbox_enabled": policy.inbox_enabled,
            "ops_enabled": policy.ops_enabled,
            "map_enabled": policy.map_enabled,
            "topic_scan_enabled": policy.topic_scan_enabled,
            "default_category": policy.default_category,
            "review_state": policy.review_state,
            "key_state": policy.key_state,
            "key_status": policy.key_display_text,
            "retention_window": policy.retention_window,
            "mapped_groups": list(policy.mapped_groups),
            "source": policy.source,
        }
        target = observation.to_target
        if not target or target == message.channel or target.lower() == "channel":
            target = channel_label
        observation = replace(
            observation,
            to_target=target,
            groups=tuple(_clean(group) for group in groups if _clean(group)),
        )
    structured = analyze_mesh_message(message.text) if "topic_scan" in surfaces else None
    structured_topics = structured.topics if structured is not None and structured.recognized else ()
    analysis_message = replace(message, topics=tuple((*message.topics, *structured_topics))) if structured_topics else message
    if structured is not None and structured.recognized:
        structured_provenance = structured.as_dict()
        if structured.grid or structured.state:
            payload_evidence = location_evidence_from_values(
                grid=structured.grid,
                state=structured.state,
                location_kind="structured_report",
                source_family=message.transport,
                source_ref=source_ref,
                observed_utc=observation.received_utc,
                explanation="Location reported in the mesh body; transport coordinates remain separate.",
            )
            structured_provenance["payload_claim"] = {
                "location_evidence": location_evidence_to_provenance(payload_evidence),
            }
            observation = replace(
                observation,
                lat=None,
                lon=None,
                grid=structured.grid or "",
                state=structured.state or "",
                location_confidence=payload_evidence.legacy_confidence,
            )
        provenance["mesh_structured"] = structured_provenance
        observation = replace(
            observation,
            subject=structured.subject[:80] or observation.subject,
            summary=structured.summary[:240] or observation.summary,
            state=structured.state or observation.state,
            grid=structured.grid or observation.grid,
        )
    projected_topics = _mesh_message_projection_topics(analysis_message, policy=policy, surfaces=surfaces)
    override = mesh_message_topic_override(db_path, source_ref)
    operator_attention = (
        _mesh_message_needs_attention(analysis_message, projected_topics)
        if "ops_center" in surfaces
        else False
    )
    if override is not None:
        projected_topics, operator_attention = override
        provenance["topic_override"] = {
            "topics": list(projected_topics),
            "operator_attention": bool(operator_attention),
        }
    observation = replace(
        observation,
        observed_topics=projected_topics,
        operator_attention=operator_attention,
        provenance=provenance,
    )
    observation = _apply_route_derived_location(db_path, observation, message)
    upsert_observation(db_path, observation)
    _enqueue_mesh_message_projection(db_path, message, source_ref=source_ref)
    return source_ref


def project_mesh_node_to_observation(db_path: str | Path, node: MeshNode) -> Observation:
    source_ref, retained = _upsert_retained_mesh_node(db_path, node)
    observation = observation_from_mesh_node(retained, source_ref=source_ref)
    upsert_observation(db_path, observation)
    return observation


def observation_from_mesh_message(message: MeshMessage, *, source_ref: str | None = None) -> Observation:
    ref = source_ref or mesh_source_ref(message)
    received = (message.rx_time or utc_now()).isoformat()
    subject = _single_line(message.text)
    channel = _clean(message.channel)
    target = _clean(message.to_node or channel)
    if target.lower() == "channel" and channel in {"0", "public"}:
        target = "Public"
    group = "Public" if channel in {"0", "public"} else channel
    topics = _mesh_message_topics(message)
    return Observation(
        observation_id=f"{_clean(message.transport) or 'mesh'}:{ref}",
        source_family=_clean(message.transport) or "mesh",
        source_ref=ref,
        source_app="Local Mesh",
        received_utc=received,
        event_utc=received,
        from_call=_clean(message.from_node),
        to_target=target,
        groups=(group,) if group else (),
        observed_topics=topics,
        operator_attention=_mesh_message_needs_attention(message, topics),
        status=_clean(message.severity),
        subject=subject[:80],
        summary=subject[:240],
        grid=_clean(message.grid).upper(),
        lat=message.lat,
        lon=message.lon,
        location_confidence="declared" if message.lat is not None and message.lon is not None else ("grid" if message.grid else "unknown"),
        route_eligible=False,
        publish_authorized=False,
        provenance={
            "adapter_id": message.adapter_id,
            "transport": message.transport,
            "message_id": message.message_id,
            "channel": message.channel,
            "portnum": message.portnum,
            "routing": message.routing_context(),
        },
    )


def observation_from_mesh_node(node: MeshNode, *, source_ref: str | None = None) -> Observation:
    ref = source_ref or mesh_node_source_ref(node)
    seen = (node.last_heard or utc_now()).isoformat()
    label = _clean(node.display_name)
    summary_parts = [label or "Mesh node"]
    if node.grid:
        summary_parts.append(_clean(node.grid).upper())
    if node.hop_count is not None:
        summary_parts.append(f"{node.hop_count} hop")
    elif node.direct_receive is True:
        summary_parts.append("direct")
    return Observation(
        observation_id=f"{_clean(node.transport) or 'mesh'}:{ref}",
        source_family=_clean(node.transport) or "mesh",
        source_ref=ref,
        source_app="Local Mesh",
        received_utc=seen,
        event_utc=seen,
        from_call=_clean(node.callsign or node.short_name or node.node_id),
        observed_topics=("Comms",),
        operator_attention=False,
        status="seen",
        subject=f"Mesh node: {label}"[:80],
        summary=" | ".join(summary_parts)[:240],
        grid=_clean(node.grid).upper(),
        lat=node.lat,
        lon=node.lon,
        location_confidence="declared" if node.lat is not None and node.lon is not None else ("grid" if node.grid else "unknown"),
        route_eligible=False,
        publish_authorized=False,
        provenance={
            "adapter_id": node.adapter_id,
            "transport": node.transport,
            "node_id": node.node_id,
            "long_name": node.long_name,
            "short_name": node.short_name,
            "role": node.role,
            "routing": node.routing_context(),
            "battery_percent": node.battery_percent,
        },
    )


def _apply_route_derived_location(db_path: str | Path, observation: Observation, message: MeshMessage) -> Observation:
    if observation.lat is not None and observation.lon is not None:
        return observation
    if _clean(observation.grid):
        return observation
    structured = observation.provenance.get("mesh_structured") if isinstance(observation.provenance, Mapping) else None
    payload_claim = structured.get("payload_claim") if isinstance(structured, Mapping) else None
    if isinstance(payload_claim, Mapping) and isinstance(payload_claim.get("location_evidence"), Mapping):
        return observation
    located_node, location_kind = _first_located_message_node(db_path, message)
    if not located_node:
        return observation
    lat = located_node.get("lat")
    lon = located_node.get("lon")
    grid = _clean(located_node.get("grid")).upper()
    if (lat is None or lon is None) and not grid:
        return observation
    label = _clean(
        located_node.get("callsign")
        or located_node.get("short_name")
        or located_node.get("long_name")
        or located_node.get("node_id")
    )
    if location_kind == "sender":
        source_type = "sender_node"
        reason = "Known mesh sender location used as approximate message origin."
        confidence = "sender_lookup"
    else:
        source_type = "route_derived"
        reason = "First known mesh relay/router used as approximate sender area."
        confidence = "route_derived"
    provenance = dict(observation.provenance or {})
    provenance["location_source"] = {
        "type": source_type,
        "label": label,
        "node_id": _clean(located_node.get("node_id")),
        "source_ref": _clean(located_node.get("source_ref")),
        "reason": reason,
    }
    return replace(
        observation,
        lat=lat if lat is not None and lon is not None else observation.lat,
        lon=lon if lat is not None and lon is not None else observation.lon,
        grid=grid,
        location_confidence=confidence,
        provenance=provenance,
    )


def _first_located_message_node(db_path: str | Path, message: MeshMessage) -> tuple[dict[str, Any], str]:
    candidates = _message_location_candidates(message)
    if not candidates:
        return {}, ""
    conn = connect_sqlite(db_path, row_factory=sqlite3.Row)
    try:
        ensure_mesh_schema(conn)
        for candidate, location_kind in candidates:
            row = conn.execute(
                """
                SELECT
                    source_ref,
                    adapter_id,
                    transport,
                    node_id,
                    long_name,
                    short_name,
                    callsign,
                    lat,
                    lon,
                    grid
                FROM mesh_nodes
                WHERE adapter_id=?
                  AND transport=?
                  AND (
                    node_id=?
                    OR callsign=?
                    OR short_name=?
                    OR long_name=?
                  )
                  AND ((lat IS NOT NULL AND lon IS NOT NULL) OR COALESCE(grid, '') <> '')
                ORDER BY
                    CASE WHEN node_id=? THEN 0 WHEN callsign=? THEN 1 WHEN short_name=? THEN 2 ELSE 3 END,
                    COALESCE(last_heard_utc, updated_utc, '') DESC
                LIMIT 1
                """,
                (
                    _clean(message.adapter_id),
                    _clean(message.transport),
                    candidate,
                    candidate.upper(),
                    candidate,
                    candidate,
                    candidate,
                    candidate.upper(),
                    candidate,
                ),
            ).fetchone()
            if row is not None:
                return dict(row), location_kind
    finally:
        conn.close()
    return {}, ""


def _first_located_route_node(db_path: str | Path, message: MeshMessage) -> dict[str, Any]:
    located, kind = _first_located_message_node(db_path, message)
    return located if kind == "route" else {}


def _message_location_candidates(message: MeshMessage) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    for raw, kind in (
        (message.from_node, "sender"),
        (message.via_node, "route"),
        *((hop, "route") for hop in (message.path_hops or ())),
    ):
        candidate = _clean(raw)
        if candidate and all(candidate != existing for existing, _ in candidates):
            candidates.append((candidate, kind))
    return tuple(candidates)


def _route_node_candidates(message: MeshMessage) -> tuple[str, ...]:
    candidates: list[str] = []
    for raw in (message.via_node, *(message.path_hops or ())):
        candidate = _clean(raw)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def prune_mesh_messages_by_channel_policy(
    db_path: str | Path,
    *,
    now_utc: datetime | None = None,
) -> int:
    policies = list_mesh_channel_policies(db_path)
    if not policies:
        return 0
    now = now_utc or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    removed = 0
    for policy in policies:
        window = (policy.retention_window or "").strip().lower()
        if window == "keep pinned":
            continue
        cutoff = _retention_cutoff(now, window)
        source_refs = _expired_mesh_source_refs(db_path, policy, cutoff_iso=cutoff.isoformat() if cutoff else "")
        if not source_refs:
            continue
        delete_observations_by_source_refs(db_path, source_refs, source_family=policy.transport)
        conn = connect_sqlite(db_path)
        try:
            ensure_mesh_schema(conn)
            placeholders = ",".join("?" for _ in source_refs)
            with conn:
                cur = conn.execute(
                    f"DELETE FROM mesh_messages WHERE source_ref IN ({placeholders})",
                    tuple(source_refs),
                )
            removed += int(cur.rowcount or 0)
        finally:
            conn.close()
        for source_ref in source_refs:
            _enqueue_mesh_message_projection(
                db_path,
                None,
                source_ref=source_ref,
                adapter_id=policy.adapter_id,
                transport=policy.transport,
                operation="delete",
            )
    return removed


def _enqueue_mesh_message_projection(
    db_path: str | Path,
    message: MeshMessage | None,
    *,
    source_ref: str,
    adapter_id: str = "",
    transport: str = "",
    operation: str = "upsert",
) -> None:
    """Queue a policy-complete Mesh receipt when the projection queue exists.

    Mesh ingest can run before message-index startup and in small standalone
    tests, so this helper never owns queue schema.  The coordinator's bounded
    reconciliation later catches any receipt stored before that schema exists.
    """

    clean_transport = _clean(message.transport if message is not None else transport) or "mesh"
    clean_adapter = _clean(message.adapter_id if message is not None else adapter_id) or "adapter"
    stamp = utc_now().isoformat()
    conn = connect_sqlite(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_projection_dirty'"
        ).fetchone()
        if exists is None:
            return
        with conn:
            enqueue_dirty_conn(
                conn,
                DirtyProjectionItem(
                    source_id=f"mesh:{clean_transport}:{clean_adapter}",
                    source_family="mesh",
                    external_kind="mesh_message",
                    external_key=_clean(source_ref),
                    operation=_clean(operation).lower() or "upsert",
                    source_version=f"{_clean(source_ref)}:{stamp}",
                ),
                observed_utc=stamp,
            )
    finally:
        conn.close()


def _retention_cutoff(now: datetime, window: str) -> datetime | None:
    if window == "none":
        return None
    if window == "24h":
        return now - timedelta(hours=24)
    if window == "7d":
        return now - timedelta(days=7)
    if window == "30d":
        return now - timedelta(days=30)
    return now - timedelta(days=7)


def _expired_mesh_source_refs(
    db_path: str | Path,
    policy: MeshChannelPolicy,
    *,
    cutoff_iso: str,
) -> list[str]:
    clauses = ["adapter_id=?", "transport=?", "channel=?"]
    params: list[Any] = [_clean(policy.adapter_id), _clean(policy.transport), _clean(policy.channel_id)]
    if cutoff_iso:
        clauses.append("COALESCE(NULLIF(rx_utc, ''), NULLIF(updated_utc, ''), '') < ?")
        params.append(cutoff_iso)
    conn = connect_sqlite(db_path)
    try:
        ensure_mesh_schema(conn)
        rows = conn.execute(
            f"SELECT source_ref FROM mesh_messages WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
        return [str(row[0] or "").strip() for row in rows if row and str(row[0] or "").strip()]
    finally:
        conn.close()


def list_mesh_messages(
    db_path: str | Path,
    *,
    transport: str = "",
    adapter_id: str = "",
    channel: str = "",
    since_utc: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if transport:
        clauses.append("transport=?")
        params.append(_clean(transport))
    if adapter_id:
        clauses.append("adapter_id=?")
        params.append(_clean(adapter_id))
    if channel:
        clauses.append("channel=?")
        params.append(_clean(channel))
    if since_utc:
        clauses.append("COALESCE(rx_utc, '') >= ?")
        params.append(_clean(since_utc))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit or 200)))

    conn = connect_sqlite(db_path, row_factory=sqlite3.Row)
    try:
        ensure_mesh_schema(conn)
        rows = conn.execute(
            f"""
            SELECT
                source_ref,
                adapter_id,
                transport,
                message_id,
                from_node,
                to_node,
                channel,
                portnum,
                text,
                rx_utc,
                hop_count,
                route_type,
                direct_receive,
                via_node,
                path_hops_json,
                snr,
                rssi,
                lat,
                lon,
                grid,
                topics_json,
                severity,
                raw_payload_json,
                updated_utc
            FROM mesh_messages
            {where}
            ORDER BY COALESCE(rx_utc, updated_utc, '') DESC, source_ref DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def list_mesh_nodes(
    db_path: str | Path,
    *,
    transport: str = "",
    adapter_id: str = "",
    callsign: str = "",
    limit: int = 200,
    read_only: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if transport:
        clauses.append("transport=?")
        params.append(_clean(transport))
    if adapter_id:
        clauses.append("adapter_id=?")
        params.append(_clean(adapter_id))
    if callsign:
        clauses.append("callsign=?")
        params.append(_clean(callsign).upper())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit or 200)))

    conn = (connect_sqlite_readonly(db_path, timeout=0.25, row_factory=sqlite3.Row)
            if read_only else connect_sqlite(db_path, row_factory=sqlite3.Row))
    try:
        if not read_only:
            ensure_mesh_schema(conn)
        rows = conn.execute(
            f"""
            SELECT
                source_ref,
                adapter_id,
                transport,
                node_id,
                public_key_or_hash,
                long_name,
                short_name,
                callsign,
                role,
                last_heard_utc,
                hop_count,
                route_type,
                direct_receive,
                via_node,
                path_hops_json,
                snr,
                rssi,
                battery_percent,
                lat,
                lon,
                grid,
                raw_payload_json,
                updated_utc
            FROM mesh_nodes
            {where}
            ORDER BY COALESCE(last_heard_utc, updated_utc, '') DESC, source_ref DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [_node_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def _mesh_message_values(source_ref: str, message: MeshMessage, updated_utc: str) -> tuple[Any, ...]:
    topics = _mesh_message_topics(message)
    return (
        source_ref,
        _clean(message.adapter_id),
        _clean(message.transport),
        _clean(message.message_id),
        _clean(message.from_node),
        _clean(message.to_node),
        _clean(message.channel),
        _clean(message.portnum),
        str(message.text or ""),
        message.rx_time.isoformat() if message.rx_time else "",
        message.hop_count,
        _clean(message.route_type),
        _optional_bool_int(message.direct_receive),
        _clean(message.via_node),
        _json_dumps(tuple(_clean(hop) for hop in message.path_hops if _clean(hop))),
        message.snr,
        message.rssi,
        message.lat,
        message.lon,
        _clean(message.grid).upper(),
        _json_dumps(topics),
        _clean(message.severity) or "info",
        _json_dumps(message.raw),
        updated_utc,
        _clean(message.public_key_or_hash),
        _clean(message.contact_node_id),
    )


def _mesh_node_values(source_ref: str, node: MeshNode, updated_utc: str) -> tuple[Any, ...]:
    return (
        source_ref,
        _clean(node.adapter_id),
        _clean(node.transport),
        _clean(node.node_id),
        _clean(node.public_key_or_hash),
        _clean(node.long_name),
        _clean(node.short_name),
        _clean(node.callsign).upper(),
        _clean(node.role),
        node.last_heard.isoformat() if node.last_heard else "",
        node.hop_count,
        _clean(node.route_type),
        _optional_bool_int(node.direct_receive),
        _clean(node.via_node),
        _json_dumps(tuple(_clean(hop) for hop in node.path_hops if _clean(hop))),
        node.snr,
        node.rssi,
        node.battery_percent,
        node.lat,
        node.lon,
        _clean(node.grid).upper(),
        _json_dumps(node.raw),
        updated_utc,
    )


def _mesh_channel_policy_values(
    source_ref: str,
    policy: MeshChannelPolicy,
    updated_utc: str,
) -> tuple[Any, ...]:
    return (
        source_ref,
        _clean(policy.adapter_id),
        _clean(policy.transport),
        _clean(policy.channel_id),
        _clean(policy.channel_name),
        _clean(policy.channel_role),
        _clean(policy.channel_privacy),
        _json_dumps(tuple(_clean(group).upper() for group in policy.mapped_groups if _clean(group))),
        _clean(policy.retention_window) or "7d",
        int(bool(policy.inbox_enabled)),
        int(bool(policy.ops_enabled)),
        int(bool(policy.map_enabled)),
        int(bool(policy.topic_scan_enabled)),
        _clean(policy.default_category) or "auto",
        _clean(policy.review_state) or "pending",
        _clean(policy.key_state) or "not_required",
        _clean(policy.key_hint),
        _clean(policy.source) or "device",
        updated_utc,
    )


def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["topics"] = _json_tuple(result.get("topics_json"))
    result["direct_receive"] = _optional_bool(result.get("direct_receive"))
    result["path_hops"] = _json_tuple(result.get("path_hops_json"))
    result["raw_payload"] = _json_mapping(result.get("raw_payload_json"))
    return result


def _node_row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["direct_receive"] = _optional_bool(result.get("direct_receive"))
    result["path_hops"] = _json_tuple(result.get("path_hops_json"))
    result["raw_payload"] = _json_mapping(result.get("raw_payload_json"))
    return result


def _health_row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    result["connected"] = bool(result.get("connected"))
    result["warnings"] = _json_tuple(result.get("warnings_json"))
    snapshot = source_connection_from_mesh_health(result)
    result["lifecycle_state"] = str(result.get("lifecycle_state") or snapshot.lifecycle_state)
    result["guidance"] = str(result.get("guidance") or snapshot.guidance)
    result["required"] = bool(result.get("required"))
    result["attention"] = bool(snapshot.attention)
    result["source_connection"] = snapshot.as_dict()
    return result


def _policy_from_row(row: Mapping[str, Any]) -> MeshChannelPolicy:
    result = dict(row)
    result["mapped_groups"] = _json_tuple(result.get("mapped_groups_json"))
    result["inbox_enabled"] = bool(result.get("inbox_enabled"))
    result["ops_enabled"] = bool(result.get("ops_enabled"))
    result["map_enabled"] = bool(result.get("map_enabled"))
    result["topic_scan_enabled"] = bool(result.get("topic_scan_enabled"))
    return policy_from_mapping(result)


def _single_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _mesh_message_topics(message: MeshMessage) -> tuple[str, ...]:
    topics: list[str] = []
    seen: set[str] = set()

    def add(topic: object) -> None:
        value = _single_line(topic)
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        topics.append(value)

    for topic in message.topics:
        add(topic)
    for topic in normalize_topic_terms(message.text or ""):
        add(topic)

    if not topics and _single_line(message.text):
        add(SOCIAL_TOPIC)
    return tuple(topics)


def _mesh_message_projection_topics(
    message: MeshMessage,
    *,
    policy: MeshChannelPolicy | None,
    surfaces: Sequence[str],
) -> tuple[str, ...]:
    surface_set = {str(surface or "").strip().lower() for surface in surfaces}
    if "topic_scan" not in surface_set:
        return ()
    default_category = _clean(getattr(policy, "default_category", "")).lower() if policy is not None else "auto"
    if default_category == "ignore":
        return ()
    if default_category == "social":
        return (SOCIAL_TOPIC,) if _single_line(message.text) else ()
    return _apply_mesh_topic_corrections(_mesh_message_topics(message), message.text)


def _apply_mesh_topic_corrections(topics: Sequence[str], text: object) -> tuple[str, ...]:
    message_text = str(text or "").lower()
    corrected = tuple(topics)
    if "flood advertis" in message_text or "flood advert" in message_text:
        return (SOCIAL_TOPIC,) if _single_line(text) else ()
    if not corrected and _single_line(text):
        return (SOCIAL_TOPIC,)
    return corrected


def _mesh_message_needs_attention(message: MeshMessage, topics: Sequence[str]) -> bool:
    severity = _clean(message.severity).lower()
    if severity in ATTENTION_SEVERITIES:
        return True
    return any(_clean(topic).lower() != SOCIAL_TOPIC.lower() for topic in topics)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _optional_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except Exception:
        normalized = str(value).strip().lower()
        if normalized in {"true", "yes", "on"}:
            return True
        if normalized in {"false", "no", "off"}:
            return False
    return None


def _optional_bool_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(bool(value))


def _json_dumps(values: object) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _json_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        loaded = json.loads(value)
    except Exception:
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item).strip() for item in loaded if str(item).strip())


def _json_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    present = {str(row[1] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
