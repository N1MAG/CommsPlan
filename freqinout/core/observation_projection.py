from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from freqinout.core.condition_alerts import ConditionAlertMatch
from freqinout.core.location_confidence import location_evidence_from_values, location_evidence_to_provenance
from freqinout.core.message_intelligence import MessageIntelligence


CONFIRMED_STATES = {"CONFIRMED", "FIRST_HAND", "SECOND_HAND"}


@dataclass(frozen=True)
class ObservationEligibility:
    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


@dataclass(frozen=True)
class Observation:
    observation_id: str
    source_family: str
    source_ref: str
    source_radio_id: int | None = None
    source_app: str = ""
    received_utc: str = ""
    event_utc: str = ""
    from_call: str = ""
    to_target: str = ""
    groups: tuple[str, ...] = ()
    observed_topics: tuple[str, ...] = ()
    operator_attention: bool = False
    status: str = ""
    urgency: str = ""
    subject: str = ""
    summary: str = ""
    state: str = ""
    grid: str = ""
    lat: float | None = None
    lon: float | None = None
    location_confidence: str = "unknown"
    auth_state: str = ""
    trusted_state: str = ""
    confirmed_state: str = ""
    exercise_flag: bool = False
    route_eligible: bool = False
    publish_authorized: bool = False
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def provenance_json(self) -> str:
        return json.dumps(dict(self.provenance or {}), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def as_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["provenance_json"] = self.provenance_json
        return record


def observation_from_message_intelligence(
    info: MessageIntelligence,
    *,
    source_ref: str,
    source_family: str | None = None,
    source_radio_id: int | None = None,
    source_app: str = "",
    received_utc: str = "",
    event_utc: str = "",
    status: str = "",
    urgency: str = "",
    auth_state: str = "",
    trusted_state: str = "",
    exercise_flag: bool = False,
    extra_provenance: Mapping[str, Any] | None = None,
) -> Observation:
    source = _clean(source_family or info.source_type or "message")
    ref = _clean(source_ref)
    provenance = {
        "source_type": info.source_type,
        "source_ref": ref,
        "form_name": info.form_name,
        "confidence": info.confidence,
        "routing_candidate": info.routing_candidate,
        "routing_reasons": list(info.routing_reasons),
    }
    location_evidence = location_evidence_from_values(
        grid=info.grid,
        state=info.state,
        location_kind=_message_location_kind(source),
        source_family=source,
        source_ref=ref,
        observed_utc=_clean(received_utc or event_utc or info.date_summary),
        explanation=_message_location_explanation(source),
    )
    provenance["location_confidence_record"] = location_evidence_to_provenance(location_evidence)
    if extra_provenance:
        provenance.update(dict(extra_provenance))
    return Observation(
        observation_id=_observation_id(source, ref),
        source_family=source,
        source_ref=ref,
        source_radio_id=source_radio_id,
        source_app=_clean(source_app),
        received_utc=_clean(received_utc),
        event_utc=_clean(event_utc or info.date_summary),
        from_call=_clean_call(info.from_call),
        to_target=_clean_commstat_target(info.to_call) if source.lower() == "commstat" else _clean_target(info.to_call),
        groups=_dedupe(
            _clean_commstat_target(value) if source.lower() == "commstat" else _clean_target(value)
            for value in info.groups
        ),
        observed_topics=_dedupe(info.topics),
        operator_attention=bool(info.operator_attention),
        status=_clean(status),
        urgency=_clean(urgency),
        subject=_clean(info.subject),
        summary=_clean(info.summary),
        state=_clean_state(info.state),
        grid=_clean_grid(info.grid),
        location_confidence=_location_confidence(grid=info.grid, state=info.state),
        auth_state=_clean(auth_state),
        trusted_state=_clean(trusted_state),
        confirmed_state="",
        exercise_flag=bool(exercise_flag),
        route_eligible=False,
        publish_authorized=False,
        provenance=provenance,
    )


def observation_from_local_report(report: Mapping[str, Any]) -> Observation:
    source = _clean(report.get("source_kind") or "local_report")
    report_id = _clean(report.get("id"))
    ref = _clean(report.get("raw_reference")) or f"local_operator_reports:{report_id or 'unknown'}"
    topics = _json_tuple(report.get("topics_json")) or _tuple_values(report.get("topics"))
    provenance = {
        "source_table": "local_operator_reports",
        "source_ref": ref,
        "report_id": report_id,
        "created_by": _clean(report.get("created_by")),
        "updated_by": _clean(report.get("updated_by")),
    }
    return Observation(
        observation_id=_observation_id("local_report", ref),
        source_family="local_report",
        source_ref=ref,
        source_radio_id=_int_or_none(report.get("source_radio_id")),
        source_app=_clean(report.get("source_app")),
        received_utc=_clean(report.get("created_utc")),
        event_utc=_clean(report.get("created_utc")),
        from_call=_clean_call(report.get("callsign")),
        to_target=_clean_target(report.get("source_channel") or report.get("net_session_id")),
        groups=(),
        observed_topics=_dedupe(topics),
        operator_attention=bool(topics or _clean(report.get("subject")) or _clean(report.get("body"))),
        status=_clean(report.get("status")),
        subject=_clean(report.get("subject")),
        summary=_local_report_summary(report),
        state=_clean_state(report.get("state")),
        grid=_clean_grid(report.get("grid")),
        lat=_float_or_none(report.get("lat")),
        lon=_float_or_none(report.get("lon")),
        location_confidence=_clean(report.get("location_confidence")) or _location_confidence(
            lat=report.get("lat"),
            lon=report.get("lon"),
            grid=report.get("grid"),
            state=report.get("state"),
        ),
        confirmed_state=_clean(report.get("confirmed_state")).upper() or "UNCONFIRMED",
        exercise_flag=bool(report.get("exercise_flag")),
        route_eligible=False,
        publish_authorized=False,
        provenance=provenance,
    )


def observation_from_condition_alert_match(match: ConditionAlertMatch) -> Observation:
    provenance = {
        "source_type": "condition_alert",
        "source_ref": match.source_ref,
        "rule_id": match.rule_id,
        "rule_name": match.rule_name,
        "condition_level": match.condition_level,
        "action": match.action,
        "scope": match.scope,
        "matched_text": match.matched_text,
        **dict(match.provenance or {}),
    }
    group = _clean_condition_group(match.operating_group)
    groups = _dedupe([group, *(_clean_condition_group(value) for value in match.groups)])
    subject = f"{match.rule_name or 'Condition Alert'}: Level {match.condition_level}"
    summary = f"{match.operating_group or match.to_target}: condition level {match.condition_level}"
    return Observation(
        observation_id=_observation_id("condition_alert", f"{match.rule_id}:{match.source_ref}:{match.condition_level}"),
        source_family="condition_alert",
        source_ref=match.source_ref,
        source_radio_id=match.source_radio_id,
        source_app=_clean(match.source_app),
        received_utc=_clean(match.received_utc),
        event_utc=_clean(match.received_utc),
        from_call=_clean_call(match.from_call),
        to_target=_clean_target(match.to_target),
        groups=groups,
        observed_topics=("General Intel", "Comms"),
        operator_attention=True,
        status="CONDITION ALERT",
        urgency=f"LEVEL {match.condition_level}",
        subject=subject,
        summary=summary,
        auth_state="",
        trusted_state="",
        route_eligible=False,
        publish_authorized=False,
        provenance=provenance,
    )


def observation_from_rf_pin(pin: Mapping[str, Any]) -> Observation:
    """Project a manually curated RF/map pin into the shared observation model.

    RF pins are intentionally inert: they can inform the map and operator review,
    but they do not authorize BBS routing or external publication by themselves.
    """
    ref = (
        _clean(pin.get("raw_reference"))
        or _clean(pin.get("source_ref"))
        or _clean(pin.get("pin_id"))
        or _clean(pin.get("id"))
    )
    label = _clean(pin.get("label") or pin.get("title") or pin.get("subject"))
    if not ref:
        ref = f"rf_pin:{label or _clean(pin.get('grid')) or 'unknown'}"
    topics = _json_tuple(pin.get("topics_json")) or _tuple_values(pin.get("topics"))
    target = _clean_target(pin.get("to_target") or pin.get("target") or pin.get("group"))
    groups = _dedupe(
        _clean_target(value)
        for value in (
            *(_json_tuple(pin.get("groups_json")) or _tuple_values(pin.get("groups"))),
            pin.get("group"),
            pin.get("target_group"),
        )
    )
    now = _clean(pin.get("created_utc") or pin.get("received_utc") or utc_now_iso())
    provenance = {
        "source_type": "rf_pin",
        "source_ref": ref,
        "pin_kind": _clean(pin.get("pin_kind") or pin.get("kind")),
        "pin_type": _pin_type_from_values(
            pin.get("pin_type") or pin.get("type") or pin.get("pin_kind") or pin.get("kind"),
            topics,
        ),
        "created_by": _clean(pin.get("created_by")),
        "expires_utc": _clean(pin.get("expires_utc")),
    }
    location_evidence = location_evidence_from_values(
        lat=pin.get("lat"),
        lon=pin.get("lon"),
        grid=pin.get("grid"),
        state=pin.get("state"),
        location_kind=_clean(pin.get("location_kind") or pin.get("location_confidence")),
        source_family="rf_pin",
        source_ref=ref,
        observed_utc=now,
        stale_after=_clean(pin.get("expires_utc")),
        explanation="Operational pin location",
    )
    provenance["location_confidence_record"] = location_evidence_to_provenance(location_evidence)
    return Observation(
        observation_id=_observation_id("rf_pin", ref),
        source_family="rf_pin",
        source_ref=ref,
        source_radio_id=_int_or_none(pin.get("source_radio_id")),
        source_app=_clean(pin.get("source_app")),
        received_utc=now,
        event_utc=_clean(pin.get("event_utc") or now),
        from_call=_clean_call(pin.get("callsign") or pin.get("from_call")),
        to_target=target,
        groups=groups,
        observed_topics=_dedupe(topics),
        operator_attention=True,
        status=_clean(pin.get("status") or "PIN"),
        urgency=_clean(pin.get("urgency")),
        subject=label,
        summary=_clean(pin.get("summary") or label or "RF map pin"),
        state=_clean_state(pin.get("state")),
        grid=_clean_grid(pin.get("grid")),
        lat=_float_or_none(pin.get("lat")),
        lon=_float_or_none(pin.get("lon")),
        location_confidence=_clean(pin.get("location_confidence")) or _location_confidence(
            lat=pin.get("lat"),
            lon=pin.get("lon"),
            grid=pin.get("grid"),
            state=pin.get("state"),
        ),
        confirmed_state=_clean(pin.get("confirmed_state")),
        exercise_flag=bool(pin.get("exercise_flag")),
        route_eligible=False,
        publish_authorized=False,
        provenance=provenance,
    )


def _message_location_kind(source_family: str) -> str:
    normalized = _clean(source_family).lower()
    if normalized in {"spotter", "fiospotter", "js8spotter", "commstat"}:
        return "structured_report"
    if normalized in {"meshcore", "meshtastic", "aprs"}:
        return "structured_report"
    return ""


def _message_location_explanation(source_family: str) -> str:
    normalized = _clean(source_family).lower()
    if normalized in {"spotter", "fiospotter", "js8spotter"}:
        return "Spotter report grid or state"
    if normalized == "commstat":
        return "CommStat reported location"
    if normalized in {"meshcore", "meshtastic"}:
        return "Mesh message location"
    if normalized == "aprs":
        return "APRS packet location"
    return "Message-derived location"


def _pin_type_from_values(pin_type: object, topics: Sequence[object]) -> str:
    text = _clean(pin_type).lower().replace(" ", "_").replace("-", "_")
    if text:
        return text
    topic_map = {
        "Fire": "hazard",
        "Weather": "weather",
        "Medical": "medical",
        "Comms": "comms",
        "Infrastructure": "comms",
        "Shelter": "shelter",
        "Travel/Roads": "road",
        "Food": "supply",
        "Water": "supply",
        "Fuel": "supply",
        "Logistics": "supply",
        "Security": "checkpoint",
    }
    for topic in topics:
        mapped = topic_map.get(_clean(topic))
        if mapped:
            return mapped
    return "info"


def _clean_condition_group(value: object) -> str:
    text = _clean_target(value).lstrip("@")
    return text.rstrip(">")


def explain_map_eligibility(
    observation: Observation,
    *,
    layer_enabled: bool,
    allow_unconfirmed_local: bool = False,
    exercise_layer: bool = False,
) -> ObservationEligibility:
    reasons: list[str] = []
    if not layer_enabled:
        reasons.append("layer disabled")
    if observation.exercise_flag and not exercise_layer:
        reasons.append("exercise/test report excluded from operational layer")
    if observation.source_family == "local_report":
        confirmed = observation.confirmed_state.upper() in CONFIRMED_STATES
        if not confirmed and not allow_unconfirmed_local:
            reasons.append("local report is unconfirmed")
    received_pin_unreviewed = (
        observation.source_family == "rf_pin"
        and bool(observation.provenance.get("received_pin"))
        and observation.status.upper() != "PIN"
    )
    if received_pin_unreviewed:
        reasons.append("received pin is pending review or dismissed")
    if observation.lat is not None and observation.lon is not None:
        reasons.append("location:explicit")
    elif observation.grid:
        reasons.append("location:grid")
    elif observation.state:
        reasons.append("state-only rollup; no point marker")
    else:
        reasons.append("no mappable location")
    allowed = (
        bool(layer_enabled)
        and not received_pin_unreviewed
        and not (observation.exercise_flag and not exercise_layer)
        and (observation.source_family != "local_report" or observation.confirmed_state.upper() in CONFIRMED_STATES or allow_unconfirmed_local)
        and (observation.lat is not None and observation.lon is not None or bool(observation.grid))
    )
    return ObservationEligibility(allowed=allowed, reasons=tuple(reasons))


def explain_bbs_eligibility(
    observation: Observation,
    *,
    rule_enabled: bool = False,
    dry_run_reviewed: bool = False,
    destination_scope: str = "",
) -> ObservationEligibility:
    reasons: list[str] = []
    if not rule_enabled:
        reasons.append("no enabled rule")
    if not destination_scope:
        reasons.append("no destination scope")
    if not dry_run_reviewed:
        reasons.append("dry-run preview not reviewed")
    if observation.exercise_flag:
        reasons.append("exercise/test requires exercise destination")
    if not observation.source_ref:
        reasons.append("missing source reference")
    allowed = bool(rule_enabled and dry_run_reviewed and destination_scope and observation.source_ref and not observation.exercise_flag)
    if allowed:
        reasons.append("explicit rule authorized")
    return ObservationEligibility(allowed=allowed, reasons=tuple(reasons))


def _observation_id(source_family: str, source_ref: str) -> str:
    return f"{_clean(source_family).lower()}:{_clean(source_ref) or 'unknown'}"


def _location_confidence(*, lat: object = None, lon: object = None, grid: object = "", state: object = "") -> str:
    if _float_or_none(lat) is not None and _float_or_none(lon) is not None:
        return "explicit-latlon"
    if _clean_grid(grid):
        return "grid"
    if _clean_state(state):
        return "state"
    return "unknown"


def _local_report_summary(report: Mapping[str, Any]) -> str:
    parts = (
        _clean(report.get("subject")),
        _clean(report.get("status")).upper(),
        _clean_call(report.get("callsign")),
        " ".join(part for part in (_clean(report.get("city")), _clean_state(report.get("state"))) if part),
    )
    return " | ".join(part for part in parts if part)


def _json_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except Exception:
            return ()
        return _tuple_values(loaded)
    return ()


def _tuple_values(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        return (_clean(values),) if _clean(values) else ()
    if isinstance(values, Sequence):
        return _dedupe(_clean(value) for value in values)
    return ()


def _dedupe(values: Sequence[object]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = _clean(value)
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _clean_call(value: object) -> str:
    return _clean(value).upper()


def _clean_target(value: object) -> str:
    text = _clean(value).upper()
    if text and not text.startswith("@") and len(text) <= 6 and not any(ch.isdigit() for ch in text):
        return f"@{text}"
    return text


def _clean_commstat_target(value: object) -> str:
    text = _clean(value).upper()
    return text[1:] if text.startswith("@") else text


def _clean_state(value: object) -> str:
    text = _clean(value).upper()
    return text if len(text) == 2 and text.isalpha() else ""


def _clean_grid(value: object) -> str:
    text = _clean(value).upper()
    if 4 <= len(text) <= 8 and text[:2].isalpha() and text[2:4].isdigit():
        return text
    return ""


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except Exception:
        return None


def _float_or_none(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
