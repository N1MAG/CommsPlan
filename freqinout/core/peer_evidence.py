"""Bounded, read-only peer evidence snapshot.

This module intentionally reads only records already retained by the runtime.  It
never initializes schemas, performs migrations, or treats an aggregate link row
as proof of an RF reception.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from urllib.parse import quote

LOOKBACK_SECONDS = 56 * 24 * 60 * 60
SOURCE_ROW_LIMIT = 2_000
VISIBLE_OBSERVATION_LIMIT = 100
RELAY_FRESH_SECONDS = 30 * 60
MAX_RELAY_EDGES = 3
MAX_RELAY_RESULTS = 10
MAX_RELAY_EXPANSIONS = 6_000

CLASS_DIRECT = "direct"
CLASS_REPORTED_HEARING = "reported_hearing"
CLASS_ADDRESSED = "addressed_participant"
CLASS_UNKNOWN = "unknown"

_CALL_RE = re.compile(r"^[A-Z0-9/]{3,12}$")
_RELAY_HEAD_RE = re.compile(r"^(?:[A-Z0-9/]{3,12}>\s*)+")
_DE_RE = re.compile(r"\*DE\*\s*([A-Z0-9/]{3,12})\b", re.IGNORECASE)


def _canon_call(value: object) -> str:
    """Canonicalize case and surrounding noise while retaining slash suffixes."""
    return str(value or "").strip().upper().strip(",;:()[]{}<>\"'")


def _valid_call(value: object) -> bool:
    call = _canon_call(value)
    if not _CALL_RE.fullmatch(call) or not any(ch.isdigit() for ch in call):
        return False
    return call not in {"CQ", "CQCQCQ", "ALLCALL"}


def _optional_float(value: object) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_ts(value: object) -> Optional[float]:
    result = _optional_float(value)
    return result if result is not None and result > 0 else None


def _timestamp(value: object) -> Optional[float]:
    result = _optional_ts(value)
    if result is not None:
        return result
    text = _text(value)
    if not text:
        return None
    try:
        parsed = text.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(parsed)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _stable_id(value: object) -> str:
    text = _source_value(value)
    return "" if text in {"", "0", "NONE", "NULL"} else text


def _text(value: object) -> str:
    return str(value or "").strip()


def _source_value(value: object) -> str:
    return _text(value)


def _basename(value: object) -> str:
    return Path(_text(value).replace("\\", "/")).name.upper()


@dataclass(frozen=True, slots=True)
class PeerEvidence:
    source_family: str = ""
    source_reference: str = ""
    source_radio_id: str = ""
    source_app: str = ""
    observed_ts: Optional[float] = None
    received_ts: Optional[float] = None
    origin: str = ""
    destination: str = ""
    transport_sender: str = ""
    reporter: str = ""
    subject: str = ""
    snr: Optional[float] = None
    band: str = ""
    frequency_hz: Optional[float] = None
    classification: str = CLASS_UNKNOWN
    direction_known: bool = False
    claimed_reporter: str = ""
    relay_path: tuple[str, ...] = ()
    source_session: str = ""
    evidence_scope: str = ""



@dataclass(frozen=True, slots=True)
class PeerSchedule:
    callsign: str = ""
    day_utc: str = ""
    start_utc: str = ""
    end_utc: str = ""
    band: str = ""
    mode: str = ""
    frequency: str = ""
    source_type: str = ""
    confidence: Optional[float] = None
    sample_count: Optional[int] = None
    weeks_seen: Optional[int] = None
    weeks_observed: Optional[int] = None
    last_seen_ts: Optional[float] = None

    @property
    def frequency_hz(self) -> Optional[float]:
        value = _optional_float(self.frequency)
        if value is None or value <= 0:
            return None
        return value * 1_000_000.0 if value < 1000 else value

    @property
    def frequency_mhz(self) -> Optional[float]:
        value = _optional_float(self.frequency)
        if value is None or value <= 0:
            return None
        return value / 1_000_000.0 if value >= 1000 else value

    @property
    def explicit(self) -> bool:
        return not self.source_type.upper().startswith("INFERRED")


@dataclass(frozen=True, slots=True)
class PeerFrequencySummary:
    band: str = ""
    frequency_hz: Optional[float] = None
    count: int = 0
    latest_observed_ts: Optional[float] = None
    source_families: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PeerTimeSummary:
    day_utc: int = 0
    hour_utc: int = 0
    count: int = 0
    latest_observed_ts: Optional[float] = None
    source_families: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PeerRelayCandidate:
    path: tuple[str, ...] = ()
    band: str = ""
    frequency_hz: Optional[float] = None
    source_radio_id: str = ""
    source_app: str = ""
    edges: int = 0
    latest_observed_ts: Optional[float] = None
    verified: bool = False
    direction: str = "receiving"
    detail: str = "Unverified receiving path; this is not a send route. Passband compatibility is unverified."

    source_session: str = ""
    evidence_scope: str = ""

    @property
    def relay_calls(self) -> tuple[str, ...]:
        return self.path[1:-1] if len(self.path) > 2 else ()


@dataclass(frozen=True, slots=True)
class PeerEvidenceCoverage:
    detail: str = ""
    errors: tuple[str, ...] = ()
    truncated: bool = False
    lookback_seconds: int = LOOKBACK_SECONDS
    source_limits: tuple[tuple[str, int], ...] = ()

    @property
    def available(self) -> bool:
        return not any(err in {"database_missing", "database_unreadable"} for err in self.errors)


@dataclass(frozen=True, slots=True)
class PeerEvidenceSnapshot:
    callsign: str
    observations: tuple[PeerEvidence, ...] = ()
    schedules: tuple[PeerSchedule, ...] = ()
    frequency_summaries: tuple[PeerFrequencySummary, ...] = ()
    time_summaries: tuple[PeerTimeSummary, ...] = ()
    relay_candidates: tuple[PeerRelayCandidate, ...] = ()
    coverage: PeerEvidenceCoverage = PeerEvidenceCoverage()

    @property
    def coverage_detail(self) -> str:
        return self.coverage.detail

    @property
    def coverage_errors(self) -> tuple[str, ...]:
        return self.coverage.errors

    @property
    def coverage_truncated(self) -> bool:
        return self.coverage.truncated

    @property
    def truncated(self) -> bool:
        return self.coverage.truncated


@dataclass(frozen=True, slots=True)
class _RowBatch:
    rows: tuple[dict[str, Any], ...]
    truncated: bool = False
    error: str = ""


class _ReadGuard:
    def __init__(self, cancelled: Optional[Callable[[], bool]]) -> None:
        self.cancelled = cancelled
        self.deadline = time.monotonic() + 2.0
        self.reason = ""

    def check(self) -> int:
        try:
            if self.cancelled is not None and self.cancelled():
                self.reason = "cancelled"
                return 1
        except Exception:
            self.reason = "cancelled"
            return 1
        if time.monotonic() >= self.deadline:
            self.reason = "read_budget_exceeded"
            return 1
        return 0


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(db_path.expanduser().resolve()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _load_recent_since(
    conn: sqlite3.Connection, table: str, timestamp_column: str,
    since_ts: float, until_ts: float, guard: _ReadGuard,
) -> _RowBatch:
    if not _table_exists(conn, table):
        return _RowBatch((), error=f"missing_table:{table}")
    try:
        columns = _table_columns(conn, table)
        if timestamp_column not in columns:
            return _RowBatch((), error=f"missing_timestamp:{table}.{timestamp_column}")
        fields = sorted(columns)
        select_fields = ", ".join(
            f'substr("{field}",1,4097) AS "{field}"'
            if field in {"raw_text", "decoded_text"} else f'"{field}"'
            for field in fields
        )
        query = (
            f'SELECT {select_fields}, rowid AS "__rowid" FROM "{table}" '
            f'WHERE "{timestamp_column}" >= ? AND "{timestamp_column}" <= ? '
            f'ORDER BY "{timestamp_column}" DESC, rowid DESC LIMIT ?'
        )
        fetched = conn.execute(
            query, (float(since_ts), float(until_ts), SOURCE_ROW_LIMIT + 1)
        ).fetchall()
        rows = tuple({str(key): row[key] for key in row.keys()} for row in fetched[:SOURCE_ROW_LIMIT])
        return _RowBatch(rows, len(fetched) > SOURCE_ROW_LIMIT)
    except Exception as exc:
        if guard.reason:
            return _RowBatch((), error=guard.reason)
        return _RowBatch((), error=f"read_error:{table}:{type(exc).__name__}")


def _row_value(row: dict[str, Any], name: str, default: Any = None) -> Any:
    return row.get(name, default)


def _link_observation(row: dict[str, Any], family: str) -> Optional[PeerEvidence]:
    origin = _canon_call(_row_value(row, "origin"))
    destination = _canon_call(_row_value(row, "destination"))
    ts = _timestamp(_row_value(row, "ts"))
    if not ts or (not origin and not destination):
        return None
    source_id = _stable_id(_row_value(row, "source_id"))
    if not source_id:
        source_id = f"row:{_row_value(row, '__rowid', '')}"
    app = _source_value(_row_value(row, "app_instance_id")) or _source_value(_row_value(row, "source"))
    radio = _source_value(_row_value(row, "source_radio_id"))
    return PeerEvidence(
        source_family=family,
        source_reference=source_id,
        source_radio_id=radio,
        source_app=app,
        observed_ts=ts,
        received_ts=_timestamp(_row_value(row, "received_ts") or _row_value(row, "receive_ts")),
        origin=origin,
        destination=destination,
        snr=_optional_float(_row_value(row, "snr")),
        band=_text(_row_value(row, "band")).upper(),
        frequency_hz=_optional_float(_row_value(row, "freq_hz")),
        classification=CLASS_UNKNOWN,
        direction_known=bool(_row_value(row, "direction_known", 0)),
    )


def _hearing_subjects(
    text: object, *, known_sender: str = "", known_target: str = ""
) -> tuple[tuple[str, ...], str, tuple[str, ...], bool]:
    """Parse only the native anchored HEARING response grammar."""
    body = _text(text)
    if not body or len(body) > 4096:
        return (), "", (), False
    relay_match = _RELAY_HEAD_RE.match(body.upper())
    relay_path: tuple[str, ...] = ()
    relayed = bool(relay_match)
    if relay_match:
        relay_path = tuple(
            _canon_call(token[:-1])
            for token in re.findall(r"[A-Z0-9/]{3,12}>", relay_match.group(0), flags=re.IGNORECASE)
        )
        body = body[relay_match.end():].strip()
    # Native directed replies may retain an explicit envelope such as
    # ``SENDER: TARGET HEARING CALL1`` or ``TARGET HEARING CALL1``.
    # Strip it only when the calls match the retained row metadata.
    sender = _canon_call(known_sender)
    target = _canon_call(known_target)
    envelope = re.match(
        r"^(?P<first>[A-Z0-9/]{3,12})\s*:?(?:\s+(?P<second>[A-Z0-9/]{3,12}))?\s+HEARING(?:\s+|$)(?P<body>.*)$",
        body, flags=re.IGNORECASE | re.DOTALL,
    )
    if envelope:
        first = _canon_call(envelope.group("first"))
        second = _canon_call(envelope.group("second"))
        valid_envelope = (
            first == sender and (not second or second == target)
        ) or (first == target and second == "")
        if valid_envelope:
            body = "HEARING " + envelope.group("body")
    # A bare native response is exactly HEARING followed by an optional list.
    match = re.match(r"^HEARING(?:\s+|$)(.*)$", body, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return (), "", relay_path, relayed
    remainder = match.group(1).strip()
    claimed = ""
    de_match = _DE_RE.search(remainder)
    if de_match:
        claimed = _canon_call(de_match.group(1)) if _valid_call(de_match.group(1)) else ""
        remainder = remainder[: de_match.start()].strip()
    subjects: list[str] = []
    # Reject prose and punctuation-heavy tokens rather than treating words as calls.
    for token in re.split(r"[\s,;]+", remainder):
        clean = _canon_call(token)
        if not clean:
            continue
        if not _valid_call(clean):
            return (), claimed, relay_path, relayed
        if clean not in subjects:
            subjects.append(clean)
        if len(subjects) >= 32:
            break
    if not subjects:
        return (), claimed, relay_path, relayed
    return tuple(subjects), claimed, relay_path, relayed


def _message_observations(row: dict[str, Any]) -> tuple[PeerEvidence, ...]:
    source_family = "js8_messages"
    legacy_sender = _canon_call(_row_value(row, "from_call"))
    destination = _canon_call(_row_value(row, "to_call"))
    ts = _timestamp(_row_value(row, "utc_ts"))
    if not ts or (not legacy_sender and not destination):
        return ()
    source_id = _stable_id(_row_value(row, "source_key"))
    native_id = _stable_id(_row_value(row, "source_id"))
    if source_id or native_id:
        source_reference = f"{source_id}:{native_id}" if source_id else native_id
    else:
        source_reference = f"row:{_row_value(row, '__rowid', '')}"
    radio = _source_value(_row_value(row, "source_radio_id"))
    app = _source_value(_row_value(row, "js8_instance_id"))
    # New receipt context is authoritative when present. Historical rows retain
    # their event fields but no guessed receive metadata.
    rx_sender = _canon_call(_row_value(row, "rx_sender"))
    receipt_kind = _text(_row_value(row, "receipt_kind")).lower()
    msg_type = _text(_row_value(row, "msg_type")).upper()
    # RX activity snapshots are status observations, not retained frames.
    receipt_kind_valid = (
        receipt_kind in {"directed_txt", "rx_directed"}
        and "ACTIVITY" not in msg_type
    )
    # Historical js8_messages rows have no trustworthy receive context; do
    # not promote similarly named legacy columns to RF metadata.
    band = _text(_row_value(row, "rx_band")).upper()
    frequency_hz = _optional_float(_row_value(row, "rx_freq_hz"))
    snr = _optional_float(_row_value(row, "rx_snr"))
    received_ts = _timestamp(_row_value(row, "received_ts"))
    raw = _text(_row_value(row, "raw_text")) or _text(_row_value(row, "decoded_text"))
    subjects, claimed, relay_path, relayed = _hearing_subjects(
        raw, known_sender=rx_sender or legacy_sender, known_target=destination
    )
    has_de_ambiguity = bool(_DE_RE.search(raw))
    transport_sender = rx_sender or (legacy_sender if not (relay_path or claimed) else "")
    output: list[PeerEvidence] = []
    if subjects and radio:
        for subject in subjects:
            output.append(
                PeerEvidence(
                    source_family=source_family,
                    source_reference=f"{source_reference}:hearing:{subject}",
                    source_radio_id=radio,
                    source_app=app,
                    observed_ts=ts,
                    received_ts=received_ts,
                    origin=subject,
                    destination=transport_sender,
                    transport_sender=transport_sender,
                    reporter=transport_sender,
                    subject=subject,
                    # rx_snr measures the receiver-to-reporter hop; it is
                    # not a signal measurement for each listed subject.
                    snr=None,
                    band=band,
                    frequency_hz=frequency_hz,
                    classification=CLASS_REPORTED_HEARING,
                    direction_known=not (relayed and bool(claimed)),
                    claimed_reporter=claimed,
                    relay_path=relay_path,
                )
            )
        # A qualified native receipt also proves the transport sender was
        # heard by this radio.  Keep that measured frame separate from the
        # reported subject edge: rx_snr belongs only to this local hop.
        if receipt_kind_valid and rx_sender:
            output.append(
                PeerEvidence(
                    source_family=source_family,
                    source_reference=f"{source_reference}:receipt",
                    source_radio_id=radio,
                    source_app=app,
                    observed_ts=ts,
                    received_ts=received_ts,
                    origin=transport_sender,
                    destination=destination,
                    transport_sender=transport_sender,
                    snr=snr,
                    band=band,
                    frequency_hz=frequency_hz,
                    classification=CLASS_DIRECT,
                    direction_known=True,
                )
            )
        return tuple(output)
    # A new receipt row qualifies from explicit metadata. Historical DIRECTED.TXT
    # rows qualify only when they do not carry a relay/DE ambiguity.
    legacy_direct = (
        _basename(_row_value(row, "source_path")) == "DIRECTED.TXT"
        and not relay_path
        and not has_de_ambiguity
    )
    direct = bool(radio and ((receipt_kind_valid and rx_sender) or (legacy_direct and legacy_sender)))
    origin = rx_sender if receipt_kind_valid and rx_sender else legacy_sender
    classification = CLASS_DIRECT if direct and origin else CLASS_UNKNOWN
    if classification == CLASS_UNKNOWN and destination and not origin:
        classification = CLASS_ADDRESSED
    return (
        PeerEvidence(
            source_family=source_family,
            source_reference=source_reference,
            source_radio_id=radio,
            source_app=app,
            observed_ts=ts,
            received_ts=received_ts,
            origin=origin,
            destination=destination,
            transport_sender=transport_sender,
            snr=snr,
            band=band,
            frequency_hz=frequency_hz,
            classification=classification,
            direction_known=bool(classification == CLASS_DIRECT),
        ),
    )


def _target_in_observation(obs: PeerEvidence, target: str) -> bool:
    return target in {
        obs.origin,
        obs.destination,
        obs.reporter,
        obs.subject,
    }


def _dedupe(observations: Iterable[PeerEvidence]) -> tuple[PeerEvidence, ...]:
    seen: set[tuple[Any, ...]] = set()
    result: list[PeerEvidence] = []
    for obs in observations:
        key = (
            obs.source_family,
            obs.source_reference,
            obs.source_radio_id,
            obs.source_app,
            obs.source_session,
            obs.evidence_scope,
            None if (
                obs.source_family == "js8_messages"
                and obs.source_reference
                and not obs.source_reference.startswith("row:")
            ) else obs.observed_ts,
            obs.band,
            obs.frequency_hz,
            obs.origin,
            obs.destination,
            obs.transport_sender,
            obs.reporter,
            obs.subject,
            obs.classification,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(obs)
    result.sort(key=lambda item: (item.observed_ts or 0.0, item.source_family, item.source_reference), reverse=True)
    return tuple(result)


def _optional_int(value: object) -> Optional[int]:
    try:
        return int(value) if value is not None and _text(value) != "" else None
    except (TypeError, ValueError, OverflowError):
        return None


def _schedule_rows(conn: sqlite3.Connection, table: str, target: str) -> tuple[PeerSchedule, ...] | None:
    if not _table_exists(conn, table):
        return None
    try:
        columns = _table_columns(conn, table)
        owner_col = "owner_callsign" if "owner_callsign" in columns else "callsign" if "callsign" in columns else ""
        if not owner_col:
            return ()
        fields = sorted(columns.intersection({
            owner_col, "day_utc", "start_utc", "end_utc", "band", "mode", "frequency",
            "source_type", "source", "confidence", "sample_count", "weeks_seen",
            "weeks_observed", "last_seen_ts",
        }))
        select_fields = ", ".join(f'"{field}"' for field in fields)
        query = (
            f'SELECT {select_fields} FROM "{table}" WHERE UPPER(TRIM("{owner_col}"))=? '+
            "ORDER BY rowid DESC LIMIT 2000"
        )
        rows = conn.execute(query, (target,)).fetchall()
    except Exception:
        return ()
    result: list[PeerSchedule] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        data = {str(key): row[key] for key in row.keys()}
        schedule = PeerSchedule(
            callsign=_canon_call(data.get(owner_col)),
            day_utc=_text(data.get("day_utc")),
            start_utc=_text(data.get("start_utc")),
            end_utc=_text(data.get("end_utc")),
            band=_text(data.get("band")).upper(),
            mode=_text(data.get("mode")).upper(),
            frequency=_text(data.get("frequency")),
            source_type=_text(data.get("source_type")) or ("INFERRED" if table.endswith("inferred") else "IMPORTED"),
            confidence=_optional_float(data.get("confidence")),
            sample_count=_optional_int(data.get("sample_count")),
            weeks_seen=_optional_int(data.get("weeks_seen")),
            weeks_observed=_optional_int(data.get("weeks_observed")),
            last_seen_ts=_timestamp(data.get("last_seen_ts")),
        )
        key = tuple(schedule.__dict__.values()) if hasattr(schedule, "__dict__") else (
            schedule.callsign, schedule.day_utc, schedule.start_utc, schedule.end_utc,
            schedule.band, schedule.mode, schedule.frequency, schedule.source_type,
        )
        if key not in seen:
            seen.add(key)
            result.append(schedule)
    return tuple(result)


def _summaries(observations: Sequence[PeerEvidence]) -> tuple[tuple[PeerFrequencySummary, ...], tuple[PeerTimeSummary, ...]]:
    by_frequency: dict[tuple[str, Optional[float]], list[PeerEvidence]] = defaultdict(list)
    by_time: dict[tuple[int, int], list[PeerEvidence]] = defaultdict(list)
    for obs in observations:
        if obs.band or obs.frequency_hz is not None:
            by_frequency[(obs.band, obs.frequency_hz)].append(obs)
        if obs.observed_ts:
            try:
                dt = _dt.datetime.fromtimestamp(obs.observed_ts, tz=_dt.timezone.utc)
            except (OverflowError, OSError, ValueError):
                dt = None
            if dt is not None:
                by_time[(dt.weekday(), dt.hour)].append(obs)
    frequencies = tuple(
        PeerFrequencySummary(
            band=key[0], frequency_hz=key[1], count=len(rows),
            latest_observed_ts=max((row.observed_ts or 0.0 for row in rows), default=None),
            source_families=tuple(sorted({row.source_family for row in rows})),
        )
        for key, rows in sorted(by_frequency.items(), key=lambda item: (item[0][0], item[0][1] or 0.0))
    )
    times = tuple(
        PeerTimeSummary(
            day_utc=key[0], hour_utc=key[1], count=len(rows),
            latest_observed_ts=max((row.observed_ts or 0.0 for row in rows), default=None),
            source_families=tuple(sorted({row.source_family for row in rows})),
        )
        for key, rows in sorted(by_time.items())
    )
    return frequencies, times


def _relay_candidates(observations: Sequence[PeerEvidence], target: str, local_call: str, now_ts: float, *, allow_band_only: bool = False, max_age_seconds: float = RELAY_FRESH_SECONDS) -> tuple[PeerRelayCandidate, ...]:
    if not local_call or not target:
        return ()
    eligible = [
        obs for obs in observations
        if obs.classification in {CLASS_DIRECT, CLASS_REPORTED_HEARING}
        and obs.origin and obs.destination and obs.band and obs.source_radio_id
        and obs.direction_known
        and (obs.frequency_hz is not None or (allow_band_only and obs.evidence_scope == "band_only"))
        and not (obs.relay_path and obs.claimed_reporter)
        and obs.observed_ts is not None
        and 0 <= now_ts - obs.observed_ts <= max_age_seconds
    ]
    grouped: dict[tuple, list[PeerEvidence]] = defaultdict(list)
    for obs in eligible:
        grouped[(obs.band, obs.source_radio_id, obs.source_app, obs.frequency_hz, obs.source_session, obs.evidence_scope)].append(obs)
    candidates: list[PeerRelayCandidate] = []
    expansions = 0
    for (band, radio, app, freq, session, scope), edges in grouped.items():
        graph: dict[str, list[PeerEvidence]] = defaultdict(list)
        for edge in edges:
            graph[edge.origin].append(edge)
        queue: deque[tuple[str, tuple[str, ...], tuple[PeerEvidence, ...]]] = deque([(target, (target,), ())])
        while queue and len(candidates) < MAX_RELAY_RESULTS:
            node, path, used = queue.popleft()
            expansions += 1
            if expansions > MAX_RELAY_EXPANSIONS:
                return tuple(candidates[:MAX_RELAY_RESULTS])
            if len(used) >= MAX_RELAY_EDGES:
                continue
            for edge in graph.get(node, ()):
                if edge.destination in path:
                    continue
                next_path = path + (edge.destination,)
                next_used = used + (edge,)
                if edge.destination == local_call:
                    latest = max(item.observed_ts or 0.0 for item in next_used)
                    candidates.append(PeerRelayCandidate(
                        path=next_path, band=band, frequency_hz=freq,
                        source_radio_id=radio, source_app=app,
                        edges=len(next_used), latest_observed_ts=latest,
                        source_session=session, evidence_scope=scope,
                        detail=("Band-only unverified receiving path; exact frequency and passband unknown."
                                if scope == "band_only" else PeerRelayCandidate().detail),
                    ))
                    if len(candidates) >= MAX_RELAY_RESULTS:
                        break
                else:
                    queue.append((edge.destination, next_path, next_used))
    candidates.sort(key=lambda item: (item.edges, -(item.latest_observed_ts or 0.0), item.path))
    return tuple(candidates[:MAX_RELAY_RESULTS])


def load_peer_evidence(
    db_path: str | Path,
    callsign: str,
    *,
    local_call: str = "",
    now_ts: Optional[float] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> PeerEvidenceSnapshot:
    """Load a bounded immutable peer snapshot from an existing SQLite database."""
    target = _canon_call(callsign)
    local = _canon_call(local_call)
    now = float(now_ts if now_ts is not None else time.time())
    if cancelled is not None:
        try:
            if cancelled():
                return PeerEvidenceSnapshot(
                    target,
                    coverage=PeerEvidenceCoverage(
                        detail="Peer evidence read was cancelled before opening the database.",
                        errors=("cancelled",),
                    ),
                )
        except Exception:
            return PeerEvidenceSnapshot(
                target,
                coverage=PeerEvidenceCoverage(
                    detail="Peer evidence read was cancelled before opening the database.",
                    errors=("cancelled",),
                ),
            )
    guard = _ReadGuard(cancelled)
    errors: list[str] = []
    truncated = False
    limits: list[tuple[str, int]] = []
    from freqinout.core.subspace_hearing import peer_observations
    native = peer_observations(now=now)
    observations: list[PeerEvidence] = [e for e in native if _target_in_observation(e, target)]
    support_observations: list[PeerEvidence] = []
    relay_metadata_unavailable = False
    path = Path(db_path)
    if not path.exists():
        coverage = PeerEvidenceCoverage(
            detail="No retained peer database is available.", errors=("database_missing",),
            truncated=False, source_limits=(),
        )
        return PeerEvidenceSnapshot(target, observations=tuple(observations[:VISIBLE_OBSERVATION_LIMIT]), coverage=coverage)
    try:
        conn = _readonly_connection(path)
        conn.set_progress_handler(guard.check, 1000)
    except Exception:
        coverage = PeerEvidenceCoverage(
            detail="The retained peer database could not be opened read-only.", errors=("database_unreadable",),
            truncated=False, source_limits=(),
        )
        return PeerEvidenceSnapshot(target, coverage=coverage)
    since = now - LOOKBACK_SECONDS
    try:
        for table, ts_column in (("js8_messages", "utc_ts"), ("js8_links", "ts"), ("varac_links", "ts")):
            batch = _load_recent_since(conn, table, ts_column, since, now, guard)
            limits.append((table, len(batch.rows)))
            if batch.error:
                errors.append(batch.error)
                if batch.error in {"cancelled", "read_budget_exceeded"}:
                    break
                continue
            truncated = truncated or batch.truncated
            for row in batch.rows:
                if table == "js8_messages":
                    rows = _message_observations(row)
                else:
                    item = _link_observation(row, table)
                    rows = (item,) if item is not None else ()
                for item in rows:
                    if item is not None:
                        scoped_item = item
                        # A target appearing only as the destination is an
                        # addressed participant; the decoded transmitter is
                        # the station directly heard.
                        if (
                            item.classification == CLASS_DIRECT
                            and target == item.destination
                            and target != item.origin
                        ):
                            scoped_item = replace(item, classification=CLASS_ADDRESSED, direction_known=False)
                        if _target_in_observation(scoped_item, target):
                            observations.append(scoped_item)
                        if local and item.classification == CLASS_DIRECT and item.origin:
                            # A directed/group address is display metadata;
                            # the radio's qualified direct receipt always
                            # terminates at this local station for route
                            # support.  Preserve the original item above.
                            support_observations.append(replace(item, destination=local))
                        elif local and _target_in_observation(item, local):
                            support_observations.append(item)
                        if (
                            local
                            and item.classification in {CLASS_DIRECT, CLASS_REPORTED_HEARING}
                            and (not item.band or item.source_radio_id == "")
                        ):
                            relay_metadata_unavailable = True
        deduped = _dedupe(observations)
        support = _dedupe(support_observations)
        relay_inputs = _dedupe((*deduped, *support))
        total_matching = len(deduped)
        if total_matching > VISIBLE_OBSERVATION_LIMIT:
            truncated = True
        visible = deduped[:VISIBLE_OBSERVATION_LIMIT]
        explicit = _schedule_rows(conn, "peer_hf_schedule", target)
        inferred = _schedule_rows(conn, "peer_hf_schedule_inferred", target)
        if explicit is None:
            errors.append("missing_table:peer_hf_schedule")
            schedules = inferred or ()
        elif explicit:
            schedules = explicit
        else:
            if inferred is None:
                errors.append("missing_table:peer_hf_schedule_inferred")
            schedules = inferred or ()
        frequency_summaries, time_summaries = _summaries(deduped)
        relay = _relay_candidates(relay_inputs, target, local, now)
    except Exception as exc:
        errors.append(f"read_error:peer_evidence:{type(exc).__name__}")
        visible = ()
        schedules = ()
        frequency_summaries = ()
        time_summaries = ()
        relay = ()
    finally:
        try:
            conn.set_progress_handler(None, 0)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    detail_parts = [f"Retained evidence window: {len(deduped) if 'deduped' in locals() else 0} matching observations; showing up to {VISIBLE_OBSERVATION_LIMIT}."]
    if truncated:
        detail_parts.append("History is bounded or paged; older retained rows are not in this snapshot.")
    if relay_metadata_unavailable:
        errors.append("relay_metadata_unavailable")
        detail_parts.append("Receiving path candidates are unavailable because retained evidence lacks exact band or endpoint metadata.")
    if errors:
        detail_parts.append("Some retained evidence sources were unavailable.")
    coverage = PeerEvidenceCoverage(
        detail=" ".join(detail_parts), errors=tuple(dict.fromkeys(errors)),
        truncated=truncated, source_limits=tuple(limits),
    )
    return PeerEvidenceSnapshot(
        callsign=target,
        observations=tuple(visible),
        schedules=tuple(schedules),
        frequency_summaries=frequency_summaries,
        time_summaries=time_summaries,
        relay_candidates=relay,
        coverage=coverage,
    )
