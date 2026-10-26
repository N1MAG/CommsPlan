"""Incremental, off-UI projection lane for scanner-discovered message files.

The scanner owns filesystem discovery.  This module owns only bounded database
work and delegates all normalized-message DML to the shared projection writer.
It never deletes, moves, or opens source files for writing.
"""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, TimeoutError
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Mapping, Sequence

from freqinout.core.message_file_scanner import FileRecord, FileScanDelta, file_path_display, file_path_key
from freqinout.core.message_projection_store import content_hash
from freqinout.core.message_projection_writer import (
    MAX_BUNDLES_PER_TRANSACTION,
    ProjectionDeleteRequest,
    ProjectionWriteResult,
    get_projection_writer,
)
from freqinout.core.message_source_projectors import prepare_file_projection_bundles
from freqinout.core.sqlite_utils import connect_sqlite_readonly, connect_sqlite_runtime_write
from freqinout.core.perf_metrics import span as perf_span


_CACHE_KEY = "file_scanner:v1"


@dataclass(frozen=True)
class FileProjectionPipelineResult:
    """Immutable result suitable for UI notification after background work."""

    state: str
    added_or_changed: int = 0
    removed: int = 0
    unchanged: int = 0
    inventory_writes: int = 0
    projected: int = 0
    tombstoned: int = 0
    deferred: int = 0
    transactions: int = 0
    elapsed_ms: float = 0.0
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added_or_changed or self.removed)


def _text(value: object) -> str:
    """SQLite-safe printable text for non-path scanner values."""

    raw = str(value or "").encode("utf-8", "surrogateescape")
    return raw.decode("utf-8", "backslashreplace")


def _normalize_records(records: Mapping[str, Sequence[FileRecord]] | None) -> tuple[FileRecord, ...]:
    items: list[FileRecord] = []
    for origin, rows in (records or {}).items():
        origin_text = _text(origin).strip().lower()
        for row in rows or ():
            if not isinstance(row, FileRecord):
                continue
            items.append(
                FileRecord(
                    # Keep the raw Path in memory so the opaque inventory key
                    # remains a reversible fsencode identity.  Every persisted
                    # text field is escaped at the SQL/build boundary below.
                    path=Path(row.path),
                    origin=_text(row.origin or origin_text).strip().lower() or "file",
                    size=int(row.size or 0),
                    mtime=float(row.mtime or 0.0),
                    source_id=_text(row.source_id),
                    source_label=_text(row.source_label),
                )
            )
    return tuple(items)


def _record_identity(record: FileRecord) -> tuple[str, str]:
    return _text(record.origin).strip().lower(), file_path_key(record.path)


def _record_fingerprint(records: Sequence[FileRecord]) -> str:
    # Encode each filesystem identity once, then reuse it for ordering and wire.
    indexed = sorted(((_record_identity(record), record) for record in records), key=lambda item:item[0])
    return content_hash(
        "mip3-file-inventory-v1",
        "\n".join(
            "|".join(
                (
                    origin,
                    path_key,
                    str(int(record.size or 0)),
                    f"{float(record.mtime or 0.0):.6f}",
                    _text(record.source_id),
                    _text(record.source_label),
                )
            )
            for (origin, path_key), record in indexed
        ),
    )


def _dir_mtime_json(dir_mtimes: Mapping[str, float] | None) -> str:
    normalized: dict[str, float] = {}
    for path, value in (dir_mtimes or {}).items():
        try:
            normalized[file_path_display(path)] = float(value)
        except Exception:
            continue
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _source_id(record: FileRecord) -> str:
    origin = _text(record.origin).lower() or "file"
    return _text(record.source_id) or f"{origin}:{file_path_key(record.path.parent)}"


def _external_kind(record: FileRecord) -> str:
    return f"{_text(record.origin).lower() or 'file'}_file"


def _external_key(record: FileRecord) -> str:
    return f"{file_path_key(record.path)}:{float(record.mtime or 0.0):.6f}:{int(record.size or 0)}"


class MessageFileProjectionPipeline:
    """Persist scanner inventory and project only the supplied delta.

    Call :meth:`run` from a scanner/background worker, never the Qt event
    loop.  The writer itself remains the only normalized projection writer.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def run(
        self,
        records: Mapping[str, Sequence[FileRecord]],
        dir_mtimes: Mapping[str, float],
        watch_signature: str,
        delta: FileScanDelta,
        *,
        cancel_event: threading.Event | None = None,
    ) -> FileProjectionPipelineResult:
        start = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            return self._result("cancelled", delta, error="cancelled", elapsed_ms=self._elapsed(start))
        with perf_span("messages.file_inventory_normalize", min_ms=5.0):
            current = _normalize_records(records)
        changed = _normalize_records({"_": delta.added_or_changed})
        removed = _normalize_records({"_": delta.removed})
        replaced = tuple(
            (_normalize_records({"_": (prior,)})[0], _normalize_records({"_": (now,)})[0])
            for prior, now in delta.replaced
            if isinstance(prior, FileRecord) and isinstance(now, FileRecord)
        )
        signature = _text(watch_signature)
        mtimes_json = _dir_mtime_json(dir_mtimes)
        with perf_span("messages.file_inventory_fingerprint", min_ms=5.0, meta={"records":len(current)}):
            fingerprint = _record_fingerprint(current)

        # This read-only test is the hot unchanged path: no transaction, no
        # write connection, no projection preparation and no writer submission.
        with perf_span("messages.file_inventory_cache_read", min_ms=5.0):
            cache = self._load_cache()
        if (
            delta.is_empty
            and cache is not None
            and cache["watch_signature"] == signature
            and cache["dir_mtimes_json"] == mtimes_json
            and cache["inventory_fingerprint"] == fingerprint
        ):
            return self._result("unchanged", delta, elapsed_ms=self._elapsed(start))
        seed_inventory = cache is None or (bool(current) and not self._has_inventory())
        # The first run after the additive MIP-3 migration is a bounded catch-up
        # even when the GUI supplied its older scanner cache as the comparison
        # snapshot.  That legacy cache proves discovery, not normalized
        # projection completeness.
        projection_records = current if seed_inventory else changed

        projected = tombstoned = deferred = transactions = 0
        error = ""
        writer = get_projection_writer(self._db_path)
        try:
            for batch_start in range(0, len(projection_records), MAX_BUNDLES_PER_TRANSACTION):
                if cancel_event is not None and cancel_event.is_set():
                    return self._result("cancelled", delta, projected=projected, tombstoned=tombstoned,
                                        transactions=transactions, error="cancelled", elapsed_ms=self._elapsed(start))
                batch = projection_records[batch_start : batch_start + MAX_BUNDLES_PER_TRANSACTION]
                with perf_span("messages.file_prepare", min_ms=5.0, meta={"records":len(batch)}):
                    bundles = self._prepare_bundles(batch)
                with perf_span("messages.file_writer_wait", min_ms=5.0):
                    future = writer.submit(bundles, cancel_event=cancel_event) if cancel_event is not None else writer.submit(bundles)
                    outcome = self._await_write(future, cancel_event, len(bundles))
                projected += int(outcome.committed_bundles)
                deferred += int(outcome.deferred_bundles)
                transactions += int(outcome.transactions)
                if outcome.state != "committed":
                    error = outcome.error or outcome.state
                    return self._result(
                        "cancelled" if outcome.state == "cancelled" else "deferred" if outcome.deferred_bundles else "failed",
                        delta,
                        projected=projected,
                        tombstoned=tombstoned,
                        deferred=deferred,
                        transactions=transactions,
                        elapsed_ms=self._elapsed(start),
                        error=error,
                    )

            prior_versions = list(removed) + [prior for prior, _current in replaced]
            for batch_start in range(0, len(prior_versions), MAX_BUNDLES_PER_TRANSACTION):
                requests = tuple(
                    ProjectionDeleteRequest(_source_id(record), _external_kind(record), _external_key(record))
                    for record in prior_versions[batch_start : batch_start + MAX_BUNDLES_PER_TRANSACTION]
                )
                with perf_span("messages.file_delete_writer_wait", min_ms=5.0):
                    future = (writer.submit_deletions(requests, cancel_event=cancel_event)
                              if cancel_event is not None else writer.submit_deletions(requests))
                    outcome = self._await_write(future, cancel_event, len(requests))
                tombstoned += int(outcome.message_upserts)
                deferred += int(outcome.deferred_bundles)
                transactions += int(outcome.transactions)
                if outcome.state != "committed":
                    error = outcome.error or outcome.state
                    return self._result(
                        "cancelled" if outcome.state == "cancelled" else "deferred" if outcome.deferred_bundles else "failed",
                        delta,
                        projected=projected,
                        tombstoned=tombstoned,
                        deferred=deferred,
                        transactions=transactions,
                        elapsed_ms=self._elapsed(start),
                        error=error,
                    )

            if cancel_event is not None and cancel_event.is_set():
                return self._result("cancelled", delta, projected=projected, tombstoned=tombstoned,
                                    transactions=transactions, error="cancelled", elapsed_ms=self._elapsed(start))
            inventory_writes = self._persist_inventory(
                current,
                current if seed_inventory else changed,
                removed,
                signature,
                mtimes_json,
                fingerprint,
            )
            return self._result(
                "updated" if not delta.is_empty else "cached",
                delta,
                projected=projected,
                tombstoned=tombstoned,
                transactions=transactions,
                inventory_writes=inventory_writes,
                elapsed_ms=self._elapsed(start),
            )
        except Exception as exc:
            return self._result(
                "failed", delta, projected=projected, tombstoned=tombstoned,
                deferred=deferred, transactions=transactions, elapsed_ms=self._elapsed(start), error=_text(exc)
            )

    @staticmethod
    def _await_write(future: Future, cancel_event: threading.Event | None, count: int) -> ProjectionWriteResult:
        if cancel_event is None:
            return future.result()
        while True:
            if cancel_event.is_set():
                # A running writer may finish its current complete bundle.
                # Never advance the scan checkpoint: the next scan safely
                # reconciles committed and deferred work through the same writer.
                future.cancel()
                return ProjectionWriteResult(state="cancelled", attempted_bundles=count,
                                             deferred_bundles=count, error="cancelled")
            try:
                return future.result(timeout=0.1)
            except TimeoutError:
                continue
            except CancelledError:
                return ProjectionWriteResult(state="cancelled", attempted_bundles=count,
                                             deferred_bundles=count, error="cancelled")

    def _prepare_bundles(self, records: Sequence[FileRecord]):
        conn = connect_sqlite_readonly(self._db_path, timeout=5.0, row_factory=sqlite3.Row, busy_timeout_ms=5000)
        try:
            # No schema call: initialization is a startup boundary.
            return prepare_file_projection_bundles(conn, records)
        finally:
            conn.close()

    def _load_cache(self) -> sqlite3.Row | None:
        try:
            conn = connect_sqlite_readonly(self._db_path, timeout=2.0, row_factory=sqlite3.Row, busy_timeout_ms=2000)
            try:
                return conn.execute(
                    "SELECT watch_signature, dir_mtimes_json, inventory_fingerprint FROM message_file_scan_cache WHERE cache_key=?",
                    (_CACHE_KEY,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None

    def _has_inventory(self) -> bool:
        try:
            conn = connect_sqlite_readonly(self._db_path, timeout=2.0, busy_timeout_ms=2000)
            try:
                return bool(conn.execute("SELECT 1 FROM message_file_scan_inventory LIMIT 1").fetchone())
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def _persist_inventory(
        self,
        current: Sequence[FileRecord],
        changed: Sequence[FileRecord],
        removed: Sequence[FileRecord],
        watch_signature: str,
        mtimes_json: str,
        fingerprint: str,
    ) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn = connect_sqlite_runtime_write(self._db_path, timeout=5.0, row_factory=sqlite3.Row, busy_timeout_ms=5000)
        writes = 0
        try:
            with conn:
                for record in changed:
                    origin, path_key = _record_identity(record)
                    conn.execute(
                        """
                        INSERT INTO message_file_scan_inventory
                            (origin, path_key, path_display, source_id, source_label, size, mtime, updated_utc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(origin, path_key) DO UPDATE SET
                            path_display=excluded.path_display, source_id=excluded.source_id,
                            source_label=excluded.source_label, size=excluded.size, mtime=excluded.mtime,
                            updated_utc=excluded.updated_utc
                        """,
                        (origin, path_key, file_path_display(record.path), _text(record.source_id), _text(record.source_label), int(record.size or 0), float(record.mtime or 0.0), now),
                    )
                    writes += 1
                for record in removed:
                    origin, path_key = _record_identity(record)
                    cur = conn.execute("DELETE FROM message_file_scan_inventory WHERE origin=? AND path_key=?", (origin, path_key))
                    writes += max(0, int(cur.rowcount or 0))
                conn.execute(
                    """
                    INSERT INTO message_file_scan_cache
                        (cache_key, watch_signature, dir_mtimes_json, inventory_fingerprint, record_count, updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        watch_signature=excluded.watch_signature, dir_mtimes_json=excluded.dir_mtimes_json,
                        inventory_fingerprint=excluded.inventory_fingerprint, record_count=excluded.record_count,
                        updated_utc=excluded.updated_utc
                    """,
                    (_CACHE_KEY, watch_signature, mtimes_json, fingerprint, len(current), now),
                )
                writes += 1
            return writes
        finally:
            conn.close()

    @staticmethod
    def _elapsed(start: float) -> float:
        return round((time.monotonic() - start) * 1000.0, 3)

    @staticmethod
    def _result(
        state: str,
        delta: FileScanDelta,
        *,
        inventory_writes: int = 0,
        projected: int = 0,
        tombstoned: int = 0,
        deferred: int = 0,
        transactions: int = 0,
        elapsed_ms: float = 0.0,
        error: str = "",
    ) -> FileProjectionPipelineResult:
        return FileProjectionPipelineResult(
            state=state,
            added_or_changed=len(delta.added_or_changed),
            removed=len(delta.removed),
            unchanged=len(delta.unchanged),
            inventory_writes=inventory_writes,
            projected=projected,
            tombstoned=tombstoned,
            deferred=deferred,
            transactions=transactions,
            elapsed_ms=elapsed_ms,
            error=error,
        )
