"""Read-only sender-origin enrichment for traffic-summary entries.

The adapter deliberately keeps origin evidence separate from the report's
event geography.  It reads the existing projection/source stores and an
optional Call Finder cache; it never creates tables, refreshes references, or
mutates the traffic database.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from freqinout.core.call_finder import CallFinderStore
from freqinout.core.commstat_sitrep import STATE_NAME_TO_CODE, VALID_STATE_CODES
from freqinout.core.traffic_summary import SummaryCancelled


_PORTABLE_SUFFIX = re.compile(
    r"/(?:P|M|MM|QRP|SOTA|ROVER|MOBILE|PORTABLE|[0-9])$", re.IGNORECASE
)
_STATE = re.compile(r"^[A-Z]{2,3}$")
_GRID = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-X]{2})?$", re.IGNORECASE)
_SELF_SCOPE = frozenset({
    "self", "sender", "operator", "station", "origin", "local",
    "self_location", "sender_location", "operator_location", "station_location",
    "my_qth",
})
_ORIGIN_KEYS = {
    "origin_state", "origin_grid", "operator_state", "operator_grid",
    "self_state", "self_grid", "sender_state", "sender_grid",
    "station_state", "station_grid",
}


@dataclass(frozen=True)
class _Evidence:
    state: str = ""
    grid: str = ""
    state_ambiguous: bool = False
    grid_ambiguous: bool = False

    @property
    def present(self) -> bool:
        return bool(self.state or self.grid or self.state_ambiguous or self.grid_ambiguous)


def _field(entry: object, name: str, default: object = "") -> object:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled and cancelled():
        raise SummaryCancelled("Origin lookup cancelled.")


def _text(value: object, limit: int = 80) -> str:
    return str(value or "").strip()[:limit]


def _call(value: object) -> str:
    return _text(value, 40).upper()


def _canonical_call(value: object) -> str:
    return _PORTABLE_SUFFIX.sub("", _call(value))


def _state(value: object) -> str:
    value = _text(value, 16).upper()
    return value if _STATE.fullmatch(value) else ""


def _grid(value: object) -> str:
    value = re.sub(r"\s+", "", _text(value, 16)).upper()
    return value if _GRID.fullmatch(value) else ""


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _unknown() -> dict[str, str]:
    return {"origin_state": "", "origin_grid": "", "origin_source": "unknown"}


def _metadata_evidence(metadata: Mapping[str, Any], *, require_self: bool = False) -> _Evidence:
    """Read only explicit origin keys; ordinary event state/grid are ignored."""
    scope = _text(metadata.get("location_kind") or metadata.get("origin_scope") or metadata.get("scope"), 40)
    scope = scope.lower().replace("-", "_").replace(" ", "_")
    nested = metadata.get("origin")
    nested = nested if isinstance(nested, Mapping) else {}
    merged = {**nested, **metadata}
    if require_self and scope not in _SELF_SCOPE:
        return _Evidence()
    states = [_state(merged.get(key)) for key in _ORIGIN_KEYS if key.endswith("_state")]
    grids = [_grid(merged.get(key)) for key in _ORIGIN_KEYS if key.endswith("_grid")]
    states = [value for value in states if value]
    grids = [value for value in grids if value]
    return _Evidence(
        state=states[0] if len(set(states)) == 1 else "",
        grid=grids[0] if len(set(grids)) == 1 else "",
        state_ambiguous=len(set(states)) > 1,
        grid_ambiguous=len(set(grids)) > 1,
    )


_EXPLICIT_STATE_KEYS = (
    "state", "state_code", "origin_state", "operator_state", "self_state",
    "sender_state", "station_state",
)


def _explicit_payload_state(payload: Mapping[str, Any]) -> _Evidence:
    """Accept only authored state fields, never a parser-derived row value."""
    nested = payload.get("origin")
    nested = nested if isinstance(nested, Mapping) else {}
    values = [_state({**nested, **payload}.get(key)) for key in _EXPLICIT_STATE_KEYS]
    values = [value for value in values if value]
    unique = set(values)
    if len(unique) > 1:
        return _Evidence(state_ambiguous=True)
    return _Evidence(state=next(iter(unique), ""))


def _self_prefix_state(*texts: object) -> _Evidence:
    """Read a state only when a self-status body begins with that state."""
    for raw in texts:
        text = re.sub(r"^[\s\W]+", "", str(raw or "").upper()).strip()
        if not text:
            continue
        for name, code in sorted(STATE_NAME_TO_CODE.items(), key=lambda item: -len(item[0])):
            if re.match(rf"^{re.escape(name)}(?:\s+|\s*[-:;])", text):
                return _Evidence(state=code)
        match = re.match(r"^(?:NTR\s+)?([A-Z]{2})(?:\s+|\s*[-:;])", text)
        if match and match.group(1) in VALID_STATE_CODES:
            return _Evidence(state=match.group(1))
    return _Evidence()


def _merge_evidence(values: Iterable[_Evidence]) -> _Evidence:
    values = tuple(value for value in values if value.present)
    states = {value.state for value in values if value.state}
    grids = {value.grid for value in values if value.grid}
    state_ambiguous = any(value.state_ambiguous for value in values) or len(states) > 1
    grid_ambiguous = any(value.grid_ambiguous for value in values) or len(grids) > 1
    return _Evidence(
        state="" if state_ambiguous else next(iter(states), ""),
        grid="" if grid_ambiguous else next(iter(grids), ""),
        state_ambiguous=state_ambiguous,
        grid_ambiguous=grid_ambiguous,
    )


def _open_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _load_roster(conn: sqlite3.Connection, calls: set[str], cancelled: Callable[[], bool] | None) -> dict[str, _Evidence]:
    if not calls or not _table_exists(conn, "operator_checkins"):
        return {}
    columns = _table_columns(conn, "operator_checkins")
    if "callsign" not in columns:
        return {}
    state_col = "state" if "state" in columns else "NULL"
    grid_col = "grid" if "grid" in columns else "NULL"
    # ``operator_checkins`` is a compatibility table shared by roster import
    # and observed-message ingest.  Its state/grid columns are mutable and
    # observed ingest may replace an imported location.  Treat a row as a
    # roster location only when the roster importer left an explicit marker
    # and no later check-in evidence is recorded.  A plain callsign/state/grid
    # row is therefore never promoted to registered origin.
    roster_marker_cols = tuple(
        name for name in ("roster_parent_group", "roster_region", "tier")
        if name in columns
    )
    if not roster_marker_cols:
        return {}
    marker_expr = ", ".join(roster_marker_cols)
    checkin_col = "checkin_count" if "checkin_count" in columns else "NULL"
    net_col = "last_net" if "last_net" in columns else "NULL"
    role_col = "last_role" if "last_role" in columns else "NULL"
    first_seen_col = "first_seen_utc" if "first_seen_utc" in columns else "NULL"
    last_seen_col = "last_seen_utc" if "last_seen_utc" in columns else "NULL"
    variants: set[str] = set()
    for call in calls:
        canonical = _canonical_call(call)
        variants.add(call)
        variants.add(canonical)
        for suffix in ("/P", "/M", "/MM", "/QRP", "/SOTA", "/ROVER", "/MOBILE", "/PORTABLE"):
            variants.add(canonical + suffix)
        if canonical and canonical[-1:].isdigit():
            variants.add(canonical)
    rows: list[sqlite3.Row] = []
    variant_values = tuple(value for value in variants if value)
    for start in range(0, len(variant_values), 200):
        _check_cancelled(cancelled)
        batch = variant_values[start:start + 200]
        marks = ",".join("?" for _ in batch)
        rows.extend(conn.execute(
            f"SELECT callsign, {state_col} AS state, {grid_col} AS grid, "
            f"{marker_expr}, {checkin_col} AS checkin_count, {net_col} AS last_net, "
            f"{role_col} AS last_role, {first_seen_col} AS first_seen_utc, "
            f"{last_seen_col} AS last_seen_utc FROM operator_checkins "
            f"WHERE UPPER(callsign) IN ({marks})", batch,
        ).fetchall())
    by_exact: dict[str, list[_Evidence]] = {}
    by_canonical: dict[str, list[_Evidence]] = {}
    for row in rows:
        _check_cancelled(cancelled)
        marker_present = any(_text(row[name], 80) for name in roster_marker_cols)
        try:
            checkin_count = int(row["checkin_count"] or 0)
        except (TypeError, ValueError):
            checkin_count = 0
        first_seen = _text(row["first_seen_utc"], 80)
        last_seen = _text(row["last_seen_utc"], 80)
        observed_update = (
            checkin_count > 0
            or bool(_text(row["last_net"], 80))
            or bool(_text(row["last_role"], 80))
            or bool(first_seen and last_seen and first_seen != last_seen)
        )
        if not marker_present or observed_update:
            continue
        exact = _call(row["callsign"])
        canonical = _canonical_call(exact)
        evidence = _Evidence(_state(row["state"]), _grid(row["grid"]))
        if exact:
            by_exact.setdefault(exact, []).append(evidence)
        if canonical:
            by_canonical.setdefault(canonical, []).append(evidence)
    result: dict[str, _Evidence] = {}
    for call in calls:
        candidates = by_exact.get(call) or by_canonical.get(_canonical_call(call), ())
        if candidates:
            result[call] = _merge_evidence(candidates)
    return result


def _load_projection(conn: sqlite3.Connection, message_ids: set[str], cancelled: Callable[[], bool] | None) -> dict[str, tuple[str, _Evidence]]:
    if not message_ids or not _table_exists(conn, "message_projection"):
        return {}
    columns = _table_columns(conn, "message_projection")
    if "message_id" not in columns:
        return {}
    family_col = "source_family" if "source_family" in columns else "''"
    state_col = "state_code" if "state_code" in columns else "NULL"
    grid_col = "grid" if "grid" in columns else "NULL"
    ids = tuple(message_ids)
    result: dict[str, tuple[str, _Evidence]] = {}
    for start in range(0, len(ids), 400):
        _check_cancelled(cancelled)
        batch = ids[start:start + 400]
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT message_id, {family_col} AS source_family, {state_col} AS state, {grid_col} AS grid "
            f"FROM message_projection WHERE message_id IN ({marks})", batch,
        ).fetchall()
        for row in rows:
            result[str(row["message_id"])] = (
                _text(row["source_family"], 32).lower(),
                _Evidence(_state(row["state"]), _grid(row["grid"])),
            )
    return result


def _load_refs(conn: sqlite3.Connection, entries: list[object], cancelled: Callable[[], bool] | None) -> dict[str, list[sqlite3.Row]]:
    if not entries or not _table_exists(conn, "message_external_refs"):
        return {}
    columns = _table_columns(conn, "message_external_refs")
    required = {"message_id", "external_key"}
    if not required.issubset(columns):
        return {}
    ids = tuple(str(_field(entry, "message_id", "")) for entry in entries if _field(entry, "message_id", ""))
    if not ids:
        return {}
    selected: dict[str, set[str]] = {}
    for entry in entries:
        mid = str(_field(entry, "message_id", ""))
        raw_refs = _field(entry, "source_refs", ()) or ()
        if isinstance(raw_refs, str):
            raw_refs = (raw_refs,)
        selected[mid] = {_text(value, 240) for value in raw_refs if _text(value, 240)}
    result: dict[str, list[sqlite3.Row]] = {}
    for start in range(0, len(ids), 400):
        _check_cancelled(cancelled)
        batch = ids[start:start + 400]
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(f"SELECT * FROM message_external_refs WHERE message_id IN ({marks})", batch).fetchall()
        for row in rows:
            mid = str(row["message_id"])
            allowed = selected.get(mid, set())
            key = _text(row["external_key"], 240)
            receipt_match = any(
                token.startswith("receipt:") and token.endswith(":" + key)
                for token in allowed
            )
            if allowed and key not in allowed and not receipt_match:
                continue
            result.setdefault(mid, []).append(row)
    return result


def _load_spotter(conn: sqlite3.Connection, entries: list[object], refs: Mapping[str, list[sqlite3.Row]], projection: Mapping[str, tuple[str, _Evidence]], cancelled: Callable[[], bool] | None) -> dict[str, _Evidence]:
    result: dict[str, _Evidence] = {}
    for entry in entries:
        _check_cancelled(cancelled)
        mid = str(_field(entry, "message_id", ""))
        family = _text(_field(entry, "source_family", ""), 32).lower()
        if family != "spotter":
            continue
        candidates: list[_Evidence] = []
        projected = projection.get(mid)
        if projected and projected[0] == "spotter" and projected[1].present:
            candidates.append(projected[1])
        for ref in refs.get(mid, ()):
            metadata = _json_object(ref["metadata_json"] if "metadata_json" in ref.keys() else "")
            candidates.append(_metadata_evidence(metadata))
        if candidates:
            evidence = _merge_evidence(candidates)
            if evidence.present:
                result[mid] = evidence
    return result


def _commstat_self_scope(row: sqlite3.Row) -> bool:
    value = _text(row["scope"] if "scope" in row.keys() else "", 40)
    value = value.lower().replace("-", "_").replace(" ", "_")
    return value in _SELF_SCOPE


def _load_commstat(conn: sqlite3.Connection, entries: list[object], refs: Mapping[str, list[sqlite3.Row]], cancelled: Callable[[], bool] | None) -> dict[str, _Evidence]:
    if not _table_exists(conn, "commstat_artifacts"):
        return {}
    columns = _table_columns(conn, "commstat_artifacts")
    if "from_call" not in columns:
        return {}
    state_col = "state_code" if "state_code" in columns else "NULL"
    grid_col = "grid" if "grid" in columns else "NULL"
    scope_col = "scope" if "scope" in columns else "''"
    key_col = "artifact_key" if "artifact_key" in columns else "NULL"
    id_col = "id" if "id" in columns else "rowid"
    payload_col = "payload_json" if "payload_json" in columns else "NULL"
    title_col = "title" if "title" in columns else "NULL"
    body_col = "body_text" if "body_text" in columns else "NULL"
    remarks_col = "remarks_text" if "remarks_text" in columns else "NULL"
    event_col = "event_ts" if "event_ts" in columns else "NULL"
    wanted_keys: set[str] = set()
    wanted_ids: set[str] = set()
    requested_calls: set[str] = set()
    event_values: list[float] = []
    for entry in entries:
        if _text(_field(entry, "source_family", ""), 32).lower() != "commstat":
            continue
        requested_calls.add(_call(_field(entry, "from_call", "")))
        try:
            event_value = float(_field(entry, "received_ts", 0.0) or 0.0)
            if event_value <= 0:
                event_value = float(_field(entry, "event_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            event_value = 0.0
        if event_value > 0:
            event_values.append(event_value)
        for ref in refs.get(str(_field(entry, "message_id", "")), ()):
            key = _text(ref["external_key"] if "external_key" in ref.keys() else "", 240)
            if key:
                wanted_keys.add(key)
            metadata = _json_object(ref["metadata_json"] if "metadata_json" in ref.keys() else "")
            row_id = _text(metadata.get("row_id"), 80)
            if row_id and _text(metadata.get("source_table"), 80).lower() == "commstat_artifacts":
                wanted_ids.add(row_id)
    requested_calls.discard("")
    rows: list[sqlite3.Row] = []

    def fetch(
        where: str,
        params: tuple[object, ...],
        limit: int,
        *,
        discard_on_overflow: bool = False,
    ) -> None:
        if limit <= 0:
            return
        _check_cancelled(cancelled)
        fetched = conn.execute(
            f"SELECT {id_col} AS id, {key_col} AS artifact_key, from_call, {state_col} AS state_code, "
            f"{grid_col} AS grid, {scope_col} AS scope, {payload_col} AS payload_json, "
            f"{title_col} AS title, {body_col} AS body_text, {remarks_col} AS remarks_text, "
            f"{event_col} AS event_ts "
            f"FROM commstat_artifacts WHERE {where} LIMIT ?", (*params, limit + (1 if discard_on_overflow else 0)),
        ).fetchall()
        if discard_on_overflow and len(fetched) > limit:
            return
        rows.extend(fetched)

    if wanted_keys or wanted_ids:
        if wanted_keys and "artifact_key" in columns:
            values = tuple(wanted_keys)
            for start in range(0, len(values), 200):
                batch = values[start:start + 200]
                fetch("artifact_key IN (" + ",".join("?" for _ in batch) + ")", batch, len(batch))
        if wanted_ids and "id" in columns:
            values = tuple(wanted_ids)
            for start in range(0, len(values), 200):
                batch = values[start:start + 200]
                fetch("CAST(id AS TEXT) IN (" + ",".join("?" for _ in batch) + ")", batch, len(batch))

    # Entries without a usable artifact reference still get bounded same-period
    # evidence. The event range comes from the selected entries, never the
    # whole artifact table.
    if requested_calls and event_values:
        low, high = min(event_values), max(event_values)
        calls = tuple(sorted(requested_calls))
        for start in range(0, len(calls), 100):
            batch = calls[start:start + 100]
            marks = ",".join("?" for _ in batch)
            fetch(
                f"UPPER(from_call) IN ({marks}) AND {event_col}>=? AND {event_col}<=?",
                (*batch, low, high),
                2000,
                discard_on_overflow=True,
            )
    by_key: dict[str, list[sqlite3.Row]] = {}
    by_call: dict[str, list[sqlite3.Row]] = {}
    by_id: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        _check_cancelled(cancelled)
        key = _text(row["artifact_key"] or row["id"], 240)
        if key:
            by_key.setdefault(key, []).append(row)
        row_id = _text(row["id"], 80)
        if row_id:
            by_id.setdefault(row_id, []).append(row)
        call = _call(row["from_call"])
        if call:
            by_call.setdefault(call, []).append(row)
    result: dict[str, _Evidence] = {}
    for entry in entries:
        _check_cancelled(cancelled)
        mid = str(_field(entry, "message_id", ""))
        if _text(_field(entry, "source_family", ""), 32).lower() != "commstat":
            continue
        rows_for_entry: list[sqlite3.Row] = []
        for ref in refs.get(mid, ()):
            key = _text(ref["external_key"] if "external_key" in ref.keys() else "", 240)
            rows_for_entry.extend(by_key.get(key, ()))
            metadata = _json_object(ref["metadata_json"] if "metadata_json" in ref.keys() else "")
            if _text(metadata.get("source_table"), 80).lower() == "commstat_artifacts":
                rows_for_entry.extend(by_id.get(_text(metadata.get("row_id"), 80), ()))
        if not rows_for_entry:
            event_ts = _field(entry, "received_ts", 0.0)
            if not event_ts:
                event_ts = _field(entry, "event_ts", 0.0)
            try:
                event_ts = float(event_ts or 0.0)
            except (TypeError, ValueError):
                event_ts = 0.0
            rows_for_entry = [row for row in by_call.get(_call(_field(entry, "from_call", "")), ())
                              if event_ts > 0 and row["event_ts"] is not None
                              and abs(float(row["event_ts"]) - event_ts) < 0.001]
        candidates: list[_Evidence] = []
        for row in rows_for_entry:
            if not _commstat_self_scope(row):
                continue
            payload = _json_object(row["payload_json"])
            # state_code is often inferred from free-form comments while the
            # grid is an authored CommStat field.  Keep the grid, but accept
            # the state only from explicit payload state or a clear self-body
            # prefix such as ``NM-`` or ``IN quiet``.
            explicit_state = _explicit_payload_state(payload)
            prefixed_state = _self_prefix_state(
                row["title"], row["body_text"], row["remarks_text"],
                payload.get("comments"), payload.get("remarks"), payload.get("message"),
            )
            state_evidence = _merge_evidence((explicit_state, prefixed_state))
            candidates.append(_Evidence(state=state_evidence.state,
                                        state_ambiguous=state_evidence.state_ambiguous,
                                        grid=_grid(row["grid"])))
            candidates.append(_metadata_evidence(payload, require_self=False))
        for ref in refs.get(mid, ()):
            metadata = _json_object(ref["metadata_json"] if "metadata_json" in ref.keys() else "")
            candidates.append(_metadata_evidence(metadata, require_self=True))
        evidence = _merge_evidence(candidates)
        if evidence.present:
            result[mid] = evidence
    return result


def _load_call_finder(calls: tuple[str, ...], reference_root: str | Path | None, cancelled: Callable[[], bool] | None) -> dict[str, _Evidence]:
    if not calls or reference_root is None:
        return {}
    _check_cancelled(cancelled)
    try:
        store = CallFinderStore(Path(reference_root).expanduser())
        result: dict[str, _Evidence] = {}
        for start in range(0, len(calls), 128):
            _check_cancelled(cancelled)
            batch = calls[start:start + 128]
            lookup = store.exact_many(batch)
            by_call: dict[str, list[_Evidence]] = {call: [] for call in batch}
            for record in lookup.rows:
                call = _call(record.callsign)
                if call not in by_call:
                    continue
                by_call[call].append(_Evidence(_state(record.state), _grid(record.estimated_grid6)))
            for call, values in by_call.items():
                evidence = _merge_evidence(values)
                if evidence.present:
                    result[call] = evidence
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        return {}


def _choose(candidates: Mapping[str, _Evidence]) -> dict[str, str]:
    selected_state = selected_grid = ""
    state_blocked = grid_blocked = False
    state_source = grid_source = ""
    for name in ("roster", "spotter", "commstat", "call_finder"):
        evidence = candidates.get(name, _Evidence())
        if not evidence.present:
            continue
        if not selected_state and not state_blocked:
            if evidence.state_ambiguous:
                state_blocked = True
                state_source = name
            elif evidence.state:
                selected_state = evidence.state
                state_source = name
        if not selected_grid and not grid_blocked:
            if evidence.grid_ambiguous:
                grid_blocked = True
                grid_source = name
            elif evidence.grid:
                selected_grid = evidence.grid
                grid_source = name
    sources = []
    for source_name in (state_source, grid_source):
        if source_name and source_name not in sources:
            sources.append(source_name)
    return {
        "origin_state": selected_state,
        "origin_grid": selected_grid,
        "origin_source": "+".join(sources) if sources else "unknown",
    }


def resolve_origins(
    db_path: str | Path,
    entries: Iterable[object],
    *,
    reference_root: str | Path | None = None,
    cancelled: Callable[[], bool] | None = None,
    connection: sqlite3.Connection | None = None,
) -> dict[str, dict[str, str]]:
    """Resolve sender-origin evidence without changing any source database.

    The result contains one record per supplied ``message_id``.  Evidence is
    considered in the documented order: roster, explicit Spotter, CommStat
    self-location, then an exact Call Finder cache match.  Empty state/grid
    values are unknown; no callsign prefix or report event geography is used.
    When ``connection`` is supplied it is borrowed for the duration of the
    read, preserving a caller-owned transaction snapshot; it is never closed
    or reconfigured by this function.
    """
    materialized = list(entries)
    result = {str(_field(entry, "message_id", "")): _unknown() for entry in materialized if str(_field(entry, "message_id", ""))}
    if not result:
        return {}
    _check_cancelled(cancelled)
    calls = {_call(_field(entry, "from_call", "")) for entry in materialized if _call(_field(entry, "from_call", ""))}
    owns_connection = connection is None
    if owns_connection:
        try:
            connection = _open_readonly(db_path)
        except (OSError, sqlite3.Error):
            return result
    conn = connection
    try:
        roster = _load_roster(conn, calls, cancelled)
        refs = _load_refs(conn, materialized, cancelled)
        projection = _load_projection(conn, set(result), cancelled)
        spotter = _load_spotter(conn, materialized, refs, projection, cancelled)
        commstat = _load_commstat(conn, materialized, refs, cancelled)
    finally:
        if owns_connection:
            conn.close()

    # A sender's explicit self-location in any selected-period entry is useful
    # for that sender's other entries too. Aggregate only the supplied entries,
    # so evidence cannot leak in from outside the frozen report scope.
    spotter_by_call: dict[str, list[_Evidence]] = {}
    commstat_by_call: dict[str, list[_Evidence]] = {}
    for entry in materialized:
        call = _call(_field(entry, "from_call", ""))
        mid = str(_field(entry, "message_id", ""))
        if not call:
            continue
        if spotter.get(mid, _Evidence()).present:
            spotter_by_call.setdefault(call, []).append(spotter[mid])
        if commstat.get(mid, _Evidence()).present:
            commstat_by_call.setdefault(call, []).append(commstat[mid])
    spotter_by_call_merged = {call: _merge_evidence(values) for call, values in spotter_by_call.items()}
    commstat_by_call_merged = {call: _merge_evidence(values) for call, values in commstat_by_call.items()}

    # Do not query the optional FCC cache for fields already resolved by any
    # higher-priority evidence in the selected period.  An ambiguity also
    # blocks fallback so a cache row cannot silently adjudicate a conflict.
    finder_calls = set()
    for call in calls:
        higher = _merge_evidence(
            value for value in (
                roster.get(call, _Evidence()),
                spotter_by_call_merged.get(call, _Evidence()),
                commstat_by_call_merged.get(call, _Evidence()),
            ) if value.present
        )
        if ((not higher.state and not higher.state_ambiguous)
                or (not higher.grid and not higher.grid_ambiguous)):
            finder_calls.add(call)
    finder = _load_call_finder(tuple(sorted(finder_calls)), reference_root, cancelled)
    for entry in materialized:
        _check_cancelled(cancelled)
        mid = str(_field(entry, "message_id", ""))
        if not mid:
            continue
        call = _call(_field(entry, "from_call", ""))
        spotter_evidence = _merge_evidence(
            value for value in (spotter.get(mid, _Evidence()), spotter_by_call_merged.get(call, _Evidence()))
            if value.present
        )
        commstat_evidence = _merge_evidence(
            value for value in (commstat.get(mid, _Evidence()), commstat_by_call_merged.get(call, _Evidence()))
            if value.present
        )
        result[mid] = _choose({
            "roster": roster.get(call, _Evidence()),
            "spotter": spotter_evidence,
            "commstat": commstat_evidence,
            "call_finder": finder.get(call, _Evidence()),
        })
    return result
