from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from freqinout.core.js8_spotter_decode import (
    parse_spotter_bracket_fields,
    split_spotter_form_text,
    summarize_spotter_form_text,
)
from freqinout.core.js8_spotter_forms import FORM_TOKEN_RE
from freqinout.core.js8_spotter_status import classify_spotter_status
from freqinout.core.commstat_sitrep import resolve_commstat_reported_for_state
from freqinout.core.message_search_values import searchable_text_values
from freqinout.core.commstat_brevity import intelligence_values


TOPIC_TAXONOMY: tuple[str, ...] = (
    "Weather",
    "Fire",
    "Medical",
    "Power",
    "Water",
    "Fuel",
    "Food",
    "Travel/Roads",
    "Comms",
    "Security",
    "Shelter",
    "Logistics",
    "Infrastructure",
    "General Intel",
)


_TOPIC_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "Weather": (
        "weather",
        "wx",
        "wefax",
        "wind",
        "rain",
        "snow",
        "storm",
        "temperature",
        "temp",
        "warning",
        "flood",
        "surge",
        "hurricane",
        "tornado",
    ),
    "Fire": ("fire", "wildfire", "evac", "smoke", "burn", "red flag"),
    "Medical": ("medical", "med", "injur", "hospital", "triage", "ems", "patient", "health"),
    "Power": ("power", "grid down", "outage", "generator", "battery", "electric"),
    "Water": ("water", "potable", "well", "sewer", "sanitation"),
    "Fuel": ("fuel", "gas", "diesel", "propane"),
    "Food": ("food", "supply", "supplies", "grocery", "ration"),
    "Travel/Roads": ("travel", "road", "route", "bridge", "traffic", "closed", "closure"),
    "Comms": ("comms", "communications", "radio", "repeater", "internet", "cell", "telecom"),
    "Security": ("security", "crime", "civil unrest", "threat", "riot", "looting", "violence"),
    "Shelter": ("shelter", "evacuation center", "refuge", "housing"),
    "Logistics": ("logistics", "transport", "delivery", "staging", "resource", "warehouse"),
    "Infrastructure": ("infrastructure", "bridge", "dam", "road", "rail", "utility", "water plant"),
    "General Intel": ("intel", "s2", "s-2", "sitrep", "statrep", "situation", "awareness", "osint"),
}

@dataclass(frozen=True)
class MessageIntelligence:
    source_type: str = ""
    form_name: str = ""
    from_call: str = ""
    to_call: str = ""
    subject: str = ""
    date_summary: str = ""
    state: str = ""
    grid: str = ""
    groups: tuple[str, ...] = ()
    body: str = ""
    topics: tuple[str, ...] = ()
    topic_evidence: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    actionable: bool = False
    operator_attention: bool = False
    routing_candidate: bool = False
    routing_reasons: tuple[str, ...] = ()
    summary: str = ""
    confidence: float = 0.0
    metadata: Mapping[str, str] = field(default_factory=dict)


def normalize_topic_terms(text: object) -> tuple[str, ...]:
    found: list[str] = []
    for text_value in searchable_text_values(text):
        for topic in _topic_evidence_for_text(text_value):
            if topic not in found:
                found.append(topic)
    return tuple(found)


def collect_topic_evidence(parts: Mapping[str, object] | Sequence[tuple[str, object]]) -> Mapping[str, tuple[str, ...]]:
    items = parts.items() if isinstance(parts, Mapping) else parts
    found: dict[str, list[str]] = {}
    for label, value in items:
        label_text = str(label or "text").strip() or "text"
        for text_value in searchable_text_values(value):
            for topic, terms in _topic_evidence_for_text(text_value).items():
                bucket = found.setdefault(topic, [])
                for term in terms:
                    evidence = f"{label_text}:{term}"
                    if evidence not in bucket:
                        bucket.append(evidence)
    return {topic: tuple(values) for topic, values in found.items()}


def _topic_evidence_for_text(text: object) -> Mapping[str, tuple[str, ...]]:
    haystack = f" {str(text or '').lower()} "
    found: dict[str, tuple[str, ...]] = {}
    for topic in TOPIC_TAXONOMY:
        patterns = _TOPIC_PATTERNS.get(topic, ())
        matched = tuple(term for term in patterns if _term_matches(haystack, term))
        if matched:
            found[topic] = matched
    return found


def summarize_intelligence(info: MessageIntelligence) -> str:
    parts: list[str] = []
    if info.form_name:
        parts.append(info.form_name)
    if info.from_call or info.to_call:
        route = " -> ".join(part for part in (info.from_call, info.to_call) if part)
        if route:
            parts.append(route)
    if info.subject:
        parts.append(info.subject)
    if info.date_summary:
        parts.append(info.date_summary)
    summary = " | ".join(part for part in parts if part).strip()
    return summary or info.subject or info.form_name or info.body[:80].strip()


def summarize_spotter_intelligence(info: MessageIntelligence) -> str:
    form = str(info.form_name or "").strip()
    route = " -> ".join(part for part in (info.from_call, info.to_call) if part)
    area = " / ".join(part for part in (info.state, info.grid) if part)
    subject = str(info.subject or "").strip()
    subject = _clean_spotter_summary_subject(subject)
    if not subject:
        subject = _spotter_status_summary(info.body)
    parts = [part for part in (form, route, area, subject, info.date_summary) if part]
    return " | ".join(parts).strip() or summarize_intelligence(info)


def analyze_spotter_text(
    text: object,
    *,
    form_name: object = "",
    from_call: object = "",
    to_call: object = "",
    source_type: str = "spotter",
) -> MessageIntelligence:
    raw = str(text or "").strip()
    fields = parse_spotter_bracket_fields(raw)
    parsed_form_code, response_code, _remainder, _datecode = split_spotter_form_text(raw)
    status_key, status_label, status_evidence = classify_spotter_status(
        parsed_form_code,
        response_code,
    )
    status_summary = status_label if status_key != "unknown" else ""
    subject = _first_nonempty(
        fields.get("NA"),
        fields.get("CM"),
        fields.get("DE"),
        fields.get("AV"),
        fields.get("NE"),
        _spotter_status_summary(raw),
        status_summary,
    )
    state = _clean_state(_first_nonempty(fields.get("ST"), _field_after_label(raw, "State (2-letter code)"), _field_after_label(raw, "State")))
    grid = _clean_grid(_first_nonempty(fields.get("GR"), _field_after_label(raw, "Maidenhead Grid Square"), _field_after_label(raw, "Grid")))
    to_value = _clean_group_or_call(_first_nonempty(to_call, fields.get("TO")))
    from_value = _clean_call(_first_nonempty(from_call, fields.get("FR")))
    date_summary = _form_date_summary(fields.get("DA", ""))
    form_title = str(form_name or "").strip()
    topic_evidence = collect_topic_evidence((("body", raw), ("form", form_title), ("subject", subject)))
    topics = tuple(topic_evidence.keys())
    metadata = {str(k): str(v) for k, v in fields.items()}
    if status_summary:
        metadata.update(
            {
                "operational_status": status_key,
                "operational_status_label": status_label,
                "operational_status_evidence": status_evidence,
            }
        )
    routing_candidate, routing_reasons = _routing_candidate(
        source_type=source_type,
        topics=topics,
        state=state,
        grid=grid,
        groups=_groups_from_values(to_value, raw),
        subject=subject,
        body=raw,
    )
    operator_attention = bool(topics or state or grid or subject)
    info = MessageIntelligence(
        source_type=source_type,
        form_name=form_title or _spotter_form_code(raw),
        from_call=from_value,
        to_call=to_value,
        subject=subject or summarize_spotter_form_text(raw, form_title=form_title),
        date_summary=date_summary,
        state=state,
        grid=grid,
        groups=_groups_from_values(to_value, raw),
        body=raw,
        topics=topics,
        topic_evidence=topic_evidence,
        actionable=operator_attention,
        operator_attention=operator_attention,
        routing_candidate=routing_candidate,
        routing_reasons=routing_reasons,
        confidence=0.82 if fields else 0.45,
        metadata=metadata,
    )
    info = _with_summary(info)
    return MessageIntelligence(
        source_type=info.source_type,
        form_name=info.form_name,
        from_call=info.from_call,
        to_call=info.to_call,
        subject=info.subject,
        date_summary=info.date_summary,
        state=info.state,
        grid=info.grid,
        groups=info.groups,
        body=info.body,
        topics=info.topics,
        topic_evidence=info.topic_evidence,
        actionable=info.actionable,
        operator_attention=info.operator_attention,
        routing_candidate=info.routing_candidate,
        routing_reasons=info.routing_reasons,
        summary=summarize_spotter_intelligence(info),
        confidence=info.confidence,
        metadata=info.metadata,
    )


def analyze_form_text(
    text: object,
    *,
    form_name: object = "",
    source_type: str = "form",
    path: object = None,
    fields: Mapping[str, str] | None = None,
) -> MessageIntelligence:
    raw = str(text or "").strip()
    parsed_fields = {
        str(k or "").strip(): str(v or "").strip()
        for k, v in (fields or {}).items()
        if str(v or "").strip() and not str(k or "").strip().startswith("_")
    }
    from_call = _clean_call(
        _first_nonempty(
            parsed_fields.get("from"),
            _extract_hdr_call(raw, ":hdr_fm:"),
            _field_after_label(raw, "From"),
            _call_from_filename(path),
        )
    )
    to_call = _clean_group_or_call(
        _first_nonempty(
            parsed_fields.get("to"),
            _extract_hdr_call(raw, ":hdr_to:"),
            _field_after_label(raw, "To"),
        )
    )
    subject = _first_nonempty(
        parsed_fields.get("subject"),
        _match_field(raw, r":sub:\s*(.*?)\s*(?=:)"),
        _message_subject_from_body(raw),
        _subject_from_filename(path),
    )
    date_summary = _form_date_summary(
        _first_nonempty(
            parsed_fields.get("date_summary"),
            _field_after_label(raw, "Date/Msg ID"),
            _field_after_label(raw, "Date/Time/Msg ID"),
            _field_after_label(raw, "Date Time Msg ID"),
            _field_after_label(raw, "DTG"),
            _field_after_label(raw, "Date"),
            _match_field(raw, r"\b(\d{6}[-_\sT]?\d{4,6}z?)\b"),
            _match_field(raw, r"\b(\d{8}[-_\sT]?\d{4,6}z?)\b"),
        )
    )
    form_title = str(form_name or parsed_fields.get("form_title", "") or "").strip()
    if not form_title:
        form_title = _infer_form_title(raw, path)
    body = _first_nonempty(parsed_fields.get("body"), _message_body(raw), raw)
    state = _clean_state(
        _first_nonempty(
            parsed_fields.get("state"),
            _field_after_label(raw, "State"),
            _leading_state_from_body(body),
        )
    )
    grid = _clean_grid(_first_nonempty(parsed_fields.get("grid"), _field_after_label(raw, "Grid"), body))
    precedence = _first_nonempty(
        parsed_fields.get("precedence"),
        parsed_fields.get("prec"),
        _field_after_label(raw, "Msg Precedence"),
        _field_after_label(raw, "Precedence"),
        _field_after_label(raw, "Prec"),
    )
    region = _first_nonempty(
        parsed_fields.get("region"),
        parsed_fields.get("scope"),
        _field_after_label(raw, "Region"),
        _field_after_label(raw, "Scope"),
    )
    topic_evidence = collect_topic_evidence(
        (
            ("path", str(path or "")),
            ("form", form_title),
            ("subject", subject),
            ("body", body),
            ("state", state),
            ("grid", grid),
            ("precedence", precedence),
            ("region", region),
        )
    )
    topics = tuple(topic_evidence.keys())
    groups = _groups_from_values(to_call, raw, str(path or ""))
    routing_candidate, routing_reasons = _routing_candidate(
        source_type=source_type,
        topics=topics,
        state=state,
        grid=grid,
        groups=groups,
        subject=subject,
        body=body,
    )
    operator_attention = bool(topics or state or grid or subject)
    info = MessageIntelligence(
        source_type=source_type,
        form_name=form_title,
        from_call=from_call,
        to_call=to_call,
        subject=subject,
        date_summary=date_summary,
        state=state,
        grid=grid,
        groups=groups,
        body=body,
        topics=topics,
        topic_evidence=topic_evidence,
        actionable=operator_attention,
        operator_attention=operator_attention,
        routing_candidate=routing_candidate,
        routing_reasons=routing_reasons,
        confidence=0.72 if (from_call or to_call or subject or form_title) else 0.35,
        metadata={k: v for k, v in {**parsed_fields, "precedence": precedence, "region": region}.items() if v},
    )
    return _with_summary(info)


def analyze_commstat_fields(
    *,
    artifact_kind: object = "",
    title: object = "",
    body: object = "",
    from_call: object = "",
    target: object = "",
    report_group: object = "",
    state: object = "",
    grid: object = "",
    scope: object = "",
    status: object = "",
    alert_color: object = "",
    subtype: object = "",
    remarks: object = "",
    brevity_code: object = "",
    brevity_summary: object = "",
    brevity_models: object = (),
    transport: object = "",
    reach: object = "",
    source_family: object = "CommStat",
    event_utc: object = "",
) -> MessageIntelligence:
    kind = _commstat_kind_label(artifact_kind)
    title_text = _first_nonempty(title, brevity_summary, remarks, body)
    body_text = _first_nonempty(body, remarks, brevity_summary, title)
    status_text = str(status or "").strip().upper()
    alert_text = str(alert_color or "").strip().upper()
    to_value = _clean_commstat_group_or_call(_first_nonempty(target, report_group))
    group_value = _clean_commstat_group_or_call(report_group)
    from_value = _clean_call(from_call)
    state_value = _clean_state(state)
    grid_value = _clean_grid(grid)
    inferred_state_confidence = ""
    inferred_geo_confidence = ""
    resolved_state, inferred_state_confidence, inferred_geo_confidence = resolve_commstat_reported_for_state(
        state_code=state_value,
        grid=grid_value,
        scope=scope,
        remarks=_first_nonempty(body_text, remarks, title_text),
    )
    state_value = _clean_state(resolved_state)
    combined = " ".join(
        part
        for part in (
            kind,
            title_text,
            body_text,
            remarks,
            brevity_code,
            brevity_summary,
            status_text,
            alert_text,
            subtype,
            scope,
            transport,
            reach,
            source_family,
        )
        if str(part or "").strip()
    )
    topic_evidence = collect_topic_evidence(
        (
            ("kind", kind),
            ("title", title_text),
            ("body", body_text),
            ("remarks", remarks),
            ("brevity", " ".join(intelligence_values(brevity_models) if isinstance(brevity_models, (list, tuple)) else [])),
            ("status", status_text),
            ("alert", alert_text),
            ("subtype", subtype),
            ("scope", scope),
            ("transport", transport),
            ("reach", reach),
            ("source", source_family),
        )
    )
    topics = tuple(topic_evidence.keys())
    if kind in {"CommStat Alert", "CommStat StatRep", "CommStat SitRep"} and "General Intel" not in topics:
        topics = tuple([*topics, "General Intel"])
        topic_evidence = {**topic_evidence, "General Intel": (f"kind:{kind}",)}
    elevated = status_text not in {"", "INFO", "READ", "NEW", "GREEN", "OK", "NORMAL"} or alert_text in {
        "RED",
        "YELLOW",
        "ORANGE",
    }
    groups = _commstat_groups_from_values(to_value, group_value, title_text, body_text)
    routing_candidate, routing_reasons = _routing_candidate(
        source_type="commstat",
        topics=topics,
        state=state_value,
        grid=grid_value,
        groups=groups,
        subject=title_text,
        body=body_text,
    )
    operator_attention = bool(elevated or topics or state_value or grid_value or title_text or body_text)
    metadata = {
        "kind": kind,
        "status": status_text,
        "alert": alert_text,
        "scope": str(scope or "").strip(),
        "subtype": str(subtype or "").strip(),
        "transport": str(transport or "").strip(),
        "reach": str(reach or "").strip(),
        "source": str(source_family or "").strip(),
        "state_confidence": inferred_state_confidence,
        "geo_confidence": inferred_geo_confidence,
    }
    info = MessageIntelligence(
        source_type="commstat",
        form_name=kind,
        from_call=from_value,
        to_call=to_value,
        subject=title_text[:120],
        date_summary=_form_date_summary(event_utc),
        state=state_value,
        grid=grid_value,
        groups=groups,
        body=body_text,
        topics=topics,
        topic_evidence=topic_evidence,
        actionable=operator_attention,
        operator_attention=operator_attention,
        routing_candidate=routing_candidate,
        routing_reasons=routing_reasons,
        confidence=0.78 if (from_value or to_value or title_text or body_text) else 0.5,
        metadata={k: v for k, v in metadata.items() if v},
    )
    return _with_summary(info)


def _with_summary(info: MessageIntelligence) -> MessageIntelligence:
    return MessageIntelligence(
        source_type=info.source_type,
        form_name=info.form_name,
        from_call=info.from_call,
        to_call=info.to_call,
        subject=info.subject,
        date_summary=info.date_summary,
        state=info.state,
        grid=info.grid,
        groups=info.groups,
        body=info.body,
        topics=info.topics,
        topic_evidence=info.topic_evidence,
        actionable=info.actionable,
        operator_attention=info.operator_attention,
        routing_candidate=info.routing_candidate,
        routing_reasons=info.routing_reasons,
        summary=summarize_intelligence(info),
        confidence=info.confidence,
        metadata=info.metadata,
    )


def _routing_candidate(
    *,
    source_type: object,
    topics: Sequence[str],
    state: object = "",
    grid: object = "",
    groups: Sequence[str] = (),
    subject: object = "",
    body: object = "",
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    source = str(source_type or "").strip().lower()
    if source in {"spotter", "commstat", "flmsg", "flamp", "bbs", "form"}:
        reasons.append(f"source:{source}")
    if topics:
        reasons.append("topics:" + ",".join(topics))
    if str(grid or "").strip():
        reasons.append("location:grid")
    elif str(state or "").strip():
        reasons.append("location:state")
    if groups:
        reasons.append("groups:" + ",".join(groups))
    if str(subject or "").strip():
        reasons.append("subject")
    if str(body or "").strip():
        reasons.append("body")
    has_source = any(reason.startswith("source:") for reason in reasons)
    has_content = bool(topics or str(subject or "").strip() or str(body or "").strip())
    has_scope = bool(str(grid or "").strip() or str(state or "").strip() or groups)
    return bool(has_source and has_content and has_scope), tuple(reasons)


def _clean_spotter_summary_subject(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^box\s+\d+\s*[-:]\s*", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return ""
    lower = text.lower()
    noisy_prefixes = (
        "to",
        "from",
        "state (2-letter code)",
        "state",
        "maidenhead grid square",
        "grid",
        "date/msg id",
        "date/time/msg id",
        "date",
    )
    if any(lower == prefix or lower.startswith(f"{prefix}:") for prefix in noisy_prefixes):
        return ""
    return re.sub(r"\s+", " ", text)


def _spotter_status_summary(text: object) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""
    status_blocks = (
        ("Current Operational Status", "overall"),
        ("Power", "power"),
        ("Water", "water"),
        ("Medical", "medical"),
        ("Communications", "communications"),
        ("Internet", "internet"),
        ("Travel", "travel"),
        ("Food", "food"),
        ("Fuel", "fuel"),
        ("Crime", "crime"),
        ("Civil Unrest", "civil unrest"),
        ("Political", "political"),
    )
    values: list[tuple[str, str, str]] = []
    for label, name in status_blocks:
        value = _block_after_label(raw, label)
        if value:
            state = _condition_state(value)
            if state:
                values.append((name, state, value))
    non_green = [(name, state) for name, state, _value in values if state not in {"green", "ok", "normal"}]
    if non_green:
        return "Non-green: " + ", ".join(f"{name} {state.upper()}" for name, state in non_green[:4])
    if values and all(state in {"green", "ok", "normal"} for _name, state, _value in values):
        return "Green / No significant issues"
    if re.search(r"\b(no significant issues|operational steady|all clear|green)\b", raw, flags=re.IGNORECASE):
        return "Green / No significant issues"
    return ""


def _condition_state(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if re.search(r"\b(red|critical|severe|emergency)\b", text):
        return "red"
    if re.search(r"\b(yellow|warning|watch|degraded|limited|shortage|outage|offline|down)\b", text):
        return "yellow"
    if re.search(r"\b(green|normal|ok|okay|steady|all clear|no significant)\b", text):
        return "green"
    return ""


def _commstat_kind_label(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"ALERT", "ALERTS"}:
        return "CommStat Alert"
    if text in {"STATREP", "STAT", "STATUS"}:
        return "CommStat StatRep"
    if text in {"SITREP", "SIT"}:
        return "CommStat SitRep"
    if text in {"CHECKIN", "CHECK-IN", "CHECK_IN"}:
        return "CommStat Check-In"
    if text in {"MESSAGE", "MSG", ""}:
        return "CommStat Message"
    return f"CommStat {text.title()}"


def _term_matches(haystack: str, term: str) -> bool:
    needle = str(term or "").strip().lower()
    if not needle:
        return False
    if re.fullmatch(r"[a-z0-9+-]{1,4}", needle):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack))
    return needle in haystack


def _first_nonempty(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return re.sub(r"\s+", " ", text)
    return ""


def _clean_call(value: object) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"\b[A-Z]{1,2}\d[A-Z0-9]{1,5}(?:/[A-Z0-9]{1,4})?\b", text)
    return match.group(0) if match else text


def _clean_group_or_call(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("@"):
        return re.sub(r"[^@A-Z0-9_-]+", "", text)
    return _clean_call(text)


def _clean_commstat_group_or_call(value: object) -> str:
    text = _clean_group_or_call(value)
    return text[1:] if text.startswith("@") else text


def _clean_state(value: object) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"\b[A-Z]{2}\b", text)
    return match.group(0) if match else ""


def _clean_grid(value: object) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"\b[A-R]{2}[0-9]{2}(?:[A-X]{2})?\b", text)
    return match.group(0) if match else ""


def _groups_from_values(*values: object) -> tuple[str, ...]:
    groups: list[str] = []
    for value in values:
        for found in re.findall(r"@[A-Z0-9_-]{2,}", str(value or "").upper()):
            if found not in groups:
                groups.append(found)
        for found in re.findall(r"\b(MAGNET|GHOSTNET|AMRRON|MR[0-9A-Z]{2,}|S2(?:UNDERGROUND)?)\b", str(value or "").upper()):
            clean = found if found.startswith("@") else f"@{found}"
            if clean not in groups:
                groups.append(clean)
    return tuple(groups)


def _commstat_groups_from_values(*values: object) -> tuple[str, ...]:
    return tuple(group[1:] if group.startswith("@") else group for group in _groups_from_values(*values))


def _spotter_form_code(text: str) -> str:
    match = FORM_TOKEN_RE.search(text or "")
    return match.group(0).upper() if match else ""


def _form_date_summary(value: object) -> str:
    txt = str(value or "").strip()
    if not txt:
        return ""
    match = re.search(r"\b(\d{6})[-_\sT]?(\d{4,6})z?\b", txt, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}-{match.group(2)[:4]}z"
    match = re.search(r"\b(\d{8})[-_\sT]?(\d{4,6})z?\b", txt, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}-{match.group(2)[:4]}z"
    return txt[:24]


def _extract_hdr_call(text: str, marker: str) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if line.lower().startswith(str(marker or "").lower()):
            for next_line in lines[idx + 1 :]:
                value = _clean_group_or_call(next_line)
                if value:
                    return value
            break
    return ""


def _match_field(text: str, pattern: str) -> str:
    match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1).strip()) if match else ""


def _field_after_label(text: str, label: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines()]
    label_key = str(label or "").strip().lower()
    for idx, line in enumerate(lines):
        inline = re.match(rf"^{re.escape(label_key)}\s*:\s*(.+)$", line, flags=re.IGNORECASE)
        if inline:
            return inline.group(1).strip()
        if line.lower().rstrip(":") == label_key:
            for next_line in lines[idx + 1 :]:
                if next_line:
                    return next_line
    return ""


def _block_after_label(text: str, label: str) -> str:
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    label_key = str(label or "").strip().lower()
    start = -1
    for idx, line in enumerate(lines):
        inline = re.match(rf"^{re.escape(label_key)}\s*:\s*(.+)$", line.strip(), flags=re.IGNORECASE)
        if inline:
            return re.sub(r"\s+", " ", inline.group(1).strip())
        if line.strip().lower().rstrip(":") == label_key:
            start = idx + 1
            break
    if start < 0:
        return ""
    collected: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if collected and _looks_like_form_label(stripped):
            break
        if stripped or collected:
            collected.append(stripped)
    return re.sub(r"\s+", " ", "\n".join(collected).strip())


def _looks_like_form_label(value: str) -> bool:
    text = str(value or "").strip().lower().rstrip(":")
    if not text or len(text) > 48:
        return False
    return text in {
        "date/msg id",
        "date msg id",
        "date/time/msg id",
        "date time msg id",
        "dtg",
        "date",
        "to",
        "from",
        "msg precedence",
        "precedence",
        "region",
        "subject",
        "message",
        "remarks",
        "comments",
        "incident",
        "state",
        "grid",
    }


def _message_subject_from_body(text: str) -> str:
    return _first_nonempty(_field_after_label(text, "Subject"), _field_after_label(text, "Incident"))


def _message_body(text: str) -> str:
    for label in ("Message", "Comments", "Remarks", "Body"):
        value = _block_after_label(text, label)
        if value:
            return value
    return ""


def _leading_state_from_body(text: object) -> str:
    value = str(text or "").strip()
    match = re.match(r"^([A-Z]{2})\s*(?:[-:|/]\s*)", value, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _infer_form_title(text: str, path: object = None) -> str:
    for line in str(text or "").splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean and ("form" in clean.lower() or "sitrep" in clean.lower() or "statrep" in clean.lower()):
            return clean[:80]
    return _subject_from_filename(path)


def _call_from_filename(path: object) -> str:
    try:
        stem = Path(str(path)).stem
    except Exception:
        stem = str(path or "")
    for token in re.split(r"[-_\s]+", stem):
        call = _clean_call(token)
        if call:
            return call
    return ""


def _subject_from_filename(path: object) -> str:
    try:
        stem = Path(str(path)).stem
    except Exception:
        stem = str(path or "")
    if not stem:
        return ""
    tokens = [token for token in re.split(r"[-_]", stem) if token]
    if len(tokens) <= 1:
        return stem
    start = 0
    for idx, token in enumerate(tokens):
        if re.fullmatch(r"\d{6,8}", token) or re.fullmatch(r"\d{4,6}z", token, flags=re.IGNORECASE):
            start = idx + 1
            if idx + 1 < len(tokens) and re.fullmatch(r"\d{3,6}z", tokens[idx + 1], flags=re.IGNORECASE):
                start = idx + 2
            break
    subject = " ".join(tokens[start:] or tokens[-1:]).strip()
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", subject)
