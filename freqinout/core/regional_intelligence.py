from __future__ import annotations

import datetime as dt
import sqlite3
from freqinout.core.sqlite_utils import connect_sqlite_readonly
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence

from freqinout.core.message_intelligence import normalize_topic_terms
from freqinout.core.message_search_values import is_no_report_placeholder, searchable_text_values
from freqinout.core.observation_projection import Observation
from freqinout.core.observation_queries import ObservationQuery, query_observations
from freqinout.core.commstat_sitrep import infer_state_and_geo, resolve_commstat_reported_for_state


FEMA_REGIONS = {
    "R01": ("CT", "ME", "MA", "NH", "RI", "VT"),
    "R02": ("NJ", "NY", "PR", "VI"),
    "R03": ("DC", "DE", "MD", "PA", "VA", "WV"),
    "R04": ("AL", "FL", "GA", "KY", "MS", "NC", "SC", "TN"),
    "R05": ("IL", "IN", "MI", "MN", "OH", "WI"),
    "R06": ("AR", "LA", "NM", "OK", "TX"),
    "R07": ("IA", "KS", "MO", "NE"),
    "R08": ("CO", "MT", "ND", "SD", "UT", "WY"),
    "R09": ("AZ", "CA", "HI", "NV", "GU", "AS", "MP"),
    "R10": ("AK", "ID", "OR", "WA"),
}
STATE_TO_FEMA_REGION = {state: region for region, states in FEMA_REGIONS.items() for state in states}
US_STATE_ABBR_FROM_NAME = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
}

SENSITIVITY_WINDOWS = {
    "current": ((6, 1.0), (24, 0.25)),
    "active": ((6, 1.0), (72, 0.60), (168, 0.15)),
    "extended": ((24, 1.0), (168, 0.60), (336, 0.20)),
}

TOPIC_SEVERITY = {
    "Fire": 3.0,
    "Weather": 2.4,
    "Power": 2.5,
    "Water": 2.5,
    "Medical": 2.8,
    "Comms": 2.0,
    "Travel/Roads": 1.8,
    "Food": 1.9,
    "Fuel": 1.8,
    "Security": 2.3,
    "Shelter": 2.2,
    "Logistics": 1.4,
    "General Intel": 1.0,
}

SIGNAL_SOURCES = {"js8call", "js8", "varac"}
PATH_SOURCES = {"propagation", "peer_schedule", "path"}
PLANNING_SOURCES = {"rf_pin", "planning_pin"}
AMBIGUOUS_STATE_WORDS = {"IN", "OR"}
NON_LOCATION_PRECEDERS = {
    "BACK",
    "BEEN",
    "CHECK",
    "CONFIRMED",
    "DOWN",
    "FOUND",
    "HEARD",
    "ISSUE",
    "ISSUES",
    "LOST",
    "NEEDED",
    "OPEN",
    "OUT",
    "PENDING",
    "REPORTED",
    "REPORTS",
    "RESTORED",
    "RUNNING",
    "SEEN",
    "SHOWING",
    "SHOWN",
    "STARTED",
    "UPDATED",
}


@dataclass(frozen=True)
class RegionalEvidenceItem:
    evidence_id: str
    source_family: str
    source_ref: str
    evidence_type: str
    topic: str
    severity_hint: str
    confidence: float
    event_time_utc: str
    age_hours: float
    decay_weight: float
    reporter_callsign: str
    target: str
    state: str
    fema_region: str
    summary: str
    score: float


@dataclass(frozen=True)
class RegionalTopicRollup:
    topic: str
    score: float = 0.0
    evidence_count: int = 0
    reporter_count: int = 0
    newest_age_hours: float | None = None
    level: str = "gray"


@dataclass(frozen=True)
class RegionalAreaRollup:
    area_type: str
    area_id: str
    label: str
    fema_region: str = ""
    score: float = 0.0
    level: str = "gray"
    evidence_count: int = 0
    reporter_count: int = 0
    signal_count: int = 0
    newest_age_hours: float | None = None
    trend: str = "flat"
    top_topics: tuple[RegionalTopicRollup, ...] = ()
    evidence: tuple[RegionalEvidenceItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RegionalIntelligenceSnapshot:
    sensitivity: str
    topic_filter: str = ""
    state_rollups: tuple[RegionalAreaRollup, ...] = ()
    fema_rollups: tuple[RegionalAreaRollup, ...] = ()
    generated_utc: str = ""


def build_regional_intelligence_from_db(
    db_path: str | Path,
    *,
    sensitivity: str = "active",
    topic_filter: str = "",
    operating_group: str = "",
    search_text: str = "",
    state: str = "",
    max_age_sec: int = 0,
    limit: int = 5000,
    readonly: bool = False,
    now: dt.datetime | None = None,
    station_index: Mapping[str, Mapping[str, object]] | None = None,
) -> RegionalIntelligenceSnapshot:
    now_utc = _coerce_utc(now) or dt.datetime.now(dt.timezone.utc)
    since_utc = ""
    try:
        seconds = int(max_age_sec or 0)
    except Exception:
        seconds = 0
    if seconds > 0:
        since_utc = (now_utc - dt.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
    state_filter = str(state or "").strip().upper()
    observations = query_observations(
        db_path,
        ObservationQuery(
            topic=str(topic_filter or "").strip(),
            operating_group=str(operating_group or "").strip(),
            search_text=str(search_text or "").strip(),
            state="",
            since_utc=since_utc,
            limit=max(1, int(limit or 5000)),
        ),
        readonly=readonly,
    )
    observations = _enrich_commstat_observations_from_artifacts(db_path, observations)
    if state_filter:
        observations = tuple(
            obs
            for obs in observations
            if _state_for_observation(obs, station_index or {}) == state_filter
        )
    return build_regional_intelligence(
        observations,
        sensitivity=sensitivity,
        topic_filter=topic_filter,
        now=now_utc,
        station_index=station_index,
    )


def _enrich_commstat_observations_from_artifacts(
    db_path: str | Path,
    observations: Sequence[Observation],
) -> tuple[Observation, ...]:
    commstat_refs = {
        str(obs.source_ref or "").strip()
        for obs in observations or ()
        if str(obs.source_family or "").strip().lower() == "commstat"
        and str(obs.source_ref or "").strip().startswith("commstat_artifacts:")
    }
    if not commstat_refs:
        return tuple(observations or ())
    ids: list[int] = []
    ref_by_id: dict[int, str] = {}
    for ref in commstat_refs:
        try:
            artifact_id = int(ref.rsplit(":", 1)[1])
        except Exception:
            continue
        ids.append(artifact_id)
        ref_by_id[artifact_id] = ref
    if not ids:
        return tuple(observations or ())
    placeholders = ",".join("?" for _ in ids)
    lookup: dict[str, dict[str, object]] = {}
    try:
        with connect_sqlite_readonly(str(db_path)) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='commstat_artifacts'"
            ).fetchone()
            if not exists:
                return tuple(observations or ())
            rows = conn.execute(
                f"""
                SELECT id, from_call, target, report_group, grid, state_code, scope,
                       status_label, alert_color, title, body_text, remarks_text,
                       transport_mode, reach_mode
                FROM commstat_artifacts
                WHERE id IN ({placeholders})
                """,
                ids,
            ).fetchall()
    except Exception:
        return tuple(observations or ())
    for row in rows:
        ref = ref_by_id.get(int(row[0] or 0), "")
        if not ref:
            continue
        lookup[ref] = {
            "from_call": str(row[1] or "").strip(),
            "to_target": str(row[2] or "").strip(),
            "report_group": str(row[3] or "").strip(),
            "grid": str(row[4] or "").strip().upper(),
            "state": str(row[5] or "").strip().upper(),
            "scope": str(row[6] or "").strip(),
            "status": str(row[7] or "").strip(),
            "urgency": str(row[8] or "").strip(),
            "title": str(row[9] or "").strip(),
            "body_text": str(row[10] or "").strip(),
            "remarks_text": str(row[11] or "").strip(),
            "transport_mode": str(row[12] or "").strip(),
            "reach_mode": str(row[13] or "").strip(),
        }
    enriched: list[Observation] = []
    for obs in observations or ():
        artifact = lookup.get(str(obs.source_ref or "").strip())
        if not artifact:
            enriched.append(obs)
            continue
        provenance = dict(obs.provenance or {})
        provenance.update({key: value for key, value in artifact.items() if key not in {"from_call", "to_target", "grid", "state", "status", "urgency"} and value})
        state = str(artifact.get("state") or obs.state or "").strip().upper()
        grid = str(artifact.get("grid") or obs.grid or "").strip().upper()
        resolved_state, state_confidence, geo_confidence = resolve_commstat_reported_for_state(
            state_code=state,
            grid=grid,
            scope=artifact.get("scope"),
            remarks=" ".join(
                str(artifact.get(key) or "")
                for key in ("body_text", "remarks_text", "title")
                if str(artifact.get(key) or "").strip()
            ),
        )
        if resolved_state:
            state = resolved_state
            provenance.setdefault("state_confidence", state_confidence)
            provenance.setdefault("geo_confidence", geo_confidence)
        enriched.append(
            replace(
                obs,
                from_call=str(artifact.get("from_call") or obs.from_call or "").strip(),
                to_target=str(artifact.get("to_target") or obs.to_target or "").strip(),
                state=state,
                grid=grid,
                status=str(artifact.get("status") or obs.status or "").strip(),
                urgency=str(artifact.get("urgency") or obs.urgency or "").strip(),
                provenance=provenance,
            )
        )
    return tuple(enriched)


def build_regional_intelligence(
    observations: Sequence[Observation],
    *,
    sensitivity: str = "active",
    topic_filter: str = "",
    now: dt.datetime | None = None,
    station_index: Mapping[str, Mapping[str, object]] | None = None,
) -> RegionalIntelligenceSnapshot:
    sensitivity_key = _sensitivity_key(sensitivity)
    now_utc = _coerce_utc(now) or dt.datetime.now(dt.timezone.utc)
    topic = str(topic_filter or "").strip()
    evidence = tuple(
        item
        for obs in observations or ()
        for item in _evidence_items_for_observation(
            obs,
            sensitivity=sensitivity_key,
            topic_filter=topic,
            now=now_utc,
            station_index=station_index or {},
        )
    )
    state_rollups = tuple(
        _area_rollup("state", state, items, sensitivity=sensitivity_key)
        for state, items in sorted(_group_by(evidence, "state").items())
        if state
    )
    fema_rollups = tuple(
        _area_rollup("fema_region", region, items, sensitivity=sensitivity_key)
        for region, items in sorted(_group_by(evidence, "fema_region").items())
        if region
    )
    return RegionalIntelligenceSnapshot(
        sensitivity=sensitivity_key,
        topic_filter=topic,
        state_rollups=tuple(sorted(state_rollups, key=_rollup_sort_key)),
        fema_rollups=tuple(sorted(fema_rollups, key=_rollup_sort_key)),
        generated_utc=now_utc.replace(microsecond=0).isoformat(),
    )


def _evidence_items_for_observation(
    obs: Observation,
    *,
    sensitivity: str,
    topic_filter: str,
    now: dt.datetime,
    station_index: Mapping[str, Mapping[str, object]],
) -> tuple[RegionalEvidenceItem, ...]:
    event_time = _parse_time(obs.event_utc or obs.received_utc)
    if event_time is None:
        return ()
    age_hours = max(0.0, (now - event_time).total_seconds() / 3600.0)
    decay = _decay_weight(age_hours, sensitivity=sensitivity)
    if decay <= 0:
        return ()
    source = str(obs.source_family or "").strip().lower()
    evidence_type = _evidence_type_for_source(source)
    reporter = str(obs.from_call or "").strip().upper()
    state = _state_for_observation(obs, station_index)
    if not state:
        return ()
    fema_region = STATE_TO_FEMA_REGION.get(state, "")
    if not fema_region:
        return ()
    topics = tuple(
        topic for topic in _usable_topics(obs.observed_topics) if _topic_has_visible_evidence(obs, topic)
    )
    if topic_filter:
        topics = tuple(t for t in topics if t.lower() == topic_filter.lower())
    if not topics:
        return ()
    confidence = _confidence_for_observation(obs)
    severity_hint = _severity_hint_for_observation(obs)
    status_multiplier = _status_multiplier(obs)
    summary = _summary_for_observation(obs)
    items: list[RegionalEvidenceItem] = []
    for topic in topics:
        base = TOPIC_SEVERITY.get(topic, 1.0)
        score = base * confidence * decay * status_multiplier
        if evidence_type == "signal":
            score = min(score, 0.8 * decay)
        if score <= 0:
            continue
        items.append(
            RegionalEvidenceItem(
                evidence_id=str(obs.observation_id or obs.source_ref or ""),
                source_family=source,
                source_ref=str(obs.source_ref or ""),
                evidence_type=evidence_type,
                topic=topic,
                severity_hint=severity_hint,
                confidence=round(confidence, 3),
                event_time_utc=event_time.replace(microsecond=0).isoformat(),
                age_hours=round(age_hours, 2),
                decay_weight=round(decay, 3),
                reporter_callsign=reporter,
                target=str(obs.to_target or "").strip(),
                state=state,
                fema_region=fema_region,
                summary=summary,
                score=round(score, 3),
            )
        )
    return tuple(items)


def _area_rollup(
    area_type: str,
    area_id: str,
    evidence: Sequence[RegionalEvidenceItem],
    *,
    sensitivity: str,
) -> RegionalAreaRollup:
    items = tuple(evidence)
    reporters = {item.reporter_callsign for item in items if item.reporter_callsign}
    signal_count = sum(1 for item in items if item.evidence_type in {"signal", "path"})
    score = _combined_score(items)
    newest = min((item.age_hours for item in items), default=None)
    topic_rollups = tuple(
        sorted(
            (
                _topic_rollup(topic, topic_items)
                for topic, topic_items in _group_by(items, "topic").items()
            ),
            key=lambda rollup: (-rollup.score, rollup.topic),
        )[:5]
    )
    trend = _trend_for_items(items, sensitivity=sensitivity)
    return RegionalAreaRollup(
        area_type=area_type,
        area_id=area_id,
        label=_area_label(area_type, area_id),
        fema_region=STATE_TO_FEMA_REGION.get(area_id, "") if area_type == "state" else area_id,
        score=round(score, 3),
        level=_level_for_score(score, bool(items)),
        evidence_count=len(items),
        reporter_count=len(reporters),
        signal_count=signal_count,
        newest_age_hours=round(newest, 2) if newest is not None else None,
        trend=trend,
        top_topics=topic_rollups,
        evidence=tuple(sorted(items, key=lambda item: (item.age_hours, -item.score))[:10]),
    )


def _topic_rollup(topic: str, evidence: Sequence[RegionalEvidenceItem]) -> RegionalTopicRollup:
    items = tuple(evidence)
    reporters = {item.reporter_callsign for item in items if item.reporter_callsign}
    score = _combined_score(items)
    newest = min((item.age_hours for item in items), default=None)
    return RegionalTopicRollup(
        topic=topic,
        score=round(score, 3),
        evidence_count=len(items),
        reporter_count=len(reporters),
        newest_age_hours=round(newest, 2) if newest is not None else None,
        level=_level_for_score(score, bool(items)),
    )


def _combined_score(items: Sequence[RegionalEvidenceItem]) -> float:
    if not items:
        return 0.0
    concern_items = tuple(item for item in items if item.severity_hint != "normal")
    if not concern_items:
        return min(1.2, max((item.score for item in items), default=0.0))
    base = sum(item.score for item in concern_items)
    reporters = {item.reporter_callsign for item in concern_items if item.reporter_callsign}
    topics = {item.topic for item in concern_items if item.topic}
    diversity_boost = min(1.8, 1.0 + max(0, len(reporters) - 1) * 0.18)
    topic_boost = min(1.35, 1.0 + max(0, len(topics) - 1) * 0.08)
    return base * diversity_boost * topic_boost


def _trend_for_items(items: Sequence[RegionalEvidenceItem], *, sensitivity: str) -> str:
    current = sum(item.score for item in items if item.age_hours <= 6)
    context_cutoff = 24 if sensitivity == "current" else 72
    context = sum(item.score for item in items if 6 < item.age_hours <= context_cutoff)
    if current >= max(1.0, context * 0.8) and current > 0:
        return "increasing"
    if context >= max(1.0, current * 2.5) and current <= 0.2:
        return "fading"
    return "flat"


def _level_for_score(score: float, has_data: bool) -> str:
    if not has_data:
        return "gray"
    if score < 0.4:
        return "green"
    if score < 1.5:
        return "blue"
    if score < 4.0:
        return "yellow"
    if score < 8.0:
        return "orange"
    return "red"


def _decay_weight(age_hours: float, *, sensitivity: str) -> float:
    windows = SENSITIVITY_WINDOWS.get(_sensitivity_key(sensitivity), SENSITIVITY_WINDOWS["active"])
    for max_hours, weight in windows:
        if age_hours <= max_hours:
            return float(weight)
    return 0.0


def _usable_topics(values: Sequence[object]) -> tuple[str, ...]:
    topics: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        topic = str(value or "").strip()
        if not topic or is_no_report_placeholder(topic):
            continue
        key = topic.lower()
        if key in seen:
            continue
        seen.add(key)
        topics.append(topic)
    return tuple(topics)


def _topic_has_visible_evidence(obs: Observation, topic: str) -> bool:
    topic_name = str(topic or "").strip()
    if not topic_name or is_no_report_placeholder(topic_name):
        return False
    topic_key = topic_name.casefold()
    provenance = obs.provenance if isinstance(obs.provenance, Mapping) else {}
    topic_evidence = provenance.get("topic_evidence")
    if isinstance(topic_evidence, Mapping):
        for key, evidence in topic_evidence.items():
            if str(key or "").strip().casefold() != topic_key:
                continue
            if searchable_text_values(evidence):
                return True
    visible_parts: list[object] = [
        obs.subject,
        obs.summary,
        obs.status,
        obs.urgency,
    ]
    for key in ("body", "text", "raw_text", "search_text", "detail", "message", "form_name"):
        if key in provenance:
            visible_parts.append(provenance.get(key))
    normalized: set[str] = set()
    for value in searchable_text_values(visible_parts):
        normalized.update(term.casefold() for term in normalize_topic_terms(value))
    return topic_key in normalized


def _state_for_observation(
    obs: Observation,
    station_index: Mapping[str, Mapping[str, object]],
) -> str:
    source = str(obs.source_family or "").strip().lower()
    text_state = _state_from_observation_text(obs)
    if source == "commstat" and _commstat_scope_is_report_location(obs) and text_state:
        return text_state
    state = str(obs.state or "").strip().upper()
    if len(state) == 2 and state in STATE_TO_FEMA_REGION:
        return state
    if text_state:
        return text_state
    reporter = str(obs.from_call or "").strip().upper()
    meta = station_index.get(reporter, {}) if reporter else {}
    state = str(meta.get("state") or "").strip().upper() if isinstance(meta, Mapping) else ""
    return state if len(state) == 2 and state in STATE_TO_FEMA_REGION else ""


def _commstat_scope_is_report_location(obs: Observation) -> bool:
    provenance = obs.provenance if isinstance(obs.provenance, Mapping) else {}
    return _commstat_scope_text_is_report_location(provenance.get("scope"))


def _commstat_scope_text_is_report_location(scope_value: object) -> bool:
    scope = re.sub(r"[^a-z0-9]+", " ", str(scope_value or "").lower()).strip()
    return scope not in {"", "my qth", "my location", "1"}



def _state_from_observation_text(obs: Observation) -> str:
    provenance = obs.provenance if isinstance(obs.provenance, Mapping) else {}
    parts: list[object] = [obs.subject, obs.summary, obs.status, obs.urgency, obs.grid]
    for key in (
        "body",
        "body_text",
        "remarks",
        "remarks_text",
        "text",
        "raw_text",
        "search_text",
        "detail",
        "message",
        "title",
        "location",
        "area",
    ):
        if key in provenance:
            parts.append(provenance.get(key))
    text = " ".join(value.strip() for value in searchable_text_values(parts) if value.strip())
    if not text:
        return ""
    if str(obs.source_family or "").strip().lower() == "commstat":
        inferred_state, _, _ = infer_state_and_geo(obs.grid, text)
        if inferred_state and inferred_state in STATE_TO_FEMA_REGION:
            return inferred_state
    upper = text.upper()
    for name, abbr in sorted(US_STATE_ABBR_FROM_NAME.items(), key=lambda item: -len(item[0])):
        for match in re.finditer(rf"\b{re.escape(name)}\b", upper):
            trailing = upper[match.end() : match.end() + 12]
            if re.match(r"\s+(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|LN|LANE|BLVD)\b", trailing):
                continue
            return abbr
    state_values = set(STATE_TO_FEMA_REGION)
    patterns = (
        r"^\s*([A-Z]{2})\s*[:;-]",
        r"\b([A-Z]{2})\s*/\s*[A-R]{2}\d{2}(?:[A-X]{2})?\b",
        r"\b(?:STATE|ST|LOC|LOCATION|AREA)\s*[:=]?\s*([A-Z]{2})\b",
        r"\b[A-Z][A-Z .'-]{2,40}\s+([A-Z]{2})\s+\d{5}(?:-\d{4})?\b",
        r"\b[A-Z][A-Z .'-]{2,40}\s+([A-Z]{2})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, upper):
            abbr = match.group(1).strip().upper()
            if abbr in state_values and _state_abbr_context_is_location(upper, match):
                return abbr
    return ""


def _state_abbr_context_is_location(text: str, match: re.Match[str]) -> bool:
    abbr = str(match.group(1) or "").strip().upper()
    if abbr not in AMBIGUOUS_STATE_WORDS:
        return True
    prefix = text[: match.start(1)]
    words = re.findall(r"[A-Z]+", prefix)
    prev = words[-1] if words else ""
    if prev in NON_LOCATION_PRECEDERS:
        return False
    return True


def _confidence_for_observation(obs: Observation) -> float:
    provenance = obs.provenance if isinstance(obs.provenance, Mapping) else {}
    try:
        confidence = float(provenance.get("confidence", 0.72) or 0.72)
    except Exception:
        confidence = 0.72
    source = str(obs.source_family or "").strip().lower()
    if source in SIGNAL_SOURCES:
        confidence = min(confidence, 0.45)
    if source in PLANNING_SOURCES:
        confidence = min(confidence, 0.15)
    return max(0.0, min(1.0, confidence))


def _evidence_type_for_source(source: str) -> str:
    if source in SIGNAL_SOURCES:
        return "signal"
    if source in PATH_SOURCES:
        return "path"
    if source in PLANNING_SOURCES:
        return "planning"
    if source in {"commstat", "sitrep", "spotter"}:
        return "status"
    return "impact"


def _severity_hint_for_observation(obs: Observation) -> str:
    text = " ".join(
        str(value or "").strip().lower()
        for value in (obs.status, obs.urgency, obs.subject, obs.summary)
        if str(value or "").strip()
    )
    if any(word in text for word in ("emergency", "severe", "red", "evac", "grid down", "contaminated")):
        return "severe"
    if any(word in text for word in ("watch", "warning", "degraded", "yellow", "outage", "wildfire", "hurricane")):
        return "degraded"
    if any(word in text for word in ("green", "normal", "all clear", "functioning")):
        return "normal"
    return "unknown"


def _status_multiplier(obs: Observation) -> float:
    hint = _severity_hint_for_observation(obs)
    if hint == "severe":
        return 1.45
    if hint == "degraded":
        return 1.15
    if hint == "normal":
        return 0.18
    return 1.0


def _summary_for_observation(obs: Observation) -> str:
    text = str(obs.subject or obs.summary or obs.source_ref or "").strip()
    return " ".join(text.split())[:180]


def _group_by(items: Sequence[RegionalEvidenceItem], attr: str) -> dict[str, list[RegionalEvidenceItem]]:
    grouped: dict[str, list[RegionalEvidenceItem]] = {}
    for item in items:
        key = str(getattr(item, attr, "") or "").strip()
        if key:
            grouped.setdefault(key, []).append(item)
    return grouped


def _area_label(area_type: str, area_id: str) -> str:
    if area_type == "fema_region":
        return f"FEMA Region {area_id[1:]}" if area_id.upper().startswith("R") else area_id
    return area_id


def _rollup_sort_key(rollup: RegionalAreaRollup) -> tuple[float, str]:
    return (-rollup.score, rollup.area_id)


def _sensitivity_key(value: object) -> str:
    key = str(value or "active").strip().lower()
    return key if key in SENSITIVITY_WINDOWS else "active"


def _coerce_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _parse_time(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return _coerce_utc(parsed)
