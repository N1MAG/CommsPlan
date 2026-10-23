"""Offline, read-only traffic digest. Stored receipt times are not first receipt proof."""
from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Callable


REPORT_MARKER = "COMMSPLAN-TRAFFIC-SUMMARY:v1:"
COMPUTED_START = "[COMPUTED COUNTS]"
COMPUTED_END = "[/COMPUTED COUNTS]"


class SummaryCancelled(RuntimeError):
    pass


class SummaryLimitError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class SummaryScope:
    start_ts: float
    end_ts: float
    groups: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    radio_ids: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    max_records: int = 50000

    def __post_init__(self):
        object.__setattr__(self, "start_ts", float(self.start_ts))
        object.__setattr__(self, "end_ts", float(self.end_ts))
        if not all(math.isfinite(v) for v in (self.start_ts, self.end_ts)) or self.end_ts <= self.start_ts:
            raise ValueError("Choose an end time after the start time.")
        if not 1 <= self.max_records <= 100000:
            raise ValueError("Report population limit must be between 1 and 100000.")
        for key in ("groups", "sources", "radio_ids", "states"):
            values = getattr(self, key)
            normalized = tuple(sorted({str(v).strip().lstrip("@").upper() if key in {"groups", "states"}
                                       else str(v).strip().lower() if key == "sources"
                                       else str(v).strip() for v in values if str(v).strip()}))
            object.__setattr__(self, key, normalized)


@dataclasses.dataclass(frozen=True)
class ReportEntry:
    message_id: str
    source_family: str
    from_call: str
    group_name: str
    reported_state: str
    grid: str
    received_ts: float
    event_ts: float
    severity: str
    excerpt: str
    topics: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    provenance: str = "unknown"
    direction: str = "unknown"
    sector_status: tuple[tuple[str, str], ...] = ()
    body_text: str = ""
    origin_state: str = ""
    origin_grid: str = ""
    origin_source: str = ""
    event_places: tuple[str, ...] = ()
    report_scope: str = ""
    body_complete: bool = False


@dataclasses.dataclass(frozen=True)
class Exclusion:
    message_id: str
    reason: str


@dataclasses.dataclass(frozen=True)
class SummarySnapshot:
    report_id: str
    generated_utc: str
    scope: SummaryScope
    entries: tuple[ReportEntry, ...]
    exclusions: tuple[Exclusion, ...]
    raw_count: int
    content_hash: str = ""
    source_coverage: tuple[str, ...] = ()
    schema_version: int = 1
    station_callsign: str = ""


def _json(value, default):
    try:
        result = json.loads(value or "")
        return result if isinstance(result, type(default)) else default
    except (ValueError, TypeError):
        return default


def _clean(value: object) -> str:
    # Received message text is inert data, never markup or executable instructions.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))


def _flat(value: object, limit: int = 420) -> str:
    return re.sub(r"\s+", " ", _clean(value)).strip()[:limit]


def _cancel(check):
    if check and check():
        raise SummaryCancelled("Report generation cancelled.")


def _direction(value: object) -> str:
    value = str(value or "").strip().lower()
    if value in {"in", "incoming", "rx", "received", "receive"}:
        return "incoming"
    if value in {"out", "outgoing", "tx", "sent", "draft", "send"}:
        return "outgoing"
    return "unknown"


def _open(path):
    # No schema assurance, journal mutation, source scanning or network access.
    conn = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def summary_filter_options(db_path: str | Path, *, cancelled=None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read a bounded station-wide choice catalog; never mutate source data."""
    _cancel(cancelled)
    conn = _open(db_path)
    conn.set_progress_handler(lambda: int(bool(cancelled and cancelled())), 2000)
    try:
        conn.execute("BEGIN")
        groups = tuple(str(row[0]) for row in conn.execute(
            "SELECT DISTINCT UPPER(LTRIM(TRIM(group_name),'@')) AS value FROM message_projection "
            "WHERE deleted=0 AND inbox_visible=1 AND TRIM(COALESCE(group_name,''))<>'' ORDER BY value LIMIT 1001"))
        sources = tuple(str(row[0]) for row in conn.execute(
            "SELECT DISTINCT LOWER(TRIM(s.source_family)) AS value FROM message_sources s "
            "WHERE TRIM(COALESCE(s.source_family,''))<>'' AND EXISTS "
            "(SELECT 1 FROM message_external_refs r JOIN message_projection p ON p.message_id=r.message_id "
            "WHERE r.source_id=s.source_id AND p.deleted=0 AND p.inbox_visible=1) "
            "ORDER BY value LIMIT 1001"))
        _cancel(cancelled)
        if len(groups) > 1000 or len(sources) > 1000:
            raise SummaryLimitError("Too many filter choices to display. All-traffic generation remains available.")
        return groups, sources
    finally:
        conn.close()


def latest_summary_timestamp(db_path: str | Path) -> float | None:
    with _open(db_path) as conn:
        try:
            value = conn.execute("SELECT MAX(received_ts) FROM message_projection WHERE deleted=0 AND inbox_visible=1").fetchone()[0]
            return float(value) if value and float(value) > 0 else None
        finally:
            conn.close()


def resolve_latest_scope(db_path: str | Path, scope: SummaryScope) -> SummaryScope:
    latest = latest_summary_timestamp(db_path)
    if latest is None:
        raise ValueError("No stored receipt timestamps are available. Choose an explicit UTC period.")
    end = latest + 1
    return dataclasses.replace(scope, start_ts=end - (scope.end_ts - scope.start_ts), end_ts=end)


def _sql_scope(scope):
    where = ["p.received_ts>0", "p.received_ts>=?", "p.received_ts<?"]
    args: list[object] = [scope.start_ts, scope.end_ts]
    for column, values in (("UPPER(LTRIM(TRIM(p.group_name),'@'))", scope.groups),
                           ("UPPER(p.state_code)", scope.states)):
        if values:
            where.append(column + " IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
    if scope.sources or scope.radio_ids:
        parts = []
        # Qualify both requested source and requested radio on the SAME receipt.
        for column, values in (("LOWER(TRIM(s.source_family))", scope.sources), ("CAST(s.radio_id AS TEXT)", scope.radio_ids)):
            if values:
                parts.append(column + " IN (" + ",".join("?" for _ in values) + ")")
                args.extend(values)
        where.append("EXISTS (SELECT 1 FROM message_external_refs r JOIN message_sources s ON s.source_id=r.source_id "
                     "WHERE r.message_id=p.message_id AND " + " AND ".join(parts) + ")")
    return " AND ".join(where), args


def _is_test(text: str) -> bool:
    # Conservative explicit synthetic declarations only; equipment testing is real traffic.
    return bool(re.search(r"^(?:TEST(?:\s+DEMO)?(?:\s|[|:.-]|$)|(?:[A-Z]{2}\s+)?test\s+(?:demo|internet)|ignore\s+this\s+is\s+a\s+test\b)", text.strip(), re.I))


def generate_summary(db_path: str | Path, scope: SummaryScope, *,
                     cancelled: Callable[[], bool] | None = None,
                     station_callsign: str | None = None,
                     reference_root: str | Path | None = None) -> SummarySnapshot:
    _cancel(cancelled)
    if station_callsign and not re.fullmatch(r"(?=[A-Z0-9/]*[A-Z])(?=[A-Z0-9/]*[0-9])[A-Z0-9/]{3,32}", station_callsign.strip().upper()):
        raise ValueError("Enter a valid station callsign or leave it blank for a draft.")
    conn = _open(db_path)
    conn.set_progress_handler(lambda: int(bool(cancelled and cancelled())), 2000)
    try:
        conn.execute("BEGIN")
        where, args = _sql_scope(scope)
        cursor = conn.execute("SELECT p.* FROM message_projection p WHERE " + where +
                              " ORDER BY p.received_ts,p.message_id LIMIT ?", (*args, scope.max_records + 1))
        rows = []
        while True:
            _cancel(cancelled)
            chunk = cursor.fetchmany(256)
            if not chunk:
                break
            rows.extend(dict(row) for row in chunk)
            if len(rows) > scope.max_records:
                raise SummaryLimitError("Too much traffic for one report. Narrow the period or filters; no partial report was generated.")
        refs: dict[str, set[str]] = collections.defaultdict(set)
        paths: dict[str, list[dict]] = collections.defaultdict(list)
        receipt_directions: dict[str, list[str]] = collections.defaultdict(list)
        coverage: set[str] = set()
        for offset in range(0, len(rows), 400):
            _cancel(cancelled)
            ids = [row["message_id"] for row in rows[offset:offset + 400]]
            marks = ",".join("?" for _ in ids)
            query = ("SELECT r.*,s.source_label,s.source_family,s.provenance_json,s.last_ingested_utc "
                     "FROM message_external_refs r LEFT JOIN message_sources s ON s.source_id=r.source_id "
                     "WHERE r.message_id IN (" + marks + ")")
            for row in conn.execute(query, ids):
                _cancel(cancelled)
                meta = _json(row["metadata_json"], {})
                mid = row["message_id"]
                values = meta.get("source_refs", [])
                if isinstance(values, list):
                    refs[mid].update(str(v) for v in values if v)
                refs[mid].add("receipt:" + str(row["source_id"]) + ":" + str(row["external_kind"]) + ":" + str(row["external_key"]))
                paths[mid].append({**_json(row["provenance_json"], {}), **meta})
                receipt_directions[mid].append(_direction(meta.get("direction")))
                label = _flat(row["source_label"] or row["source_family"] or "Unknown source", 100)
                coverage.add(label + ": last ingest " + _flat(row["last_ingested_utc"] or "unknown", 40))
        # Resolve retained local artifact bodies in the same read transaction.
        # Projection previews are often only 480 characters; never call those complete.
        bodies, report_scopes = {}, {}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "commstat_artifacts" in tables:
            artifact_ids = collections.defaultdict(list)
            for mid, evidence in paths.items():
                for meta in evidence:
                    if meta.get("source_table") == "commstat_artifacts":
                        try:
                            artifact_ids[int(meta["row_id"])].append(mid)
                        except (KeyError, ValueError, TypeError):
                            pass
            columns = {r[1] for r in conn.execute("PRAGMA table_info(commstat_artifacts)")}
            keys = list(artifact_ids) if "id" in columns and ({"body_text", "remarks_text"} & columns) else []
            body_column = "body_text" if "body_text" in columns else "''"
            remarks_column = "remarks_text" if "remarks_text" in columns else "''"
            scope_column = "scope" if "scope" in columns else "''"
            for offset in range(0, len(keys), 400):
                _cancel(cancelled)
                part = keys[offset:offset + 400]
                for artifact in conn.execute(f"SELECT id,{body_column} AS body_text,{remarks_column} AS remarks_text,{scope_column} AS scope FROM commstat_artifacts WHERE id IN (" + ",".join("?" for _ in part) + ")", part):
                    for mid in artifact_ids[artifact["id"]]:
                        bodies[mid] = _clean(artifact["remarks_text"] or artifact["body_text"])
                        report_scopes[mid] = _flat(artifact["scope"], 40)
        # A shared original source reference plus exact sender/time/remarks is strong
        # enough to remove the historical fused SitRep presentation, not other forwards.
        counterparts = collections.defaultdict(list)
        for row in rows:
            if row["source_family"] == "commstat" and row["from_call"]:
                counterparts[(row["from_call"], row["received_ts"], (row["body_preview"] or "").strip())].append(row)
        mirror_targets, fused_status = {}, {}
        for row in rows:
            body = _clean(row["body_preview"] or row["summary"] or row["subject"])
            if row["source_family"] != "sitrep" or "Remarks: " not in body:
                continue
            candidates = counterparts.get((row["from_call"], row["received_ts"], body.partition("Remarks: ")[2].strip()), [])
            proven = [other for other in candidates if refs[row["message_id"]] & refs[other["message_id"]]]
            if len(proven) == 1:
                target = proven[0]["message_id"]
                mirror_targets[row["message_id"]] = target
                fused_status[target] = _json(row["entities_json"], {})
        entries, exclusions = [], []
        for row in rows:
            _cancel(cancelled)
            mid = row["message_id"]
            body = _clean(row["body_preview"] or row["summary"] or row["subject"])
            subject = _clean(row["subject"])
            entities = _json(row["entities_json"], {})
            if mid in fused_status:
                # Keep the linked structured status fields when collapsing a fused
                # presentation; excluding its row must not discard its evidence.
                entities = {**fused_status[mid], **entities}
            intel = _json(row["intelligence_json"], {})
            reason = ""
            if row["deleted"]:
                exclusions.append(Exclusion(mid, "deleted"))
                continue
            if not row["inbox_visible"]:
                exclusions.append(Exclusion(mid, "suppressed_control"))
                continue
            if mid in mirror_targets:
                reason = "linked_sitrep_mirror"
            derived = entities.get("derived_report") or intel.get("derived_report")
            if REPORT_MARKER in body or "STATION TRAFFIC SUMMARY" in body.upper() or "WEEKLY_SNAPSHOT" in subject.upper() or derived:
                reason = "derived_summary"
            if _is_test(body):
                reason = "explicit_test_demo"
            known_directions = {_direction(entities.get("direction", intel.get("direction"))), *receipt_directions[mid]} - {"unknown"}
            direction = "mixed" if len(known_directions) > 1 else next(iter(known_directions), "unknown")
            if direction == "outgoing":
                reason = "outgoing_or_draft"
            if reason:
                exclusions.append(Exclusion(mid, reason))
                continue
            evidence = paths.get(mid, [])
            path_values = []
            for item in evidence:
                if item.get("local_rf_received") is True:
                    path_values.append("local_rf")
                elif item.get("ingest_origin") in {"import", "imported", "manual"}:
                    path_values.append("imported")
                elif item.get("ingest_origin") in {"internet", "internet_only"}:
                    path_values.append("internet")
                else:
                    path_values.append("unknown")
            provenance = "unknown"
            if path_values and "unknown" not in path_values:
                provenance = path_values[0] if len(set(path_values)) == 1 else "mixed"
            sectors = tuple((key, str(entities[key]).lower()) for key in
                            ("overall_status", "power", "water", "medical", "communications", "internet", "travel", "food", "fuel", "crime", "civil_unrest", "political") if key in entities)
            entries.append(ReportEntry(
                message_id=str(mid), source_family=str(row["source_family"] or "unknown"),
                from_call=_flat(row["from_call"], 64), group_name=_flat(row["group_name"], 64),
                reported_state=_flat(row["state_code"], 32), grid=_flat(row["grid"], 16),
                received_ts=float(row["received_ts"] or 0), event_ts=float(row["event_ts"] or 0),
                severity=str(row["severity"] or "unknown"), excerpt=_flat(body),
                topics=tuple(_flat(v, 64) for v in _json(row["topics_json"], []) if isinstance(v, str)),
                source_refs=tuple(sorted(refs[mid])), provenance=provenance, direction=direction,
                sector_status=sectors, body_text=bodies.get(mid, body) if station_callsign is not None else "",
                report_scope=report_scopes.get(mid, str(entities.get("scope", ""))) if station_callsign is not None else "",
                body_complete=mid in bodies if station_callsign is not None else False,

            ))
        if station_callsign is not None:
            from freqinout.core.traffic_summary_origins import resolve_origins
            from freqinout.core.traffic_summary_findings import event_places
            origins = resolve_origins(db_path, entries, reference_root=reference_root, cancelled=cancelled, connection=conn)
            entries = [dataclasses.replace(e, **origins.get(e.message_id, {}),
                        event_places=event_places(e.body_text or e.excerpt)) for e in entries]
        snapshot = SummarySnapshot(str(uuid.uuid4()), dt.datetime.now(dt.timezone.utc).isoformat(), scope,
                                   tuple(entries), tuple(exclusions), len(rows), source_coverage=tuple(sorted(coverage)),
                                   schema_version=2 if station_callsign is not None else 1,
                                   station_callsign=_flat(station_callsign, 32).upper())
        return dataclasses.replace(snapshot, content_hash=snapshot_digest(snapshot))
    except sqlite3.OperationalError as exc:
        if cancelled and cancelled():
            raise SummaryCancelled("Report generation cancelled.") from exc
        raise
    finally:
        conn.close()


def snapshot_dict(snapshot: SummarySnapshot) -> dict:
    data = dataclasses.asdict(snapshot)
    if snapshot.schema_version == 1:
        # Preserve the canonical hash and saved revisions of the original format.
        data.pop("station_callsign", None)
        for entry in data["entries"]:
            for key in ("body_text", "origin_state", "origin_grid", "origin_source", "event_places", "report_scope", "body_complete"):
                entry.pop(key, None)
    return data


def snapshot_digest(snapshot: SummarySnapshot) -> str:
    data = snapshot_dict(snapshot)
    data.pop("content_hash", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def snapshot_from_dict(data: dict) -> SummarySnapshot:
    scope = SummaryScope(**data["scope"])
    entries = []
    for values in data["entries"]:
        values = dict(values)
        if data.get("schema_version", 1) == 1 and any(key in values for key in
                ("body_text", "origin_state", "origin_grid", "origin_source", "event_places", "report_scope", "body_complete")):
            raise ValueError("Legacy snapshots cannot contain unprotected enrichment fields.")
        for key in ("topics", "source_refs", "event_places"):
            values[key] = tuple(values.get(key, ()))
        values["sector_status"] = tuple(tuple(v) for v in values.get("sector_status", ()))
        entries.append(ReportEntry(**values))
    result = SummarySnapshot(data["report_id"], data["generated_utc"], scope, tuple(entries),
                             tuple(Exclusion(**v) for v in data["exclusions"]), data["raw_count"],
                             data.get("content_hash", ""), tuple(data.get("source_coverage", ())), data.get("schema_version", 1), data.get("station_callsign", ""))
    if result.schema_version not in (1, 2) or result.content_hash != snapshot_digest(result):
        raise ValueError("Report snapshot format or integrity check failed.")
    return result


def _utc(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _counts(values):
    return "; ".join(f"{key}: {value}" for key, value in sorted(values.items())) or "None"


def protected_counts(text: str) -> str:
    starts = list(re.finditer(r"(?m)^" + re.escape(COMPUTED_START) + r"$", text))
    ends = list(re.finditer(r"(?m)^" + re.escape(COMPUTED_END) + r"$", text))
    if len(starts) != 1 or len(ends) != 1 or starts[0].start() >= ends[0].start():
        raise ValueError("Keep the computed counts block intact. Change report inclusion to recalculate counts.")
    return text[starts[0].start():ends[0].end()]


def estimate_airtime_seconds(text: str) -> float:
    from freqinout.core.traffic_summary_outputs import estimate_airtime_seconds as estimate
    return estimate(text)


def render_summary(snapshot: SummarySnapshot, *, compact: bool = False, notes: str = "",
                   excluded_ids=(), report_kind: str = "legacy", target_minutes: int = 3) -> str:
    if report_kind in {"rf", "detailed"}:
        from freqinout.core.traffic_summary_outputs import render_rf, render_detailed
        if report_kind == "rf":
            return render_rf(snapshot, target_minutes=target_minutes, notes=notes, excluded_ids=excluded_ids)
        return render_detailed(snapshot, notes=notes, excluded_ids=excluded_ids)
    if report_kind != "legacy":
        raise ValueError("Choose RF Brief or Detailed Report.")
    if snapshot.schema_version == 2:
        from freqinout.core.traffic_summary_findings import render_consolidated
        return render_consolidated(snapshot, notes=notes, excluded_ids=excluded_ids)
    excluded = set(excluded_ids)
    valid = {entry.message_id for entry in snapshot.entries}
    if excluded - valid:
        raise ValueError("An excluded message is not part of this snapshot.")
    entries = [entry for entry in snapshot.entries if entry.message_id not in excluded]
    severity = collections.Counter(entry.severity for entry in entries)
    families = collections.Counter(entry.source_family for entry in entries)
    paths = collections.Counter(entry.provenance for entry in entries)
    reasons = collections.Counter(item.reason for item in snapshot.exclusions)
    statuses = collections.Counter(dict(entry.sector_status).get("overall_status", "unknown")
                                   for entry in entries if entry.sector_status)
    topics = collections.Counter(topic for e in entries for topic in set(e.topics))
    states = collections.Counter(e.reported_state or "Unknown" for e in entries)
    metrics = ["Stored topic tags (categories overlap): " + _counts(topics),
               f"Uncategorized entries: {sum(not e.topics for e in entries)}.",
               "Reported state tags (event location unqualified): " + _counts(states),
               f"Missing reported grid: {sum(not e.grid for e in entries)}."]
    if statuses:
        metrics.append("Structured sector status (reported fields; categories overlap):")
        for sector in ("power", "water", "medical", "communications", "internet", "travel", "food", "fuel"):
            values = collections.Counter(dict(e.sector_status).get(sector, "unknown") for e in entries if e.sector_status)
            metrics.append(sector.title() + ": " + _counts(values))
    lines = ["STATION TRAFFIC SUMMARY", REPORT_MARKER + snapshot.report_id,
             "Stored traffic window (legacy received_ts): " + _utc(snapshot.scope.start_ts) + " to " + _utc(snapshot.scope.end_ts),
             "Start included; end excluded. Stored time may be source/event time or file mtime, not actual receipt.",
             "Scope: " + "; ".join(f"{name}={','.join(getattr(snapshot.scope, name)) or 'all'}"
                                     for name in ("groups", "sources", "radio_ids", "states")),
             "Generated UTC: " + snapshot.generated_utc, "", COMPUTED_START,
             f"Stored candidate entries: {snapshot.raw_count}; selected traffic entries: {len(entries)}.",
             "Automatic exclusions: " + _counts(reasons) + f"; operator exclusions: {len(excluded)}.",
             "Primary sources: " + _counts(families),
             "Stored application labels (not confirmed hazard severity): " + _counts(severity),
             "Receive-path evidence: " + _counts(paths),
             "Direction evidence: " + _counts(collections.Counter(e.direction for e in entries)),
             "Structured reported overall status: " + _counts(statuses), *metrics, COMPUTED_END, "",
             "FINDINGS - ATTRIBUTED RECEIVED CONTENT"]
    ordered = sorted(entries, key=lambda entry: ({"critical": 0, "warning": 1, "watch": 2}.get(entry.severity, 3), -entry.received_ts, entry.message_id))
    # Compact omits routine entries only. Never silently hide elevated traffic.
    elevated = [e for e in ordered if e.severity in {"critical", "warning", "watch"}]
    limit = max(len(elevated), 8 if compact else 40)
    shown = ordered[:limit]
    if not entries:
        lines.append("No eligible traffic received in this stored window. This does not establish an all-clear.")
    for index, entry in enumerate(shown, 1):
        place = "; ".join(v for v in ("reported state " + entry.reported_state if entry.reported_state else "",
                                       "reported grid " + entry.grid if entry.grid else "") if v)
        lines.append(f"R{index:03d} [{entry.severity}] {entry.from_call or 'Unattributed sender'} / {entry.source_family} / "
                     f"{_utc(entry.received_ts)}" + (" / " + place if place else " / event location unqualified"))
        lines.append("  Received excerpt: " + entry.excerpt)
        if entry.event_ts and entry.received_ts - entry.event_ts > 86400:
            lines.append("  Older source event timestamp: " + _utc(entry.event_ts))
    if len(shown) < len(entries):
        lines.append(f"{len(entries)-len(shown)} additional routine entries retained in the frozen report inventory.")
    lines += ["", "TOPIC AND LOCATION INTERPRETATION",
              "Topic tags organize content; they do not establish incidents, impact or urgency.",
              "Reported state/grid may describe the sender rather than the event. Read the attributed content.",
              "", "LIMITATIONS",
              "Entries are not confirmed incidents or independent reports. Further forwards/repeats and outgoing traffic may remain.",
              "First-ever receipt is unproven. Legacy projected time can use source/event time; late imports/replays may fall in the wrong receipt period.",
              "Records without a positive stored timestamp are outside this time window.",
              "Unknown receive paths, incomplete topic/location tags and secondary claims require operator review.",
              "No current conditions, truth confidence, outstanding duty, or automatic all-clear is inferred."]
    if notes.strip():
        lines += ["", "OPERATOR NOTES", _clean(notes).strip()]
    lines += ["", "REFERENCES"]
    for index, entry in enumerate(shown, 1):
        lines.append(f"R{index:03d} message_id={entry.message_id}")
    lines += ["", "SOURCE COVERAGE (stored source metadata, not live monitoring)"]
    lines += list(snapshot.source_coverage[:30])
    if len(snapshot.source_coverage) > 30:
        lines.append(f"{len(snapshot.source_coverage)-30} additional source records retained locally.")
    return "\n".join(lines) + "\n"
