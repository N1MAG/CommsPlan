"""Durable, worker-owned state for manually reviewed forwarding intents.

The store owns only the two ``manual_forward_*`` tables in the supplied nets
database.  It does not know about radios, endpoints, or dispatch; callers must
use the existing guarded transport paths after a claim is returned.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any


MIN_EXPIRY_SECONDS = 60
DEFAULT_EXPIRY_SECONDS = 60 * 60
MAX_EXPIRY_SECONDS = 24 * 60 * 60
MAX_ATTEMPTS = 3
RECOVERY_AFTER_SECONDS = 120
MAX_LIST_LIMIT = 100

ACTIVE_DEDUPE_STATES = ("dispatching", "submitted", "unknown", "sent", "acked")
CLAIMABLE_STATES = ("draft", "failed")
FINISH_STATES = ("submitted", "unknown", "sent", "acked", "failed")


def _canonical_json(value: object) -> str:
    """Serialize a JSON-compatible value without nondeterministic formatting."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _source_from_request(request: Mapping[str, Any]) -> dict[str, Any]:
    raw = request.get("source")
    source = dict(raw) if isinstance(raw, Mapping) else {}
    # Flat request fields are accepted for callers that have not yet assembled
    # the UI's nested source object.  Nested values remain authoritative.
    aliases = {
        "source_id": ("source_id", "id"),
        "family": ("family", "source_family"),
        "sender": ("sender", "original_sender"),
        "body": ("body", "payload"),
    }
    for key, names in aliases.items():
        if key not in source:
            for name in names:
                if name in source:
                    source[key] = source[name]
                    break
        if key not in source:
            for name in names:
                if name in request:
                    source[key] = request[name]
                    break
    if "body" not in source and "payload" in request and not isinstance(request.get("payload"), Mapping):
        source["body"] = request.get("payload")
    return source


def _destination_from_request(request: Mapping[str, Any]) -> dict[str, Any]:
    raw = request.get("destination", request.get("dest"))
    destination = dict(raw) if isinstance(raw, Mapping) else {}
    aliases = {
        "id": ("id", "destination_id", "endpoint_id"),
        "transport": ("transport", "transport_label"),
        "recipient": ("recipient",),
    }
    for key, names in aliases.items():
        if key not in destination:
            for name in names:
                if name in request:
                    destination[key] = request[name]
                    break
    return destination


def _request_parts(request: object) -> tuple[dict[str, Any], dict[str, Any], str, str, float, str, object]:
    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping")
    # Validate/canonicalize before opening a write transaction.  The exact
    # caller snapshot is stored; derived source/destination views are only for
    # indexing and convenient responses.
    snapshot = dict(request)
    snapshot_json = _canonical_json(snapshot)
    source = _source_from_request(snapshot)
    destination = _destination_from_request(snapshot)
    source_id = _text(source.get("source_id") or source.get("id"))
    relay_id = _text(snapshot.get("relay_id"))
    if not relay_id:
        raise ValueError("relay_id is required")
    if not source_id:
        raise ValueError("source_id is required")
    if not _text(source.get("family")):
        raise ValueError("source family is required")
    if not _text(source.get("sender")):
        raise ValueError("source sender is required")
    if "body" not in source:
        raise ValueError("source body is required")
    if not isinstance(destination, Mapping) or not _text(destination.get("id")):
        raise ValueError("destination id is required")
    if not _text(destination.get("transport")):
        raise ValueError("destination transport is required")
    try:
        ttl_raw = snapshot.get("expires_seconds", DEFAULT_EXPIRY_SECONDS)
        ttl = float(ttl_raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("expires_seconds is invalid") from None
    if not math.isfinite(ttl):
        raise ValueError("expires_seconds is invalid")
    ttl = max(MIN_EXPIRY_SECONDS, min(MAX_EXPIRY_SECONDS, ttl))
    payload = snapshot.get("payload", source.get("body", ""))
    return snapshot, source, destination, source_id, ttl, snapshot_json, payload


def _dedupe_key(source: Mapping[str, Any], destination: Mapping[str, Any], payload: object = "") -> str:
    """Return the durable duplicate identity, excluding mutable UI metadata."""

    # Relay/draft references, labels, and source display metadata are not event
    # identity.  These explicit fields are the stable source provenance.
    if source.get("native_kind") and source.get("native_id"):
        # Projection/UI identities may differ while pointing at one native
        # receipt. Prefer that immutable native identity when available.
        source_identity = {
            key: source.get(key, "")
            for key in ("family", "native_kind", "native_id")
            if key in source
        }
    else:
        source_identity = {
            key: source.get(key, "")
            for key in ("source_id", "family", "native_kind", "native_id", "source_ref")
            if key in source
        }
    destination_identity = {
        key: destination.get(key, "")
        for key in (
            "transport", "id", "fingerprint", "endpoint_id", "endpoint_fingerprint",
            "config_id", "configuration_id", "channel_id", "channel", "node_id",
            "destination_kind", "kind",
        )
        if key in destination
    }
    transport = _text(destination.get("transport")).casefold()
    recipient = destination.get("recipient", "")
    if transport == "js8":
        recipient = _text(recipient).upper()
    material = {
        "source": source_identity,
        "payload_sha256": _hash_text(_canonical_json(payload)),
        "destination": destination_identity,
        "recipient": recipient,
    }
    return _hash_text(_canonical_json(material))


class ForwardStore:
    """A small transactional store intended to run from worker code only."""

    def __init__(self, db_path: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path)
        self.clock = clock
        self.timeout = 5.0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS manual_forward_intents (
                relay_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_hash TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                expires REAL NOT NULL,
                updated REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_manual_forward_source_updated
                ON manual_forward_intents(source_id, updated DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_forward_active_dedupe
                ON manual_forward_intents(dedupe_key)
                WHERE state IN ('dispatching','submitted','unknown','sent','acked');
            CREATE TABLE IF NOT EXISTS manual_forward_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                relay_id TEXT NOT NULL,
                state TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                event_time REAL NOT NULL,
                request_hash TEXT NOT NULL DEFAULT '',
                payload_evidence_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_manual_forward_audit_relay
                ON manual_forward_audit(relay_id, audit_id);
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(manual_forward_intents)")}
        if "request_hash" not in columns:
            conn.execute("ALTER TABLE manual_forward_intents ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
        audit_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(manual_forward_audit)")}
        for name, definition in (("request_hash", "TEXT NOT NULL DEFAULT ''"), ("payload_evidence_json", "TEXT NOT NULL DEFAULT '{}'")):
            if name not in audit_columns:
                conn.execute(f"ALTER TABLE manual_forward_audit ADD COLUMN {name} {definition}")

    def _append_audit(self, conn: sqlite3.Connection, relay_id: str, state: str, detail: str, now: float) -> None:
        row = conn.execute("SELECT request_json,request_hash FROM manual_forward_intents WHERE relay_id=?", (relay_id,)).fetchone()
        request = (self._decode_request(row["request_json"])[0] or {}) if row else {}
        conn.execute(
            "INSERT INTO manual_forward_audit(relay_id,state,detail,event_time,request_hash,payload_evidence_json) VALUES(?,?,?,?,?,?)",
            (relay_id, state, detail, now, row["request_hash"] if row else "", _canonical_json(request.get("payload_evidence") or {})),
        )

    @staticmethod
    def _decode_request(request_json: str) -> tuple[dict[str, Any] | None, str]:
        try:
            value = json.loads(request_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "corrupt request payload"
        if not isinstance(value, dict):
            return None, "corrupt request payload"
        return value, ""

    def _record(self, conn: sqlite3.Connection, row: sqlite3.Row, *, ok: bool = True, detail: str | None = None) -> dict[str, Any]:
        request, decode_detail = self._decode_request(row["request_json"])
        request = request or {}
        source = _source_from_request(request)
        destination = _destination_from_request(request)
        effective_detail = detail if detail is not None else str(row["detail"] or "")
        if decode_detail and not effective_detail:
            effective_detail = decode_detail
        history_rows = conn.execute(
            "SELECT audit_id,state,detail,event_time,request_hash,payload_evidence_json FROM manual_forward_audit WHERE relay_id=? ORDER BY audit_id DESC LIMIT 20",
            (row["relay_id"],),
        ).fetchall()
        history = [
            {"audit_id": int(item["audit_id"]), "state": str(item["state"]), "detail": str(item["detail"] or ""), "event_time": float(item["event_time"]), "request_hash": str(item["request_hash"]), "payload_evidence": self._decode_request(item["payload_evidence_json"] or "{}")[0] or {}}
            for item in reversed(history_rows)
        ]
        result: dict[str, Any] = {
            "ok": bool(ok),
            "relay_id": str(row["relay_id"]),
            "source_id": str(row["source_id"]),
            "dedupe_key": str(row["dedupe_key"]),
            "request_hash": str(row["request_hash"] or ""),
            "state": str(row["state"]),
            "detail": effective_detail,
            "created": float(row["created"]),
            "expires": float(row["expires"]),
            "updated": float(row["updated"]),
            "attempts": int(row["attempts"]),
            "request": request,
            "source": source,
            "destination": destination,
            "recipient": _text(destination.get("recipient")),
            "history": history,
        }
        if decode_detail:
            result["corrupt"] = True
        return result

    @staticmethod
    def _blocked(relay_id: str = "", detail: str = "") -> dict[str, Any]:
        return {"ok": False, "blocked": True, "relay_id": relay_id, "state": "blocked", "detail": detail}

    def _expire_row(self, conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> sqlite3.Row:
        if str(row["state"]) in CLAIMABLE_STATES and float(row["expires"]) <= now:
            conn.execute(
                "UPDATE manual_forward_intents SET state='expired', detail=?, updated=? WHERE relay_id=? AND state=?",
                ("intent expired", now, row["relay_id"], row["state"]),
            )
            self._append_audit(conn, str(row["relay_id"]), "expired", "intent expired", now)
            return conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (row["relay_id"],)).fetchone()
        return row

    def save(self, request: dict) -> dict:
        """Insert or update a draft/failed intent without extending expiry."""

        try:
            snapshot, source, destination, source_id, ttl, snapshot_json, payload = _request_parts(request)
            dedupe_key = _dedupe_key(source, destination, source.get("body", ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._blocked(detail=str(exc))
        relay_id = _text(snapshot.get("relay_id"))
        now = float(self.clock())
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay_id,)).fetchone()
            if row is not None:
                row = self._expire_row(conn, row, now)
                if str(row["state"]) not in CLAIMABLE_STATES:
                    return self._record(conn, row, ok=False, detail="intent is not editable in its current state")
                old_request, _ = self._decode_request(row["request_json"])
                old_source = _source_from_request(old_request or {})
                if _text(old_source.get("source_id") or old_source.get("id")) != source_id:
                    return self._record(conn, row, ok=False, detail="source_id cannot change for an existing relay_id")
                existing = conn.execute(
                    "SELECT * FROM manual_forward_intents WHERE dedupe_key=? AND relay_id<>? AND state IN ('dispatching','submitted','unknown','sent','acked')",
                    (dedupe_key, relay_id),
                ).fetchone()
                if existing is not None:
                    return self._record(conn, row, ok=False, detail="duplicate active relay intent")
                expires = min(float(row["expires"]), now + ttl)
                conn.execute(
                    "UPDATE manual_forward_intents SET state='draft',dedupe_key=?,request_json=?,request_hash=?,detail='',expires=?,updated=? WHERE relay_id=?",
                    (dedupe_key, snapshot_json, _hash_text(snapshot_json), expires, now, relay_id),
                )
                self._append_audit(conn, relay_id, "draft", "draft updated", now)
            else:
                active = conn.execute(
                    "SELECT relay_id FROM manual_forward_intents WHERE dedupe_key=? AND state IN ('dispatching','submitted','unknown','sent','acked')",
                    (dedupe_key,),
                ).fetchone()
                if active is not None:
                    return self._blocked(relay_id, "duplicate active relay intent")
                expires = now + ttl
                conn.execute(
                    "INSERT INTO manual_forward_intents(relay_id,source_id,dedupe_key,request_json,request_hash,state,detail,created,expires,updated,attempts) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                    (relay_id, source_id, dedupe_key, snapshot_json, _hash_text(snapshot_json), "draft", "", now, expires, now),
                )
                self._append_audit(conn, relay_id, "draft", "intent created", now)
            row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay_id,)).fetchone()
            return self._record(conn, row)

    def claim(self, request: dict) -> dict:
        """Atomically save a new request when needed and claim one send lease."""

        try:
            snapshot, source, destination, source_id, ttl, snapshot_json, payload = _request_parts(request)
            dedupe_key = _dedupe_key(source, destination, source.get("body", ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._blocked(detail=str(exc))
        relay_id = _text(snapshot.get("relay_id"))
        now = float(self.clock())
        try:
            with self._transaction() as conn:
                row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay_id,)).fetchone()
                if row is None:
                    active = conn.execute(
                        "SELECT relay_id FROM manual_forward_intents WHERE dedupe_key=? AND state IN ('dispatching','submitted','unknown','sent','acked')",
                        (dedupe_key,),
                    ).fetchone()
                    if active is not None:
                        return self._blocked(relay_id, "duplicate active relay intent")
                    expires = now + ttl
                    conn.execute(
                        "INSERT INTO manual_forward_intents(relay_id,source_id,dedupe_key,request_json,request_hash,state,detail,created,expires,updated,attempts) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                        (relay_id, source_id, dedupe_key, snapshot_json, _hash_text(snapshot_json), "draft", "", now, expires, now),
                    )
                    self._append_audit(conn, relay_id, "draft", "intent created", now)
                    row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay_id,)).fetchone()
                if row["request_json"] != snapshot_json:
                    return self._record(conn, row, ok=False, detail="request snapshot does not match stored intent")
                row = self._expire_row(conn, row, now)
                if str(row["state"]) not in CLAIMABLE_STATES:
                    return self._record(conn, row, ok=False, detail="intent is not claimable")
                if int(row["attempts"]) >= MAX_ATTEMPTS:
                    return self._record(conn, row, ok=False, detail="maximum dispatch attempts reached")
                duplicate = conn.execute(
                    "SELECT relay_id FROM manual_forward_intents WHERE dedupe_key=? AND relay_id<>? AND state IN ('dispatching','submitted','unknown','sent','acked')",
                    (dedupe_key, relay_id),
                ).fetchone()
                if duplicate is not None:
                    return self._record(conn, row, ok=False, detail="duplicate active relay intent")
                attempts = int(row["attempts"]) + 1
                conn.execute(
                    "UPDATE manual_forward_intents SET state='dispatching',detail=?,attempts=?,updated=? WHERE relay_id=? AND state IN ('draft','failed')",
                    ("claimed before RF dispatch", attempts, now, relay_id),
                )
                self._append_audit(conn, relay_id, "dispatching", "claimed before RF dispatch", now)
                row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay_id,)).fetchone()
                return self._record(conn, row)
        except sqlite3.IntegrityError:
            return self._blocked(relay_id, "duplicate active relay intent")

    def finish(self, relay_id: str, state: str, detail: str = "", attempt: int | None = None) -> dict:
        """Append valid completion evidence for the current dispatch claim."""

        relay = _text(relay_id)
        normalized = "submitted" if _text(state).casefold() == "accepted" else _text(state).casefold()
        if normalized not in FINISH_STATES:
            return self._blocked(relay, "invalid finish state")
        now = float(self.clock())
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay,)).fetchone()
            if row is None:
                return self._blocked(relay, "unknown relay_id")
            if str(row["state"]) != "dispatching":
                return self._record(conn, row, ok=False, detail="late or stale completion ignored")
            if attempt is not None:
                try:
                    attempt_number = int(attempt)
                except (TypeError, ValueError, OverflowError):
                    return self._record(conn, row, ok=False, detail="invalid dispatch attempt")
                if attempt_number != int(row["attempts"]):
                    return self._record(conn, row, ok=False, detail="stale dispatch attempt ignored")
            conn.execute(
                "UPDATE manual_forward_intents SET state=?,detail=?,updated=? WHERE relay_id=? AND state='dispatching'",
                (normalized, _text(detail), now, relay),
            )
            self._append_audit(conn, relay, normalized, _text(detail), now)
            row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay,)).fetchone()
            return self._record(conn, row)

    def cancel(self, relay_id: str) -> dict:
        """Cancel a reviewable draft/failed intent while retaining audit history."""

        relay = _text(relay_id)
        now = float(self.clock())
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay,)).fetchone()
            if row is None:
                return self._blocked(relay, "unknown relay_id")
            row = self._expire_row(conn, row, now)
            if str(row["state"]) not in CLAIMABLE_STATES:
                return self._record(conn, row, ok=False, detail="only draft or failed intents may be cancelled")
            conn.execute(
                "UPDATE manual_forward_intents SET state='cancelled',detail=?,updated=? WHERE relay_id=? AND state IN ('draft','failed')",
                ("cancelled by operator", now, relay),
            )
            self._append_audit(conn, relay, "cancelled", "cancelled by operator", now)
            row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay,)).fetchone()
            return self._record(conn, row)

    def list(self, source_id: str = "", limit: int = MAX_LIST_LIMIT) -> list[dict[str, Any]]:
        """Return a bounded newest-first projection and expire stale drafts."""

        try:
            bounded_limit = max(1, min(MAX_LIST_LIMIT, int(limit)))
        except (TypeError, ValueError, OverflowError):
            bounded_limit = MAX_LIST_LIMIT
        now = float(self.clock())
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM manual_forward_intents WHERE (?='' OR source_id=?) ORDER BY updated DESC LIMIT ?",
                (_text(source_id), _text(source_id), bounded_limit),
            ).fetchall()
            for row in rows:
                self._expire_row(conn, row, now)
            if rows:
                rows = conn.execute(
                    "SELECT * FROM manual_forward_intents WHERE (?='' OR source_id=?) ORDER BY updated DESC LIMIT ?",
                    (_text(source_id), _text(source_id), bounded_limit),
                ).fetchall()
            return [self._record(conn, row) for row in rows]

    def recover(self) -> list[dict[str, Any]]:
        """Mark expired dispatch leases unknown; never assume RF success."""

        now = float(self.clock())
        cutoff = now - RECOVERY_AFTER_SECONDS
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM manual_forward_intents WHERE state='dispatching' AND updated<=? ORDER BY updated ASC",
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE manual_forward_intents SET state='unknown',detail=?,updated=? WHERE relay_id=? AND state='dispatching'",
                    ("dispatch lease expired; completion unknown", now, row["relay_id"]),
                )
                self._append_audit(conn, str(row["relay_id"]), "unknown", "dispatch lease expired; completion unknown", now)
            return [
                self._record(conn, conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (row["relay_id"],)).fetchone())
                for row in rows
            ]

    def authorization_current(self, relay_id: str, attempt: int) -> bool:
        """Read-only lease check required immediately before transport I/O."""

        relay = _text(relay_id)
        try:
            attempt_number = int(attempt)
        except (TypeError, ValueError, OverflowError):
            return False
        if not self.db_path.exists():
            return False
        try:
            conn = self._connect()
        except sqlite3.Error:
            return False
        try:
            row = conn.execute(
                "SELECT relay_id,request_json,request_hash,state,expires,attempts FROM manual_forward_intents WHERE relay_id=?",
                (relay,),
            ).fetchone()
            if row is None or str(row["state"]) != "dispatching":
                return False
            if int(row["attempts"]) != attempt_number or float(row["expires"]) <= float(self.clock()):
                return False
            request, decode_detail = self._decode_request(row["request_json"])
            if decode_detail or request is None:
                return False
            if str(row["request_hash"] or "") != _hash_text(str(row["request_json"])):
                return False
            if _canonical_json(request) != str(row["request_json"]):
                return False
            try:
                _request_parts(request)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            return _text(request.get("relay_id")) == relay
        except (sqlite3.Error, TypeError, ValueError, OverflowError):
            return False
        finally:
            conn.close()


__all__ = ["ForwardStore"]
