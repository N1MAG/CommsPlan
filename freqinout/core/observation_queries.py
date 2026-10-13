from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from freqinout.core.controlfreq_awareness import (
    AwarenessPin,
    AwarenessSnapshot,
    build_awareness_snapshot,
    is_awareness_traffic_observation,
)
from freqinout.core.observation_projection import (
    Observation,
    ObservationEligibility,
    explain_bbs_eligibility,
    explain_map_eligibility,
)
from freqinout.core.observation_store import list_observations


@dataclass(frozen=True)
class ObservationQuery:
    source_family: str = ""
    source_families: tuple[str, ...] = ()
    topic: str = ""
    from_call: str = ""
    to_target: str = ""
    operating_group: str = ""
    search_text: str = ""
    status: str = ""
    state: str = ""
    grid: str = ""
    since_utc: str = ""
    limit: int = 200


@dataclass(frozen=True)
class ObservationViewRow:
    observation: Observation
    map_eligibility: ObservationEligibility | None = None
    bbs_eligibility: ObservationEligibility | None = None

    @property
    def summary_line(self) -> str:
        parts = [
            self.observation.source_family,
            self.observation.status,
            " -> ".join(
                part
                for part in (self.observation.from_call, self.observation.to_target)
                if part
            ),
            self.observation.subject or self.observation.summary,
        ]
        return " | ".join(part for part in parts if part)


@dataclass(frozen=True)
class OperationalActivitySnapshot:
    latest: tuple[Observation, ...] = ()
    high_attention: tuple[Observation, ...] = ()
    condition_alerts: tuple[Observation, ...] = ()
    topics: tuple[str, ...] = ()


def query_observations(
    db_path: str | Path,
    query: ObservationQuery | None = None,
    *,
    readonly: bool = False,
) -> tuple[Observation, ...]:
    q = query or ObservationQuery()
    source_families = tuple(
        _normalize_source_family(source)
        for source in (q.source_families or ())
        if _normalize_source_family(source)
    )
    if source_families:
        rows: list[Observation] = []
        per_source_limit = max(1, int(q.limit or 200))
        for source_family in source_families:
            rows.extend(
                list_observations(
                    db_path,
                    source_family=source_family,
                    from_call=q.from_call,
                    to_target=q.to_target,
                    topic=q.topic,
                    status=q.status,
                    state=q.state,
                    grid=q.grid,
                    since_utc=q.since_utc,
                    limit=per_source_limit,
                    readonly=readonly,
                )
            )
    else:
        rows = list_observations(
            db_path,
            source_family=q.source_family,
            from_call=q.from_call,
            to_target=q.to_target,
            topic=q.topic,
            status=q.status,
            state=q.state,
            grid=q.grid,
            since_utc=q.since_utc,
            limit=q.limit,
            readonly=readonly,
        )
    group = _normalize_group(q.operating_group)
    search_text = str(q.search_text or "").strip()
    filtered = [
        observation
        for observation in rows
        if (not source_families or _normalize_source_family(observation.source_family) in source_families)
        and (not group or _observation_matches_group(observation, group))
        and _observation_matches_search(observation, search_text)
    ]
    filtered.sort(key=_observation_sort_key, reverse=True)
    return tuple(filtered[: max(1, int(q.limit or 200))])


def matching_observation_callsigns(
    db_path: str | Path,
    query: ObservationQuery | None = None,
    *,
    readonly: bool = False,
) -> frozenset[str]:
    """Return station callsigns represented by a shared observation query."""
    calls: set[str] = set()
    for observation in query_observations(db_path, query, readonly=readonly):
        for value in (observation.from_call,):
            call = _normalize_callsign(value)
            if not call:
                continue
            calls.add(call)
            base = _base_callsign(call)
            if base:
                calls.add(base)
    return frozenset(calls)


def map_observation_rows(
    db_path: str | Path,
    query: ObservationQuery | None = None,
    *,
    layer_enabled: bool,
    allow_unconfirmed_local: bool = False,
    exercise_layer: bool = False,
    readonly: bool = False,
) -> tuple[ObservationViewRow, ...]:
    return tuple(
        ObservationViewRow(
            observation=observation,
            map_eligibility=explain_map_eligibility(
                observation,
                layer_enabled=layer_enabled,
                allow_unconfirmed_local=allow_unconfirmed_local,
                exercise_layer=exercise_layer,
            ),
        )
        for observation in query_observations(db_path, query, readonly=readonly)
    )


def bbs_observation_rows(
    db_path: str | Path,
    query: ObservationQuery | None = None,
    *,
    rule_enabled: bool = False,
    dry_run_reviewed: bool = False,
    destination_scope: str = "",
) -> tuple[ObservationViewRow, ...]:
    return tuple(
        ObservationViewRow(
            observation=observation,
            bbs_eligibility=explain_bbs_eligibility(
                observation,
                rule_enabled=rule_enabled,
                dry_run_reviewed=dry_run_reviewed,
                destination_scope=destination_scope,
            ),
        )
        for observation in query_observations(db_path, query)
    )


def eligible_map_observations(rows: Sequence[ObservationViewRow]) -> tuple[Observation, ...]:
    return tuple(
        row.observation
        for row in rows
        if row.map_eligibility is not None and row.map_eligibility.allowed
    )


def operational_activity_snapshot(
    db_path: str | Path,
    query: ObservationQuery | None = None,
    *,
    operating_group: str = "",
    limit: int = 50,
) -> OperationalActivitySnapshot:
    """Return a compact, UI-ready activity snapshot from projected observations."""
    q = query or ObservationQuery(limit=limit)
    q = ObservationQuery(
        source_family=q.source_family,
        source_families=q.source_families,
        topic=q.topic,
        from_call=q.from_call,
        to_target=q.to_target,
        operating_group=q.operating_group or operating_group,
        search_text=q.search_text,
        status=q.status,
        state=q.state,
        grid=q.grid,
        since_utc=q.since_utc,
        limit=max(limit, q.limit or limit),
    )
    group = _normalize_group(operating_group)
    observations = tuple(
        observation
        for observation in query_observations(db_path, q)
        if not group or _observation_matches_group(observation, group)
    )[: max(1, int(limit or 50))]
    return activity_snapshot_from_observations(observations)


def activity_snapshot_from_observations(
    observations: Sequence[Observation],
) -> OperationalActivitySnapshot:
    """Summarize an already-scoped observation sequence for the legacy Activity header."""
    traffic_observations = tuple(
        observation for observation in observations if is_awareness_traffic_observation(observation)
    )
    high_attention = tuple(observation for observation in traffic_observations if observation.operator_attention)
    condition_alerts = tuple(
        observation
        for observation in traffic_observations
        if str(observation.source_family or "").strip().lower() == "condition_alert"
    )
    topics = tuple(
        sorted(
            {
                str(topic or "").strip()
                for observation in traffic_observations
                for topic in observation.observed_topics
                if str(topic or "").strip()
            }
        )
    )
    return OperationalActivitySnapshot(
        latest=traffic_observations,
        high_attention=high_attention,
        condition_alerts=condition_alerts,
        topics=topics,
    )


def operational_awareness_snapshot(
    db_path: str | Path,
    query: ObservationQuery | None = None,
    *,
    operating_group: str = "",
    local_callsign: str = "",
    active_groups: Sequence[str] = (),
    next_groups: Sequence[str] = (),
    pins: Sequence[AwarenessPin | dict[str, object]] = (),
    limit: int = 80,
    visible_attention_limit: int = 7,
) -> AwarenessSnapshot:
    """Return the ControlFreq awareness projection for the current traffic scope."""
    q = query or ObservationQuery(limit=limit)
    q = ObservationQuery(
        source_family=q.source_family,
        source_families=q.source_families,
        topic=q.topic,
        from_call=q.from_call,
        to_target=q.to_target,
        operating_group=q.operating_group or operating_group,
        search_text=q.search_text,
        status=q.status,
        state=q.state,
        grid=q.grid,
        since_utc=q.since_utc,
        limit=max(limit, q.limit or limit),
    )
    group = _normalize_group(operating_group)
    scoped_observations = tuple(
        observation
        for observation in query_observations(db_path, q)
        if not group or _observation_matches_group(observation, group)
    )[: max(1, int(limit or 80))]
    effective_active_groups = tuple(active_groups) or ((group,) if group else ())
    return build_awareness_snapshot(
        scoped_observations,
        local_callsign=local_callsign,
        active_groups=effective_active_groups,
        next_groups=next_groups,
        pins=pins,
        visible_attention_limit=visible_attention_limit,
    )


def _normalize_group(value: str) -> str:
    return str(value or "").strip().upper().lstrip("@")


def _normalize_source_family(value: object) -> str:
    source = str(value or "").strip().lower()
    aliases = {
        "js8": "js8call",
        "js8spotter": "spotter",
        "spotter_traffic": "spotter",
        "local": "local_report",
        "local_report": "local_report",
        "planning_pin": "rf_pin",
        "rf_pins": "rf_pin",
    }
    return aliases.get(source, source)


def _observation_matches_group(observation: Observation, group: str) -> bool:
    candidates = [
        observation.to_target,
        *observation.groups,
    ]
    return any(_normalize_group(candidate) == group for candidate in candidates)


def _normalize_callsign(value: object) -> str:
    call = str(value or "").strip().upper().lstrip("@").rstrip(">")
    if not call or call in {"ALL", "ANY", "ANYNET", "UNASSIGNED"}:
        return ""
    return call


def _base_callsign(value: object) -> str:
    call = _normalize_callsign(value)
    if not call:
        return ""
    for separator in (">", "/", "-", " "):
        if separator in call:
            call = call.split(separator, 1)[0]
    return call.strip().upper()


def _observation_matches_search(observation: Observation, search_text: str) -> bool:
    query = str(search_text or "").strip().lower()
    if not query:
        return True
    provenance = observation.provenance if isinstance(observation.provenance, dict) else {}
    values = [
        observation.source_family,
        observation.source_app,
        observation.from_call,
        observation.to_target,
        *observation.groups,
        *observation.observed_topics,
        observation.status,
        observation.urgency,
        observation.subject,
        observation.summary,
        observation.state,
        observation.grid,
        provenance.get("form_name", ""),
        provenance.get("form_id", ""),
        provenance.get("message_type", ""),
        provenance.get("search_text", ""),
    ]
    return query in " ".join(str(value or "") for value in values).lower()


def _observation_sort_key(observation: Observation) -> tuple[str, str]:
    return (
        str(observation.event_utc or observation.received_utc or ""),
        str(observation.observation_id or ""),
    )
