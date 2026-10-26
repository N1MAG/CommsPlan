from __future__ import annotations

import datetime as dt
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Iterable


TRAILING_CALL_NOISE_RE = re.compile(r"[^A-Z0-9/]+$")
PORTABLE_SUFFIX_RE = re.compile(r"/(P|M|MM|QRP|SOTA|ROVER|[A-Z0-9]{1,4})$")
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,12}(?:/[A-Z0-9]{1,6})?$")


class OperatorIdentityError(ValueError):
    """Base error for an operator-identity mutation."""


class CallsignConflictError(OperatorIdentityError):
    """Raised when a callsign already belongs to another identity."""


@dataclass(frozen=True)
class OperatorIdentity:
    operator_id: str
    current_callsign: str
    matched_callsign: str
    effective_from: float
    effective_to: float | None


@dataclass(frozen=True)
class CallsignHistoryEntry:
    operator_id: str
    callsign: str
    effective_from: float
    effective_to: float | None
    provenance: str
    note: str


def canonical_callsign(value: object) -> str:
    callsign = str(value or "").strip().upper()
    callsign = TRAILING_CALL_NOISE_RE.sub("", callsign)
    if not callsign:
        return ""
    return PORTABLE_SUFFIX_RE.sub("", callsign)


def validate_callsign(value: object) -> str:
    callsign = canonical_callsign(value)
    if not callsign or not CALLSIGN_RE.fullmatch(callsign):
        raise OperatorIdentityError("Enter a valid callsign.")
    return callsign


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _as_epoch(value: object | None, *, default: float | None = None) -> float:
    if value is None or value == "":
        if default is None:
            return dt.datetime.now(dt.timezone.utc).timestamp()
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        parsed = dt.datetime.strptime(text, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).timestamp()


def _identity_tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
         WHERE type='table'
           AND name IN ('operator_identities','operator_callsign_history','operator_identity_audit')
        """
    ).fetchone()
    return bool(row and int(row[0] or 0) == 3)


def ensure_operator_identity_schema(
    conn: sqlite3.Connection, *, backfill_operator_rows: bool = True
) -> None:
    """Create identity tables and idempotently map current operator rows."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_identities (
            operator_id TEXT PRIMARY KEY,
            current_callsign TEXT NOT NULL COLLATE NOCASE UNIQUE,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_callsign_history (
            alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operator_id TEXT NOT NULL,
            callsign TEXT NOT NULL COLLATE NOCASE,
            effective_from REAL NOT NULL DEFAULT 0,
            effective_to REAL,
            provenance TEXT NOT NULL DEFAULT 'migration',
            note TEXT NOT NULL DEFAULT '',
            created_utc TEXT NOT NULL,
            UNIQUE(operator_id, callsign, effective_from)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_identity_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operator_id TEXT NOT NULL,
            action TEXT NOT NULL,
            old_callsign TEXT,
            new_callsign TEXT,
            effective_at REAL,
            changed_utc TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operator_alias_lookup "
        "ON operator_callsign_history(callsign COLLATE NOCASE, effective_from DESC, effective_to)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operator_alias_identity "
        "ON operator_callsign_history(operator_id, effective_from DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_alias_current "
        "ON operator_callsign_history(callsign COLLATE NOCASE) WHERE effective_to IS NULL"
    )
    if not backfill_operator_rows:
        return
    operator_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_checkins'"
    ).fetchone()
    if operator_table is None:
        return
    operator_columns = {
        str(row[1] or "") for row in conn.execute("PRAGMA table_info(operator_checkins)").fetchall()
    }
    if "operator_id" not in operator_columns:
        return
    # ``operator_checkins`` remains a compatibility roster keyed by the exact
    # observed callsign.  It can therefore contain both a base call and a
    # portable/variant call which intentionally resolve to one stable operator
    # identity.  Older builds briefly made this index UNIQUE; remove that
    # migration artifact before backfilling so existing rosters self-heal.
    index_name = "idx_operator_checkins_identity"
    index = next((row for row in conn.execute("PRAGMA index_list(operator_checkins)")
                  if row[1] == index_name), None)
    definition = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    sql = re.sub(r"\s+", "", str(definition[0] or "").lower()) if definition else ""
    columns = [row[2] for row in conn.execute(f"PRAGMA index_info({index_name})")]
    valid = (index is not None and not index[2] and bool(index[4])
             and columns == ["operator_id"]
             and sql.partition("where")[2] == "operator_idisnotnullandoperator_id<>''")
    if not valid:
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
        conn.execute(
            f"CREATE INDEX {index_name} ON operator_checkins(operator_id) "
            "WHERE operator_id IS NOT NULL AND operator_id <> ''"
        )

    now_iso = _utc_now_iso()
    rows = conn.execute(
        """
        SELECT o.callsign, COALESCE(o.operator_id,''), COALESCE(o.first_seen_utc,'')
          FROM operator_checkins o
          LEFT JOIN operator_identities i ON i.operator_id=o.operator_id
         WHERE COALESCE(o.callsign,'') <> ''
           AND (COALESCE(o.operator_id,'') = '' OR i.operator_id IS NULL)
        """
    ).fetchall()
    for raw_call, raw_operator_id, first_seen in rows:
        callsign = canonical_callsign(raw_call)
        if not callsign:
            continue
        operator_id = str(raw_operator_id or "").strip()
        if not operator_id:
            current = conn.execute(
                "SELECT operator_id FROM operator_callsign_history "
                "WHERE callsign=? COLLATE NOCASE AND effective_to IS NULL LIMIT 1",
                (callsign,),
            ).fetchone()
            operator_id = str(current[0]) if current else uuid.uuid4().hex
            conn.execute(
                "UPDATE operator_checkins SET operator_id=? WHERE callsign=? COLLATE NOCASE",
                (operator_id, raw_call),
            )
        conn.execute(
            """
            INSERT INTO operator_identities(operator_id, current_callsign, created_utc, updated_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(operator_id) DO UPDATE SET
                current_callsign=excluded.current_callsign,
                updated_utc=excluded.updated_utc
            """,
            (operator_id, callsign, now_iso, now_iso),
        )
        effective_from = 0.0
        try:
            effective_from = _as_epoch(first_seen, default=0.0)
        except (TypeError, ValueError):
            pass
        conn.execute(
            """
            INSERT OR IGNORE INTO operator_callsign_history(
                operator_id, callsign, effective_from, effective_to,
                provenance, note, created_utc
            ) VALUES (?, ?, ?, NULL, 'migration', '', ?)
            """,
            (operator_id, callsign, effective_from, now_iso),
        )


def resolve_operator_identity(
    conn: sqlite3.Connection,
    callsign: object,
    *,
    at_utc: object | None = None,
    schema_ready: bool = False,
) -> OperatorIdentity | None:
    """Resolve a current or former callsign with one indexed alias lookup."""
    normalized = canonical_callsign(callsign)
    if not normalized:
        return None
    if not schema_ready and not _identity_tables_exist(conn):
        ensure_operator_identity_schema(conn, backfill_operator_rows=False)
    params: list[object] = [normalized]
    time_clause = ""
    if at_utc is not None:
        epoch = _as_epoch(at_utc)
        time_clause = " AND h.effective_from <= ? AND (h.effective_to IS NULL OR h.effective_to > ?)"
        params.extend((epoch, epoch))
    row = conn.execute(
        """
        SELECT h.operator_id, i.current_callsign, h.callsign,
               h.effective_from, h.effective_to
          FROM operator_callsign_history h
          JOIN operator_identities i ON i.operator_id=h.operator_id
         WHERE h.callsign=? COLLATE NOCASE
        """
        + time_clause
        + " ORDER BY CASE WHEN h.effective_to IS NULL THEN 0 ELSE 1 END, h.effective_from DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    return OperatorIdentity(
        operator_id=str(row[0]),
        current_callsign=str(row[1] or "").upper(),
        matched_callsign=str(row[2] or "").upper(),
        effective_from=float(row[3] or 0.0),
        effective_to=None if row[4] is None else float(row[4]),
    )


def get_or_create_operator_identity(
    conn: sqlite3.Connection,
    callsign: object,
    *,
    at_utc: object | None = None,
    provenance: str = "ingest",
) -> OperatorIdentity:
    """Resolve an effective callsign owner, or create a separate identity.

    When a trustworthy evidence time is supplied, a closed former alias may
    resolve to its original operator. Without one, only an open alias is used;
    this deliberately avoids silently merging later callsign reuse.
    """
    normalized = validate_callsign(callsign)
    if not _identity_tables_exist(conn):
        ensure_operator_identity_schema(conn, backfill_operator_rows=False)
    if at_utc is not None:
        existing = resolve_operator_identity(conn, normalized, at_utc=at_utc)
    else:
        row = conn.execute(
            """
            SELECT h.operator_id, i.current_callsign, h.callsign,
                   h.effective_from, h.effective_to
              FROM operator_callsign_history h
              JOIN operator_identities i ON i.operator_id=h.operator_id
             WHERE h.callsign=? COLLATE NOCASE AND h.effective_to IS NULL
             LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        existing = (
            OperatorIdentity(
                operator_id=str(row[0]),
                current_callsign=str(row[1] or "").upper(),
                matched_callsign=str(row[2] or "").upper(),
                effective_from=float(row[3] or 0.0),
                effective_to=None if row[4] is None else float(row[4]),
            )
            if row
            else None
        )
    if existing is not None:
        return existing

    # Receipt time is the attribution fallback when no trustworthy event time
    # exists; never create a zero-based interval that overlaps old ownership.
    effective_epoch = _as_epoch(at_utc)
    operator_id = uuid.uuid4().hex
    now_iso = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO operator_identities(operator_id, current_callsign, created_utc, updated_utc)
        VALUES (?, ?, ?, ?)
        """,
        (operator_id, normalized, now_iso, now_iso),
    )
    conn.execute(
        """
        INSERT INTO operator_callsign_history(
            operator_id, callsign, effective_from, effective_to,
            provenance, note, created_utc
        ) VALUES (?, ?, ?, NULL, ?, '', ?)
        """,
        (operator_id, normalized, effective_epoch, str(provenance or "ingest"), now_iso),
    )
    return OperatorIdentity(operator_id, normalized, normalized, effective_epoch, None)


def list_callsign_history(
    conn: sqlite3.Connection, callsign_or_operator_id: object
) -> tuple[CallsignHistoryEntry, ...]:
    if not _identity_tables_exist(conn):
        ensure_operator_identity_schema(conn, backfill_operator_rows=False)
    token = str(callsign_or_operator_id or "").strip()
    identity = resolve_operator_identity(conn, token)
    operator_id = identity.operator_id if identity else token
    rows = conn.execute(
        """
        SELECT operator_id, callsign, effective_from, effective_to, provenance, note
          FROM operator_callsign_history
         WHERE operator_id=?
         ORDER BY effective_from DESC, alias_id DESC
        """,
        (operator_id,),
    ).fetchall()
    return tuple(
        CallsignHistoryEntry(
            operator_id=str(row[0]),
            callsign=str(row[1] or "").upper(),
            effective_from=float(row[2] or 0.0),
            effective_to=None if row[3] is None else float(row[3]),
            provenance=str(row[4] or ""),
            note=str(row[5] or ""),
        )
        for row in rows
    )


def callsigns_for_operator(conn: sqlite3.Connection, operator_id: str) -> tuple[str, ...]:
    return tuple(entry.callsign for entry in list_callsign_history(conn, operator_id))


def change_operator_callsign(
    conn: sqlite3.Connection,
    old_callsign: object,
    new_callsign: object,
    *,
    effective_at: object | None = None,
    note: str = "",
    provenance: str = "manual",
) -> OperatorIdentity:
    """Atomically move an operator to a new callsign and retain alias history."""
    old_call = validate_callsign(old_callsign)
    new_call = validate_callsign(new_callsign)
    if old_call == new_call:
        raise OperatorIdentityError("The new callsign is unchanged.")
    effective_epoch = _as_epoch(effective_at)
    ensure_operator_identity_schema(conn)
    conn.execute("SAVEPOINT change_operator_callsign")
    try:
        identity = resolve_operator_identity(conn, old_call)
        if identity is None or identity.current_callsign != old_call:
            raise OperatorIdentityError(f"{old_call} is not a current operator callsign.")
        conflicting_row = conn.execute(
            "SELECT operator_id FROM operator_callsign_history WHERE callsign=? COLLATE NOCASE LIMIT 1",
            (new_call,),
        ).fetchone()
        if conflicting_row and str(conflicting_row[0]) != identity.operator_id:
            raise CallsignConflictError(
                f"{new_call} already belongs to another operator identity. Review both records before linking."
            )
        current_alias = conn.execute(
            """
            SELECT alias_id, effective_from
              FROM operator_callsign_history
             WHERE operator_id=? AND callsign=? COLLATE NOCASE AND effective_to IS NULL
             LIMIT 1
            """,
            (identity.operator_id, old_call),
        ).fetchone()
        if current_alias is None:
            raise OperatorIdentityError(f"Current callsign history for {old_call} is missing.")
        if effective_epoch < float(current_alias[1] or 0.0):
            raise OperatorIdentityError("The effective time precedes the current callsign assignment.")
        if conn.execute(
            "SELECT 1 FROM operator_checkins WHERE callsign=? COLLATE NOCASE",
            (new_call,),
        ).fetchone():
            raise CallsignConflictError(f"{new_call} already exists in Operator History.")

        now_iso = _utc_now_iso()
        conn.execute(
            "UPDATE operator_callsign_history SET effective_to=? WHERE alias_id=?",
            (effective_epoch, int(current_alias[0])),
        )
        conn.execute(
            """
            INSERT INTO operator_callsign_history(
                operator_id, callsign, effective_from, effective_to,
                provenance, note, created_utc
            ) VALUES (?, ?, ?, NULL, ?, ?, ?)
            """,
            (identity.operator_id, new_call, effective_epoch, provenance, str(note or "").strip(), now_iso),
        )
        conn.execute(
            "UPDATE operator_identities SET current_callsign=?, updated_utc=? WHERE operator_id=?",
            (new_call, now_iso, identity.operator_id),
        )
        # The compatibility roster may retain multiple observed-call rows for
        # one identity (for example K1ABC and K1ABC/P).  Rename one primary row
        # rather than collapsing or overwriting those evidence-bearing rows.
        roster_rows = conn.execute(
            "SELECT callsign FROM operator_checkins WHERE operator_id=? "
            "ORDER BY CASE WHEN callsign=? COLLATE NOCASE THEN 0 ELSE 1 END, callsign COLLATE NOCASE",
            (identity.operator_id, old_call),
        ).fetchall()
        if roster_rows:
            primary_roster_call = str(roster_rows[0][0] or "").strip()
            conn.execute(
                "UPDATE operator_checkins SET callsign=? WHERE callsign=? COLLATE NOCASE",
                (new_call, primary_roster_call),
            )
        # Explicit/inferred peer schedule rows are operator-owned configuration,
        # not immutable received evidence, so they follow the stable identity.
        for table_name in ("peer_hf_schedule", "peer_hf_schedule_inferred"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
            ).fetchone():
                conn.execute(
                    f"UPDATE {table_name} SET owner_callsign=? WHERE UPPER(TRIM(owner_callsign))=?",
                    (new_call, old_call),
                )
        conn.execute(
            """
            INSERT INTO operator_identity_audit(
                operator_id, action, old_callsign, new_callsign,
                effective_at, changed_utc, detail
            ) VALUES (?, 'change_callsign', ?, ?, ?, ?, ?)
            """,
            (identity.operator_id, old_call, new_call, effective_epoch, now_iso, str(note or "").strip()),
        )
        conn.execute("RELEASE SAVEPOINT change_operator_callsign")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT change_operator_callsign")
        conn.execute("RELEASE SAVEPOINT change_operator_callsign")
        raise
    return OperatorIdentity(
        operator_id=identity.operator_id,
        current_callsign=new_call,
        matched_callsign=new_call,
        effective_from=effective_epoch,
        effective_to=None,
    )


def build_operator_alias_index(
    conn: sqlite3.Connection,
) -> dict[str, OperatorIdentity]:
    """Return a compact alias-keyed identity map for autocomplete."""
    if not _identity_tables_exist(conn):
        ensure_operator_identity_schema(conn, backfill_operator_rows=False)
    rows: Iterable[sqlite3.Row | tuple] = conn.execute(
        """
        SELECT h.callsign, h.operator_id, i.current_callsign,
               h.effective_from, h.effective_to
          FROM operator_callsign_history h
          JOIN operator_identities i ON i.operator_id=h.operator_id
         ORDER BY i.current_callsign COLLATE NOCASE, h.effective_from DESC
        """
    ).fetchall()
    return {
        str(row[0] or "").upper(): OperatorIdentity(
            operator_id=str(row[1]),
            current_callsign=str(row[2] or "").upper(),
            matched_callsign=str(row[0] or "").upper(),
            effective_from=float(row[3] or 0.0),
            effective_to=None if row[4] is None else float(row[4]),
        )
        for row in rows
    }
