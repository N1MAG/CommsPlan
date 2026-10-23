from __future__ import annotations

import datetime
import json
import re
import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from freqinout.core.group_utils import normalize_group_name
from freqinout.core.commstat_brevity import intelligence_values
from freqinout.core.commstat_sitrep import parse_commstat_message
from freqinout.core.js8_message_policy import (
    JS8_MESSAGE_POLICY_VERSION,
    canonicalize_js8_payload,
    classify_js8_payload,
    directed_js8_payload,
    unique_js8_analysis_text,
)
from freqinout.core.message_intelligence import analyze_commstat_fields, analyze_spotter_text
from freqinout.core.message_semantics import commstat_status_receipt, status_receipt_summary
from freqinout.core.message_file_metadata import cached_message_file_row_summary
from freqinout.core.message_file_scanner import FileRecord, file_path_display, file_path_key
from freqinout.core.message_canonical_identity import (
    canonical_message_key,
    canonical_station_message_id,
    file_content_digest,
)
from freqinout.core.message_projection_store import (
    ExternalMessageRef,
    MessageArtifactRecord,
    MessageProjectionCheckpoint,
    MessageProjectionRecord,
    MessageSourceRecord,
    content_hash,
    ensure_message_projection_schema,
    set_message_projection_checkpoint,
    stable_message_id,
    upsert_external_ref,
    upsert_message_artifact,
    upsert_message_projection,
    upsert_message_source,
)
from freqinout.core.message_projection_writer import ProjectionBundle
from freqinout.core.sqlite_utils import connect_sqlite, table_exists

PROJECTOR_VERSION = 4
FILE_PROJECTOR_VERSION = 6
JS8_COMMSTAT_CLASSIFICATION_VERSION = 2
SPOTTER_PROVENANCE_VERSION = 1
DEFAULT_SOURCE_NATIVE_LIMIT = 5000
_PROJECTION_WRITE_LOCK = threading.Lock()

ProjectionBundleSink = Callable[[ProjectionBundle], None]


def native_projector_version(source_family: object) -> int:
    """Return the bounded replay version for one native source family."""

    family = _text(source_family).lower()
    if family in {"sitrep", "commstat"}:
        return 5
    if family in {"js8", "spotter", "varac", "sitrep", "commstat", "mesh"}:
        return PROJECTOR_VERSION
    return PROJECTOR_VERSION


def _js8_receipt_kind(source_key: object, source_path: object) -> str:
    evidence = f"{_text(source_key)} {_text(source_path)}".lower()
    if "directed" in evidence:
        return "directed_txt"
    if "inbox" in evidence or ".db3" in evidence:
        return "inbox_db"
    if "api" in evidence or not _text(source_path):
        return "api"
    return "file"


def _js8_source_key(row: object) -> str:
    source_key = _text(_row_value(row, "source_key"))
    source_path = _text(_row_value(row, "source_path"))
    kind = _js8_receipt_kind(source_key, source_path)
    if source_key:
        return f"{kind}:{source_key}"
    if source_path:
        return f"{kind}:legacy:{source_path}"
    return f"api:{_text(_row_value(row, 'js8_instance_id')) or 'legacy'}"


def _js8_source_label(source_key: object, source_path: object) -> str:
    kind = _js8_receipt_kind(source_key, source_path)
    label = {
        "directed_txt": "JS8Call DIRECTED.TXT",
        "inbox_db": "JS8Call inbox.db3",
        "api": "JS8Call API",
        "file": "JS8Call file",
    }[kind]
    return _source_label(label, source_key)


def _emit_projection_bundle(
    conn: sqlite3.Connection,
    source: MessageSourceRecord,
    message: MessageProjectionRecord,
    ref: ExternalMessageRef,
    *,
    artifacts: Sequence[MessageArtifactRecord] = (),
    bundle_sink: ProjectionBundleSink | None = None,
) -> None:
    """Send the existing exact projector output to a sink or legacy DML path."""

    if bundle_sink is not None:
        bundle_sink(ProjectionBundle(source, message, (ref,), tuple(artifacts)))
        return
    _upsert_bundle(conn, source, message, ref, artifacts=artifacts)


def _targeted_keys(external_keys: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(key) for key in external_keys or () if _text(key)))[:100]


def _targeted_source_ids(source_ids: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(value) for value in source_ids or () if _text(value)))[:100]


def _file_metadata_key(rec: FileRecord) -> tuple[str, str, float, int]:
    return (
        _text(rec.origin).lower(),
        str(rec.path),
        float(rec.mtime or 0.0),
        int(rec.size or 0),
    )


def _load_file_projection_metadata(
    conn: sqlite3.Connection,
    records: Sequence[FileRecord],
) -> dict[tuple[str, str, float, int], dict[str, object]]:
    if not records:
        return {}
    # Schema ownership belongs to startup.  A projection read must never turn
    # a missing optional metadata cache into runtime DDL.
    if not table_exists(conn, "message_file_metadata"):
        return {}
    keys = {_file_metadata_key(rec) for rec in records}
    if not keys:
        return {}
    out: dict[tuple[str, str, float, int], dict[str, object]] = {}
    try:
        rows = conn.execute(
            """
            SELECT origin, path, mtime, size, source_id, source_label, source_family,
                   msg_type, display_type, status, from_call, to_call, title,
                   rcv_display, report_ts, age_ts_source, topics_json, actionable,
                   search_text
              FROM message_file_metadata
             WHERE origin IN ('flmsg', 'flamp', 'bbs', 'bbs_archive')
            """
        ).fetchall()
    except Exception:
        return {}
    for row in rows:
        try:
            key = (
                _text(row["origin"]).lower(),
                _text(row["path"]),
                float(row["mtime"] or 0.0),
                int(row["size"] or 0),
            )
        except Exception:
            continue
        if key not in keys:
            continue
        out[key] = {str(name): row[name] for name in row.keys()}
    return out


def _load_file_read_states(
    conn: sqlite3.Connection,
    records: Sequence[FileRecord],
) -> dict[tuple[str, str, float, int], str]:
    if not records or not table_exists(conn, "message_read_state"):
        return {}
    keys = {_file_metadata_key(rec) for rec in records}
    out: dict[tuple[str, str, float, int], str] = {}
    try:
        rows = conn.execute(
            "SELECT origin, path, mtime, size, status FROM message_read_state"
        ).fetchall()
    except Exception:
        return {}
    for row in rows:
        try:
            key = (
                _text(row["origin"]).lower(),
                _text(row["path"]),
                float(row["mtime"] or 0.0),
                int(row["size"] or 0),
            )
        except Exception:
            continue
        if key in keys:
            out[key] = _text(row["status"]).upper() or "NEW"
    return out


def _file_projection_bundle(
    rec: FileRecord,
    *,
    metadata: Mapping[str, object] | None = None,
    read_status: str = "",
) -> ProjectionBundle:
    """Build the exact file-scanner projection unit without SQLite writes.

    This is the single semantics seam used by both the compatibility deep
    rebuild and the MIP-3 incremental file pipeline.  Filesystem paths are
    converted to printable escaped text at this boundary, before any value can
    reach SQLite through the writer.
    """

    origin = _text(rec.origin).lower() or "file"
    path_text = file_path_display(rec.path)
    parent_text = file_path_display(rec.path.parent)
    safe_path = Path(path_text)
    meta = cached_message_file_row_summary(
        rec,
        metadata,
        fallback_origin=origin,
        fallback_title=file_path_display(rec.path.name),
        title_limit=240,
    )
    # File projections often retain only a title, so preserve derived-report
    # lineage from a bounded prefix while the ingest worker already owns file I/O.
    # This survives renamed files and FLMsg/FLAmp/BBS mirrors without opening files
    # from report generation or Qt rendering.
    lineage = {}
    try:
        with rec.path.open("rb") as handle:
            prefix = handle.read(8192).decode("utf-8", errors="replace")
        match = re.search(r"COMMSPLAN-TRAFFIC-SUMMARY:v1:([A-Za-z0-9_-]{1,80})(?:\s|$)", prefix)
        if match:
            lineage = {"derived_report": True, "report_id": match.group(1), "report_format_version": 1}
    except OSError:
        pass
    received_ts = float(rec.mtime or 0.0)
    event_ts = float(getattr(meta, "report_ts", 0.0) or received_ts)
    status = (read_status or _text(getattr(meta, "status", "")) or "NEW").upper()
    title = _text(getattr(meta, "title", "")) or file_path_display(rec.path.name)
    message_type = _text(getattr(meta, "msg_type", "")) or _file_message_type(origin, safe_path)
    display_type = _text(getattr(meta, "display_type", "")) or _file_source_base_label(origin)
    from_call = _upper(getattr(meta, "from_call", ""))
    to_call = _upper(getattr(meta, "to_call", ""))
    search_text = _text(getattr(meta, "search_text", "")) or _search_text(origin, title, path_text)
    topics = tuple(getattr(meta, "topics", ()) or ()) or _topics(title, origin)
    source_id = _text(rec.source_id) or f"{origin}:{file_path_key(rec.path.parent)}"
    source_label = _text(rec.source_label) or _source_label(_file_source_base_label(origin), "")
    external_kind = f"{origin}_file"
    # The display spelling is intentionally not an identity: two raw byte
    # paths can render similarly after escaping.  Use the reversible opaque
    # fsencode key for every persisted file-version identity.
    external_key = f"{file_path_key(rec.path)}:{float(rec.mtime or 0.0):.6f}:{int(rec.size or 0)}"
    file_digest = file_content_digest(rec.path)
    file_message_identity = (
        f"{safe_path.name.casefold()}:{file_digest}" if file_digest else ""
    )
    message_id = canonical_station_message_id(
        origin,
        event_ts=event_ts,
        from_call=from_call,
        to_call=to_call,
        payload=title,
        message_type=message_type,
        durable_id=file_message_identity,
        payload_digest=file_digest,
    )
    source = MessageSourceRecord(
        source_id=source_id,
        source_family=origin,
        source_label=source_label,
        endpoint_or_path=parent_text,
        capabilities={"read": True, "delete": True, "native_open": True},
        provenance={"source": "file_scan", "origin": origin},
        last_seen_utc=_utc_from_ts(received_ts),
        last_ingested_utc=_utc_now(),
    )
    projection = MessageProjectionRecord(
        message_id=message_id,
        canonical_key=canonical_message_key(origin, message_id),
        content_hash=content_hash(
            FILE_PROJECTOR_VERSION,
            "file",
            file_digest or external_key,
            status,
            event_ts,
            received_ts,
        ),
        primary_source_id=source_id,
        source_family=origin,
        source_label=source.source_label,
        message_type=message_type,
        display_type=display_type,
        status=status,
        severity=_severity_from_status(status),
        read_state=_read_state(status),
        from_call=from_call,
        to_call=to_call,
        group_name=_group(to_call),
        event_ts=event_ts,
        received_ts=received_ts,
        event_utc=_utc_from_ts(event_ts),
        received_utc=_utc_from_ts(received_ts),
        subject=title,
        summary=title,
        body_preview=title,
        topics=topics,
        entities={
            **lineage,
            "origin": origin,
            "path": path_text,
            "extension": safe_path.suffix.lower(),
            "q_id": _q_id_from_path(safe_path),
            "age_ts_source": "received",
            "report_ts": event_ts if event_ts != received_ts else 0.0,
        },
        retention_class="artifact",
        search_text=search_text,
        projection_version=FILE_PROJECTOR_VERSION,
    )
    artifact_type = {"flamp": "flamp_transfer", "flmsg": "form_file", "bbs": "bbs_file"}.get(origin, f"{origin}_file")
    q_id = _q_id_from_path(safe_path)
    return ProjectionBundle(
        source,
        projection,
        (
            ExternalMessageRef(
                message_id=message_id,
                source_id=source_id,
                external_kind=external_kind,
                external_key=external_key,
                external_path=path_text,
                external_mtime=float(rec.mtime or 0.0),
                external_size=int(rec.size or 0),
                external_hash=file_digest,
                delete_capability="file_delete",
                read_capability="fio_read_state",
                metadata={
                    **lineage,
                    "origin": origin,
                    "source": "file_scan",
                    "receipt_source_kind": f"{origin}_folder",
                },
            ),
        ),
        (
            MessageArtifactRecord(
                artifact_id=stable_message_id(message_id, artifact_type, path_text, rec.mtime, rec.size),
                message_id=message_id,
                artifact_type=artifact_type,
                source_id=source_id,
                external_key=external_key,
                path=path_text,
                content_hash=file_digest or content_hash(path_text, rec.mtime, rec.size),
                q_id=q_id,
                block_id=_block_id_from_path(safe_path),
                transfer_id=q_id,
                transfer_state="seen" if q_id else "",
                metadata={**lineage, "mtime": float(rec.mtime or 0.0), "size": int(rec.size or 0)},
            ),
        ),
    )


def prepare_file_projection_bundles(
    conn: sqlite3.Connection,
    records: Sequence[FileRecord],
) -> tuple[ProjectionBundle, ...]:
    """Prepare file bundles only; metadata reads are bounded to supplied files."""

    items = tuple(record for record in records if isinstance(record, FileRecord))[:100]
    metadata = _load_file_projection_metadata(conn, items)
    read_states = _load_file_read_states(conn, items)
    return tuple(
        _file_projection_bundle(
            record,
            metadata=metadata.get(_file_metadata_key(record)),
            read_status=read_states.get(_file_metadata_key(record), ""),
        )
        for record in sorted(items, key=lambda item: float(item.mtime or 0.0), reverse=True)
    )


def project_native_message_sources(
    db_path: str | Path,
    *,
    sources: Sequence[str] = ("js8", "spotter", "varac", "sitrep", "commstat"),
    limit: int = DEFAULT_SOURCE_NATIVE_LIMIT,
    force: bool = False,
) -> dict[str, int]:
    """Project local source tables directly into the normalized message projection."""
    clean_sources = tuple(str(source or "").strip().lower() for source in sources if str(source or "").strip())
    if not clean_sources:
        return {}
    bounded_limit = max(1, min(int(limit or DEFAULT_SOURCE_NATIVE_LIMIT), 50000))
    with _PROJECTION_WRITE_LOCK:
        conn = connect_sqlite(db_path, timeout=15.0, row_factory=sqlite3.Row, busy_timeout_ms=15000)
        try:
            ensure_message_projection_schema(conn)
            out: dict[str, int] = {}
            projectors: dict[str, Callable[[sqlite3.Connection, int, bool], int]] = {
                "js8": _project_js8_messages,
                "spotter": _project_spotter_traffic,
                "varac": _project_varac_messages,
                "sitrep": _project_sitrep_events,
                "commstat": _project_commstat_artifacts,
            }
            for source in clean_sources:
                projector = projectors.get(source)
                if projector is None:
                    continue
                out[source] = projector(conn, bounded_limit, bool(force))
            return out
        finally:
            conn.close()


def project_native_file_records(
    db_path: str | Path,
    records: Mapping[str, Sequence[FileRecord]],
    *,
    force: bool = False,
) -> int:
    """Project file-scanner records directly into the normalized message projection."""
    flattened: list[FileRecord] = []
    for origin, values in (records or {}).items():
        origin_norm = _text(origin).lower()
        for rec in values or ():
            if not isinstance(rec, FileRecord):
                continue
            flattened.append(
                FileRecord(
                    # Projection persistence must never receive a surrogate
                    # filesystem path.  Normal paths are unchanged; invalid
                    # UTF-8 names become an escaped printable representation.
                    path=Path(file_path_display(rec.path)),
                    origin=origin_norm or _text(rec.origin).lower() or "file",
                    size=int(rec.size or 0),
                    mtime=float(rec.mtime or 0.0),
                    source_id=_text(getattr(rec, "source_id", "")),
                    source_label=_text(getattr(rec, "source_label", "")),
                )
            )
    fingerprint = content_hash(
        FILE_PROJECTOR_VERSION,
        "file_records",
        len(flattened),
        "\n".join(
            sorted(
                f"{rec.origin}|{rec.path}|{float(rec.mtime or 0.0):.6f}|{int(rec.size or 0)}|{rec.source_id}"
                for rec in flattened
            )
        ),
    )
    with _PROJECTION_WRITE_LOCK:
        conn = connect_sqlite(db_path, timeout=15.0, row_factory=sqlite3.Row, busy_timeout_ms=15000)
        try:
            ensure_message_projection_schema(conn)
            file_metadata = _load_file_projection_metadata(conn, flattened)
            file_read_states = _load_file_read_states(conn, flattened)
            fingerprint = content_hash(
                fingerprint,
                "metadata",
                "\n".join(
                    sorted(
                        "|".join(
                            (
                                key[0],
                                key[1],
                                f"{key[2]:.6f}",
                                str(key[3]),
                                _text(meta.get("title", "")),
                                _text(meta.get("report_ts", "")),
                                _text(meta.get("age_ts_source", "")),
                                file_read_states.get(key, "") or _text(meta.get("status", "")),
                            )
                        )
                        for key, meta in file_metadata.items()
                    )
                ),
                "read_states",
                "\n".join(
                    sorted(
                        "|".join(
                            (key[0], key[1], f"{key[2]:.6f}", str(key[3]), status)
                        )
                        for key, status in file_read_states.items()
                    )
                ),
            )
            checkpoint_id = "native:file_records"
            if _checkpoint_matches(conn, checkpoint_id, fingerprint, force=force):
                return 0
            projected = 0
            with conn:
                for rec in sorted(flattened, key=lambda item: float(item.mtime or 0.0), reverse=True):
                    bundle = _file_projection_bundle(
                        rec,
                        metadata=file_metadata.get(_file_metadata_key(rec)),
                        read_status=file_read_states.get(_file_metadata_key(rec), ""),
                    )
                    _upsert_bundle(
                        conn,
                        bundle.source,
                        bundle.message,
                        bundle.refs[0],
                        artifacts=bundle.artifacts,
                    )
                    projected += 1
                _set_checkpoint(conn, checkpoint_id, fingerprint, flattened)
            return projected
        finally:
            conn.close()


def _project_js8_messages(
    conn: sqlite3.Connection,
    limit: int,
    force: bool,
    *,
    external_keys: Sequence[str] | None = None,
    source_ids: Sequence[str] | None = None,
    bundle_sink: ProjectionBundleSink | None = None,
) -> int:
    if not table_exists(conn, "js8_messages"):
        return 0
    targeted = _targeted_keys(external_keys)
    targeted_sources = _targeted_source_ids(source_ids)
    checkpoint_id = "native:js8_messages"
    fingerprint = ""
    if not targeted:
        fingerprint = _table_fingerprint(conn, "js8_messages", "COUNT(*)", "MAX(COALESCE(id, 0))", "MAX(COALESCE(source_id, 0))", "MAX(COALESCE(utc_ts, 0))", "MAX(COALESCE(read_ts, 0))")
        fingerprint = content_hash(
            JS8_MESSAGE_POLICY_VERSION,
            JS8_COMMSTAT_CLASSIFICATION_VERSION,
            fingerprint,
        )
        if _checkpoint_matches(conn, checkpoint_id, fingerprint, force=force):
            with conn:
                return _reconcile_js8_projection_policy(conn, limit=limit)
    query = """
        SELECT id, from_call, to_call, msg_type, utc_str, utc_ts, raw_text, decoded_text,
               state, read_ts, flag_state, source_key, source_id, source_radio_id,
               js8_instance_id, source_path
          FROM js8_messages
    """
    params: list[object] = []
    if targeted:
        marks = ",".join("?" for _ in targeted)
        query += f" WHERE CAST(COALESCE(source_id, id) AS TEXT) IN ({marks})"
        params.extend(targeted)
        if targeted_sources:
            source_marks = ",".join("?" for _ in targeted_sources)
            query += (
                " AND ('js8:' || CASE "
                "WHEN COALESCE(source_key,'') != '' THEN "
                "CASE WHEN LOWER(source_key || ' ' || COALESCE(source_path,'')) LIKE '%directed%' THEN 'directed_txt' "
                "WHEN LOWER(source_key || ' ' || COALESCE(source_path,'')) LIKE '%inbox%' "
                "OR LOWER(source_key || ' ' || COALESCE(source_path,'')) LIKE '%.db3%' THEN 'inbox_db' "
                "WHEN LOWER(source_key) LIKE '%api%' OR COALESCE(source_path,'') = '' THEN 'api' ELSE 'file' END "
                "|| ':' || source_key "
                "WHEN COALESCE(source_path,'') != '' THEN "
                "CASE WHEN LOWER(source_path) LIKE '%directed%' THEN 'directed_txt' "
                "WHEN LOWER(source_path) LIKE '%inbox%' OR LOWER(source_path) LIKE '%.db3%' THEN 'inbox_db' "
                "ELSE 'file' END || ':legacy:' || source_path "
                "ELSE 'api:' || COALESCE(NULLIF(js8_instance_id,''), 'legacy') END) "
                f"IN ({source_marks})"
            )
            params.extend(targeted_sources)
    query += " ORDER BY utc_ts DESC, source_id DESC, id DESC LIMIT ?"
    params.append(100 if targeted else limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    projected = 0
    with (nullcontext(conn) if targeted else conn):
        for row in rows:
            source_key = _js8_source_key(row)
            source_id = f"js8:{source_key}"
            external_key = _text(row["source_id"]) or _text(row["id"])
            status = _upper(row["state"]) or "UNREAD"
            raw_body = _text(row["raw_text"])
            decoded_body = _text(row["decoded_text"])
            raw_payload = directed_js8_payload(raw_body)
            decoded_payload = directed_js8_payload(decoded_body)
            decision = classify_js8_payload(raw_payload or decoded_payload)
            if decision.inbox_visible:
                body = canonicalize_js8_payload(decoded_payload or raw_payload)
                analysis_body = unique_js8_analysis_text(raw_payload, decoded_payload)
            else:
                body = decision.canonical_text
                analysis_body = body
            event_ts = _float(row["utc_ts"])
            form_name = _text(row["msg_type"])
            if not form_name.upper().startswith("F!"):
                form_name = ""
            intelligence = analyze_spotter_text(
                analysis_body,
                form_name=form_name,
                from_call=row["from_call"],
                to_call=row["to_call"],
                source_type="js8",
            )
            commstat = _analyze_local_js8_commstat(
                raw_payload=raw_payload,
                decoded_payload=decoded_payload,
                from_call=row["from_call"],
                to_call=row["to_call"],
                event_utc=_text(row["utc_str"]) or _utc_from_ts(event_ts),
            )
            if commstat is not None:
                intelligence = commstat["intelligence"]
            canonical_payload = raw_payload or decoded_payload or body
            message_id = canonical_station_message_id(
                "js8",
                event_ts=event_ts,
                from_call=row["from_call"],
                to_call=row["to_call"],
                payload=canonical_payload,
                message_type=form_name,
                durable_id=f"{source_id}:{external_key}" if event_ts <= 0 else "",
            )
            receipt_kind = _js8_receipt_kind(source_key, row["source_path"])
            source = MessageSourceRecord(
                source_id=source_id,
                source_family="js8",
                source_label=_js8_source_label(
                    _text(row["source_key"]) or _text(row["js8_instance_id"]),
                    row["source_path"],
                ),
                radio_id=_optional_int(row["source_radio_id"]),
                app_instance_id=_text(row["js8_instance_id"]),
                endpoint_or_path=_text(row["source_path"]),
                capabilities={"read": True, "delete": True, "native_open": True},
                provenance={
                    "source_table": "js8_messages",
                    "source_key": _text(row["source_key"]),
                    "qualified_source_key": source_key,
                    "receipt_source_kind": receipt_kind,
                },
                last_seen_utc=_utc_from_ts(event_ts),
                last_ingested_utc=_utc_now(),
            )
            projection = MessageProjectionRecord(
                message_id=message_id,
                canonical_key=canonical_message_key("js8", message_id),
                content_hash=content_hash(
                    native_projector_version("js8"),
                    JS8_MESSAGE_POLICY_VERSION,
                    JS8_COMMSTAT_CLASSIFICATION_VERSION,
                    "js8",
                    status,
                    canonical_payload,
                ),
                primary_source_id=source_id,
                source_family="js8",
                source_label=source.source_label,
                radio_id=source.radio_id,
                app_instance_id=source.app_instance_id,
                message_type=commstat["form_name"] if commstat is not None else (_text(row["msg_type"]) or "MSG"),
                display_type="CommStat" if commstat is not None else "JS8",
                status=commstat["status"] if commstat is not None else status,
                severity=_severity_from_status(commstat["status"]) if commstat is not None else "info",
                read_state=_read_state(status),
                from_call=_upper(row["from_call"]),
                to_call=_upper(row["to_call"]),
                group_name=_group(row["to_call"]),
                state_code=_upper(intelligence.state),
                grid=_upper(intelligence.grid),
                event_ts=event_ts,
                received_ts=event_ts,
                event_utc=_text(row["utc_str"]) or _utc_from_ts(event_ts),
                received_utc=_text(row["utc_str"]) or _utc_from_ts(event_ts),
                subject=(commstat["subject"] if commstat is not None else intelligence.subject) or _subject(body),
                summary=(commstat["summary"] if commstat is not None else intelligence.summary or body)[:240],
                body_preview=body[:1200],
                topics=tuple(intelligence.topics) or _topics(body),
                entities={
                    "from_call": _upper(row["from_call"]),
                    "to_call": _upper(row["to_call"]),
                    "state": _upper(intelligence.state),
                    "grid": _upper(intelligence.grid),
                    **(commstat["entities"] if commstat is not None else {}),
                },
                actionable=bool(intelligence.actionable) if commstat is not None else False,
                operator_attention=bool(intelligence.operator_attention) if commstat is not None else False,
                confidence=float(intelligence.confidence or 0.0) if commstat is not None else 0.0,
                recommended_action="review" if commstat is not None and commstat["status"] in {"YELLOW", "RED"} else "",
                inbox_visible=decision.inbox_visible,
                inbox_suppression_reason="" if decision.inbox_visible else decision.reason,
                classification_version=decision.classification_version,
                retention_class="normal",
                search_text=_search_text(row["from_call"], row["to_call"], row["msg_type"], body),
                projection_version=native_projector_version("js8"),
            )
            _emit_projection_bundle(
                conn,
                source,
                projection,
                ExternalMessageRef(
                    message_id=message_id,
                    source_id=source_id,
                    external_kind="js8_message",
                    external_key=external_key,
                    external_path=_text(row["source_path"]),
                    delete_capability="delete_source",
                    read_capability="mark_read",
                    metadata={
                        "source_table": "js8_messages",
                        "row_id": _text(row["id"]),
                        "receipt_source_kind": receipt_kind,
                        **({"commstat_subtype": commstat["subtype"]} if commstat is not None else {}),
                    },
                ),
                bundle_sink=bundle_sink,
            )
            projected += 1
        if not targeted:
            _set_checkpoint(conn, checkpoint_id, fingerprint, rows)
            projected += _reconcile_js8_projection_policy(conn, limit=limit)
    return projected


def _analyze_local_js8_commstat(
    *,
    raw_payload: object,
    decoded_payload: object,
    from_call: object,
    to_call: object,
    event_utc: object,
) -> dict[str, Any] | None:
    """Classify a locally received JS8 CommStat message without changing its source.

    This deliberately accepts only the parser's ``js8`` transport result.  A
    CommStat artifact that arrived through an internet marker remains outside
    the RF Spotter path, even when it happens to be present in a JS8 table.
    The helper is pure so projection preparation never reads files, SQLite, or
    network state while rendering rows.
    """

    raw_text = _text(raw_payload)
    decoded_text = _text(decoded_payload)
    receipt = commstat_status_receipt(decoded_text, raw_text)
    if receipt is not None:
        summary = status_receipt_summary(sender=from_call, receipt=receipt)
        intelligence = analyze_commstat_fields(
            artifact_kind="STATUS_RECEIPT",
            title="CommStat · Status receipt",
            body=summary,
            from_call=from_call,
            target=to_call,
            report_group=to_call,
            status="INFO",
            subtype="STATUS_RECEIPT",
            remarks=summary,
            transport="js8",
            source_family="JS8Call RF",
            event_utc=event_utc,
        )
        return {
            "intelligence": intelligence,
            "subtype": "STATUS_RECEIPT",
            "form_name": "CommStat/Status receipt",
            "status": "INFO",
            "subject": "CommStat · Status receipt",
            "summary": summary,
            "entities": {
                "form_name": "CommStat/Status receipt",
                "commstat_subtype": "STATUS_RECEIPT",
                "commstat_transport": "js8",
                "acknowledged_callsign": receipt.report_callsign,
                "acknowledged_report_id": receipt.report_id,
                "commstat_raw_evidence": raw_text[:4000],
                "commstat_decoded_evidence": decoded_text[:4000],
            },
        }
    parsed: dict[str, object] | None = None
    evidence = ""
    for candidate in dict.fromkeys(value for value in (decoded_text, raw_text) if value):
        result = parse_commstat_message(
            candidate,
            target_hint=to_call,
            source_value=1,  # Local JS8Call RF ingest path.
        )
        if result is not None:
            parsed = result
            evidence = candidate
            break
    if parsed is None:
        return None

    metadata = dict(parsed.get("metadata") or {})
    # Markers such as {&%3} identify internet-delivered CommStat traffic. Do
    # not turn it into a Spotter/RF CommStat presentation merely because a JS8
    # source table happened to contain a copy.
    if _text(metadata.get("transport_mode")).lower() != "js8":
        return None

    subtype = _text(parsed.get("subtype")) or "COMMSTAT"
    status_payload = dict(parsed.get("status_payload") or {})
    status, status_summary = _commstat_status_summary(status_payload)
    remarks = _text(metadata.get("remarks_text"))
    title = f"CommStat · {status_summary}"
    intelligence = analyze_commstat_fields(
        artifact_kind="STATREP",
        title=title,
        body=remarks or evidence,
        from_call=from_call,
        target=to_call,
        report_group=metadata.get("report_group", ""),
        state=metadata.get("state_code", ""),
        grid=parsed.get("grid", ""),
        scope=parsed.get("scope", ""),
        status=status,
        subtype=subtype,
        remarks=remarks,
        brevity_code=metadata.get("brevity_code", ""),
        brevity_summary=metadata.get("brevity_summary", ""),
        brevity_models=metadata.get("brevity_models", ()),
        transport="js8",
        reach=metadata.get("reach_mode", ""),
        source_family="JS8Call RF",
        event_utc=event_utc,
    )
    location = " / ".join(part for part in (_upper(intelligence.state), _upper(intelligence.grid)) if part)
    summary_parts = ["CommStat", status_summary]
    if location:
        summary_parts.append(location)
    if remarks:
        summary_parts.append(remarks[:100])
    raw_evidence = raw_text[:4000]
    decoded_evidence = decoded_text[:4000]
    return {
        "intelligence": intelligence,
        "subtype": subtype,
        "form_name": f"CommStat/{subtype}",
        "status": status,
        "subject": title,
        "summary": " | ".join(summary_parts),
        "entities": {
            "form_name": f"CommStat/{subtype}",
            "commstat_subtype": subtype,
            "commstat_status_summary": status_summary,
            "commstat_transport": "js8",
            "commstat_scope": _text(parsed.get("scope")),
            "commstat_raw_evidence": raw_evidence,
            "commstat_decoded_evidence": decoded_evidence,
        },
    }


def _commstat_status_summary(status_payload: Mapping[str, object]) -> tuple[str, str]:
    """Return a compact overall CommStat status and only meaningful changes."""

    codes = _text(status_payload.get("status"))
    if not codes:
        return "INFO", "Status not reported"
    expanded = "1" * 12 if codes == "+" else codes
    label_by_code = {"1": "Green", "2": "Yellow", "3": "Red", "4": "Unknown"}
    overall = label_by_code.get(expanded[:1], "Unknown")
    labels = (
        "Overall", "Power", "Water", "Medical", "Communications", "Travel",
        "Internet", "Fuel", "Food", "Crime", "Civil unrest", "Political",
    )
    changes = [
        f"{labels[index]} {label_by_code.get(code, 'Unknown')}"
        for index, code in enumerate(expanded[:12])
        if index and code in {"2", "3"}
    ]
    detail = ", ".join(changes[:2])
    if len(changes) > 2:
        detail += f" +{len(changes) - 2}"
    return overall.upper() if overall in {"Green", "Yellow", "Red"} else "INFO", (overall + (f" · {detail}" if detail else ""))


def _reconcile_js8_projection_policy(conn: sqlite3.Connection, *, limit: int) -> int:
    """Reclassify a bounded legacy JS8 projection batch without deleting evidence."""

    rows = conn.execute(
        """
        SELECT message_id, body_preview, summary
          FROM message_projection
         WHERE source_family='js8'
           AND COALESCE(classification_version, 0) < ?
         ORDER BY rowid
         LIMIT ?
        """,
        (JS8_MESSAGE_POLICY_VERSION, max(1, min(5000, int(limit or 5000)))),
    ).fetchall()
    if not rows:
        return 0
    from freqinout.core.ops_focus import index_message_for_ops_focus

    for row in rows:
        body = _text(row["body_preview"])
        decision = classify_js8_payload(body or row["summary"])
        summary = canonicalize_js8_payload(row["summary"])
        conn.execute(
            """
            UPDATE message_projection
               SET body_preview=?, summary=?, inbox_visible=?, inbox_suppression_reason=?,
                   classification_version=?
             WHERE message_id=?
            """,
            (
                decision.canonical_text if body else body,
                summary,
                1 if decision.inbox_visible else 0,
                "" if decision.inbox_visible else decision.reason,
                decision.classification_version,
                _text(row["message_id"]),
            ),
        )
        refreshed = conn.execute(
            "SELECT * FROM message_projection WHERE message_id=?",
            (_text(row["message_id"]),),
        ).fetchone()
        if refreshed is not None:
            index_message_for_ops_focus(conn, refreshed)
    return len(rows)


def _project_spotter_traffic(conn: sqlite3.Connection, limit: int, force: bool, *, external_keys: Sequence[str] | None = None, source_ids: Sequence[str] | None = None, bundle_sink: ProjectionBundleSink | None = None) -> int:
    if not table_exists(conn, "spotter_traffic"):
        return 0
    targeted = _targeted_keys(external_keys)
    targeted_sources = _targeted_source_ids(source_ids)
    checkpoint_id = "native:spotter_traffic"
    fingerprint = ""
    if not targeted:
        fingerprint = content_hash(
            SPOTTER_PROVENANCE_VERSION,
            _table_fingerprint(conn, "spotter_traffic", "COUNT(*)", "MAX(COALESCE(id, 0))", "MAX(COALESCE(utc_ts, 0))", "MAX(COALESCE(read_ts, 0))"),
        )
        if _checkpoint_matches(conn, checkpoint_id, fingerprint, force=force):
            return 0
    imported_expression = (
        "EXISTS (SELECT 1 FROM js8spotter_import_log il "
        "WHERE il.imported_kind='spotter_traffic' "
        "AND CAST(il.imported_id AS TEXT)=CAST(spotter_traffic.id AS TEXT))"
        if table_exists(conn, "js8spotter_import_log")
        else "0"
    )
    query = f"""
        SELECT id, utc_str, utc_ts, from_call, to_call, form_id, spotter_token,
               raw_text, decoded_text, state, read_ts, flag_state, relay_via,
               source_radio_id, js8_instance_id, {imported_expression} AS is_imported
          FROM spotter_traffic
    """
    params: list[object] = []
    if targeted:
        marks = ",".join("?" for _ in targeted)
        query += f" WHERE CAST(id AS TEXT) IN ({marks})"
        params.extend(targeted)
        if targeted_sources:
            source_marks = ",".join("?" for _ in targeted_sources)
            query += (
                " AND ('spotter:' || COALESCE(NULLIF(js8_instance_id,''), "
                "NULLIF(CAST(source_radio_id AS TEXT),''), 'legacy')) "
                f"IN ({source_marks})"
            )
            params.extend(targeted_sources)
    query += " ORDER BY COALESCE(utc_ts, 0) DESC, id DESC LIMIT ?"
    params.append(100 if targeted else limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    projected = 0
    with (nullcontext(conn) if targeted else conn):
        for row in rows:
            is_imported = bool(_int(row["is_imported"]))
            source_key = _text(row["js8_instance_id"]) or _text(row["source_radio_id"]) or "legacy"
            source_id = f"spotter:{source_key}"
            external_key = _text(row["id"])
            status = _upper(row["state"]) or "UNREAD"
            raw_body = _text(row["raw_text"])
            body = _text(row["decoded_text"]) or raw_body
            analysis_body = "\n".join(part for part in (raw_body, body) if part)
            event_ts = _float(row["utc_ts"])
            msg_type = _text(row["form_id"])
            if msg_type and not msg_type.startswith("F!"):
                msg_type = f"F!{msg_type}"
            message_id = canonical_station_message_id(
                "spotter",
                event_ts=event_ts,
                from_call=row["from_call"],
                to_call=row["to_call"],
                payload=raw_body or body,
                message_type=msg_type,
                durable_id=f"{source_id}:{external_key}" if event_ts <= 0 else "",
            )
            intelligence = analyze_spotter_text(
                analysis_body,
                form_name=msg_type,
                from_call=row["from_call"],
                to_call=row["to_call"],
            )
            source = MessageSourceRecord(
                source_id=source_id,
                source_family="spotter",
                source_label=(
                    _source_label("Imported JS8Spotter", source_key)
                    if is_imported else _source_label("FIOSpotter", source_key)
                ),
                radio_id=_optional_int(row["source_radio_id"]),
                app_instance_id=_text(row["js8_instance_id"]),
                capabilities={"read": True, "delete": True, "native_open": True},
                provenance={
                    "source_table": "spotter_traffic",
                    "source_key": source_key,
                    "ingest_origin": "js8spotter-db-import" if is_imported else "local-js8-receive",
                    "local_rf_received": not is_imported,
                },
                last_seen_utc=_utc_from_ts(event_ts),
                last_ingested_utc=_utc_now(),
            )
            projection = MessageProjectionRecord(
                message_id=message_id,
                canonical_key=canonical_message_key("spotter", message_id),
                content_hash=content_hash(
                    native_projector_version("spotter"),
                    SPOTTER_PROVENANCE_VERSION,
                    "spotter",
                    is_imported,
                    status,
                    raw_body or body,
                ),
                primary_source_id=source_id,
                source_family="spotter",
                source_label=source.source_label,
                radio_id=source.radio_id,
                app_instance_id=source.app_instance_id,
                message_type=msg_type or "F!",
                display_type="Spotter",
                status=status,
                severity="info",
                read_state=_read_state(status),
                from_call=_upper(row["from_call"]),
                to_call=_upper(row["to_call"]),
                group_name=_group(row["to_call"]),
                state_code=_upper(intelligence.state),
                grid=_upper(intelligence.grid),
                event_ts=event_ts,
                received_ts=event_ts,
                event_utc=_text(row["utc_str"]) or _utc_from_ts(event_ts),
                received_utc=_text(row["utc_str"]) or _utc_from_ts(event_ts),
                subject=intelligence.subject or _subject(body),
                summary=(intelligence.summary or body)[:240],
                body_preview=body[:1200],
                topics=tuple(intelligence.topics) or _topics(body),
                entities={
                    "relay_via": _upper(row["relay_via"]),
                    "spotter_token": _text(row["spotter_token"]),
                    "state": _upper(intelligence.state),
                    "grid": _upper(intelligence.grid),
                    "ingest_origin": "js8spotter-db-import" if is_imported else "local-js8-receive",
                    "local_rf_received": not is_imported,
                },
                retention_class="normal",
                search_text=_search_text(row["from_call"], row["to_call"], msg_type, body),
                projection_version=native_projector_version("spotter"),
            )
            _emit_projection_bundle(
                conn,
                source,
                projection,
                ExternalMessageRef(
                    message_id=message_id,
                    source_id=source_id,
                    external_kind="spotter_message",
                    external_key=external_key,
                    delete_capability="delete_source",
                    read_capability="mark_read",
                    metadata={
                        "source_table": "spotter_traffic",
                        "row_id": external_key,
                        "receipt_source_kind": (
                            "js8spotter_import" if is_imported else "fio_spotter_receive"
                        ),
                    },
                ),
                bundle_sink=bundle_sink,
            )
            projected += 1
        if not targeted:
            _set_checkpoint(conn, checkpoint_id, fingerprint, rows)
    return projected


def _project_varac_messages(conn: sqlite3.Connection, limit: int, force: bool, *, external_keys: Sequence[str] | None = None, source_ids: Sequence[str] | None = None, bundle_sink: ProjectionBundleSink | None = None) -> int:
    if not table_exists(conn, "varac_messages"):
        return 0
    targeted = _targeted_keys(external_keys)
    targeted_sources = _targeted_source_ids(source_ids)
    checkpoint_id = "native:varac_messages"
    fingerprint = ""
    if not targeted:
        fingerprint = _table_fingerprint(conn, "varac_messages", "COUNT(*)", "MAX(COALESCE(id, 0))", "MAX(COALESCE(ts, 0))", "SUM(COALESCE(is_deleted, 0))", "SUM(COALESCE(read_status, 0))")
        if _checkpoint_matches(conn, checkpoint_id, fingerprint, force=force):
            return 0
    query = """
        SELECT ingest_source_key, id, guid, source, msg_type, from_call, to_call,
               subject, body, ts, band, freq_hz, snr, read_status, folder,
               file_path, vmail_guid, is_deleted, folder_label, urgent,
               has_attachment, via_callsign
         FROM varac_messages
         WHERE COALESCE(is_deleted, 0) = 0
    """
    params: list[object] = []
    if targeted:
        marks = ",".join("?" for _ in targeted)
        query += (
            " AND CAST(COALESCE(NULLIF(guid, ''), NULLIF(vmail_guid, ''), id) AS TEXT) "
            f"IN ({marks})"
        )
        params.extend(targeted)
        if targeted_sources:
            source_marks = ",".join("?" for _ in targeted_sources)
            query += (
                " AND ('varac:' || COALESCE(NULLIF(ingest_source_key,''), 'legacy') || ':' || "
                "COALESCE(NULLIF(source,''), 'varac')) "
                f"IN ({source_marks})"
            )
            params.extend(targeted_sources)
    query += " ORDER BY COALESCE(ts, 0) DESC, id DESC LIMIT ?"
    params.append(100 if targeted else limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    projected = 0
    with (nullcontext(conn) if targeted else conn):
        for row in rows:
            if _upper(row["msg_type"]) == "QSO":
                continue
            source_key = _text(row["ingest_source_key"]) or "legacy"
            source_name = _text(row["source"]) or "varac"
            source_id = f"varac:{source_key}:{source_name}"
            external_key = _text(row["guid"]) or _text(row["vmail_guid"]) or _text(row["id"])
            status = "READ" if _int(row["read_status"]) else ("ALERT" if _int(row["urgent"]) else "UNREAD")
            body = _text(row["body"])
            subject = _text(row["subject"]) or _subject(body)
            event_ts = _float(row["ts"])
            message_id = canonical_station_message_id(
                "varac",
                event_ts=event_ts,
                from_call=row["from_call"],
                to_call=row["to_call"],
                payload="\n".join(part for part in (subject, body) if part),
                message_type=row["msg_type"],
                durable_id=external_key if _text(row["guid"]) or _text(row["vmail_guid"]) else "",
            )
            source = MessageSourceRecord(
                source_id=source_id,
                source_family="varac",
                source_label=_source_label("VarAC", source_key),
                endpoint_or_path=_text(row["file_path"]),
                capabilities={"read": True, "delete": True, "native_open": True},
                provenance={"source_table": "varac_messages", "source_key": source_key, "source": source_name},
                last_seen_utc=_utc_from_ts(event_ts),
                last_ingested_utc=_utc_now(),
            )
            projection = MessageProjectionRecord(
                message_id=message_id,
                canonical_key=canonical_message_key("varac", message_id),
                content_hash=content_hash(
                    native_projector_version("varac"),
                    "varac",
                    external_key,
                    status,
                    subject,
                    body,
                ),
                primary_source_id=source_id,
                source_family="varac",
                source_label=source.source_label,
                message_type=_text(row["msg_type"]) or "VarAC",
                display_type="VarAC",
                status=status,
                severity="warning" if status == "ALERT" else "info",
                read_state=_read_state(status),
                from_call=_upper(row["from_call"]),
                to_call=_upper(row["to_call"]),
                group_name=_group(row["to_call"]),
                event_ts=event_ts,
                received_ts=event_ts,
                event_utc=_utc_from_ts(event_ts),
                received_utc=_utc_from_ts(event_ts),
                subject=subject,
                summary=(subject or body)[:240],
                body_preview=body[:1200],
                topics=_topics(subject, body, row["folder"], row["band"]),
                entities={
                    "folder": _text(row["folder"]),
                    "folder_label": _text(row["folder_label"]),
                    "band": _text(row["band"]),
                    "freq_hz": _float(row["freq_hz"]),
                    "snr": _float(row["snr"]),
                    "via_callsign": _upper(row["via_callsign"]),
                    "has_attachment": bool(_int(row["has_attachment"])),
                },
                actionable=status == "ALERT",
                operator_attention=status == "ALERT",
                retention_class="normal",
                search_text=_search_text(row["from_call"], row["to_call"], row["msg_type"], subject, body),
                projection_version=native_projector_version("varac"),
            )
            artifacts = []
            file_path = _text(row["file_path"])
            if file_path:
                artifacts.append(
                    MessageArtifactRecord(
                        artifact_id=stable_message_id(message_id, "varac_file", file_path),
                        message_id=message_id,
                        artifact_type="varac_file",
                        source_id=source_id,
                        external_key=external_key,
                        path=file_path,
                        content_hash=content_hash(file_path),
                    )
                )
            _emit_projection_bundle(
                conn,
                source,
                projection,
                ExternalMessageRef(
                    message_id=message_id,
                    source_id=source_id,
                    external_kind="varac_message",
                    external_key=external_key,
                    external_path=file_path,
                    delete_capability="delete_source",
                    read_capability="mark_read",
                    metadata={
                        "source_table": "varac_messages",
                        "source": source_name,
                        "row_id": _text(row["id"]),
                        "receipt_source_kind": f"varac_{source_name.lower()}",
                    },
                ),
                artifacts=artifacts,
                bundle_sink=bundle_sink,
            )
            projected += 1
        if not targeted:
            _set_checkpoint(conn, checkpoint_id, fingerprint, rows)
    return projected


def _project_mesh_messages(
    conn: sqlite3.Connection,
    limit: int,
    force: bool,
    *,
    external_keys: Sequence[str] | None = None,
    source_ids: Sequence[str] | None = None,
    bundle_sink: ProjectionBundleSink | None = None,
) -> int:
    """Project policy-approved local mesh messages into the canonical Inbox.

    ``mesh_messages`` remains the transport receipt and
    ``observation_projection`` remains the policy-owned operational record.
    A row is eligible here only when that observation explicitly grants the
    Inbox surface.  Ops-only, Map-only, pending, and rejected channel traffic
    therefore cannot leak into the Inbox read model.
    """

    if not table_exists(conn, "mesh_messages") or not table_exists(conn, "observation_projection"):
        return 0
    targeted = _targeted_keys(external_keys)
    targeted_sources = _targeted_source_ids(source_ids)
    query = """
        SELECT
            m.source_ref,
            m.adapter_id,
            m.transport,
            m.message_id AS transport_message_id,
            m.from_node,
            m.to_node,
            m.channel,
            m.portnum,
            m.text,
            m.rx_utc,
            m.hop_count,
            m.route_type,
            m.direct_receive,
            m.via_node,
            m.path_hops_json,
            m.snr,
            m.rssi,
            m.lat AS message_lat,
            m.lon AS message_lon,
            m.grid AS message_grid,
            m.topics_json AS message_topics_json,
            m.severity AS message_severity,
            m.raw_payload_json,
            m.updated_utc,
            o.source_radio_id,
            o.received_utc,
            o.event_utc,
            o.from_call,
            o.to_target,
            o.groups_json,
            o.observed_topics_json,
            o.operator_attention,
            o.status AS observation_status,
            o.urgency,
            o.subject AS observation_subject,
            o.summary AS observation_summary,
            o.state,
            o.grid AS observation_grid,
            o.lat AS observation_lat,
            o.lon AS observation_lon,
            o.provenance_json
        FROM mesh_messages AS m
        JOIN observation_projection AS o
          ON o.source_ref=m.source_ref
         AND LOWER(COALESCE(o.source_family,''))=LOWER(COALESCE(m.transport,''))
        WHERE 1=1
    """
    params: list[object] = []
    if targeted:
        marks = ",".join("?" for _ in targeted)
        query += f" AND m.source_ref IN ({marks})"
        params.extend(targeted)
        if targeted_sources:
            source_marks = ",".join("?" for _ in targeted_sources)
            query += (
                " AND ('mesh:' || COALESCE(NULLIF(m.transport,''),'mesh') || ':' || "
                f"COALESCE(NULLIF(m.adapter_id,''),'adapter')) IN ({source_marks})"
            )
            params.extend(targeted_sources)
    query += " ORDER BY COALESCE(NULLIF(m.rx_utc,''), m.updated_utc) DESC, m.rowid DESC LIMIT ?"
    params.append(100 if targeted else max(1, int(limit or 1)))
    rows = conn.execute(query, tuple(params)).fetchall()
    projected = 0
    for row in rows:
        provenance = _json_object(row["provenance_json"])
        surfaces = {
            _text(surface).lower()
            for surface in provenance.get("surfaces", ())
            if _text(surface)
        }
        if "inbox" not in surfaces:
            continue
        transport = _text(row["transport"]).lower() or "mesh"
        adapter_id = _text(row["adapter_id"]) or "adapter"
        source_ref = _text(row["source_ref"])
        source_id = f"mesh:{transport}:{adapter_id}"
        source_label = "MeshCore" if transport == "meshcore" else (
            "Meshtastic" if transport == "meshtastic" else "Local Mesh"
        )
        received_utc = _text(row["received_utc"] or row["rx_utc"] or row["updated_utc"])
        event_utc = _text(row["event_utc"] or row["rx_utc"] or received_utc)
        received_ts = _ts_from_utc(received_utc)
        event_ts = _ts_from_utc(event_utc) or received_ts
        body = _text(row["text"])
        subject = _text(row["observation_subject"]) or _subject(body)
        summary = _text(row["observation_summary"]) or body[:240]
        source_status = _upper(row["observation_status"] or row["message_severity"])
        status = "UNREAD" if source_status in {"", "INFO", "SEEN"} else source_status
        groups = [_group(value) for value in _json_array(row["groups_json"]) if _group(value)]
        topics = tuple(
            dict.fromkeys(
                _text(value)
                for value in (
                    *_json_array(row["observed_topics_json"]),
                    *_json_array(row["message_topics_json"]),
                )
                if _text(value)
            )
        )
        attention = bool(_int(row["operator_attention"]))
        mesh_structured = provenance.get("mesh_structured")
        if not isinstance(mesh_structured, dict):
            mesh_structured = {}
        structured_fingerprint = (
            json.dumps(mesh_structured, sort_keys=True, separators=(",", ":"))
            if mesh_structured else ""
        )
        content_hash_parts = [
            native_projector_version("mesh"),
            source_ref,
            row["updated_utc"],
            status,
            subject,
            body,
            topics,
        ]
        if structured_fingerprint:
            content_hash_parts.append(structured_fingerprint)
        payload_claim = mesh_structured.get("payload_claim") if mesh_structured else {}
        payload_location_claim = (
            isinstance(payload_claim, dict)
            and isinstance(payload_claim.get("location_evidence"), dict)
            and bool(payload_claim["location_evidence"].get("location_value"))
        )
        message_id = canonical_station_message_id(
            transport,
            event_ts=event_ts,
            from_call=row["from_call"] or row["from_node"],
            to_call=row["to_target"] or row["to_node"] or row["channel"],
            payload=body,
            message_type=source_label,
            durable_id=source_ref,
        )
        source = MessageSourceRecord(
            source_id=source_id,
            source_family=transport,
            source_label=f"{source_label} {adapter_id}",
            radio_id=_optional_int(row["source_radio_id"]),
            app_instance_id=adapter_id,
            capabilities={"read": False, "delete": False, "native_open": False},
            provenance={
                "source_table": "mesh_messages",
                "adapter_id": adapter_id,
                "transport": transport,
            },
            last_seen_utc=received_utc,
            last_ingested_utc=_utc_now(),
        )
        mesh_intelligence = {
            "mesh_policy": provenance.get("channel_policy", {}),
            "routing": provenance.get("routing", {}),
        }
        if mesh_structured:
            mesh_intelligence["mesh_structured"] = mesh_structured
        projection = MessageProjectionRecord(
            message_id=message_id,
            canonical_key=canonical_message_key(transport, message_id),
            content_hash=content_hash(*content_hash_parts),
            primary_source_id=source_id,
            source_family=transport,
            source_label=source.source_label,
            radio_id=_optional_int(row["source_radio_id"]),
            app_instance_id=adapter_id,
            message_type=source_label,
            display_type=source_label,
            status=status,
            severity=_severity_from_status(row["urgency"] or source_status),
            read_state=_read_state(status),
            from_call=_upper(row["from_call"] or row["from_node"]),
            to_call=_upper(row["to_target"] or row["to_node"]),
            group_name=groups[0] if groups else _group(row["channel"]),
            state_code=_upper(row["state"]),
            grid=_upper(row["observation_grid"] or row["message_grid"]),
            lat=(None if payload_location_claim else (
                row["observation_lat"] if row["observation_lat"] is not None else row["message_lat"]
            )),
            lon=(None if payload_location_claim else (
                row["observation_lon"] if row["observation_lon"] is not None else row["message_lon"]
            )),
            event_ts=event_ts,
            received_ts=received_ts or event_ts,
            event_utc=event_utc,
            received_utc=received_utc,
            subject=subject,
            summary=summary[:240],
            body_preview=body[:1200],
            topics=topics,
            entities={
                "adapter_id": adapter_id,
                "transport_message_id": _text(row["transport_message_id"]),
                "channel": _text(row["channel"]),
                "portnum": _text(row["portnum"]),
                "hop_count": _int(row["hop_count"]),
                "route_type": _text(row["route_type"]),
                "direct_receive": bool(_int(row["direct_receive"])),
                "via_node": _text(row["via_node"]),
                "path_hops": _json_array(row["path_hops_json"]),
                "snr": row["snr"],
                "rssi": row["rssi"],
            },
            actionable=attention,
            operator_attention=attention,
            intelligence_version=1,
            intelligence_utc=_utc_now(),
            intelligence=mesh_intelligence,
            retention_class="normal",
            search_text=_search_text(
                row["from_call"], row["from_node"], row["to_target"], row["to_node"],
                row["channel"], subject, summary, body, " ".join(topics),
            ),
            projection_version=native_projector_version("mesh"),
        )
        _emit_projection_bundle(
            conn,
            source,
            projection,
            ExternalMessageRef(
                message_id=message_id,
                source_id=source_id,
                external_kind="mesh_message",
                external_key=source_ref,
                metadata={
                    "source_table": "mesh_messages",
                    "adapter_id": adapter_id,
                    "transport": transport,
                    "transport_message_id": _text(row["transport_message_id"]),
                    "surfaces": sorted(surfaces),
                },
            ),
            bundle_sink=bundle_sink,
        )
        projected += 1
    return projected


def _project_sitrep_events(conn: sqlite3.Connection, limit: int, force: bool, *, external_keys: Sequence[str] | None = None, source_ids: Sequence[str] | None = None, bundle_sink: ProjectionBundleSink | None = None) -> int:
    if not table_exists(conn, "sitrep_events"):
        return 0
    targeted = _targeted_keys(external_keys)
    checkpoint_id = "native:sitrep_events"
    fingerprint = ""
    if not targeted:
        fingerprint = _table_fingerprint(conn, "sitrep_events", "COUNT(*)", "MAX(COALESCE(id, 0))", "MAX(COALESCE(event_ts, 0))", "MAX(COALESCE(updated_ts, 0))")
        fingerprint = content_hash(5, fingerprint)
        if _checkpoint_matches(conn, checkpoint_id, fingerprint, force=force):
            return 0
    query = """
        SELECT id, report_key, event_ts, event_ts_utc, from_call, target, report_group,
               grid, state_code, state_confidence, geo_confidence, scope, subtype,
               overall_status, power, water, medical, communications, internet,
               travel, food, fuel, crime, civil_unrest, political, transport_mode,
               remarks_text, brevity_code, brevity_summary, source_first, source_last,
               source_count, sources_json, source_refs_json, raw_payload_json, updated_ts
          FROM sitrep_events
    """
    params: list[object] = []
    if targeted:
        marks = ",".join("?" for _ in targeted)
        query += (
            " WHERE CAST(COALESCE(NULLIF(report_key, ''), id) AS TEXT) "
            f"IN ({marks})"
        )
        params.extend(targeted)
    query += " ORDER BY COALESCE(event_ts, 0) DESC, id DESC LIMIT ?"
    params.append(100 if targeted else limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    projected = 0
    with (nullcontext(conn) if targeted else conn):
        for row in rows:
            source_id = "sitrep:fused"
            external_key = _text(row["report_key"]) or _text(row["id"])
            status = _status_from_condition(row["overall_status"])
            body = _sitrep_body(row)
            records = _brevity_payload(row["raw_payload_json"]).get("brevity_models", [])
            if not isinstance(records, list):
                records = []
            topic_body = _sitrep_body(row, include_brevity=False)
            event_ts = _float(row["event_ts"])
            message_id = canonical_station_message_id(
                "sitrep",
                event_ts=event_ts,
                from_call=row["from_call"],
                to_call=row["target"],
                payload=body,
                message_type=row["subtype"],
                durable_id=external_key if event_ts <= 0 else "",
            )
            existing_ref = conn.execute(
                "SELECT message_id FROM message_external_refs WHERE source_id=? AND external_kind=? AND external_key=?",
                (source_id, "sitrep_event", external_key),
            ).fetchone()
            if existing_ref:
                message_id = existing_ref[0]
            source = MessageSourceRecord(
                source_id=source_id,
                source_family="sitrep",
                source_label="SitRep",
                capabilities={"read": True, "delete": True, "native_open": True},
                provenance={"source_table": "sitrep_events"},
                last_seen_utc=_utc_from_ts(event_ts),
                last_ingested_utc=_utc_now(),
            )
            projection = MessageProjectionRecord(
                message_id=message_id,
                canonical_key=canonical_message_key("sitrep", message_id),
                content_hash=content_hash(native_projector_version("sitrep"), "sitrep", external_key, row["updated_ts"], body),
                primary_source_id=source_id,
                source_family="sitrep",
                source_label=source.source_label,
                message_type=_text(row["subtype"]) or "SitRep",
                display_type="SitRep",
                status=status,
                severity=_severity_from_status(status),
                read_state=_read_state(status),
                from_call=_upper(row["from_call"]),
                to_call=_upper(row["target"]),
                group_name=_group(row["report_group"] or row["target"]),
                scope=_text(row["scope"]),
                state_code=_upper(row["state_code"]),
                grid=_upper(row["grid"]),
                event_ts=event_ts,
                received_ts=event_ts,
                event_utc=_text(row["event_ts_utc"]) or _utc_from_ts(event_ts),
                received_utc=_text(row["event_ts_utc"]) or _utc_from_ts(event_ts),
                subject=_sitrep_subject(row),
                summary=body[:240],
                body_preview=body[:1200],
                topics=_topics(topic_body, row["overall_status"], row["power"], row["water"], row["medical"], row["communications"], row["fuel"], *intelligence_values(records)),
                entities={**_row_entities(row, ("overall_status", "power", "water", "medical", "communications", "internet", "travel", "food", "fuel", "crime", "civil_unrest", "political")), "brevity_models": records},
                actionable=status in {"YELLOW", "RED", "ALERT"},
                operator_attention=status in {"YELLOW", "RED", "ALERT"},
                confidence=0.9,
                recommended_action="review" if status in {"YELLOW", "RED"} else "",
                retention_class="operational",
                search_text=_search_text(row["from_call"], row["target"], row["report_group"], body),
                projection_version=native_projector_version("sitrep"),
            )
            _emit_projection_bundle(
                conn,
                source,
                projection,
                ExternalMessageRef(
                    message_id=message_id,
                    source_id=source_id,
                    external_kind="sitrep_event",
                    external_key=external_key,
                    delete_capability="delete_source",
                    read_capability="mark_read",
                    metadata={"source_table": "sitrep_events", "row_id": _text(row["id"]), "source_refs": _json_array(row["source_refs_json"])},
                ),
                bundle_sink=bundle_sink,
            )
            projected += 1
        if not targeted:
            _set_checkpoint(conn, checkpoint_id, fingerprint, rows)
    return projected


def _brevity_payload(raw):
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _project_commstat_artifacts(conn: sqlite3.Connection, limit: int, force: bool, *, external_keys: Sequence[str] | None = None, source_ids: Sequence[str] | None = None, bundle_sink: ProjectionBundleSink | None = None) -> int:
    if not table_exists(conn, "commstat_artifacts"):
        return 0
    targeted = _targeted_keys(external_keys)
    checkpoint_id = "native:commstat_artifacts"
    fingerprint = ""
    if not targeted:
        fingerprint = _table_fingerprint(conn, "commstat_artifacts", "COUNT(*)", "MAX(COALESCE(id, 0))", "MAX(COALESCE(event_ts, 0))", "MAX(COALESCE(updated_ts, 0))")
        fingerprint = content_hash(5, fingerprint)
        if table_exists(conn, "commstat_artifact_deletions"):
            fingerprint = content_hash(fingerprint, _table_fingerprint(conn, "commstat_artifact_deletions", "COUNT(*)", "MAX(COALESCE(deleted_ts, 0))"))
        if _checkpoint_matches(conn, checkpoint_id, fingerprint, force=force):
            return 0
    deletion_join = ""
    deletion_where = ""
    if table_exists(conn, "commstat_artifact_deletions"):
        deletion_join = "LEFT JOIN commstat_artifact_deletions cad ON cad.artifact_key = ca.artifact_key"
        deletion_where = "WHERE cad.artifact_key IS NULL"
    query = f"""
        SELECT ca.id, ca.artifact_key, ca.artifact_kind, ca.subtype, ca.event_ts,
               ca.event_ts_utc, ca.from_call, ca.target, ca.report_group, ca.grid,
               ca.state_code, ca.scope, ca.transport_mode, ca.reach_mode,
               ca.origin_path, ca.status_label, ca.alert_color, ca.title,
               ca.body_text, ca.remarks_text, ca.source_first, ca.source_last,
               ca.source_count, ca.sources_json, ca.source_refs_json,
               ca.external_ids_json, ca.payload_json, ca.updated_ts
          FROM commstat_artifacts ca
          {deletion_join}
          {deletion_where}
    """
    params: list[object] = []
    if targeted:
        marks = ",".join("?" for _ in targeted)
        target_where = (
            "CAST(COALESCE(NULLIF(ca.artifact_key, ''), ca.id) AS TEXT) "
            f"IN ({marks})"
        )
        query += f" {' AND ' if deletion_where else ' WHERE '}{target_where}"
        params.extend(targeted)
    query += " ORDER BY COALESCE(ca.event_ts, 0) DESC, ca.id DESC LIMIT ?"
    params.append(100 if targeted else limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    projected = 0
    with (nullcontext(conn) if targeted else conn):
        for row in rows:
            source_id = "commstat:artifacts"
            external_key = _text(row["artifact_key"]) or _text(row["id"])
            status = _upper(row["status_label"]) or _upper(row["alert_color"]) or "INFO"
            body = _text(row["body_text"]) or _text(row["remarks_text"]) or _text(row["title"])
            brevity = _brevity_payload(row["payload_json"])
            records = brevity.get("brevity_models", [])
            if not isinstance(records, list):
                records = []
            interpretation = _text(brevity.get("brevity_summary"))
            display_body = "\n".join(part for part in (body, interpretation) if part)
            event_ts = _float(row["event_ts"])
            message_id = canonical_station_message_id(
                "commstat",
                event_ts=event_ts,
                from_call=row["from_call"],
                to_call=row["target"],
                payload="\n".join(
                    part for part in (_text(row["title"]), body) if part
                ),
                message_type=row["subtype"] or row["artifact_kind"],
                durable_id=external_key if event_ts <= 0 else "",
            )
            source = MessageSourceRecord(
                source_id=source_id,
                source_family="commstat",
                source_label="CommStat RF",
                endpoint_or_path=_text(row["origin_path"]),
                capabilities={"read": True, "delete": True, "native_open": True},
                provenance={"source_table": "commstat_artifacts"},
                last_seen_utc=_utc_from_ts(event_ts),
                last_ingested_utc=_utc_now(),
            )
            projection = MessageProjectionRecord(
                message_id=message_id,
                canonical_key=canonical_message_key("commstat", message_id),
                content_hash=content_hash(native_projector_version("commstat"), "commstat", external_key, row["updated_ts"], status, body, records),
                primary_source_id=source_id,
                source_family="commstat",
                source_label=source.source_label,
                message_type=_text(row["subtype"]) or _text(row["artifact_kind"]) or "CommStat",
                display_type="CommStat",
                status=status,
                severity=_severity_from_status(status),
                read_state=_read_state(status),
                from_call=_upper(row["from_call"]),
                to_call=_upper(row["target"]),
                group_name=_group(row["report_group"] or row["target"]),
                scope=_text(row["scope"]),
                state_code=_upper(row["state_code"]),
                grid=_upper(row["grid"]),
                event_ts=event_ts,
                received_ts=event_ts,
                event_utc=_text(row["event_ts_utc"]) or _utc_from_ts(event_ts),
                received_utc=_text(row["event_ts_utc"]) or _utc_from_ts(event_ts),
                subject=_text(row["title"]) or _subject(body),
                summary=display_body[:240],
                body_preview=display_body[:1200],
                topics=_topics(body, row["status_label"], row["alert_color"], row["reach_mode"], row["transport_mode"], *intelligence_values(records)),
                entities={**_row_entities(row, ("artifact_kind", "subtype", "reach_mode", "transport_mode", "source_count")), "brevity_models": records},
                actionable=status in {"YELLOW", "RED", "ALERT", "WARNING"},
                operator_attention=status in {"YELLOW", "RED", "ALERT", "WARNING"},
                confidence=0.9,
                recommended_action="review" if status in {"YELLOW", "RED", "ALERT", "WARNING"} else "",
                retention_class="operational",
                search_text=_search_text(row["from_call"], row["target"], row["report_group"], row["title"], display_body),
                projection_version=native_projector_version("commstat"),
            )
            _emit_projection_bundle(
                conn,
                source,
                projection,
                ExternalMessageRef(
                    message_id=message_id,
                    source_id=source_id,
                    external_kind="commstat_artifact",
                    external_key=external_key,
                    external_path=_text(row["origin_path"]),
                    delete_capability="delete_source",
                    read_capability="mark_read",
                    metadata={
                        "source_table": "commstat_artifacts",
                        "row_id": _text(row["id"]),
                        "source_refs": _json_array(row["source_refs_json"]),
                        "external_ids": _json_array(row["external_ids_json"]),
                    },
                ),
                artifacts=(
                    MessageArtifactRecord(
                        artifact_id=stable_message_id(message_id, "commstat_artifact", external_key),
                        message_id=message_id,
                        artifact_type="commstat_artifact",
                        source_id=source_id,
                        external_key=external_key,
                        path=_text(row["origin_path"]),
                        content_hash=content_hash(_text(row["payload_json"]), body),
                        metadata={"payload": _json_object(row["payload_json"])},
                    ),
                ),
                bundle_sink=bundle_sink,
            )
            projected += 1
        if not targeted:
            _set_checkpoint(conn, checkpoint_id, fingerprint, rows)
    return projected


def _upsert_bundle(
    conn: sqlite3.Connection,
    source: MessageSourceRecord,
    message: MessageProjectionRecord,
    ref: ExternalMessageRef,
    *,
    artifacts: Sequence[MessageArtifactRecord] = (),
) -> None:
    upsert_message_source(conn, source)
    upsert_message_projection(conn, message)
    upsert_external_ref(conn, ref)
    for artifact in artifacts:
        upsert_message_artifact(conn, artifact)


def _dirty_item_value(item: object, name: str) -> str:
    if isinstance(item, Mapping):
        return _text(item.get(name, ""))
    return _text(getattr(item, name, ""))


def prepare_native_message_bundles(
    conn: sqlite3.Connection,
    dirty_items: Sequence[object],
) -> tuple[tuple[ProjectionBundle, ...], tuple[object, ...]]:
    """Prepare exact bundles for named native rows without DML or reconciliation."""

    projectors: Mapping[str, Callable[..., int]] = {
        "js8": _project_js8_messages,
        "spotter": _project_spotter_traffic,
        "varac": _project_varac_messages,
        "sitrep": _project_sitrep_events,
        "commstat": _project_commstat_artifacts,
        "mesh": _project_mesh_messages,
    }
    grouped: dict[str, list[object]] = {}
    unsupported: list[object] = []
    for item in dirty_items:
        family = _dirty_item_value(item, "source_family").lower()
        key = _dirty_item_value(item, "external_key")
        if family not in projectors or not key:
            unsupported.append(item)
            continue
        grouped.setdefault(family, []).append(item)

    bundles: list[ProjectionBundle] = []
    missing: list[object] = list(unsupported)
    for family, family_items in grouped.items():
        requested = {
            (
                _dirty_item_value(item, "source_id"),
                _dirty_item_value(item, "external_key"),
            ): item
            for item in family_items
        }
        found: set[tuple[str, str]] = set()

        def sink(bundle: ProjectionBundle) -> None:
            bundles.append(bundle)
            for ref in bundle.refs:
                identity = (_text(ref.source_id), _text(ref.external_key))
                if identity in requested:
                    found.add(identity)

        source_keys: dict[str, list[str]] = {}
        for source_id, key in requested:
            source_keys.setdefault(source_id, []).append(key)
        for source_id, values in source_keys.items():
            keys = tuple(dict.fromkeys(values))
            for start in range(0, len(keys), 100):
                projectors[family](
                    conn,
                    100,
                    False,
                    external_keys=keys[start : start + 100],
                    source_ids=(source_id,),
                    bundle_sink=sink,
                )
        missing.extend(item for identity, item in requested.items() if identity not in found)
    return tuple(bundles), tuple(missing)


def _checkpoint_matches(conn: sqlite3.Connection, source_id: str, fingerprint: str, *, force: bool) -> bool:
    if force:
        return False
    try:
        row = conn.execute(
            "SELECT content_fingerprint FROM message_projection_checkpoint WHERE source_id=?",
            (str(source_id or "").strip(),),
        ).fetchone()
    except Exception:
        return False
    current = str(row["content_fingerprint"] or "") if row is not None else ""
    return bool(current and current == fingerprint)


def _set_checkpoint(conn: sqlite3.Connection, source_id: str, fingerprint: str, rows: Sequence[object]) -> None:
    last_key = ""
    last_ts = 0.0
    for row in rows:
        if isinstance(row, FileRecord):
            key = str(row.path)
            ts = float(row.mtime or 0.0)
        else:
            row_id = _text(_row_value(row, "id"))
            key = _text(_row_value(row, "artifact_key")) or _text(_row_value(row, "report_key")) or _text(_row_value(row, "guid")) or row_id
            ts = _float(_row_value(row, "event_ts")) or _float(_row_value(row, "utc_ts")) or _float(_row_value(row, "ts"))
        if ts >= last_ts:
            last_ts = ts
            last_key = key
    set_message_projection_checkpoint(
        conn,
        MessageProjectionCheckpoint(
            source_id=source_id,
            last_external_key=last_key,
            last_event_ts=last_ts,
            content_fingerprint=fingerprint,
        ),
    )


def _table_fingerprint(conn: sqlite3.Connection, table_name: str, *expressions: str) -> str:
    if not expressions:
        expressions = ("COUNT(*)",)
    try:
        row = conn.execute(f"SELECT {', '.join(expressions)} FROM {table_name}").fetchone()
    except Exception:
        return ""
    values = tuple(row or ())
    return content_hash(PROJECTOR_VERSION, table_name, values)


def _row_value(row: sqlite3.Row, key: str) -> object:
    try:
        return row[key]
    except Exception:
        return None


def _text(value: object) -> str:
    return str(value or "").strip()


def _upper(value: object) -> str:
    return _text(value).upper()


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _int(value: object) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _optional_int(value: object) -> int | None:
    parsed = _int(value)
    return parsed if parsed > 0 else None


def _group(value: object) -> str:
    return normalize_group_name(_text(value)).lstrip("@").upper()


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _utc_from_ts(value: object) -> str:
    ts = _float(value)
    if ts <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _ts_from_utc(value: object) -> float:
    text = _text(value)
    if not text:
        return 0.0
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


def _source_label(base: str, source_key: str) -> str:
    return f"{base} {source_key}".strip() if source_key and source_key != "legacy" else base


def _file_source_base_label(origin: str) -> str:
    return {
        "flmsg": "FLMsg",
        "flamp": "FLAmp",
        "varac": "VarAC",
        "bbs": "BBS",
    }.get(_text(origin).lower(), "File")


def _file_message_type(origin: str, path: Path) -> str:
    base = _file_source_base_label(origin)
    suffix = path.suffix.lower().lstrip(".")
    return f"{base} {suffix.upper()}".strip() if suffix else base


def _q_id_from_path(path: Path) -> str:
    name = path.name
    for pattern in (r"\bQ[0-9A-Z]{3,}\b", r"\b[A-Z]{1,4}[0-9]{3,}\b"):
        match = re.search(pattern, name.upper())
        if match:
            return match.group(0)
    return ""


def _block_id_from_path(path: Path) -> str:
    match = re.search(r"(?:BLOCK|BLK|[-_])([0-9]{1,4})(?:\D|$)", path.name.upper())
    return match.group(1) if match else ""


def _read_state(status: str) -> str:
    clean = _upper(status)
    if clean in {"READ", "GREEN", "INFO"}:
        return "read" if clean == "READ" else "info"
    if clean in {"UNREAD", "NEW", "ALERT", "YELLOW", "RED", "WARNING"}:
        return "new"
    return "info"


def _severity_from_status(status: str) -> str:
    clean = _upper(status)
    if clean in {"RED", "ALERT", "CRITICAL"}:
        return "critical"
    if clean in {"YELLOW", "WARNING", "WARN"}:
        return "warning"
    if clean in {"WATCH"}:
        return "watch"
    return "info"


def _status_from_condition(value: object) -> str:
    clean = _text(value).lower()
    if clean in {"red", "critical", "emergency"}:
        return "RED"
    if clean in {"yellow", "warning", "degraded", "limited"}:
        return "YELLOW"
    if clean in {"green", "ok", "normal"}:
        return "GREEN"
    return clean.upper() if clean else "INFO"


def _subject(*values: object) -> str:
    for value in values:
        text = _text(value).replace("\r", " ").replace("\n", " ")
        if text:
            return text[:80]
    return ""


def _topics(*values: object) -> tuple[str, ...]:
    joined = " ".join(_text(value).lower() for value in values if _text(value))
    found: list[str] = []
    rules = (
        ("Power", ("power", "generator", "battery", "fuel")),
        ("Water", ("water",)),
        ("Medical", ("medical", "med", "injury", "health")),
        ("Comms", ("comm", "radio", "internet", "phone")),
        ("Travel/Roads", ("travel", "road", "route")),
        ("Safety", ("crime", "civil", "unrest", "security")),
        ("BBS", ("bbs", "vmail", "mailbox")),
    )
    for label, needles in rules:
        if any(needle in joined for needle in needles):
            found.append(label)
    return tuple(dict.fromkeys(found))


def _search_text(*values: object) -> str:
    return " ".join(_text(value) for value in values if _text(value))[:4000]


def _json_array(value: object) -> list[object]:
    try:
        parsed = json.loads(_text(value) or "[]")
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(_text(value) or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_entities(row: sqlite3.Row, keys: Sequence[str]) -> Mapping[str, object]:
    out: dict[str, object] = {}
    for key in keys:
        value = _row_value(row, key)
        if value not in (None, ""):
            out[key] = value
    return out


def _sitrep_subject(row: sqlite3.Row) -> str:
    parts = [_upper(row["from_call"]), _text(row["overall_status"]).upper(), _text(row["brevity_code"])]
    return " ".join(part for part in parts if part)[:120]


def _sitrep_body(row: sqlite3.Row, *, include_brevity: bool = True) -> str:
    fields = (
        ("Overall", row["overall_status"]),
        ("Power", row["power"]),
        ("Water", row["water"]),
        ("Medical", row["medical"]),
        ("Comms", row["communications"]),
        ("Internet", row["internet"]),
        ("Travel", row["travel"]),
        ("Food", row["food"]),
        ("Fuel", row["fuel"]),
        ("Crime", row["crime"]),
        ("Civil Unrest", row["civil_unrest"]),
        ("Political", row["political"]),
    )
    parts = [f"{label}: {_text(value)}" for label, value in fields if _text(value)]
    remarks = _text(row["remarks_text"])
    brevity = _text(row["brevity_summary"])
    if brevity and include_brevity:
        parts.append(f"Brevity: {brevity}")
    if remarks:
        parts.append(f"Remarks: {remarks}")
    return " | ".join(parts)
