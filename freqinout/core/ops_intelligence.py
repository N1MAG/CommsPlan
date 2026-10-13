"""Bounded, read-only evidence briefs. Call only from a background read lane."""
from __future__ import annotations

from dataclasses import dataclass, replace
import datetime as dt
import json
import sqlite3
import threading
import time
from pathlib import Path

from freqinout.core.group_utils import normalize_group_name
from freqinout.core.message_summary import normalize_message_source_family
from freqinout.core.sop_awareness import SopAttentionSnapshot, SopAttentionItem
from freqinout.core.operator_identity import canonical_callsign, resolve_operator_identity
from freqinout.core.sqlite_utils import connect_sqlite_readonly


@dataclass(frozen=True)
class SearchRequest:
    kind: str
    query: str
    group: str = ""
    source: str = ""
    since: float = 0.0
    scope_label: str = "All groups · All sources · retained traffic"


@dataclass(frozen=True)
class EvidenceItem:
    message_id: str
    source_family: str
    source_ref: str
    callsign: str
    group_name: str
    received_utc: str
    summary: str
    status: str


@dataclass(frozen=True)
class SearchBrief:
    kind: str
    query: str
    scope_label: str
    evidence: tuple[EvidenceItem, ...] = ()
    limited: bool = False
    coverage: tuple[str, ...] = ()
    summaries: tuple[str, ...] = ()
    sop_matches: tuple[SopAttentionItem, ...] = ()
    sop_state: str = "pending"


def read_search_brief(path: Path, request: SearchRequest, cancel: threading.Event | None = None, sop: SopAttentionSnapshot | None = None) -> SearchBrief:
    """Exact indexed entity lookup; never initializes schema or reads message files."""
    if request.kind not in {"callsign", "topic", "event"} or not request.query.strip():
        raise ValueError("Choose callsign, topic, or SOP event and enter a value.")
    request = replace(request, group=normalize_group_name(request.group), source=request.source.strip().lower())
    families = _source_families(request.source)
    query = request.query.strip()[:120]
    canonical = canonical_callsign(query) if request.kind == "callsign" else query.lower()
    notes = ["Received reports are evidence, not independently verified facts. Contradictory reports remain visible. Source filters use the canonical primary source."]
    notes.append("Observation references without a canonical link may describe the same report; receipt counts are not independent confirmations. Link resolution covers up to 64 sources per family.")
    deadline = time.monotonic() + 2.0
    conn = connect_sqlite_readonly(path, row_factory=sqlite3.Row, timeout=.25)
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline or bool(cancel and cancel.is_set())), 1000)
    items: dict[str, EvidenceItem] = {}
    limited = False
    source_cache = {}
    try:
        if cancel and cancel.is_set():
            raise RuntimeError("Search cancelled")
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        keys = [canonical]
        if request.kind == "callsign" and {"operator_callsign_history", "operator_identities"} <= tables:
            # Multiple historical owners must not be merged into one operator.
            owners = conn.execute("SELECT DISTINCT operator_id FROM operator_callsign_history WHERE callsign=? COLLATE NOCASE LIMIT 2", (canonical,)).fetchall()
            if len(owners) > 1:
                return SearchBrief(request.kind, query, request.scope_label, coverage=("This callsign has multiple recorded owners. Select an operator in Operators before reviewing identity history.",))
            identity = resolve_operator_identity(conn, canonical, schema_ready=True)
            if identity:
                keys.append(identity.operator_id)
                notes.append(f"Recorded identity: {identity.current_callsign}; matching alias: {canonical}.")
        if {"message_projection", "ops_focus_message_entities"} <= tables:
            placeholders = ",".join("?" for _ in keys)
            clauses = ["e.kind=?", f"e.canonical_id IN ({placeholders})", "e.deleted=0", "e.archived=0", "p.deleted=0", "p.archived=0", "e.received_ts>=?"]
            params: list[object] = [request.kind, *keys, request.since]
            if request.group:
                clauses.extend(("e.group_name=?", "p.group_name=?"))
                params.extend((request.group, request.group))
            if request.source:
                clauses.append("e.source_family COLLATE NOCASE IN (" + ",".join("?" for _ in families) + ")")
                params.extend(families)
                clauses.append("p.source_family COLLATE NOCASE IN (" + ",".join("?" for _ in families) + ")")
                params.extend(families)
            clauses.append("p.received_ts>=?")
            params.append(request.since)
            rows = conn.execute("SELECT DISTINCT p.message_id,p.source_family,p.primary_source_id,p.from_call,p.group_name,p.received_utc,p.summary,p.subject,p.status FROM ops_focus_message_entities e JOIN message_projection p ON p.message_id=e.message_id WHERE " + " AND ".join(clauses) + " ORDER BY e.received_ts DESC LIMIT 201", params).fetchall()
            limited = len(rows) > 200
            for row in rows[:200]:
                item = EvidenceItem(str(row[0]), str(row[1] or ""), _message_receipt(conn, str(row[0]), str(row[2] or ""), tables), str(row[3] or ""), str(row[4] or ""), str(row[5] or ""), str(row[6] or row[7] or "No retained summary")[:1200], str(row[8] or "Unverified report"))
                items[item.message_id] = item
            if "ops_focus_backfill_state" in tables:
                states = conn.execute("SELECT complete FROM ops_focus_backfill_state LIMIT 8").fetchall()
                if not states or any(not row[0] for row in states):
                    notes.append("Historical entity indexing is incomplete; results may omit retained traffic.")
        else:
            notes.append("Message entity index is not available yet. Search does not build it.")
        # Observation topics use indexed exact labels. Common canonical variants
        # include ingest's title-case labels; arbitrary case variants are not scanned.
        if "observation_projection" in tables and request.kind != "event":
            params = []
            if request.kind == "callsign":
                match = "o.from_call=?"
                params.append(canonical)
                notes.append("Observation callsigns match the entered callsign only; historical aliases are not inferred.")
                join = ""
            elif "observation_projection_topics" in tables:
                variants = tuple(dict.fromkeys((query, canonical, query.title(), query.upper())))
                match = "t.topic IN (" + ",".join("?" for _ in variants) + ")"
                params.extend(variants)
                join = " JOIN observation_projection_topics t ON t.observation_id=o.observation_id"
            else:
                match = "0"
                join = ""
                notes.append("Observation topic index is unavailable.")
            clauses = [match, "(?=0 OR julianday(o.received_utc)>=julianday(?,'unixepoch'))"]
            params.extend((request.since, request.since))
            if request.source:
                clauses.append("o.source_family COLLATE NOCASE IN (" + ",".join("?" for _ in families) + ")")
                params.extend(families)
            rows = conn.execute("SELECT DISTINCT o.observation_id,o.source_family,o.source_ref,o.from_call,o.groups_json,o.received_utc,o.summary,o.subject,o.status FROM observation_projection o" + join + " WHERE " + " AND ".join(clauses) + " ORDER BY julianday(o.received_utc) DESC LIMIT 201", params).fetchall()
            limited = limited or len(rows) > 200
            if request.group and len(rows) > 200:
                notes.append("Observation group filtering covers the latest 200 matching candidates; older scoped reports may be omitted.")
            for row in rows[:200]:
                ref = str(row[2] or "")
                # Message-derived observations are not a second independent report,
                # including when canonical retention/deletion policy hid the message.
                # A derived receipt cannot resurrect an archived or deleted report.
                if _linked_message(conn, ref, str(row[1] or ""), tables, source_cache):
                    continue
                try:
                    groups = json.loads(row[4] or "[]")
                    if not isinstance(groups, list):
                        raise ValueError("Expected a group list")
                except (ValueError, TypeError):
                    groups = []
                    if "Some observation group labels could not be read." not in notes:
                        notes.append("Some observation group labels could not be read.")
                if request.group and request.group not in {normalize_group_name(g) for g in groups}:
                    continue
                key = "observation:" + str(row[0])
                items[key] = EvidenceItem(key, str(row[1] or ""), ref, str(row[3] or ""), (request.group or (str(groups[0]) if groups else "")), str(row[5] or ""), str(row[6] or row[7] or "No retained summary")[:1200], str(row[8] or "Unverified report"))
        elif request.kind == "event":
            notes.append("Event search covers indexed message event labels. Observation event history and incident confirmation are not available in this slice.")
        if cancel and cancel.is_set():
            raise RuntimeError("Search cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Search exceeded its time budget")
        evidence = tuple(sorted(items.values(), key=lambda item: _received_epoch(item.received_utc), reverse=True)[:200])
        limited = limited or len(items) > 200
        summaries = tuple(f"{item.callsign or item.source_family} · {item.received_utc} · {item.summary[:280]}" for item in evidence[:8])
        notes.append("An empty result means no matching indexed evidence in this scope; it does not establish absence of an event.")
        if any(item.source_ref.startswith("source:") for item in evidence):
            notes.append("Some messages lack receipt identifiers; source: entries identify their source only.")
        matches = tuple(item for item in (sop.items if sop else ())
            if (not request.group or item.group_name.upper() == request.group.upper())
            and canonical.casefold() in (item.label + " " + item.description).casefold())[:8]
        if sop and sop.state != "fresh":
            notes.append(f"SOP instruction coverage is {sop.state}; matches use the last published reminder details.")
        return SearchBrief(request.kind, query, request.scope_label, evidence, limited, tuple(notes), summaries,
            matches, sop.state if sop else "pending")
    finally:
        conn.close()


def _source_families(source: str) -> tuple[str, ...]:
    normalized = normalize_message_source_family(source)
    if source == "nbems":
        return ("nbems", "flmsg", "flamp", "fastlight")
    return {
        "js8": ("js8", "js8call"), "spotter": ("spotter", "fiospotter", "js8spotter", "sitrep"),
        "commstat": ("commstat", "commstat_rf"), "mesh": ("mesh", "meshcore", "meshtastic", "local_mesh"),
        "bbs": ("bbs", "varac_bbs", "bbs_archive"),
    }.get(normalized, (normalized,))


def _message_receipt(conn, message_id: str, source_id: str, tables: set[str]) -> str:
    if "message_external_refs" in tables:
        row = conn.execute("SELECT external_kind,external_key FROM message_external_refs WHERE message_id=? AND source_id=? ORDER BY id LIMIT 1", (message_id, source_id)).fetchone()
        if row:
            prefix = {"spotter_message": "spotter_traffic", "commstat_artifact": "commstat_artifacts"}.get(row[0], row[0])
            return f"{prefix}:{row[1]}"
    return f"source:{source_id}" if source_id else f"canonical:{message_id}"


def _linked_message(conn, ref: str, family: str, tables: set[str], source_cache: dict) -> bool:
    if "message_projection" not in tables or not ref:
        return False
    if conn.execute("SELECT 1 FROM message_projection WHERE message_id=? LIMIT 1", (ref,)).fetchone():
        return True
    if not {"message_external_refs", "message_sources"} <= tables:
        return False
    prefix, _, key = ref.partition(":")
    kind = {"spotter_traffic": "spotter_message", "commstat_artifacts": "commstat_artifact", "js8_message": "js8_message", "varac_message": "varac_message"}.get(prefix)
    if prefix == "file" and key:
        for file_kind in tuple(f"{candidate}_file" for candidate in _source_families(family)):
            if conn.execute("SELECT 1 FROM message_external_refs r JOIN message_projection p ON p.message_id=r.message_id WHERE r.external_kind=? AND r.external_path=? LIMIT 1", (file_kind, key)).fetchone():
                return True
        return False
    if not kind or not key:
        return False
    families = _source_families(family)
    if families not in source_cache:
        source_cache[families] = tuple(row[0] for row in conn.execute("SELECT source_id FROM message_sources WHERE source_family COLLATE NOCASE IN (" + ",".join("?" for _ in families) + ") LIMIT 64", families))
    sources = source_cache[families]
    if not sources:
        return False
    return bool(conn.execute("SELECT 1 FROM message_external_refs r JOIN message_projection p ON p.message_id=r.message_id WHERE r.source_id IN (" + ",".join("?" for _ in sources) + ") AND r.external_kind=? AND r.external_key=? LIMIT 1", (*sources, kind, key)).fetchone())



def _received_epoch(value: str) -> float:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return 0.0


def save_guidance_preference(path: Path, enabled: bool) -> bool:
    """Worker-only, one existing setting key; no schema or global settings reload."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True, timeout=.25)
    try:
        conn.execute("PRAGMA busy_timeout=250")
        with conn:
            conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", ("controlfreq_sop_guidance", json.dumps(bool(enabled))))
        return True
    finally:
        conn.close()
