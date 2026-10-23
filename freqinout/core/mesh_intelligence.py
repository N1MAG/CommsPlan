"""Bounded, read-only structured intelligence for policy-approved mesh text.

This module deliberately does not assign transport identity, route eligibility,
or publication authority.  It only decodes a small set of explicitly supported
payloads after the mesh policy layer has granted topic scanning.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from freqinout.core.commstat_brevity import analyze_brevity_codes, cached_catalogs, code_candidates
from freqinout.core.commstat_sitrep import parse_commstat_message
from freqinout.core.js8_spotter_decode import parse_spotter_bracket_fields, split_spotter_form_text
from freqinout.core.message_intelligence import analyze_commstat_fields, analyze_spotter_text


MESH_ANALYSIS_MAX_BYTES = 1024
SUPPORTED_MESH_SPOTTER_FORM = "F!701C"
SUPPORTED_COMMSTAT_SUBTYPES = frozenset({"COMMSTAT_12", "COMMSTAT_FWD", "SPOTTER_301", "SPOTTER_304"})
KNOWN_MESH_SPOTTER_FORMS = frozenset({
    "F!100", "F!101", "F!102", "F!103", "F!104", "F!105", "F!106", "F!107", "F!108",
    "F!300", "F!301", "F!302", "F!303", "F!304", "F!305", "F!306", "F!307", "F!308",
    "F!500", "F!504", "F!505", "F!700", "F!701", "F!701A", "F!701C", "F!702", "F!702A",
    "F!703", "F!720", "F!BDN",
})
HELD_MESH_SPOTTER_FORM = "F!701B"
_WRAPPER = "JS8MESH:"


@dataclass(frozen=True)
class MeshStructuredIntelligence:
    """JSON-safe result of bounded mesh payload analysis."""

    recognized: bool = False
    format: str = ""
    wrapper_present: bool = False
    analysis_truncated: bool = False
    analysis_bytes: int = 0
    form_code: str = ""
    claimed_author: str = ""
    claimed_target: str = ""
    subject: str = ""
    summary: str = ""
    state: str = ""
    grid: str = ""
    status: str = ""
    status_label: str = ""
    scope: str = ""
    topics: tuple[str, ...] = ()
    topic_evidence: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    brevity_code: str = ""
    brevity_summary: str = ""
    brevity_models: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return deterministic, JSON-safe metadata for observation provenance."""
        value = asdict(self)
        value["topics"] = list(self.topics)
        value["topic_evidence"] = {
            str(key): list(values) for key, values in self.topic_evidence.items()
        }
        value["brevity_models"] = [dict(model) for model in self.brevity_models]
        value["metadata"] = _json_safe(self.metadata)
        return _json_safe(value)


def analyze_mesh_message(text: object, *, asset_dir: str | Path | None = None) -> MeshStructuredIntelligence:
    """Analyze one mesh body without changing or retaining the raw body.

    A wrapper is accepted only when it is the first non-whitespace literal.
    Nested wrappers, unknown forms, and malformed supported forms produce an
    unrecognized result with no structured fields.
    """
    raw = str(text or "")
    candidate = raw.lstrip()
    wrapper_present = candidate.startswith(_WRAPPER)
    if wrapper_present:
        candidate = candidate[len(_WRAPPER) :].lstrip()
        if candidate.startswith(_WRAPPER):
            return _result(wrapper_present=True, reason="nested_wrapper")
    bounded, truncated = _bounded_utf8(candidate, MESH_ANALYSIS_MAX_BYTES)
    analysis_bytes = len(bounded.encode("utf-8"))
    if not bounded:
        return _result(wrapper_present=wrapper_present, analysis_truncated=truncated,
                       analysis_bytes=analysis_bytes, reason="empty")
    if truncated:
        return _result(wrapper_present=wrapper_present, analysis_truncated=True,
                       analysis_bytes=analysis_bytes, reason="analysis_truncated")

    form_code, response, _remainder, _datecode = split_spotter_form_text(bounded)
    normalized_form = form_code.upper()
    if normalized_form == SUPPORTED_MESH_SPOTTER_FORM:
        # 701C receipts may contain the required operational-status digit
        # alone or the optional follow-up choices as additional digits.
        if not re.fullmatch(r"[1-3][0-2]{0,2}", response):
            return _result(wrapper_present=wrapper_present, analysis_truncated=truncated,
                           analysis_bytes=analysis_bytes, reason="malformed_701c")
        info = analyze_spotter_text(bounded, source_type="mesh")
        fields = parse_spotter_bracket_fields(bounded)
        return _from_spotter(
            info,
            fields,
            form_code=normalized_form,
            wrapper_present=wrapper_present,
            analysis_truncated=truncated,
            analysis_bytes=analysis_bytes,
        )
    parsed = parse_commstat_message(
        bounded,
        target_hint="",
        source_value="mesh",
        global_id=0,
        asset_dir=Path(asset_dir) if asset_dir is not None else None,
    )
    if parsed is not None:
        subtype = str(parsed.get("subtype") or "").strip().upper()
        if subtype in SUPPORTED_COMMSTAT_SUBTYPES:
            return _from_commstat(
                bounded,
                parsed,
                wrapper_present=wrapper_present,
                analysis_truncated=truncated,
                analysis_bytes=analysis_bytes,
            )
        return _result(wrapper_present=wrapper_present, analysis_truncated=truncated,
                       analysis_bytes=analysis_bytes, reason="unsupported_commstat")

    if normalized_form == HELD_MESH_SPOTTER_FORM:
        return _result(wrapper_present=wrapper_present, analysis_truncated=truncated,
                       analysis_bytes=analysis_bytes, form_code=normalized_form,
                       reason="held_form")
    if normalized_form.startswith("F!") and normalized_form in KNOWN_MESH_SPOTTER_FORMS:
        info = analyze_spotter_text(bounded, source_type="mesh")
        fields = parse_spotter_bracket_fields(bounded)
        return _from_spotter(
            info,
            fields,
            form_code=normalized_form,
            wrapper_present=wrapper_present,
            analysis_truncated=truncated,
            analysis_bytes=analysis_bytes,
        )
    if normalized_form.startswith("F!"):
        return _result(wrapper_present=wrapper_present, analysis_truncated=truncated,
                       analysis_bytes=analysis_bytes, form_code=normalized_form,
                       reason="unknown_form")

    candidates = code_candidates(bounded)
    if candidates:
        records = analyze_brevity_codes(bounded, cached_catalogs(asset_dir) if asset_dir else {})
        if records:
            record = records[-1]
            return MeshStructuredIntelligence(
                recognized=True,
                format="brevity",
                wrapper_present=wrapper_present,
                analysis_truncated=truncated,
                analysis_bytes=analysis_bytes,
                brevity_code=str(record.get("code") or ""),
                brevity_summary=str(record.get("summary") or ""),
                brevity_models=tuple(_json_mapping(item) for item in records),
                metadata={"definition_status": str(record.get("status") or "definition_unavailable")},
            )
    return _result(wrapper_present=wrapper_present, analysis_truncated=truncated,
                   analysis_bytes=analysis_bytes, reason="unknown_payload")


def _from_spotter(info: Any, fields: Mapping[str, str], *, form_code: str, wrapper_present: bool,
                  analysis_truncated: bool, analysis_bytes: int) -> MeshStructuredIntelligence:
    metadata = {str(key): str(value) for key, value in (info.metadata or {}).items() if value}
    status = metadata.get("operational_status", "")
    return MeshStructuredIntelligence(
        recognized=True,
        format="mcform701c" if form_code == SUPPORTED_MESH_SPOTTER_FORM else "spotter",
        wrapper_present=wrapper_present,
        analysis_truncated=analysis_truncated,
        analysis_bytes=analysis_bytes,
        form_code=form_code,
        claimed_author=str(fields.get("FR") or "").strip(),
        claimed_target=str(fields.get("TO") or "").strip(),
        subject=str(info.subject or "").strip(),
        summary=str(info.summary or info.subject or "").strip(),
        state=str(info.state or "").strip().upper(),
        grid=str(info.grid or "").strip().upper(),
        status=status,
        status_label=metadata.get("operational_status_label", ""),
        topics=tuple(str(item) for item in info.topics if str(item).strip()),
        topic_evidence={str(key): tuple(str(item) for item in value) for key, value in (info.topic_evidence or {}).items()},
        metadata=metadata,
    )


def _from_commstat(body: str, parsed: Mapping[str, Any], *, wrapper_present: bool,
                   analysis_truncated: bool, analysis_bytes: int) -> MeshStructuredIntelligence:
    metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), Mapping) else {}
    status_payload = parsed.get("status_payload") if isinstance(parsed.get("status_payload"), Mapping) else {}
    subtype = str(parsed.get("subtype") or "").strip().upper()
    remarks = str(metadata.get("remarks_text") or "").strip()
    info = analyze_commstat_fields(
        artifact_kind=subtype,
        title=remarks,
        body=body,
        from_call="",
        target="",
        report_group="",
        state=metadata.get("state_code", ""),
        grid=parsed.get("grid", ""),
        scope=parsed.get("scope", ""),
        status=status_payload.get("status", status_payload.get("responses", "")),
        subtype=subtype,
        remarks=remarks,
        brevity_code=metadata.get("brevity_code", ""),
        brevity_summary=metadata.get("brevity_summary", ""),
        brevity_models=metadata.get("brevity_models", ()),
        transport="mesh",
        reach=metadata.get("reach_mode", ""),
        source_family="mesh",
    )
    fields = parse_spotter_bracket_fields(body)
    claimed_author = str(fields.get("FR") or _explicit_claimed_author(body)).strip()
    safe_metadata = {str(key): _json_safe(value) for key, value in metadata.items()
                     if key not in {"brevity_asset_dir"}}
    safe_metadata["subtype"] = subtype
    return MeshStructuredIntelligence(
        recognized=True,
        format="commstat",
        wrapper_present=wrapper_present,
        analysis_truncated=analysis_truncated,
        analysis_bytes=analysis_bytes,
        form_code=subtype,
        claimed_author=claimed_author,
        claimed_target=str(fields.get("TO") or "").strip(),
        subject=str(info.subject or "").strip(),
        summary=str(info.summary or info.subject or "").strip(),
        state=str(info.state or "").strip().upper(),
        grid=str(info.grid or parsed.get("grid") or "").strip().upper(),
        status=str(status_payload.get("status") or status_payload.get("responses") or "").strip(),
        status_label=str(info.metadata.get("status") or "").strip(),
        scope=str(parsed.get("scope") or "").strip(),
        topics=tuple(str(item) for item in info.topics if str(item).strip()),
        topic_evidence={str(key): tuple(str(item) for item in value) for key, value in (info.topic_evidence or {}).items()},
        brevity_code=str(metadata.get("brevity_code") or "").strip(),
        brevity_summary=str(metadata.get("brevity_summary") or "").strip(),
        brevity_models=tuple(_json_mapping(item) for item in (metadata.get("brevity_models") or ()) if isinstance(item, Mapping)),
        metadata=safe_metadata,
    )


def _bounded_utf8(value: str, maximum: int) -> tuple[str, bool]:
    # Encoding only a bounded prefix avoids a second full-size allocation for
    # corrupt/imported oversized bodies. Truncated input never yields semantics.
    oversized = len(value) > maximum
    encoded = value[:maximum + 1].encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return value, oversized
    return encoded[:maximum].decode("utf-8", errors="ignore"), True


def _explicit_claimed_author(value: object) -> str:
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9/_-]{2,15})\s*:", str(value or ""))
    return str(match.group(1) or "").strip().upper() if match else ""


def _result(*, wrapper_present: bool, analysis_truncated: bool = False,
            analysis_bytes: int = 0, form_code: str = "", reason: str) -> MeshStructuredIntelligence:
    return MeshStructuredIntelligence(
        wrapper_present=wrapper_present,
        analysis_truncated=analysis_truncated,
        analysis_bytes=analysis_bytes,
        form_code=form_code,
        reason=reason,
    )


def _json_mapping(value: object) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_safe(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)
