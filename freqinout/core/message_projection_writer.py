"""Bounded serialized writer for normalized message projection bundles.

This module deliberately owns no source discovery, parsing, schema migration, or
Qt objects.  Producers prepare immutable :class:`ProjectionBundle` values and
submit them here.  A single daemon serial executor owns the SQLite connection
while it is active, so foreground code and source adapters never contend as
projection writers.

The startup database initializer is responsible for ensuring the projection
schema before this writer is started.  The row helpers imported below are
schema-free runtime helpers; do not add schema assurance to this module.
"""

from __future__ import annotations

from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import dataclass, field
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from freqinout.core.message_projection_store import (
    ExternalMessageRef,
    MessageArtifactRecord,
    MessageProjectionRecord,
    MessageSourceRecord,
    PROJECTION_SCHEMA_VERSION,
    merge_message_projections,
    upsert_external_ref,
    upsert_message_artifact,
    upsert_message_projection,
    upsert_message_source,
)
from freqinout.core.message_projection_repair import plan_legacy_projection_merges
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
from freqinout.core.sqlite_utils import connect_sqlite_runtime_write
from freqinout.core.perf_metrics import emit_span, span as perf_span


# Difference/signature preparation happens before ``BEGIN IMMEDIATE`` and can
# still monopolize the Python interpreter on large JSON artifacts.  Keep that
# CPU-bound unit small as well as the transaction itself; the coordinator owns
# the larger 100-identity catch-up budget.
MAX_BUNDLES_PER_TRANSACTION = 25
MAX_REPAIRS_PER_TRANSACTION = 10
MAX_TRANSACTION_SECONDS = 0.050
INITIAL_BUSY_RETRY_SECONDS = 0.050
MAX_BUSY_RETRY_SECONDS = 2.0
_WRITER_REGISTRY_LOCK = threading.Lock()
_WRITER_REGISTRY: dict[str, "ProjectionBundleWriter"] = {}


def _clean_text(value: object) -> str:
    """Match projection-store text normalization without importing private API."""

    text = str(value or "")
    return text.encode("utf-8", "replace").decode("utf-8", "replace") if text else ""


def _clean_json(value: object, default: str = "{}") -> str:
    """Return deterministic JSON matching the store's persisted representation."""

    def clean(item: object) -> object:
        if isinstance(item, str):
            return _clean_text(item)
        if isinstance(item, Mapping):
            return {_clean_text(key): clean(val) for key, val in item.items()}
        if isinstance(item, (list, tuple, set)):
            return [clean(part) for part in item]
        return item

    try:
        return json.dumps(clean(value), sort_keys=True, separators=(",", ":"))
    except Exception:
        return default


def _stored_json(value: object, default: str = "{}") -> str:
    """Normalize a JSON value read from SQLite for differential comparison."""

    text = _clean_text(value)
    if not text:
        return default
    try:
        return _clean_json(json.loads(text), default)
    except Exception:
        return text


def source_signature(source: MessageSourceRecord) -> str:
    """Stable signature of source metadata owned by a prepared bundle.

    ``last_ingested_utc`` is a volatile observation timestamp.  Replaying an
    otherwise unchanged source must not turn it into a source-row write (and a
    wake-up of the UI refresh path).  The durable source identity, capability,
    provenance, enabled state, and last-seen facts remain part of the
    comparison so real source corrections still propagate.
    """

    return _clean_json(
        {
            "source_id": source.source_id,
            "source_family": source.source_family,
            "source_label": source.source_label,
            "radio_id": source.radio_id,
            "app_instance_id": source.app_instance_id,
            "endpoint_or_path": source.endpoint_or_path,
            "capabilities": source.capabilities,
            "provenance": source.provenance,
            "enabled": bool(source.enabled),
            "last_seen_utc": source.last_seen_utc,
        }
    )


def _stored_source_signature(row: sqlite3.Row) -> str:
    return _clean_json(
        {
            "source_id": row["source_id"],
            "source_family": row["source_family"],
            "source_label": row["source_label"],
            "radio_id": row["radio_id"],
            "app_instance_id": row["app_instance_id"],
            "endpoint_or_path": row["endpoint_or_path"],
            "capabilities": json.loads(_stored_json(row["capabilities_json"])),
            "provenance": json.loads(_stored_json(row["provenance_json"])),
            "enabled": bool(row["enabled"]),
            "last_seen_utc": row["last_seen_utc"],
        }
    )


def _stored_message_value(row: sqlite3.Row, name: str) -> object:
    """Read a stored projection value while tolerating legacy nullable rows."""

    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _message_signature(
    message: MessageProjectionRecord,
    existing: sqlite3.Row | None = None,
) -> tuple[object, ...]:
    """Canonical persisted semantics for an incoming message projection.

    Content hashes deliberately summarize payload identity, not every field
    this projection owns.  In particular classification, display, geography,
    confidence and inbox policy may be corrected without a source payload hash
    change.  Compare the complete persisted message shape here, while applying
    the same operator-state preservation rules as the UPSERT statement.

    ``projected_utc`` is write bookkeeping rather than message semantics and
    is intentionally excluded.
    """

    prior_read = _clean_text(_stored_message_value(existing, "read_state")) if existing is not None else ""
    incoming_read = _clean_text(message.read_state)
    incoming_status = _clean_text(message.status)
    preserve_read = (
        prior_read.lower() == "read"
        and incoming_read.lower() in {"new", "unread", ""}
    )
    persisted_read = prior_read if preserve_read else incoming_read
    persisted_status = (
        _clean_text(_stored_message_value(existing, "status"))
        if preserve_read and incoming_status.upper() in {"NEW", "UNREAD"}
        else incoming_status
    )

    prior_pinned = bool(_stored_message_value(existing, "pinned")) if existing is not None else False
    prior_archived = bool(_stored_message_value(existing, "archived")) if existing is not None else False
    prior_deleted = bool(_stored_message_value(existing, "deleted")) if existing is not None else False
    prior_deleted_utc = _clean_text(_stored_message_value(existing, "deleted_utc")) if existing is not None else ""
    persisted_deleted_utc = (
        prior_deleted_utc if prior_deleted and prior_deleted_utc else _clean_text(message.deleted_utc)
    )

    return (
        _clean_text(message.message_id),
        _clean_text(message.canonical_key),
        _clean_text(message.content_hash),
        _clean_text(message.primary_source_id),
        _clean_text(message.source_family),
        _clean_text(message.source_label),
        message.radio_id,
        _clean_text(message.app_instance_id),
        _clean_text(message.message_type),
        _clean_text(message.display_type),
        persisted_status,
        _clean_text(message.severity),
        persisted_read,
        _clean_text(message.from_call).upper(),
        _clean_text(message.to_call).upper(),
        _clean_text(message.group_name).lstrip("@").upper(),
        _clean_text(message.scope),
        _clean_text(message.state_code).upper(),
        _clean_text(message.grid).upper(),
        message.lat,
        message.lon,
        float(message.event_ts or 0.0),
        float(message.received_ts or 0.0),
        _clean_text(message.event_utc),
        _clean_text(message.received_utc),
        _clean_text(message.subject),
        _clean_text(message.summary),
        _clean_text(message.body_preview),
        _clean_json(list(message.topics), "[]"),
        _clean_json(message.entities),
        1 if message.actionable else 0,
        1 if message.operator_attention else 0,
        float(message.confidence or 0.0),
        _clean_text(message.recommended_action),
        int(message.intelligence_version or 0),
        _clean_text(message.intelligence_utc),
        _clean_json(message.intelligence),
        1 if prior_pinned else (1 if message.pinned else 0),
        1 if prior_archived else (1 if message.archived else 0),
        1 if prior_deleted else (1 if message.deleted else 0),
        persisted_deleted_utc,
        _clean_text(message.retention_class or "normal"),
        1 if message.inbox_visible else 0,
        _clean_text(message.inbox_suppression_reason),
        int(message.classification_version or 0),
        _clean_text(message.search_text).lower(),
        int(message.projection_version or PROJECTION_SCHEMA_VERSION),
    )


def _stored_message_signature(row: sqlite3.Row | None) -> tuple[object, ...] | None:
    """Canonical semantic signature for a persisted projection row."""

    if row is None:
        return None
    return (
        _clean_text(row["message_id"]),
        _clean_text(row["canonical_key"]),
        _clean_text(row["content_hash"]),
        _clean_text(row["primary_source_id"]),
        _clean_text(row["source_family"]),
        _clean_text(row["source_label"]),
        row["radio_id"],
        _clean_text(row["app_instance_id"]),
        _clean_text(row["message_type"]),
        _clean_text(row["display_type"]),
        _clean_text(row["status"]),
        _clean_text(row["severity"]),
        _clean_text(row["read_state"]),
        _clean_text(row["from_call"]).upper(),
        _clean_text(row["to_call"]).upper(),
        _clean_text(row["group_name"]).lstrip("@").upper(),
        _clean_text(row["scope"]),
        _clean_text(row["state_code"]).upper(),
        _clean_text(row["grid"]).upper(),
        row["lat"],
        row["lon"],
        float(row["event_ts"] or 0.0),
        float(row["received_ts"] or 0.0),
        _clean_text(row["event_utc"]),
        _clean_text(row["received_utc"]),
        _clean_text(row["subject"]),
        _clean_text(row["summary"]),
        _clean_text(row["body_preview"]),
        _stored_json(row["topics_json"], "[]"),
        _stored_json(row["entities_json"]),
        1 if bool(row["actionable"]) else 0,
        1 if bool(row["operator_attention"]) else 0,
        float(row["confidence"] or 0.0),
        _clean_text(row["recommended_action"]),
        int(row["intelligence_version"] or 0),
        _clean_text(row["intelligence_utc"]),
        _stored_json(row["intelligence_json"]),
        1 if bool(row["pinned"]) else 0,
        1 if bool(row["archived"]) else 0,
        1 if bool(row["deleted"]) else 0,
        _clean_text(row["deleted_utc"]),
        _clean_text(row["retention_class"] or "normal"),
        1 if bool(row["inbox_visible"]) else 0,
        _clean_text(row["inbox_suppression_reason"]),
        int(row["classification_version"] or 0),
        _clean_text(row["search_text"]).lower(),
        int(row["projection_version"] or PROJECTION_SCHEMA_VERSION),
    )


def _ref_signature(ref: ExternalMessageRef) -> tuple[object, ...]:
    return (
        _clean_text(ref.message_id),
        _clean_text(ref.source_id),
        _clean_text(ref.external_kind),
        _clean_text(ref.external_key),
        _clean_text(ref.external_path),
        float(ref.external_mtime or 0.0),
        int(ref.external_size or 0),
        _clean_text(ref.external_hash),
        _clean_text(ref.delete_capability),
        _clean_text(ref.read_capability),
        _clean_json(ref.metadata),
    )


def _stored_ref_signature(row: sqlite3.Row | None) -> tuple[object, ...] | None:
    if row is None:
        return None
    return (
        _clean_text(row["message_id"]),
        _clean_text(row["source_id"]),
        _clean_text(row["external_kind"]),
        _clean_text(row["external_key"]),
        _clean_text(row["external_path"]),
        float(row["external_mtime"] or 0.0),
        int(row["external_size"] or 0),
        _clean_text(row["external_hash"]),
        _clean_text(row["delete_capability"]),
        _clean_text(row["read_capability"]),
        _stored_json(row["metadata_json"]),
    )


def _artifact_signature(artifact: MessageArtifactRecord) -> tuple[object, ...]:
    return (
        _clean_text(artifact.message_id),
        _clean_text(artifact.artifact_type),
        _clean_text(artifact.source_id),
        _clean_text(artifact.external_key),
        _clean_text(artifact.path),
        _clean_text(artifact.content_hash),
        _clean_text(artifact.q_id),
        _clean_text(artifact.block_id),
        _clean_text(artifact.transfer_id),
        int(artifact.block_count or 0),
        _clean_text(artifact.missing_blocks_json),
        _clean_text(artifact.transfer_state),
        _clean_text(artifact.signature_state),
        _clean_text(artifact.verified_utc),
        _clean_json(artifact.metadata),
    )


def _stored_artifact_signature(row: sqlite3.Row | None) -> tuple[object, ...] | None:
    if row is None:
        return None
    return (
        _clean_text(row["message_id"]),
        _clean_text(row["artifact_type"]),
        _clean_text(row["source_id"]),
        _clean_text(row["external_key"]),
        _clean_text(row["path"]),
        _clean_text(row["content_hash"]),
        _clean_text(row["q_id"]),
        _clean_text(row["block_id"]),
        _clean_text(row["transfer_id"]),
        int(row["block_count"] or 0),
        _clean_text(row["missing_blocks_json"]),
        _clean_text(row["transfer_state"]),
        _clean_text(row["signature_state"]),
        _clean_text(row["verified_utc"]),
        _stored_json(row["metadata_json"]),
    )


@dataclass(frozen=True)
class ProjectionBundle:
    """One atomic projection unit prepared outside the SQLite write transaction."""

    source: MessageSourceRecord
    message: MessageProjectionRecord
    refs: tuple[ExternalMessageRef, ...] = field(default_factory=tuple)
    artifacts: tuple[MessageArtifactRecord, ...] = field(default_factory=tuple)
    dirty_keys: tuple[str, ...] = field(default_factory=tuple)
    dirty_owner: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "refs", tuple(self.refs))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "dirty_keys", tuple(_clean_text(key) for key in self.dirty_keys if _clean_text(key)))
        if not _clean_text(self.source.source_id):
            raise ValueError("ProjectionBundle.source.source_id is required")
        if not _clean_text(self.message.message_id):
            raise ValueError("ProjectionBundle.message.message_id is required")
        message_id = _clean_text(self.message.message_id)
        bad_ref = next((ref for ref in self.refs if _clean_text(ref.message_id) != message_id), None)
        if bad_ref is not None:
            raise ValueError("ProjectionBundle refs must belong to its message")
        bad_artifact = next((artifact for artifact in self.artifacts if _clean_text(artifact.message_id) != message_id), None)
        if bad_artifact is not None:
            raise ValueError("ProjectionBundle artifacts must belong to its message")

    @property
    def effective_source_signature(self) -> str:
        return source_signature(self.source)


@dataclass(frozen=True)
class ProjectionWriteResult:
    """Durable-work-friendly outcome; callers retain deferred dirty identities."""

    state: str
    attempted_bundles: int = 0
    committed_bundles: int = 0
    skipped_bundles: int = 0
    deferred_bundles: int = 0
    transactions: int = 0
    source_upserts: int = 0
    message_upserts: int = 0
    ref_upserts: int = 0
    artifact_upserts: int = 0
    indexed_messages: int = 0
    busy_retries: int = 0
    elapsed_ms: float = 0.0
    max_transaction_ms: float = 0.0
    committed_message_ids: tuple[str, ...] = field(default_factory=tuple)
    deferred_message_ids: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""

    @property
    def completed(self) -> bool:
        return self.state == "committed" and not self.deferred_bundles


@dataclass(frozen=True)
class ProjectionRepairResult:
    """Outcome of one explicit derived-index convergence pass."""

    state: str
    planned: int = 0
    repaired: int = 0
    transactions: int = 0
    busy_retries: int = 0
    max_transaction_ms: float = 0.0
    error: str = ""


@dataclass(frozen=True)
class ProjectionDeleteRequest:
    source_id: str
    external_kind: str
    external_key: str
    dirty_key: str = ""
    dirty_owner: str = ""


@dataclass(frozen=True)
class _PreparedBundle:
    bundle: ProjectionBundle
    source_needs_write: bool
    message_needs_write: bool
    refs_to_write: tuple[ExternalMessageRef, ...]
    artifacts_to_write: tuple[MessageArtifactRecord, ...]

    @property
    def has_write(self) -> bool:
        return bool(
            self.source_needs_write
            or self.message_needs_write
            or self.refs_to_write
            or self.artifacts_to_write
            or self.bundle.dirty_keys
        )


@dataclass(frozen=True)
class _TransactionResult:
    processed: int
    committed_bundles: int
    skipped_bundles: int
    source_upserts: int
    message_upserts: int
    ref_upserts: int
    artifact_upserts: int
    indexed_messages: int
    committed_message_ids: tuple[str, ...]
    transaction_ms: float = 0.0
    cancelled: bool = False
    busy: bool = False
    error: str = ""


class ProjectionBundleWriter:
    """One bounded, serialized projection writer for a message SQLite database.

    ``submit`` is the production path and is always asynchronous.  ``write_batch``
    is intentionally synchronous for deterministic tests and for controlled
    startup catch-up workers; it is protected by the same serial lock and does
    not create a second concurrent SQLite writer.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_pending_batches: int = 128,
        transaction_bundle_limit: int = MAX_BUNDLES_PER_TRANSACTION,
        transaction_budget_seconds: float = MAX_TRANSACTION_SECONDS,
        busy_retry_seconds: float = MAX_BUSY_RETRY_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self._bundle_limit = max(1, min(MAX_BUNDLES_PER_TRANSACTION, int(transaction_bundle_limit)))
        self._transaction_budget = max(0.001, float(transaction_budget_seconds))
        self._busy_retry_seconds = max(INITIAL_BUSY_RETRY_SECONDS, float(busy_retry_seconds))
        self._serial_lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._connection_owner: int | None = None
        self._closed = False
        self._pending_slots = threading.BoundedSemaphore(max(1, int(max_pending_batches)))
        self._executor = DaemonSerialExecutor(max_workers=1, thread_name_prefix="fio-message-projection")

    def submit(
        self,
        bundles: Iterable[ProjectionBundle],
        *,
        cancel_event: threading.Event | None = None,
    ) -> Future:
        """Schedule a batch without blocking a UI or source-adapter caller."""

        prepared = tuple(bundles)
        future: Future = Future()
        if self._closed:
            future.set_result(self._deferred_result("closed", prepared))
            return future
        if not self._pending_slots.acquire(blocking=False):
            future.set_result(self._deferred_result("backpressure", prepared))
            return future
        try:
            submitted = self._executor.submit(
                self._queued_write, self.write_batch, prepared, cancel_event, time.monotonic()
            )
        except BaseException as exc:
            self._pending_slots.release()
            future.set_result(self._deferred_result("closed", prepared, str(exc)))
            return future

        def release_slot(done: Future) -> None:
            self._pending_slots.release()

        submitted.add_done_callback(release_slot)
        return submitted

    @staticmethod
    def _queued_write(operation, items, cancel_event, queued_at):
        emit_span("messages.writer.queue_wait", (time.monotonic() - queued_at) * 1000.0,
                  meta={"bundles": len(items), "operation": getattr(operation, "__name__", "write")}, min_ms=2.0)
        return operation(items, cancel_event=cancel_event)

    def submit_deletions(
        self,
        requests: Iterable[ProjectionDeleteRequest],
        *,
        cancel_event: threading.Event | None = None,
    ) -> Future:
        """Serialize authoritative source deletions on the projection lane."""

        items = tuple(requests)
        future: Future = Future()
        if self._closed:
            future.set_result(
                ProjectionWriteResult(
                    state="closed",
                    attempted_bundles=len(items),
                    deferred_bundles=len(items),
                )
            )
            return future
        if not self._pending_slots.acquire(blocking=False):
            future.set_result(
                ProjectionWriteResult(
                    state="backpressure",
                    attempted_bundles=len(items),
                    deferred_bundles=len(items),
                )
            )
            return future
        try:
            submitted = self._executor.submit(
                self._queued_write, self.write_deletions, items, cancel_event, time.monotonic()
            )
        except BaseException as exc:
            self._pending_slots.release()
            future.set_result(
                ProjectionWriteResult(
                    state="closed",
                    attempted_bundles=len(items),
                    deferred_bundles=len(items),
                    error=_clean_text(exc),
                )
            )
            return future
        submitted.add_done_callback(lambda _done: self._pending_slots.release())
        return submitted

    def write_deletions(
        self,
        requests: Iterable[ProjectionDeleteRequest],
        *,
        cancel_event: threading.Event | None = None,
    ) -> ProjectionWriteResult:
        items = tuple(requests)[:MAX_BUNDLES_PER_TRANSACTION]
        if not items:
            return ProjectionWriteResult(state="committed")
        start = time.monotonic()
        with self._serial_lock:
            conn, retries, error = self._open_with_busy_retry(cancel_event)
            if conn is None:
                state = "cancelled" if self._cancelled(cancel_event) else "delayed" if retries else "failed"
                return ProjectionWriteResult(
                    state=state,
                    attempted_bundles=len(items),
                    deferred_bundles=len(items),
                    busy_retries=retries,
                    elapsed_ms=self._elapsed_ms(start),
                    error=_clean_text(error),
                )
            try:
                retry_start = time.monotonic()
                delay = INITIAL_BUSY_RETRY_SECONDS
                while True:
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        transaction_start = time.monotonic()
                        break
                    except sqlite3.OperationalError as exc:
                        if not self._is_busy_error(exc):
                            raise
                        retries += 1
                        elapsed = time.monotonic() - retry_start
                        if elapsed >= self._busy_retry_seconds:
                            return ProjectionWriteResult(
                                state="delayed",
                                attempted_bundles=len(items),
                                deferred_bundles=len(items),
                                busy_retries=retries,
                                elapsed_ms=self._elapsed_ms(start),
                                error=_clean_text(exc),
                            )
                        if not self._sleep_until_retry(
                            min(delay, self._busy_retry_seconds - elapsed),
                            cancel_event,
                        ):
                            return ProjectionWriteResult(
                                state="cancelled",
                                attempted_bundles=len(items),
                                deferred_bundles=len(items),
                                busy_retries=retries,
                                elapsed_ms=self._elapsed_ms(start),
                                error="projection deletion cancelled",
                            )
                        delay = min(self._busy_retry_seconds, delay * 2.0)
                changed_ids: list[str] = []
                receipt_changed = False
                for request in items:
                    if self._cancelled(cancel_event):
                        raise InterruptedError("projection deletion cancelled")
                    rows = conn.execute(
                        """
                        SELECT message_id, metadata_json FROM message_external_refs
                         WHERE source_id=? AND external_kind=? AND external_key=?
                        """,
                        (
                            _clean_text(request.source_id),
                            _clean_text(request.external_kind),
                            _clean_text(request.external_key),
                        ),
                    ).fetchall()
                    for row in rows:
                        message_id = _clean_text(row[0])
                        try:
                            receipt_metadata = json.loads(_clean_text(row[1]) or "{}")
                        except Exception:
                            receipt_metadata = {}
                        if not isinstance(receipt_metadata, dict):
                            receipt_metadata = {}
                        receipt_metadata["source_present"] = False
                        receipt_metadata["source_deleted_utc"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                        conn.execute(
                            """
                            UPDATE message_external_refs
                               SET metadata_json=?, updated_utc=?
                             WHERE source_id=? AND external_kind=? AND external_key=?
                            """,
                            (
                                _clean_json(receipt_metadata),
                                receipt_metadata["source_deleted_utc"],
                                _clean_text(request.source_id),
                                _clean_text(request.external_kind),
                                _clean_text(request.external_key),
                            ),
                        )
                        receipt_changed = True
                        peer_rows = conn.execute(
                            """
                            SELECT metadata_json FROM message_external_refs
                             WHERE message_id=?
                               AND NOT (source_id=? AND external_kind=? AND external_key=?)
                            """,
                            (
                                message_id,
                                _clean_text(request.source_id),
                                _clean_text(request.external_kind),
                                _clean_text(request.external_key),
                            ),
                        ).fetchall()
                        has_present_peer = False
                        for peer in peer_rows:
                            try:
                                peer_metadata = json.loads(_clean_text(peer[0]) or "{}")
                            except Exception:
                                peer_metadata = {}
                            if not isinstance(peer_metadata, dict) or peer_metadata.get("source_present", True) is not False:
                                has_present_peer = True
                                break
                        if has_present_peer:
                            # The canonical station message remains present via
                            # another source receipt.  Keep this deleted-source
                            # reference as immutable provenance.
                            continue
                        cur = conn.execute(
                            """
                            UPDATE message_projection
                               SET deleted=1, deleted_utc=?, projected_utc=?
                             WHERE message_id=? AND deleted=0
                            """,
                            (
                                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                message_id,
                            ),
                        )
                        if cur.rowcount:
                            changed_ids.append(message_id)
                            from freqinout.core.ops_focus import index_message_for_ops_focus

                            refreshed = conn.execute(
                                "SELECT * FROM message_projection WHERE message_id=?", (message_id,)
                            ).fetchone()
                            if refreshed is not None:
                                index_message_for_ops_focus(conn, refreshed)
                    if request.dirty_key:
                        if request.dirty_owner:
                            conn.execute(
                                "DELETE FROM message_projection_dirty WHERE dirty_key=? AND lease_owner=?",
                                (_clean_text(request.dirty_key), _clean_text(request.dirty_owner)),
                            )
                        else:
                            conn.execute(
                                "DELETE FROM message_projection_dirty WHERE dirty_key=?",
                                (_clean_text(request.dirty_key),),
                            )
                if changed_ids or receipt_changed:
                    conn.execute(
                        """UPDATE message_projection_generation
                              SET generation=generation+1, updated_utc=? WHERE singleton=1""",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),),
                    )
                conn.commit()
                transaction_ms = (time.monotonic() - transaction_start) * 1000.0
                return ProjectionWriteResult(
                    state="committed",
                    attempted_bundles=len(items),
                    committed_bundles=len(items),
                    transactions=1,
                    message_upserts=len(changed_ids),
                    indexed_messages=len(changed_ids),
                    busy_retries=retries,
                    elapsed_ms=self._elapsed_ms(start),
                    max_transaction_ms=transaction_ms,
                    committed_message_ids=tuple(changed_ids),
                )
            except InterruptedError as exc:
                conn.rollback()
                return ProjectionWriteResult(
                    state="cancelled",
                    attempted_bundles=len(items),
                    deferred_bundles=len(items),
                    busy_retries=retries,
                    elapsed_ms=self._elapsed_ms(start),
                    error=str(exc),
                )
            except Exception as exc:
                conn.rollback()
                return ProjectionWriteResult(
                    state="failed",
                    attempted_bundles=len(items),
                    deferred_bundles=len(items),
                    busy_retries=retries,
                    elapsed_ms=self._elapsed_ms(start),
                    error=_clean_text(exc),
                )

    def write_batch(
        self,
        bundles: Iterable[ProjectionBundle],
        *,
        cancel_event: threading.Event | None = None,
    ) -> ProjectionWriteResult:
        """Synchronously write prepared bundles through the one serial writer seam."""

        items = tuple(bundles)
        if not items:
            return ProjectionWriteResult(state="committed")
        start = time.monotonic()
        with self._serial_lock:
            if self._closed:
                return self._deferred_result("closed", items, elapsed_ms=self._elapsed_ms(start))
            conn, connection_retries, connection_error = self._open_with_busy_retry(cancel_event)
            if conn is None:
                state = "cancelled" if self._cancelled(cancel_event) else "delayed" if connection_retries else "failed"
                result = self._deferred_result(
                    state,
                    items,
                    connection_error,
                    elapsed_ms=self._elapsed_ms(start),
                )
                return ProjectionWriteResult(
                    **{**result.__dict__, "busy_retries": connection_retries}
                )
            result = self._write_all(
                conn,
                items,
                start=start,
                cancel_event=cancel_event,
                initial_busy_retries=connection_retries,
            )
            emit_span(
                "messages.write_batch",
                result.elapsed_ms,
                meta={
                    "bundles": len(items),
                    "transactions": result.transactions,
                    "message_upserts": result.message_upserts,
                    "ref_upserts": result.ref_upserts,
                    "artifact_upserts": result.artifact_upserts,
                    "busy_retries": result.busy_retries,
                    "max_transaction_ms": round(result.max_transaction_ms, 3),
                    "state": result.state,
                },
                level="warning" if result.max_transaction_ms > 100.0 else "debug",
            )
            return result

    def repair_legacy_duplicates(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ProjectionRepairResult:
        """Converge proven legacy presentations through the serialized writer.

        Planning is read-only and runs off the UI thread.  Native source rows
        and files are never changed.  Each short transaction moves receipts,
        artifacts, and operator-owned state before removing only the
        superseded derived presentation.
        """

        with self._serial_lock:
            conn, retries, error = self._open_with_busy_retry(cancel_event)
            if conn is None:
                return ProjectionRepairResult(
                    state="cancelled" if self._cancelled(cancel_event) else "deferred",
                    busy_retries=retries,
                    error=_clean_text(error),
                )
            try:
                planned = plan_legacy_projection_merges(conn)
            except Exception as exc:
                return ProjectionRepairResult(state="failed", error=_clean_text(exc))
            repaired = transactions = 0
            max_transaction_ms = 0.0
            for start in range(0, len(planned), MAX_REPAIRS_PER_TRANSACTION):
                if self._cancelled(cancel_event):
                    return ProjectionRepairResult(
                        state="cancelled",
                        planned=len(planned),
                        repaired=repaired,
                        transactions=transactions,
                        busy_retries=retries,
                        max_transaction_ms=max_transaction_ms,
                    )
                chunk = planned[start : start + MAX_REPAIRS_PER_TRANSACTION]
                retry_started = time.monotonic()
                delay = INITIAL_BUSY_RETRY_SECONDS
                while True:
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        transaction_started = time.monotonic()
                        changed = 0
                        for merge in chunk:
                            if self._cancelled(cancel_event):
                                raise InterruptedError("projection repair cancelled")
                            if merge_message_projections(
                                conn,
                                target_message_id=merge.target_message_id,
                                duplicate_message_id=merge.duplicate_message_id,
                            ):
                                changed += 1
                        if changed:
                            conn.execute(
                                """
                                UPDATE message_projection_generation
                                   SET generation=generation+1, updated_utc=?
                                 WHERE singleton=1
                                """,
                                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),),
                            )
                        conn.commit()
                        transaction_ms = (time.monotonic() - transaction_started) * 1000.0
                        max_transaction_ms = max(max_transaction_ms, transaction_ms)
                        repaired += changed
                        transactions += 1
                        break
                    except InterruptedError:
                        conn.rollback()
                        return ProjectionRepairResult(
                            state="cancelled",
                            planned=len(planned),
                            repaired=repaired,
                            transactions=transactions,
                            busy_retries=retries,
                            max_transaction_ms=max_transaction_ms,
                        )
                    except sqlite3.OperationalError as exc:
                        conn.rollback()
                        if not self._is_busy_error(exc):
                            return ProjectionRepairResult(
                                state="failed",
                                planned=len(planned),
                                repaired=repaired,
                                transactions=transactions,
                                busy_retries=retries,
                                max_transaction_ms=max_transaction_ms,
                                error=_clean_text(exc),
                            )
                        retries += 1
                        elapsed = time.monotonic() - retry_started
                        if elapsed >= self._busy_retry_seconds:
                            return ProjectionRepairResult(
                                state="deferred",
                                planned=len(planned),
                                repaired=repaired,
                                transactions=transactions,
                                busy_retries=retries,
                                max_transaction_ms=max_transaction_ms,
                                error=_clean_text(exc),
                            )
                        if not self._sleep_until_retry(
                            min(delay, self._busy_retry_seconds - elapsed), cancel_event
                        ):
                            return ProjectionRepairResult(
                                state="cancelled",
                                planned=len(planned),
                                repaired=repaired,
                                transactions=transactions,
                                busy_retries=retries,
                                max_transaction_ms=max_transaction_ms,
                            )
                        delay = min(self._busy_retry_seconds, delay * 2.0)
                    except Exception as exc:
                        conn.rollback()
                        return ProjectionRepairResult(
                            state="failed",
                            planned=len(planned),
                            repaired=repaired,
                            transactions=transactions,
                            busy_retries=retries,
                            max_transaction_ms=max_transaction_ms,
                            error=_clean_text(exc),
                        )
                if start + MAX_REPAIRS_PER_TRANSACTION < len(planned):
                    time.sleep(0)
            return ProjectionRepairResult(
                state="committed",
                planned=len(planned),
                repaired=repaired,
                transactions=transactions,
                busy_retries=retries,
                max_transaction_ms=max_transaction_ms,
            )

    def close(self, *, wait: bool = True, cancel_pending: bool = False) -> None:
        """Stop accepting work and release the writer connection on its owner thread."""

        if self._closed:
            return
        self._closed = True
        # The synchronous test seam may have made the caller the connection
        # owner.  Close that connection before scheduling cleanup on the daemon
        # worker; sqlite3 connections are thread-affine by default.
        with self._serial_lock:
            if self._connection_owner == threading.get_ident():
                self._close_connection()
        try:
            self._executor.submit(self._close_connection).result(timeout=2.0 if wait else 0.01)
        except Exception:
            # Shutdown must remain bounded.  The daemon executor is permitted to
            # release an unavailable SQLite connection at process termination.
            pass
        self._executor.shutdown(wait=wait, cancel_futures=cancel_pending)
        if not wait:
            return
        with self._serial_lock:
            self._close_connection()

    shutdown = close

    def _connection_for_current_thread(self) -> sqlite3.Connection:
        owner = threading.get_ident()
        if self._connection is not None and self._connection_owner == owner:
            return self._connection
        self._close_connection()
        conn = connect_sqlite_runtime_write(
            self._db_path,
            timeout=INITIAL_BUSY_RETRY_SECONDS,
            row_factory=sqlite3.Row,
            busy_timeout_ms=int(INITIAL_BUSY_RETRY_SECONDS * 1000),
        )
        self._connection = conn
        self._connection_owner = owner
        return conn

    def _open_with_busy_retry(
        self,
        cancel_event: threading.Event | None,
    ) -> tuple[sqlite3.Connection | None, int, str]:
        retry_start = time.monotonic()
        delay = INITIAL_BUSY_RETRY_SECONDS
        retries = 0
        while True:
            if self._cancelled(cancel_event):
                return None, retries, "cancelled while opening projection database"
            try:
                return self._connection_for_current_thread(), retries, ""
            except sqlite3.OperationalError as exc:
                if not self._is_busy_error(exc):
                    return None, retries, str(exc)
                retries += 1
                elapsed = time.monotonic() - retry_start
                if elapsed >= self._busy_retry_seconds:
                    return None, retries, str(exc)
                if not self._sleep_until_retry(
                    min(delay, self._busy_retry_seconds - elapsed), cancel_event
                ):
                    return None, retries, "cancelled while opening projection database"
                delay = min(self._busy_retry_seconds, delay * 2.0)
            except Exception as exc:
                return None, retries, str(exc)

    def _close_connection(self) -> None:
        conn, self._connection = self._connection, None
        self._connection_owner = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _write_all(
        self,
        conn: sqlite3.Connection,
        items: tuple[ProjectionBundle, ...],
        *,
        start: float,
        cancel_event: threading.Event | None,
        initial_busy_retries: int = 0,
    ) -> ProjectionWriteResult:
        index = 0
        transactions = committed = skipped = 0
        source_upserts = message_upserts = ref_upserts = artifact_upserts = indexed_messages = 0
        retries = max(0, int(initial_busy_retries))
        max_transaction_ms = 0.0
        committed_ids: list[str] = []
        difference_limit = self._bundle_limit
        while index < len(items):
            if self._cancelled(cancel_event):
                return self._result(
                    "cancelled", items, index, committed, skipped, transactions,
                    source_upserts, message_upserts, ref_upserts, artifact_upserts,
                    indexed_messages, retries, start, committed_ids,
                    max_transaction_ms=max_transaction_ms,
                )
            candidates = items[index : index + self._bundle_limit]
            attempt, busy_retries = self._write_with_busy_retry(
                conn, candidates, cancel_event, difference_limit=difference_limit
            )
            retries += busy_retries
            if attempt.busy:
                return self._result(
                    "delayed", items, index, committed, skipped, transactions,
                    source_upserts, message_upserts, ref_upserts, artifact_upserts,
                    indexed_messages, retries, start, committed_ids, attempt.error,
                    max_transaction_ms=max_transaction_ms,
                )
            if attempt.error:
                return self._result(
                    "failed", items, index, committed, skipped, transactions,
                    source_upserts, message_upserts, ref_upserts, artifact_upserts,
                    indexed_messages, retries, start, committed_ids, attempt.error,
                    max_transaction_ms=max_transaction_ms,
                )
            if attempt.processed <= 0:
                return self._result(
                    "cancelled" if attempt.cancelled else "failed", items, index,
                    committed, skipped, transactions, source_upserts, message_upserts,
                    ref_upserts, artifact_upserts, indexed_messages, retries, start,
                    committed_ids, "cancelled before a complete bundle" if attempt.cancelled else "writer made no progress",
                    max_transaction_ms=max_transaction_ms,
                )
            max_transaction_ms = max(max_transaction_ms, float(attempt.transaction_ms or 0.0))
            index += attempt.processed
            # A short SQL time slice must not repeatedly compare the entire
            # unprocessed tail. Adapt this batch only; fast batches stay large.
            # The full candidate window still defines source metadata precedence.
            difference_limit = min(difference_limit, max(1, attempt.processed))
            if attempt.committed_bundles:
                transactions += 1
            committed += attempt.committed_bundles
            skipped += attempt.skipped_bundles
            source_upserts += attempt.source_upserts
            message_upserts += attempt.message_upserts
            ref_upserts += attempt.ref_upserts
            artifact_upserts += attempt.artifact_upserts
            indexed_messages += attempt.indexed_messages
            committed_ids.extend(attempt.committed_message_ids)
            if attempt.cancelled:
                return self._result(
                    "cancelled", items, index, committed, skipped, transactions,
                    source_upserts, message_upserts, ref_upserts, artifact_upserts,
                    indexed_messages, retries, start, committed_ids,
                    max_transaction_ms=max_transaction_ms,
                )
            # Yield between short transactions so this worker does not monopolize
            # the interpreter during historical catch-up.
            if index < len(items):
                time.sleep(0)
        return self._result(
            "committed", items, index, committed, skipped, transactions,
            source_upserts, message_upserts, ref_upserts, artifact_upserts,
            indexed_messages, retries, start, committed_ids,
            max_transaction_ms=max_transaction_ms,
        )

    def _write_with_busy_retry(
        self,
        conn: sqlite3.Connection,
        candidates: tuple[ProjectionBundle, ...],
        cancel_event: threading.Event | None,
        *,
        difference_limit: int | None = None,
    ) -> tuple[_TransactionResult, int]:
        retry_start = time.monotonic()
        delay = INITIAL_BUSY_RETRY_SECONDS
        retries = 0
        while True:
            if self._cancelled(cancel_event):
                return _TransactionResult(processed=0, committed_bundles=0, skipped_bundles=0, source_upserts=0, message_upserts=0, ref_upserts=0, artifact_upserts=0, indexed_messages=0, committed_message_ids=(), cancelled=True), retries
            try:
                return self._write_transaction(
                    conn, candidates, cancel_event, difference_limit=difference_limit
                ), retries
            except sqlite3.OperationalError as exc:
                if not self._is_busy_error(exc):
                    return _TransactionResult(processed=0, committed_bundles=0, skipped_bundles=0, source_upserts=0, message_upserts=0, ref_upserts=0, artifact_upserts=0, indexed_messages=0, committed_message_ids=(), error=str(exc)), retries
                retries += 1
                elapsed = time.monotonic() - retry_start
                if elapsed >= self._busy_retry_seconds:
                    return _TransactionResult(processed=0, committed_bundles=0, skipped_bundles=0, source_upserts=0, message_upserts=0, ref_upserts=0, artifact_upserts=0, indexed_messages=0, committed_message_ids=(), busy=True, error=str(exc)), retries
                sleep_for = min(delay, self._busy_retry_seconds - elapsed)
                with perf_span("messages.writer.busy_retry_sleep", min_ms=2.0):
                    retry_ready = self._sleep_until_retry(sleep_for, cancel_event)
                if not retry_ready:
                    return _TransactionResult(processed=0, committed_bundles=0, skipped_bundles=0, source_upserts=0, message_upserts=0, ref_upserts=0, artifact_upserts=0, indexed_messages=0, committed_message_ids=(), cancelled=True), retries
                delay = min(self._busy_retry_seconds, delay * 2.0)
            except Exception as exc:
                return _TransactionResult(processed=0, committed_bundles=0, skipped_bundles=0, source_upserts=0, message_upserts=0, ref_upserts=0, artifact_upserts=0, indexed_messages=0, committed_message_ids=(), error=str(exc)), retries

    def _write_transaction(
        self,
        conn: sqlite3.Connection,
        candidates: tuple[ProjectionBundle, ...],
        cancel_event: threading.Event | None,
        *,
        difference_limit: int | None = None,
    ) -> _TransactionResult:
        limit = len(candidates) if difference_limit is None else max(1, int(difference_limit))
        prefix = candidates[:limit]
        meta = {"bundles": len(prefix), "candidates": len(candidates)}
        with perf_span("messages.writer.prepare_differences", meta=meta, min_ms=2.0):
            cpu_started = time.thread_time()
            try:
                prepared = self._prepare_differences(conn, prefix, source_bundles=candidates)
            finally:
                meta["thread_cpu_ms"] = (time.thread_time() - cpu_started) * 1000.0
        if self._cancelled(cancel_event):
            return _TransactionResult(processed=0, committed_bundles=0, skipped_bundles=0, source_upserts=0, message_upserts=0, ref_upserts=0, artifact_upserts=0, indexed_messages=0, committed_message_ids=(), cancelled=True)
        if not any(item.has_write for item in prepared):
            return _TransactionResult(
                processed=len(prepared), committed_bundles=0, skipped_bundles=len(prepared),
                source_upserts=0, message_upserts=0, ref_upserts=0, artifact_upserts=0,
                indexed_messages=0, committed_message_ids=(),
            )

        source_records = self._desired_source_records(candidates)
        source_written: set[str] = set()
        processed = committed = skipped = source_upserts = message_upserts = ref_upserts = artifact_upserts = indexed_messages = 0
        committed_ids: list[str] = []
        generation_changed = False
        transaction_open = False
        transaction_start = 0.0
        try:
            with perf_span("messages.writer.begin_wait", min_ms=2.0):
                conn.execute("BEGIN IMMEDIATE")
            transaction_open = True
            transaction_start = time.monotonic()
            for item in prepared:
                if self._cancelled(cancel_event):
                    break
                # Do not start another bundle after the time budget.  A started
                # bundle is always completed before commit; it is never split.
                if processed and time.monotonic() - transaction_start >= self._transaction_budget:
                    break
                bundle = item.bundle
                wrote_bundle = False
                source_id = _clean_text(bundle.source.source_id)
                if item.source_needs_write and source_id not in source_written:
                    upsert_message_source(conn, source_records[source_id])
                    source_written.add(source_id)
                    source_upserts += 1
                    wrote_bundle = True
                if item.message_needs_write:
                    # The store helper updates the Ops/entity index as part of
                    # this changed-message upsert.  It is intentionally not
                    # called for hash-identical messages.
                    upsert_message_projection(conn, bundle.message)
                    message_upserts += 1
                    indexed_messages += 1
                    wrote_bundle = True
                    generation_changed = True
                for ref in item.refs_to_write:
                    upsert_external_ref(conn, ref)
                    ref_upserts += 1
                    wrote_bundle = True
                    generation_changed = True
                for artifact in item.artifacts_to_write:
                    upsert_message_artifact(conn, artifact)
                    artifact_upserts += 1
                    wrote_bundle = True
                    generation_changed = True
                for dirty_key in bundle.dirty_keys:
                    if bundle.dirty_owner:
                        cur = conn.execute(
                            "DELETE FROM message_projection_dirty WHERE dirty_key=? AND lease_owner=?",
                            (_clean_text(dirty_key), _clean_text(bundle.dirty_owner)),
                        )
                    else:
                        cur = conn.execute(
                            "DELETE FROM message_projection_dirty WHERE dirty_key=?",
                            (_clean_text(dirty_key),),
                        )
                    wrote_bundle = bool(cur.rowcount) or wrote_bundle
                processed += 1
                if wrote_bundle:
                    committed += 1
                    committed_ids.append(_clean_text(bundle.message.message_id))
                else:
                    skipped += 1
            if processed:
                if generation_changed:
                    conn.execute(
                        """
                        UPDATE message_projection_generation
                           SET generation=generation+1, updated_utc=?
                         WHERE singleton=1
                        """,
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),),
                    )
                with perf_span("messages.writer.commit", min_ms=2.0):
                    # Optional source lifecycle fence serializes its generation
                    # invalidation with this final commit. Ordinary producers
                    # retain the existing finish-started-bundle behavior.
                    guard=getattr(cancel_event,"commit_guard",None)
                    with guard() if callable(guard) else nullcontext():
                        conn.commit()
                transaction_open = False
            else:
                conn.rollback()
                transaction_open = False
            transaction_ms = (
                (time.monotonic() - transaction_start) * 1000.0
                if transaction_start
                else 0.0
            )
            return _TransactionResult(
                processed=processed,
                committed_bundles=committed,
                skipped_bundles=skipped,
                source_upserts=source_upserts,
                message_upserts=message_upserts,
                ref_upserts=ref_upserts,
                artifact_upserts=artifact_upserts,
                indexed_messages=indexed_messages,
                committed_message_ids=tuple(committed_ids),
                transaction_ms=transaction_ms,
                cancelled=self._cancelled(cancel_event),
            )
        except BaseException:
            if transaction_open:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise

    def _prepare_differences(
        self,
        conn: sqlite3.Connection,
        bundles: Sequence[ProjectionBundle],
        *,
        source_bundles: Sequence[ProjectionBundle] | None = None,
    ) -> tuple[_PreparedBundle, ...]:
        source_context = bundles if source_bundles is None else source_bundles
        prefix_source_ids = {_clean_text(bundle.source.source_id) for bundle in bundles}
        desired_sources = {key: value for key, value in self._desired_source_records(source_context).items()
                           if key in prefix_source_ids}
        source_rows = self._load_source_rows(conn, desired_sources)
        source_needs = {
            source_id: row is None or _stored_source_signature(row) != self._source_signature_for(source_id, source_context)
            for source_id, row in source_rows.items()
        }
        message_rows = self._load_message_rows(conn, [_clean_text(bundle.message.message_id) for bundle in bundles])
        ref_rows = self._load_ref_rows(conn, (ref for bundle in bundles for ref in bundle.refs))
        artifact_rows = self._load_artifact_rows(conn, (artifact for bundle in bundles for artifact in bundle.artifacts))
        prepared: list[_PreparedBundle] = []
        for bundle in bundles:
            source_id = _clean_text(bundle.source.source_id)
            refs = tuple(
                ref for ref in bundle.refs
                if _stored_ref_signature(ref_rows.get(self._ref_key(ref))) != _ref_signature(ref)
            )
            artifacts = tuple(
                artifact for artifact in bundle.artifacts
                if _stored_artifact_signature(artifact_rows.get(_clean_text(artifact.artifact_id))) != _artifact_signature(artifact)
            )
            existing_message = message_rows.get(_clean_text(bundle.message.message_id))
            prepared.append(
                _PreparedBundle(
                    bundle=bundle,
                    source_needs_write=source_needs[source_id],
                    message_needs_write=(
                        _stored_message_signature(existing_message)
                        != _message_signature(bundle.message, existing_message)
                    ),
                    refs_to_write=refs,
                    artifacts_to_write=artifacts,
                )
            )
        return tuple(prepared)

    @staticmethod
    def _desired_source_records(bundles: Iterable[ProjectionBundle | _PreparedBundle]) -> dict[str, MessageSourceRecord]:
        desired: dict[str, MessageSourceRecord] = {}
        for item in bundles:
            bundle = item.bundle if isinstance(item, _PreparedBundle) else item
            desired[_clean_text(bundle.source.source_id)] = bundle.source
        return desired

    @staticmethod
    def _source_signature_for(source_id: str, bundles: Sequence[ProjectionBundle]) -> str:
        for bundle in reversed(bundles):
            if _clean_text(bundle.source.source_id) == source_id:
                return bundle.effective_source_signature
        return ""

    @staticmethod
    def _chunked(values: Sequence[str], size: int = 250) -> Iterable[Sequence[str]]:
        for start in range(0, len(values), size):
            yield values[start : start + size]

    def _load_source_rows(self, conn: sqlite3.Connection, sources: Mapping[str, MessageSourceRecord]) -> dict[str, sqlite3.Row | None]:
        values = list(sources)
        rows: dict[str, sqlite3.Row | None] = {value: None for value in values}
        for chunk in self._chunked(values):
            marks = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT * FROM message_sources WHERE source_id IN ({marks})", tuple(chunk)
            ):
                rows[_clean_text(row["source_id"])] = row
        return rows

    def _load_message_rows(
        self,
        conn: sqlite3.Connection,
        message_ids: Sequence[str],
    ) -> dict[str, sqlite3.Row | None]:
        out: dict[str, sqlite3.Row | None] = {
            value: None for value in dict.fromkeys(message_ids) if value
        }
        values = list(dict.fromkeys(message_ids))
        for chunk in self._chunked(values):
            marks = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT * FROM message_projection WHERE message_id IN ({marks})", tuple(chunk)
            ):
                out[_clean_text(row["message_id"])] = row
        return out

    @staticmethod
    def _ref_key(ref: ExternalMessageRef) -> tuple[str, str, str]:
        return (_clean_text(ref.source_id), _clean_text(ref.external_kind), _clean_text(ref.external_key))

    def _load_ref_rows(self, conn: sqlite3.Connection, refs: Iterable[ExternalMessageRef]) -> dict[tuple[str, str, str], sqlite3.Row]:
        keys = list(dict.fromkeys(self._ref_key(ref) for ref in refs))
        out: dict[tuple[str, str, str], sqlite3.Row] = {}
        for start in range(0, len(keys), 250):
            chunk = keys[start : start + 250]
            where = " OR ".join("(source_id=? AND external_kind=? AND external_key=?)" for _ in chunk)
            params = tuple(part for key in chunk for part in key)
            for row in conn.execute(f"SELECT * FROM message_external_refs WHERE {where}", params):
                out[(_clean_text(row["source_id"]), _clean_text(row["external_kind"]), _clean_text(row["external_key"]))] = row
        return out

    def _load_artifact_rows(self, conn: sqlite3.Connection, artifacts: Iterable[MessageArtifactRecord]) -> dict[str, sqlite3.Row]:
        values = list(dict.fromkeys(_clean_text(artifact.artifact_id) for artifact in artifacts))
        out: dict[str, sqlite3.Row] = {}
        for chunk in self._chunked(values):
            marks = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT * FROM message_artifacts WHERE artifact_id IN ({marks})", tuple(chunk)
            ):
                out[_clean_text(row["artifact_id"])] = row
        return out

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    @staticmethod
    def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
        text = str(exc).lower()
        return "locked" in text or "busy" in text

    def _sleep_until_retry(self, seconds: float, cancel_event: threading.Event | None) -> bool:
        if seconds <= 0:
            return not self._cancelled(cancel_event)
        if cancel_event is None:
            time.sleep(seconds)
            return True
        return not cancel_event.wait(seconds)

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return (time.monotonic() - start) * 1000.0

    def _result(
        self,
        state: str,
        items: Sequence[ProjectionBundle],
        processed: int,
        committed: int,
        skipped: int,
        transactions: int,
        source_upserts: int,
        message_upserts: int,
        ref_upserts: int,
        artifact_upserts: int,
        indexed_messages: int,
        retries: int,
        start: float,
        committed_ids: Sequence[str],
        error: str = "",
        *,
        max_transaction_ms: float = 0.0,
    ) -> ProjectionWriteResult:
        deferred = tuple(_clean_text(bundle.message.message_id) for bundle in items[processed:])
        return ProjectionWriteResult(
            state=state,
            attempted_bundles=len(items),
            committed_bundles=committed,
            skipped_bundles=skipped,
            deferred_bundles=len(items) - processed,
            transactions=transactions,
            source_upserts=source_upserts,
            message_upserts=message_upserts,
            ref_upserts=ref_upserts,
            artifact_upserts=artifact_upserts,
            indexed_messages=indexed_messages,
            busy_retries=retries,
            elapsed_ms=self._elapsed_ms(start),
            max_transaction_ms=float(max_transaction_ms or 0.0),
            committed_message_ids=tuple(committed_ids),
            deferred_message_ids=deferred,
            error=_clean_text(error),
        )

    @staticmethod
    def _deferred_result(
        state: str,
        items: Sequence[ProjectionBundle],
        error: str = "",
        *,
        elapsed_ms: float = 0.0,
    ) -> ProjectionWriteResult:
        return ProjectionWriteResult(
            state=state,
            attempted_bundles=len(items),
            deferred_bundles=len(items),
            deferred_message_ids=tuple(_clean_text(bundle.message.message_id) for bundle in items),
            elapsed_ms=elapsed_ms,
            error=_clean_text(error),
        )


def get_projection_writer(db_path: str | Path) -> ProjectionBundleWriter:
    """Return the process-wide serialized writer for one message database."""

    key = str(Path(db_path).expanduser().resolve())
    with _WRITER_REGISTRY_LOCK:
        writer = _WRITER_REGISTRY.get(key)
        if writer is None or writer._closed:
            writer = ProjectionBundleWriter(key)
            _WRITER_REGISTRY[key] = writer
        return writer


def close_projection_writers(*, wait: bool = True, cancel_pending: bool = False) -> None:
    """Boundedly close every registered projection writer during app shutdown."""

    with _WRITER_REGISTRY_LOCK:
        writers = tuple(_WRITER_REGISTRY.values())
        _WRITER_REGISTRY.clear()
    for writer in writers:
        writer.close(wait=wait, cancel_pending=cancel_pending)
