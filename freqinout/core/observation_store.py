from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from freqinout.core.observation_projection import Observation, utc_now_iso
from freqinout.core.sqlite_utils import connect_sqlite, connect_sqlite_readonly


class ObservationSchemaUnavailable(RuntimeError):
    """The observation projection cannot be read without creating or changing it."""


def ensure_observation_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS observation_projection (
            observation_id TEXT PRIMARY KEY,
            source_family TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_radio_id INTEGER,
            source_app TEXT,
            received_utc TEXT,
            event_utc TEXT,
            from_call TEXT,
            to_target TEXT,
            groups_json TEXT NOT NULL DEFAULT '[]',
            observed_topics_json TEXT NOT NULL DEFAULT '[]',
            operator_attention INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            urgency TEXT,
            subject TEXT,
            summary TEXT,
            state TEXT,
            grid TEXT,
            lat REAL,
            lon REAL,
            location_confidence TEXT,
            auth_state TEXT,
            trusted_state TEXT,
            confirmed_state TEXT,
            exercise_flag INTEGER NOT NULL DEFAULT 0,
            route_eligible INTEGER NOT NULL DEFAULT 0,
            publish_authorized INTEGER NOT NULL DEFAULT 0,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            projected_utc TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS observation_projection_topics (
            observation_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            PRIMARY KEY (observation_id, topic)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS observation_projection_checkpoint (
            source_key TEXT PRIMARY KEY,
            last_source_ref TEXT,
            last_event_utc TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_source ON observation_projection(source_family, event_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_callsign ON observation_projection(from_call, event_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_target ON observation_projection(to_target, event_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_state ON observation_projection(state, event_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_grid ON observation_projection(grid, event_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_status ON observation_projection(status, event_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_received ON observation_projection(received_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_topic ON observation_projection_topics(topic, observation_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_checkpoint_updated ON observation_projection_checkpoint(updated_utc)")


def upsert_observation(db_path: str | Path, observation: Observation, *, projected_utc: str | None = None) -> str:
    stamp = str(projected_utc or utc_now_iso()).strip()
    conn = connect_sqlite(db_path)
    try:
        ensure_observation_schema(conn)
        with conn:
            upsert_observation_conn(conn, observation, projected_utc=stamp)
        return observation.observation_id
    finally:
        conn.close()


def upsert_observation_conn(
    conn: sqlite3.Connection,
    observation: Observation,
    *,
    projected_utc: str | None = None,
) -> str:
    ensure_observation_schema(conn)
    stamp = str(projected_utc or utc_now_iso()).strip()
    conn.execute(
        """
        INSERT INTO observation_projection (
            observation_id,
            source_family,
            source_ref,
            source_radio_id,
            source_app,
            received_utc,
            event_utc,
            from_call,
            to_target,
            groups_json,
            observed_topics_json,
            operator_attention,
            status,
            urgency,
            subject,
            summary,
            state,
            grid,
            lat,
            lon,
            location_confidence,
            auth_state,
            trusted_state,
            confirmed_state,
            exercise_flag,
            route_eligible,
            publish_authorized,
            provenance_json,
            projected_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_id) DO UPDATE SET
            source_family=excluded.source_family,
            source_ref=excluded.source_ref,
            source_radio_id=excluded.source_radio_id,
            source_app=excluded.source_app,
            received_utc=excluded.received_utc,
            event_utc=excluded.event_utc,
            from_call=excluded.from_call,
            to_target=excluded.to_target,
            groups_json=excluded.groups_json,
            observed_topics_json=excluded.observed_topics_json,
            operator_attention=excluded.operator_attention,
            status=excluded.status,
            urgency=excluded.urgency,
            subject=excluded.subject,
            summary=excluded.summary,
            state=excluded.state,
            grid=excluded.grid,
            lat=excluded.lat,
            lon=excluded.lon,
            location_confidence=excluded.location_confidence,
            auth_state=excluded.auth_state,
            trusted_state=excluded.trusted_state,
            confirmed_state=excluded.confirmed_state,
            exercise_flag=excluded.exercise_flag,
            route_eligible=excluded.route_eligible,
            publish_authorized=excluded.publish_authorized,
            provenance_json=excluded.provenance_json,
            projected_utc=excluded.projected_utc
        """,
        _observation_values(observation, stamp),
    )
    conn.execute(
        "DELETE FROM observation_projection_topics WHERE observation_id=?",
        (observation.observation_id,),
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO observation_projection_topics (observation_id, topic)
        VALUES (?, ?)
        """,
        [(observation.observation_id, topic) for topic in observation.observed_topics],
    )
    # Keep Ops autocomplete and Last Known summaries incremental without
    # coupling the observation model to the GUI.
    from freqinout.core.ops_focus import index_observation_for_ops_focus

    index_observation_for_ops_focus(conn, observation)
    return observation.observation_id


def upsert_observations(
    db_path: str | Path,
    observations: Sequence[Observation],
    *,
    projected_utc: str | None = None,
) -> int:
    count = 0
    for observation in observations:
        upsert_observation(db_path, observation, projected_utc=projected_utc)
        count += 1
    return count


def list_observations(
    db_path: str | Path,
    *,
    source_family: str = "",
    from_call: str = "",
    to_target: str = "",
    topic: str = "",
    status: str = "",
    state: str = "",
    grid: str = "",
    since_utc: str = "",
    limit: int = 200,
    readonly: bool = False,
) -> list[Observation]:
    clauses: list[str] = []
    params: list[Any] = []
    if source_family:
        clauses.append("o.source_family=?")
        params.append(str(source_family).strip())
    if from_call:
        clauses.append("o.from_call=?")
        params.append(str(from_call).strip().upper())
    if to_target:
        clauses.append("o.to_target=?")
        params.append(str(to_target).strip().upper())
    if topic:
        clauses.append(
            "EXISTS (SELECT 1 FROM observation_projection_topics t WHERE t.observation_id=o.observation_id AND t.topic=?)"
        )
        params.append(str(topic).strip())
    if status:
        clauses.append("o.status=?")
        params.append(str(status).strip().upper())
    if state:
        clauses.append("o.state=?")
        params.append(str(state).strip().upper())
    if grid:
        clauses.append("o.grid=?")
        params.append(str(grid).strip().upper())
    if since_utc:
        clauses.append("COALESCE(o.event_utc, o.received_utc, '') >= ?")
        params.append(str(since_utc).strip())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit or 200)))
    conn: sqlite3.Connection | None = None
    try:
        conn = (
            connect_sqlite_readonly(db_path)
            if readonly
            else connect_sqlite(db_path)
        )
        if readonly:
            _validate_observation_read_schema(conn, db_path)
    except ObservationSchemaUnavailable:
        if conn is not None:
            conn.close()
        raise
    except (sqlite3.Error, OSError) as exc:
        if readonly:
            if conn is not None:
                conn.close()
            raise ObservationSchemaUnavailable(f"observation schema unavailable: {db_path}") from exc
        raise
    try:
        if not readonly:
            ensure_observation_schema(conn)
        rows = conn.execute(
            f"""
            SELECT
                observation_id,
                source_family,
                source_ref,
                source_radio_id,
                source_app,
                received_utc,
                event_utc,
                from_call,
                to_target,
                groups_json,
                observed_topics_json,
                operator_attention,
                status,
                urgency,
                subject,
                summary,
                state,
                grid,
                lat,
                lon,
                location_confidence,
                auth_state,
                trusted_state,
                confirmed_state,
                exercise_flag,
                route_eligible,
                publish_authorized,
                provenance_json
            FROM observation_projection o
            {where}
            ORDER BY COALESCE(o.event_utc, o.received_utc, '') DESC, o.observation_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [_row_to_observation(row) for row in rows]
    except sqlite3.Error as exc:
        if readonly:
            raise ObservationSchemaUnavailable(f"observation read unavailable: {db_path}") from exc
        raise
    finally:
        conn.close()


_OBSERVATION_COLUMNS = frozenset(
    {
        "observation_id", "source_family", "source_ref", "source_radio_id", "source_app",
        "received_utc", "event_utc", "from_call", "to_target", "groups_json",
        "observed_topics_json", "operator_attention", "status", "urgency", "subject",
        "summary", "state", "grid", "lat", "lon", "location_confidence", "auth_state",
        "trusted_state", "confirmed_state", "exercise_flag", "route_eligible",
        "publish_authorized", "provenance_json",
    }
)


def _validate_observation_read_schema(conn: sqlite3.Connection, db_path: str | Path) -> None:
    """Validate the read model without issuing DDL or opening a writable DB."""
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
                ("observation_projection", "observation_projection_topics"),
            ).fetchall()
        }
        required_tables = {"observation_projection", "observation_projection_topics"}
        if tables != required_tables:
            missing = ", ".join(sorted(required_tables - tables))
            raise ObservationSchemaUnavailable(
                f"observation schema unavailable: missing table(s) {missing or 'unknown'} ({db_path})"
            )
        projection_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(observation_projection)").fetchall()
        }
        topic_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(observation_projection_topics)").fetchall()
        }
        missing_projection = _OBSERVATION_COLUMNS - projection_columns
        if missing_projection or not {"observation_id", "topic"}.issubset(topic_columns):
            missing = sorted(missing_projection | ({"observation_id", "topic"} - topic_columns))
            raise ObservationSchemaUnavailable(
                f"observation schema unavailable: missing column(s) {', '.join(missing)} ({db_path})"
            )
    except ObservationSchemaUnavailable:
        raise
    except sqlite3.Error as exc:
        raise ObservationSchemaUnavailable(f"observation schema unavailable: {db_path}") from exc


def set_projection_checkpoint(
    db_path: str | Path,
    *,
    source_key: str,
    last_source_ref: str = "",
    last_event_utc: str = "",
    updated_utc: str | None = None,
) -> None:
    conn = connect_sqlite(db_path)
    try:
        ensure_observation_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO observation_projection_checkpoint (
                    source_key,
                    last_source_ref,
                    last_event_utc,
                    updated_utc
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    last_source_ref=excluded.last_source_ref,
                    last_event_utc=excluded.last_event_utc,
                    updated_utc=excluded.updated_utc
                """,
                (
                    str(source_key or "").strip(),
                    str(last_source_ref or "").strip(),
                    str(last_event_utc or "").strip(),
                    str(updated_utc or utc_now_iso()).strip(),
                ),
            )
    finally:
        conn.close()


def get_projection_checkpoint(db_path: str | Path, source_key: str) -> dict[str, str]:
    conn = connect_sqlite(db_path)
    try:
        ensure_observation_schema(conn)
        row = conn.execute(
            """
            SELECT source_key, last_source_ref, last_event_utc, updated_utc
            FROM observation_projection_checkpoint
            WHERE source_key=?
            """,
            (str(source_key or "").strip(),),
        ).fetchone()
        if not row:
            return {}
        return {
            "source_key": row[0] or "",
            "last_source_ref": row[1] or "",
            "last_event_utc": row[2] or "",
            "updated_utc": row[3] or "",
        }
    finally:
        conn.close()


def delete_observations_by_source_refs(
    db_path: str | Path,
    source_refs: Sequence[str],
    *,
    source_family: str = "",
) -> int:
    refs = sorted({str(ref or "").strip() for ref in source_refs if str(ref or "").strip()})
    if not refs:
        return 0
    placeholders = ",".join("?" for _ in refs)
    clauses = [f"source_ref IN ({placeholders})"]
    params: list[Any] = list(refs)
    if source_family:
        clauses.append("source_family=?")
        params.append(str(source_family or "").strip())
    conn = connect_sqlite(db_path)
    try:
        ensure_observation_schema(conn)
        rows = conn.execute(
            f"SELECT observation_id FROM observation_projection WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
        ids = [row[0] for row in rows if row and row[0]]
        if not ids:
            return 0
        id_placeholders = ",".join("?" for _ in ids)
        with conn:
            conn.execute(
                f"DELETE FROM observation_projection_topics WHERE observation_id IN ({id_placeholders})",
                tuple(ids),
            )
            cur = conn.execute(
                f"DELETE FROM observation_projection WHERE observation_id IN ({id_placeholders})",
                tuple(ids),
            )
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _observation_values(observation: Observation, projected_utc: str) -> tuple[Any, ...]:
    return (
        observation.observation_id,
        observation.source_family,
        observation.source_ref,
        observation.source_radio_id,
        observation.source_app,
        observation.received_utc,
        observation.event_utc,
        observation.from_call,
        observation.to_target,
        _json_dumps(observation.groups),
        _json_dumps(observation.observed_topics),
        1 if observation.operator_attention else 0,
        observation.status,
        observation.urgency,
        observation.subject,
        observation.summary,
        observation.state,
        observation.grid,
        observation.lat,
        observation.lon,
        observation.location_confidence,
        observation.auth_state,
        observation.trusted_state,
        observation.confirmed_state,
        1 if observation.exercise_flag else 0,
        1 if observation.route_eligible else 0,
        1 if observation.publish_authorized else 0,
        observation.provenance_json,
        projected_utc,
    )


def _row_to_observation(row: Sequence[Any]) -> Observation:
    return Observation(
        observation_id=row[0] or "",
        source_family=row[1] or "",
        source_ref=row[2] or "",
        source_radio_id=int(row[3]) if row[3] is not None else None,
        source_app=row[4] or "",
        received_utc=row[5] or "",
        event_utc=row[6] or "",
        from_call=row[7] or "",
        to_target=row[8] or "",
        groups=_json_tuple(row[9]),
        observed_topics=_json_tuple(row[10]),
        operator_attention=bool(row[11]),
        status=row[12] or "",
        urgency=row[13] or "",
        subject=row[14] or "",
        summary=row[15] or "",
        state=row[16] or "",
        grid=row[17] or "",
        lat=float(row[18]) if row[18] is not None else None,
        lon=float(row[19]) if row[19] is not None else None,
        location_confidence=row[20] or "",
        auth_state=row[21] or "",
        trusted_state=row[22] or "",
        confirmed_state=row[23] or "",
        exercise_flag=bool(row[24]),
        route_eligible=bool(row[25]),
        publish_authorized=bool(row[26]),
        provenance=_json_mapping(row[27]),
    )


def _json_dumps(values: object) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _json_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        loaded = json.loads(value)
    except Exception:
        return ()
    if not isinstance(loaded, list):
        return ()
    out: list[str] = []
    for item in loaded:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _json_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}
