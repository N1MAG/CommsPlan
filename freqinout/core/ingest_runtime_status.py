from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence

from freqinout.core.config_paths import get_config_dir
from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.core.ingest_health import IngestHealthSnapshot, IngestSourceHealth, build_ingest_health_snapshot
from freqinout.core.ingest_source_model import (
    IngestSourceInventory,
    build_ingest_source_inventory,
    js8_api_endpoint_collisions,
)
from freqinout.core.logger import log
from freqinout.core.multi_rig_runtime_status import SCOPE_ALL_ACTIVE_RUNTIME, build_multi_rig_runtime_status
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.commstat_sitrep import commstat_reach_label, commstat_transport_label
from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry
from freqinout.core.varac_ingest import load_latest_varac_sync_status


SOURCE_FRESH_SEC = 10 * 60
SOURCE_STALE_SEC = 30 * 60


@dataclass(frozen=True)
class RuntimeIngestStatusRow:
    source_id: str
    label: str
    family: str
    source_type: str
    radio_id: str = ""
    app_instance_id: str = ""
    status: str = "unknown"
    status_label: str = "Unknown"
    detail: str = ""
    location: str = ""
    last_activity_label: str = ""
    projection_count: int = 0
    components: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeSourceViewRow:
    source_id: str
    title: str
    source_kind: str
    state: str
    state_label: str
    severity: str = "info"
    detail: str = ""
    action_hint: str = ""
    location: str = ""
    radio_id: str = ""
    app_instance_id: str = ""
    last_activity_label: str = ""
    projection_count: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeSourceViewSummary:
    total: int = 0
    ready: int = 0
    missing: int = 0
    shared_endpoint: int = 0
    backoff: int = 0
    quiet: int = 0
    idle: int = 0
    stale: int = 0
    unknown: int = 0
    needs_attention: int = 0
    warning: int = 0
    info: int = 0
    ok: int = 0
    headline: str = "No runtime ingest sources configured"


def active_runtime_ingest_inventory(store: MultiRadioStore | None = None) -> IngestSourceInventory:
    active_store = store or MultiRadioStore()
    try:
        runtime_status = build_multi_rig_runtime_status(active_store)
        if runtime_status.background_ingest_scope != SCOPE_ALL_ACTIVE_RUNTIME:
            return IngestSourceInventory()
        return build_ingest_source_inventory([dict(row) for row in active_store.list_runtime_active_device_profiles()])
    except Exception as exc:
        log.debug("Ingest runtime status: failed building source inventory: %s", exc)
        return IngestSourceInventory()


def active_runtime_ingest_health_snapshot(
    *,
    store: MultiRadioStore | None = None,
    db_path: str | Path | None = None,
) -> IngestHealthSnapshot:
    inventory = active_runtime_ingest_inventory(store)
    target_db = Path(db_path) if db_path is not None else get_config_dir() / "config" / "freqinout_nets.db"
    return build_ingest_health_snapshot(
        inventory,
        db_path=target_db,
        health_registry_snapshot=get_dependency_health_registry().snapshot(),
    )


def active_runtime_ingest_status_rows(
    *,
    store: MultiRadioStore | None = None,
    db_path: str | Path | None = None,
) -> tuple[RuntimeIngestStatusRow, ...]:
    inventory = active_runtime_ingest_inventory(store)
    target_db = Path(db_path) if db_path is not None else get_config_dir() / "config" / "freqinout_nets.db"
    snapshot = build_ingest_health_snapshot(
        inventory,
        db_path=target_db,
        health_registry_snapshot=get_dependency_health_registry().snapshot(),
    )
    rows = ingest_status_rows(snapshot)
    rows = enrich_ingest_status_rows_with_projection_counts(rows, db_path=target_db)
    rows = enrich_ingest_status_rows_with_js8_endpoint_collisions(rows, inventory)
    rows = enrich_ingest_status_rows_with_js8_registry(
        rows,
        JS8ApiClientRegistry.status_dicts(),
        generated_ts=float(snapshot.generated_ts or time.time()),
    )
    return enrich_ingest_status_rows_with_varac_sync(
        rows,
        load_latest_varac_sync_status(db_path=target_db),
        generated_ts=float(snapshot.generated_ts or time.time()),
    )


def active_runtime_source_view_rows(
    *,
    store: MultiRadioStore | None = None,
    db_path: str | Path | None = None,
) -> tuple[RuntimeSourceViewRow, ...]:
    return runtime_source_view_rows(active_runtime_ingest_status_rows(store=store, db_path=db_path))


def runtime_source_view_rows(rows: tuple[RuntimeIngestStatusRow, ...]) -> tuple[RuntimeSourceViewRow, ...]:
    view_rows = [_runtime_source_view_row(row) for row in rows]
    return tuple(sorted(view_rows, key=lambda row: (_view_state_sort_key(row.state), row.title.lower(), row.source_kind.lower())))


def summarize_runtime_source_view_rows(rows: Sequence[RuntimeSourceViewRow]) -> RuntimeSourceViewSummary:
    clean_rows = tuple(row for row in rows if isinstance(row, RuntimeSourceViewRow))
    counts_by_state: dict[str, int] = {}
    counts_by_severity: dict[str, int] = {}
    for row in clean_rows:
        state = str(row.state or "").strip().lower()
        severity = str(row.severity or "").strip().lower()
        counts_by_state[state] = counts_by_state.get(state, 0) + 1
        counts_by_severity[severity] = counts_by_severity.get(severity, 0) + 1
    total = len(clean_rows)
    warning = counts_by_severity.get("warning", 0)
    ready = counts_by_state.get("ready", 0)
    idle = counts_by_state.get("idle", 0)
    backoff = counts_by_state.get("backoff", 0)
    quiet = counts_by_state.get("quiet", 0)
    stale = counts_by_state.get("stale", 0)
    if total <= 0:
        headline = "No runtime ingest sources configured"
    elif warning:
        noun = "source" if warning == 1 else "sources"
        verb = "needs" if warning == 1 else "need"
        headline = f"{warning} {noun} {verb} attention"
    elif backoff:
        headline = f"{backoff} source{'s' if backoff != 1 else ''} waiting to retry"
    elif stale:
        headline = f"{stale} source{'s' if stale != 1 else ''} stale"
    elif quiet and not ready:
        headline = f"{quiet} source{'s' if quiet != 1 else ''} quiet"
    elif idle and not ready:
        headline = f"{idle} source{'s' if idle != 1 else ''} idle"
    else:
        headline = f"{ready} source{'s' if ready != 1 else ''} ready"
        if quiet:
            headline += f", {quiet} quiet"
        if idle:
            headline += f", {idle} idle"
    return RuntimeSourceViewSummary(
        total=total,
        ready=ready,
        missing=counts_by_state.get("missing", 0),
        shared_endpoint=counts_by_state.get("shared_endpoint", 0),
        backoff=backoff,
        quiet=quiet,
        idle=idle,
        stale=stale,
        unknown=counts_by_state.get("unknown", 0),
        needs_attention=counts_by_state.get("needs_attention", 0),
        warning=warning,
        info=counts_by_severity.get("info", 0),
        ok=counts_by_severity.get("ok", 0),
        headline=headline,
    )


def runtime_source_view_rows_from_skip_reasons(source_skips: Mapping[str, object]) -> tuple[RuntimeSourceViewRow, ...]:
    rows: list[RuntimeIngestStatusRow] = []
    for health_key, raw_row in dict(source_skips or {}).items():
        if not isinstance(raw_row, Mapping):
            continue
        reason = str(raw_row.get("reason", "") or "").strip().lower().replace("-", "_")
        status = "degraded" if reason in {"missing", "missing_path", "unreadable"} else "ok"
        skipped_at_ts = _float_object(raw_row.get("skipped_at_ts"))
        rows.append(
            RuntimeIngestStatusRow(
                source_id=str(raw_row.get("source_id", "") or health_key),
                label=str(raw_row.get("label", "") or raw_row.get("source_id", "") or "Ingest source"),
                family=str(raw_row.get("family", "") or ""),
                source_type=str(raw_row.get("source_type", "") or ""),
                radio_id=str(raw_row.get("radio_id", "") or ""),
                app_instance_id=str(raw_row.get("app_instance_id", "") or ""),
                status=status,
                status_label="Needs attention" if status == "degraded" else "Observed",
                detail=_source_skip_detail(raw_row),
                location=str(raw_row.get("path", "") or raw_row.get("endpoint", "") or ""),
                last_activity_label=_relative_activity_label(skipped_at_ts, generated_ts=time.time())
                if skipped_at_ts > 0
                else "",
                metadata={**dict(raw_row), "skip_reason": reason},
            )
        )
    return runtime_source_view_rows(tuple(rows))


def _runtime_source_view_row(row: RuntimeIngestStatusRow) -> RuntimeSourceViewRow:
    metadata = dict(row.metadata or {})
    state, state_label, severity, action_hint = _runtime_source_state(row, metadata)
    return RuntimeSourceViewRow(
        source_id=row.source_id,
        title=_runtime_source_title(row),
        source_kind=_runtime_source_kind(row),
        state=state,
        state_label=state_label,
        severity=severity,
        detail=_operator_detail(row.detail, metadata=metadata),
        action_hint=action_hint,
        location=str(row.location or ""),
        radio_id=str(row.radio_id or ""),
        app_instance_id=str(row.app_instance_id or ""),
        last_activity_label=str(row.last_activity_label or ""),
        projection_count=int(row.projection_count or 0),
        metadata=metadata,
    )


def _runtime_source_state(row: RuntimeIngestStatusRow, metadata: Mapping[str, object]) -> tuple[str, str, str, str]:
    skip_reason = str(metadata.get("skip_reason", "") or "").strip().lower()
    if skip_reason in {"backoff", "cooldown"}:
        return ("backoff", "Backoff", "info", "Waiting before retry")
    if skip_reason in {"missing", "missing_path"}:
        return ("missing", "Missing", "warning", "Check the configured path or app setting")
    if skip_reason == "unreadable":
        return ("needs_attention", "Needs Attention", "warning", "Check source permissions")
    if metadata.get("endpoint_collision_labels"):
        return ("shared_endpoint", "Shared Endpoint", "warning", "Give each JS8Call instance a unique TCP port")
    if row.status == "missing":
        return ("missing", "Missing", "warning", "Check the configured path or app setting")
    detail_lc = str(row.detail or "").lower()
    if "source path missing" in detail_lc or "path missing" in detail_lc:
        return ("missing", "Missing", "warning", "Check the configured path or app setting")
    if row.family == "js8call" and row.source_type == "api":
        api_status = metadata.get("api_status", {})
        if isinstance(api_status, Mapping):
            if bool(api_status.get("connected", False)):
                return ("ready", "Ready", "ok", "")
            if bool(api_status.get("running", False)):
                return ("needs_attention", "Needs Attention", "warning", "Check JS8Call TCP API connectivity")
        if row.status == "ok":
            return ("idle", "Idle", "info", "Client will connect when needed")
    if row.status == "ok":
        freshness = str(metadata.get("freshness_state", "") or "").strip().lower()
        if freshness == "stale":
            return ("stale", "Stale", "info", "Source is quiet or has not refreshed recently")
        if freshness == "quiet":
            return ("quiet", "Quiet", "info", "Source has been quiet but is still within the expected refresh window")
        if freshness == "unknown":
            return ("unknown", "Unknown", "info", "Waiting for first source check")
        return ("ready", "Ready", "ok", "")
    if row.status == "degraded":
        return ("needs_attention", "Needs Attention", "warning", "Review this source")
    return ("idle", "Idle", "info", "")


def enrich_ingest_status_rows_with_js8_endpoint_collisions(
    rows: tuple[RuntimeIngestStatusRow, ...],
    inventory: IngestSourceInventory,
) -> tuple[RuntimeIngestStatusRow, ...]:
    collisions = js8_api_endpoint_collisions(inventory)
    if not collisions:
        return rows
    out: list[RuntimeIngestStatusRow] = []
    for row in rows:
        if row.family != "js8call" or row.source_type != "api":
            out.append(row)
            continue
        endpoint = _endpoint_key(row.location)
        labels = collisions.get(endpoint)
        if not labels:
            out.append(row)
            continue
        metadata = dict(row.metadata or {})
        metadata["endpoint_collision_labels"] = tuple(labels)
        detail = _append_detail_text(
            row.detail,
            "Endpoint shared by " + ", ".join(labels),
        )
        out.append(
            replace(
                row,
                status="degraded",
                status_label="Needs attention",
                detail=detail,
                metadata=metadata,
            )
        )
    return tuple(out)


def ingest_status_rows(snapshot: IngestHealthSnapshot) -> tuple[RuntimeIngestStatusRow, ...]:
    rows = [_status_row_for_source(source, generated_ts=float(snapshot.generated_ts or time.time())) for source in snapshot.sources]
    return tuple(sorted(rows, key=lambda row: (_family_sort_key(row.family), row.radio_id, row.label.lower())))


def enrich_ingest_status_rows_with_projection_counts(
    rows: tuple[RuntimeIngestStatusRow, ...],
    *,
    db_path: str | Path,
) -> tuple[RuntimeIngestStatusRow, ...]:
    counts = _projection_counts_by_source(db_path)
    commstat_summary = _commstat_projection_summary(db_path)
    js8_link_summaries = _js8_link_projection_summaries(db_path)
    if not counts:
        counts = {}
    out: list[RuntimeIngestStatusRow] = []
    for row in rows:
        count_keys = [row.source_id]
        if row.app_instance_id:
            count_keys.append(row.app_instance_id)
        projection_count = max((counts.get(key, 0) for key in count_keys), default=0)
        metadata = dict(row.metadata or {})
        if row.family == "commstat" and commstat_summary:
            projection_count = max(projection_count, int(commstat_summary.get("total", 0) or 0))
            metadata["projection_summary"] = commstat_summary
        link_summary = _summary_for_keys(js8_link_summaries, count_keys) if row.family == "js8call" else {}
        link_count = int(link_summary.get("total", 0) or 0)
        if link_summary:
            metadata["link_projection_summary"] = link_summary
        if projection_count <= 0:
            if link_count > 0:
                out.append(
                    replace(
                        row,
                        detail=_append_detail_count(row.detail, link_count, singular="projected link", plural="projected links"),
                        metadata=metadata,
                    )
                )
            else:
                out.append(row)
            continue
        detail = row.detail
        if row.family in {"flmsg", "flamp", "js8call", "varac"}:
            noun = "message" if projection_count == 1 else "messages"
            detail = f"{detail}; {projection_count} cached {noun}" if detail else f"{projection_count} cached {noun}"
        elif row.family == "commstat":
            noun = "artifact" if projection_count == 1 else "artifacts"
            detail = f"{detail}; {projection_count} projected {noun}" if detail else f"{projection_count} projected {noun}"
        if link_count > 0:
            detail = _append_detail_count(detail, link_count, singular="projected link", plural="projected links")
        out.append(replace(row, projection_count=projection_count, detail=detail, metadata=metadata))
    return tuple(out)


def enrich_ingest_status_rows_with_varac_sync(
    rows: tuple[RuntimeIngestStatusRow, ...],
    sync_status_by_source: dict[str, dict[str, object]],
    *,
    generated_ts: float,
) -> tuple[RuntimeIngestStatusRow, ...]:
    if not sync_status_by_source:
        return rows
    out: list[RuntimeIngestStatusRow] = []
    for row in rows:
        if row.family != "varac":
            out.append(row)
            continue
        status = sync_status_by_source.get(row.source_id) or sync_status_by_source.get("legacy")
        if not status:
            out.append(row)
            continue
        success = bool(int(status.get("success", 0) or 0))
        rows_scanned = int(status.get("rows_scanned", 0) or 0)
        rows_written = int(status.get("rows_written", 0) or 0)
        error_text = str(status.get("error_text", "") or "").strip()
        finished_ts = _float_object(status.get("run_finished_ts") or status.get("run_started_ts"))
        detail = f"Last sync scanned {rows_scanned}, wrote {rows_written}"
        if error_text:
            detail = error_text
        out.append(
            replace(
                row,
                status="ok" if success and row.status != "degraded" else row.status,
                status_label="Ready" if success and row.status != "degraded" else row.status_label,
                detail=detail,
                last_activity_label=_relative_activity_label(finished_ts, generated_ts=generated_ts),
            )
        )
    return tuple(out)


def enrich_ingest_status_rows_with_js8_registry(
    rows: tuple[RuntimeIngestStatusRow, ...],
    registry_status_rows: object,
    *,
    generated_ts: float,
) -> tuple[RuntimeIngestStatusRow, ...]:
    status_by_endpoint = {
        _js8_registry_endpoint_key(row): row
        for row in registry_status_rows or ()
        if _js8_registry_endpoint_key(row)
    }
    if not status_by_endpoint:
        return rows
    out: list[RuntimeIngestStatusRow] = []
    for row in rows:
        if row.family != "js8call" or row.source_type != "api":
            out.append(row)
            continue
        status = status_by_endpoint.get(_endpoint_key(row.location))
        if not status:
            out.append(row)
            continue
        metadata = dict(row.metadata or {})
        status_dict = _mapping_for_status(status)
        metadata["api_status"] = status_dict
        connected = bool(status_dict.get("connected", False))
        running = bool(status_dict.get("running", False))
        last_error = str(status_dict.get("last_error", "") or "").strip()
        last_activity_ts = max(
            _float_object(status_dict.get("last_message_ts")),
            _float_object(status_dict.get("last_connected_ts")),
        )
        if connected:
            detail = "Shared JS8 API client connected"
            out.append(
                replace(
                    row,
                    status="ok",
                    status_label="Ready",
                    detail=detail,
                    last_activity_label=_relative_activity_label(last_activity_ts, generated_ts=generated_ts),
                    metadata=metadata,
                )
            )
        elif running:
            detail = last_error or "Shared JS8 API client is reconnecting"
            out.append(
                replace(
                    row,
                    status="degraded",
                    status_label="Needs attention",
                    detail=detail,
                    last_activity_label=_relative_activity_label(last_activity_ts, generated_ts=generated_ts),
                    metadata=metadata,
                )
            )
        else:
            detail = "API configured; shared client idle"
            out.append(replace(row, detail=detail, metadata=metadata))
    return tuple(out)


def _status_row_for_source(source: IngestSourceHealth, *, generated_ts: float) -> RuntimeIngestStatusRow:
    if source.degraded:
        status = "degraded"
        status_label = "Needs attention"
    elif source.exists:
        status = "ok"
        status_label = "Ready"
    else:
        status = "missing"
        status_label = "Missing"
    activity_ts = max(float(source.last_success_ts or 0.0), float(source.last_checked_ts or 0.0))
    freshness = _source_freshness_metadata(
        source,
        activity_ts=activity_ts,
        generated_ts=generated_ts,
    )
    detail_parts: list[str] = []
    if source.last_error:
        detail_parts.append(source.last_error)
    if source.components:
        degraded_components = [component.name for component in source.components if component.degraded]
        if degraded_components:
            detail_parts.append("degraded: " + ", ".join(degraded_components))
    if not detail_parts:
        detail_parts.append(_default_detail_for_source(source))
    return RuntimeIngestStatusRow(
        source_id=source.source_id,
        label=source.label,
        family=source.family,
        source_type=source.source_type,
        radio_id=source.radio_id,
        app_instance_id=source.app_instance_id,
        status=status,
        status_label=status_label,
        detail="; ".join(part for part in detail_parts if part),
        location=source.path or source.endpoint,
        last_activity_label=_relative_activity_label(activity_ts, generated_ts=generated_ts),
        components=tuple(component.name for component in source.components),
        metadata={**dict(source.metadata or {}), **freshness},
    )


def _source_freshness_metadata(
    source: IngestSourceHealth,
    *,
    activity_ts: float,
    generated_ts: float,
) -> dict[str, object]:
    if activity_ts <= 0:
        return {
            "freshness_state": "unknown",
            "freshness_label": "No source check yet",
            "freshness_age_sec": 0.0,
        }
    age = max(0.0, float(generated_ts or 0.0) - float(activity_ts or 0.0))
    if source.source_type == "api":
        fresh_sec = 120.0
        stale_sec = 5 * 60.0
    else:
        fresh_sec = SOURCE_FRESH_SEC
        stale_sec = SOURCE_STALE_SEC
    if age <= fresh_sec:
        state = "fresh"
        label = "Fresh"
    elif age >= stale_sec:
        state = "stale"
        label = "Stale"
    else:
        state = "quiet"
        label = "Quiet"
    return {
        "freshness_state": state,
        "freshness_label": label,
        "freshness_age_sec": age,
        "freshness_fresh_sec": fresh_sec,
        "freshness_stale_sec": stale_sec,
    }


def _mapping_for_status(status: object) -> dict[str, object]:
    if isinstance(status, Mapping):
        return dict(status)
    as_dict = getattr(status, "as_dict", None)
    if callable(as_dict):
        try:
            value = as_dict()
            return dict(value) if isinstance(value, Mapping) else {}
        except Exception:
            return {}
    return {}


def _js8_registry_endpoint_key(status: object) -> str:
    data = _mapping_for_status(status)
    host = str(data.get("host", "") or "").strip()
    port = data.get("port")
    key = str(data.get("key", "") or "").strip()
    if key:
        return _endpoint_key(key)
    if host and port not in (None, ""):
        return _endpoint_key(f"{host}:{port}")
    return ""


def _endpoint_key(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.rsplit("://", 1)[-1]
    host, sep, port = text.rpartition(":")
    if not sep:
        return text.lower()
    try:
        port_int = int(str(port or "").strip())
    except Exception:
        return text.lower()
    host_txt = host.strip().lower() or "127.0.0.1"
    return f"{host_txt}:{port_int}"


def _projection_counts_by_source(db_path: str | Path) -> dict[str, int]:
    target_db = Path(db_path)
    if not target_db.exists():
        return {}
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(target_db)
        try:
            if _table_has_columns(conn, "message_file_metadata", {"source_id"}):
                for source_id, count in conn.execute(
                    """
                    SELECT COALESCE(source_id, '') AS source_id, COUNT(*)
                      FROM message_file_metadata
                     WHERE COALESCE(source_id, '') <> ''
                  GROUP BY COALESCE(source_id, '')
                    """
                ).fetchall():
                    key = str(source_id or "").strip()
                    if key:
                        counts[key] = counts.get(key, 0) + int(count or 0)
            if _table_has_columns(conn, "js8_messages", {"source_key"}):
                for source_key, count in conn.execute(
                    """
                    SELECT COALESCE(source_key, '') AS source_key, COUNT(*)
                      FROM js8_messages
                     WHERE COALESCE(source_key, '') <> ''
                  GROUP BY COALESCE(source_key, '')
                    """
                ).fetchall():
                    key = str(source_key or "").strip()
                    if key:
                        counts[key] = counts.get(key, 0) + int(count or 0)
            if _table_has_columns(conn, "varac_messages", {"ingest_source_key"}):
                for source_key, count in conn.execute(
                    """
                    SELECT COALESCE(ingest_source_key, '') AS source_key, COUNT(*)
                      FROM varac_messages
                     WHERE COALESCE(ingest_source_key, '') <> ''
                  GROUP BY COALESCE(ingest_source_key, '')
                    """
                ).fetchall():
                    key = str(source_key or "").strip()
                    if key:
                        counts[key] = counts.get(key, 0) + int(count or 0)
        finally:
            conn.close()
    except Exception as exc:
        log.debug("Ingest runtime status: failed reading projection counts: %s", exc)
        return {}
    return counts


def _commstat_projection_summary(db_path: str | Path) -> dict[str, object]:
    target_db = Path(db_path)
    if not target_db.exists():
        return {}
    try:
        conn = sqlite3.connect(target_db)
        try:
            if not _table_has_columns(conn, "commstat_artifacts", {"artifact_kind", "transport_mode"}):
                return {}
            total = int(conn.execute("SELECT COUNT(*) FROM commstat_artifacts").fetchone()[0] or 0)
            if total <= 0:
                return {}
            transport_counts = _count_grouped_column(conn, "commstat_artifacts", "transport_mode")
            artifact_counts = _count_grouped_column(conn, "commstat_artifacts", "artifact_kind")
            reach_counts = (
                _count_grouped_column(conn, "commstat_artifacts", "reach_mode")
                if _table_has_columns(conn, "commstat_artifacts", {"reach_mode"})
                else {}
            )
            origin_counts = (
                _count_grouped_column(conn, "commstat_artifacts", "origin_path")
                if _table_has_columns(conn, "commstat_artifacts", {"origin_path"})
                else {}
            )
            source_counts = (
                _count_grouped_column(conn, "commstat_artifacts", "source_last")
                if _table_has_columns(conn, "commstat_artifacts", {"source_last"})
                else {}
            )
            group_counts = (
                _count_grouped_column(conn, "commstat_artifacts", "report_group")
                if _table_has_columns(conn, "commstat_artifacts", {"report_group"})
                else {}
            )
            return {
                "total": total,
                "artifact_counts": artifact_counts,
                "transport_counts": transport_counts,
                "transport_labels": _label_counts(transport_counts, commstat_transport_label),
                "reach_counts": reach_counts,
                "reach_labels": _label_counts(reach_counts, commstat_reach_label),
                "origin_counts": origin_counts,
                "source_counts": source_counts,
                "group_counts": group_counts,
            }
        finally:
            conn.close()
    except Exception as exc:
        log.debug("Ingest runtime status: failed reading CommStat projection summary: %s", exc)
        return {}


def _js8_link_projection_summaries(db_path: str | Path) -> dict[str, dict[str, object]]:
    target_db = Path(db_path)
    if not target_db.exists():
        return {}
    try:
        conn = sqlite3.connect(target_db)
        try:
            if not _table_has_columns(conn, "js8_links", {"source_id", "app_instance_id", "source_radio_id"}):
                return {}
            summaries: dict[str, dict[str, object]] = {}
            _merge_js8_link_group_counts(
                summaries,
                conn,
                key_column="source_id",
                key_name="source_id",
                where="COALESCE(source_id, '') <> ''",
            )
            _merge_js8_link_group_counts(
                summaries,
                conn,
                key_column="app_instance_id",
                key_name="app_instance_id",
                where="COALESCE(app_instance_id, '') <> ''",
            )
            _merge_js8_link_group_counts(
                summaries,
                conn,
                key_column="source_radio_id",
                key_name="source_radio_id",
                where="COALESCE(source_radio_id, '') <> ''",
            )
            return summaries
        finally:
            conn.close()
    except Exception as exc:
        log.debug("Ingest runtime status: failed reading JS8 link projection summary: %s", exc)
        return {}


def _merge_js8_link_group_counts(
    summaries: dict[str, dict[str, object]],
    conn: sqlite3.Connection,
    *,
    key_column: str,
    key_name: str,
    where: str,
) -> None:
    ident_key = _sqlite_identifier(key_column)
    rows = conn.execute(
        f"""
        SELECT
            {ident_key} AS source_key,
            COUNT(*) AS total,
            COUNT(DISTINCT COALESCE(NULLIF(TRIM(origin), ''), '') || '>' || COALESCE(NULLIF(TRIM(destination), ''), '')) AS station_pairs,
            MAX(ts) AS newest_ts
          FROM js8_links
         WHERE {where}
      GROUP BY {ident_key}
        """
    ).fetchall()
    for source_key, total, station_pairs, newest_ts in rows:
        key = str(source_key or "").strip()
        if not key:
            continue
        current = dict(summaries.get(key, {}))
        current["total"] = max(int(current.get("total", 0) or 0), int(total or 0))
        current["station_pairs"] = max(int(current.get("station_pairs", 0) or 0), int(station_pairs or 0))
        current["newest_ts"] = max(float(current.get("newest_ts", 0.0) or 0.0), _float_object(newest_ts))
        current.setdefault("keys", {})[key_name] = key
        summaries[key] = current


def _summary_for_keys(summaries: Mapping[str, Mapping[str, object]], keys: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in keys:
        summary = summaries.get(str(key or "").strip())
        if not summary:
            continue
        out["total"] = max(int(out.get("total", 0) or 0), int(summary.get("total", 0) or 0))
        out["station_pairs"] = max(int(out.get("station_pairs", 0) or 0), int(summary.get("station_pairs", 0) or 0))
        out["newest_ts"] = max(float(out.get("newest_ts", 0.0) or 0.0), _float_object(summary.get("newest_ts")))
        keys_out = dict(out.get("keys", {}) or {})
        keys_out.update(dict(summary.get("keys", {}) or {}))
        if keys_out:
            out["keys"] = keys_out
    return out


def _append_detail_count(detail: str, count: int, *, singular: str, plural: str) -> str:
    label = singular if int(count or 0) == 1 else plural
    suffix = f"{int(count or 0)} {label}"
    return _append_detail_text(detail, suffix)


def _append_detail_text(detail: str, addition: str) -> str:
    suffix = str(addition or "").strip()
    if not suffix:
        return str(detail or "")
    return f"{detail}; {suffix}" if detail else suffix


def _count_grouped_column(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    ident_table = _sqlite_identifier(table)
    ident_column = _sqlite_identifier(column)
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM({ident_column}), ''), 'unknown') AS key, COUNT(*)
          FROM {ident_table}
      GROUP BY COALESCE(NULLIF(TRIM({ident_column}), ''), 'unknown')
        """
    ).fetchall()
    return {str(key or "unknown").strip().lower(): int(count or 0) for key, count in rows}


def _label_counts(counts: Mapping[str, int], labeler) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, count in dict(counts or {}).items():
        label = str(labeler(key) or "").strip() or str(key or "Unknown")
        out[label] = out.get(label, 0) + int(count or 0)
    return out


def _sqlite_identifier(value: str) -> str:
    text = str(value or "")
    if not text.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQLite identifier: {value!r}")
    return '"' + text.replace('"', '""') + '"'


def _table_has_columns(conn: sqlite3.Connection, table: str, columns: set[str]) -> bool:
    try:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            return False
        present = {str(row[1] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        return columns.issubset(present)
    except Exception:
        return False


def _default_detail_for_source(source: IngestSourceHealth) -> str:
    if source.family == "commstat":
        active = tuple(str(v) for v in (source.metadata.get("active_groups", ()) or ()) if str(v or "").strip())
        configured = tuple(str(v) for v in (source.metadata.get("configured_groups", ()) or ()) if str(v or "").strip())
        if active:
            shown = ", ".join(active[:4])
            suffix = f" +{len(active) - 4} more" if len(active) > 4 else ""
            return f"Active groups: {shown}{suffix}"
        if configured:
            return f"{len(configured)} configured groups; none active"
        return "No configured groups found"
    if source.source_type == "api":
        return "API endpoint configured" if source.endpoint else "API endpoint missing"
    if source.exists:
        return "Source path available"
    return "Source path missing"


def _relative_activity_label(activity_ts: float, *, generated_ts: float) -> str:
    if activity_ts <= 0:
        return "Not checked"
    delta = max(0.0, generated_ts - activity_ts)
    if delta < 60:
        return "Just now"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = int(minutes // 60)
    if hours < 48:
        return f"{hours} h ago"
    days = int(hours // 24)
    return f"{days} days ago"


def _float_object(value: object) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _family_sort_key(family: str) -> int:
    order = {"js8call": 0, "commstat": 1, "flmsg": 2, "flamp": 3, "varac": 4, "bbs": 5}
    return order.get(str(family or "").strip().lower(), 99)


def _runtime_source_title(row: RuntimeIngestStatusRow) -> str:
    label = str(row.label or "").strip()
    if label:
        metadata = row.metadata if isinstance(row.metadata, Mapping) else {}
        if metadata.get("attribution") in {"shared", "unverified"}:
            return f"{label} · {str(row.source_id)[-8:]}"
        return label
    kind = _runtime_source_kind(row)
    radio = str(row.radio_id or "").strip()
    return f"{radio} {kind}".strip() or kind


def _runtime_source_kind(row: RuntimeIngestStatusRow) -> str:
    family = str(row.family or "").strip().lower()
    source_type = str(row.source_type or "").strip().lower()
    role = ""
    metadata = row.metadata if isinstance(row.metadata, Mapping) else {}
    try:
        role = str(metadata.get("role", "") or "").strip().lower()
    except Exception:
        role = ""
    if family == "js8call":
        source_text = " ".join(
            str(value or "").strip().lower()
            for value in (row.source_id, row.label, row.location, source_type)
        )
        if source_type == "js8-inbox":
            return "JS8Call Inbox"
        if source_type == "spotter-directed":
            return "JS8Spotter DIRECTED.TXT"
        if source_type == "api":
            return "JS8Call API"
        if role == "inbox" or source_type == "sqlite":
            return "JS8Call Inbox"
        if role == "all":
            return "JS8Call ALL.TXT"
        if role == "directed":
            return "JS8Call DIRECTED.TXT"
        if "directed" in source_text:
            return "JS8Call DIRECTED.TXT"
        if "all.txt" in source_text or " all " in f" {source_text} ":
            return "JS8Call ALL.TXT"
        return "JS8Call"
    if family == "commstat":
        return "CommStat"
    if family == "varac":
        return "VarAC"
    if family == "flmsg":
        return "FLMSG"
    if family == "flamp":
        return "FLAMP"
    return str(row.family or row.source_type or "Source").strip() or "Source"


def _operator_detail(detail: object, *, metadata: Mapping[str, object] | None = None) -> str:
    text = str(detail or "").strip()
    replacements = {
        "API configured; shared client idle": "Configured; connects when needed",
        "Source path available": "Path found",
        "source-specific inbox path missing": "JS8Call inbox database was not found",
        "source path missing": "Path not found",
    }
    metadata = metadata if isinstance(metadata, Mapping) else {}
    link_summary = metadata.get("link_projection_summary")
    if isinstance(link_summary, Mapping) and int(link_summary.get("total", 0) or 0) > 0:
        total = int(link_summary.get("total", 0) or 0)
        pairs = int(link_summary.get("station_pairs", 0) or 0)
        path_label = "heard path" if total == 1 else "heard paths"
        if pairs > 0:
            pair_label = "station pair" if pairs == 1 else "station pairs"
            return f"{_clean_operator_detail(text, replacements)}; {total} {path_label} across {pairs} {pair_label}"
        return f"{_clean_operator_detail(text, replacements)}; {total} {path_label}"
    projection_summary = metadata.get("projection_summary")
    if isinstance(projection_summary, Mapping) and int(projection_summary.get("total", 0) or 0) > 0:
        total = int(projection_summary.get("total", 0) or 0)
        artifact_label = "artifact" if total == 1 else "artifacts"
        parts = [_clean_operator_detail(text, replacements), f"{total} CommStat {artifact_label}"]
        transport = _count_labels_for_detail(projection_summary.get("transport_labels"))
        reach = _count_labels_for_detail(projection_summary.get("reach_labels"))
        if transport:
            parts.append(f"Transport: {transport}")
        if reach:
            parts.append(f"Reach: {reach}")
        return "; ".join(part for part in parts if part)
    return _clean_operator_detail(text, replacements)


def _clean_operator_detail(text: str, replacements: Mapping[str, str]) -> str:
    if text in replacements:
        return replacements[text]
    parts = []
    for part in (part.strip() for part in str(text or "").split(";")):
        if not part:
            continue
        if re.match(r"^\d+\s+projected\s+(link|links|artifact|artifacts)$", part, flags=re.IGNORECASE):
            continue
        parts.append(part)
    if not parts:
        return ""
    return "; ".join(replacements.get(part, part) for part in parts)


def _count_labels_for_detail(value: object, *, limit: int = 3) -> str:
    if not isinstance(value, Mapping):
        return ""
    items: list[tuple[str, int]] = []
    for raw_label, raw_count in value.items():
        label = str(raw_label or "").strip()
        if not label:
            continue
        try:
            count = int(raw_count or 0)
        except Exception:
            count = 0
        if count > 0:
            items.append((label, count))
    if not items:
        return ""
    items.sort(key=lambda item: (-item[1], item[0].lower()))
    shown = [f"{label} {count}" for label, count in items[:limit]]
    if len(items) > limit:
        shown.append(f"+{len(items) - limit} more")
    return ", ".join(shown)


def _source_skip_detail(row: Mapping[str, object]) -> str:
    reason = str(row.get("reason", "") or "").strip().lower().replace("-", "_")
    if reason == "backoff":
        cooldown = _float_object(row.get("cooldown_remaining_sec"))
        if cooldown > 0:
            return f"Retry in {int(cooldown + 0.5)}s"
        return "Waiting before retry"
    if reason in {"missing", "missing_path"}:
        source_type = str(row.get("source_type", "") or "").strip().lower()
        if source_type == "js8-inbox":
            return "JS8Call inbox database was not found"
        return "Path not found"
    if reason == "unreadable":
        return "Source could not be read"
    return reason.replace("_", " ") if reason else ""


def _view_state_sort_key(state: str) -> int:
    order = {
        "needs_attention": 0,
        "missing": 1,
        "shared_endpoint": 2,
        "backoff": 3,
        "unknown": 4,
        "stale": 5,
        "quiet": 6,
        "idle": 7,
        "ready": 8,
    }
    return order.get(str(state or "").strip().lower(), 99)
