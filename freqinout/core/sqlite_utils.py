from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from freqinout.core.logger import log
from freqinout.core.perf_metrics import emit_span


def configure_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: Optional[int] = None,
) -> sqlite3.Connection:
    busy_ms = int(busy_timeout_ms if busy_timeout_ms is not None else 2000)
    try:
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    except Exception as exc:
        log.debug("SQLite PRAGMA setup failed: %s", exc)
    return conn


def connect_sqlite(
    db_path: str | Path,
    *,
    timeout: float = 2.0,
    row_factory: Any = None,
    busy_timeout_ms: Optional[int] = None,
) -> sqlite3.Connection:
    path = str(db_path)
    conn = sqlite3.connect(path, timeout=max(0.1, float(timeout)))
    if row_factory is not None:
        conn.row_factory = row_factory
    busy_ms = int(busy_timeout_ms if busy_timeout_ms is not None else max(100, float(timeout) * 1000.0))
    return configure_connection(conn, busy_timeout_ms=busy_ms)


def connect_sqlite_readonly(
    db_path: str | Path,
    *,
    timeout: float = 0.25,
    row_factory: Any = None,
    busy_timeout_ms: Optional[int] = None,
) -> sqlite3.Connection:
    """Open an existing database without connection-time journal mutations.

    UI list/read helpers use this path so opening a view cannot compete with
    ingest for a schema or journal-mode write lock.  Schema creation and
    migrations remain the responsibility of the startup initializer and
    explicit write services.
    """
    path = Path(db_path).expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=max(0.05, float(timeout)))
    except sqlite3.Error as exc:
        from freqinout.core.map_read_budget import record_map_read_error
        record_map_read_error(exc)
        raise
    if row_factory is not None:
        conn.row_factory = row_factory
    busy_ms = int(busy_timeout_ms if busy_timeout_ms is not None else max(50, float(timeout) * 1000.0))
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    from freqinout.core.map_read_budget import bound_map_connection
    return bound_map_connection(conn)


def connect_sqlite_runtime_write(
    db_path: str | Path,
    *,
    timeout: float = 2.0,
    row_factory: Any = None,
    busy_timeout_ms: Optional[int] = None,
) -> sqlite3.Connection:
    """Open an initialized database for a latency-sensitive runtime write.

    Startup/explicit administration owns schema and journal-mode setup. Runtime
    audit and claim paths use this connection so every on-air event does not
    repeat ``PRAGMA journal_mode=WAL`` or other initialization work.
    """
    conn = sqlite3.connect(str(db_path), timeout=max(0.1, float(timeout)))
    if row_factory is not None:
        conn.row_factory = row_factory
    busy_ms = int(busy_timeout_ms if busy_timeout_ms is not None else max(100, float(timeout) * 1000.0))
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
            (str(table_name or "").strip(),),
        ).fetchone()
    except Exception:
        return False
    return bool(row)


def fetch_all(
    db_path: str | Path,
    query: str,
    params: Sequence[Any] = (),
    *,
    timeout: float = 2.0,
    row_factory: Any = None,
    busy_timeout_ms: Optional[int] = None,
    span_name: str = "sqlite.read",
    span_meta: Optional[dict[str, object]] = None,
    min_ms: float = 5.0,
) -> List[Any]:
    start = time.perf_counter()
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = connect_sqlite(
            db_path,
            timeout=timeout,
            row_factory=row_factory,
            busy_timeout_ms=busy_timeout_ms,
        )
        cur = conn.cursor()
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        return list(rows)
    except Exception as exc:
        log.debug("SQLite read failed (%s): %s", db_path, exc)
        raise
    finally:
        emit_span(
            span_name,
            (time.perf_counter() - start) * 1000.0,
            meta=span_meta,
            min_ms=min_ms,
        )
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def fetch_one(
    db_path: str | Path,
    query: str,
    params: Sequence[Any] = (),
    *,
    timeout: float = 2.0,
    row_factory: Any = None,
    busy_timeout_ms: Optional[int] = None,
    span_name: str = "sqlite.read",
    span_meta: Optional[dict[str, object]] = None,
    min_ms: float = 5.0,
) -> Any:
    rows = fetch_all(
        db_path,
        query,
        params,
        timeout=timeout,
        row_factory=row_factory,
        busy_timeout_ms=busy_timeout_ms,
        span_name=span_name,
        span_meta=span_meta,
        min_ms=min_ms,
    )
    return rows[0] if rows else None


def rows_to_dicts(rows: Iterable[Any]) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(dict(row))
        except Exception:
            continue
    return out
