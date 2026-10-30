"""Durable net-local context; canonical incoming content remains in projection.

Only startup/administration calls ``ensure_ncs_workbench_schema``. Store methods
are synchronous worker/service APIs, never widget render or input callbacks.
There is deliberately no endpoint, receiver-start or transmission dependency.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Mapping

from freqinout.core.sqlite_utils import connect_sqlite_readonly

MAX_PAGE = 200
MAX_DOCUMENT_BYTES = 65536


class StaleWorkbenchWrite(ValueError):
    """The displayed record is no longer the current evidence/version."""


def _json(value: Mapping) -> str:
    text = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(text.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise ValueError('Net record exceeds the supported size; work was not saved')
    return text


def _required(value: str, label: str) -> str:
    value = str(value or '').strip()
    if not value or len(value) > 512:
        raise ValueError(f'{label} is required (maximum 512 characters)')
    return value


def ensure_ncs_workbench_schema(conn: sqlite3.Connection) -> None:
    """Additive v1 schema. Never commits a caller's startup transaction."""
    statements = (
        '''CREATE TABLE IF NOT EXISTS ncs_workbench_runs (
            run_id TEXT PRIMARY KEY, protocol TEXT NOT NULL, radio_id INTEGER NOT NULL,
            instance_id TEXT NOT NULL, label TEXT NOT NULL, group_name TEXT NOT NULL,
            started_ts REAL NOT NULL, ended_ts REAL, state TEXT NOT NULL
                CHECK(state IN ('active','ended','interrupted')),
            version INTEGER NOT NULL DEFAULT 1, draft_json TEXT NOT NULL DEFAULT '{}',
            source_json TEXT NOT NULL DEFAULT '{}')''',
        '''CREATE TABLE IF NOT EXISTS ncs_workbench_annotations (
            run_id TEXT NOT NULL REFERENCES ncs_workbench_runs(run_id),
            kind TEXT NOT NULL CHECK(kind IN ('station','report')), entity_id TEXT NOT NULL,
            evidence_version TEXT NOT NULL, version INTEGER NOT NULL,
            values_json TEXT NOT NULL, PRIMARY KEY(run_id,kind,entity_id,evidence_version))''',
        '''CREATE TABLE IF NOT EXISTS ncs_workbench_actions (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, entity_id TEXT NOT NULL, evidence_version TEXT NOT NULL, version INTEGER NOT NULL,
            values_json TEXT NOT NULL, created_ts REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS ncs_workbench_submissions (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL REFERENCES ncs_workbench_runs(run_id),
            action_json TEXT NOT NULL, outcome TEXT NOT NULL
                CHECK(outcome IN ('attempting','accepted','blocked','failed','uncertain')),
            detail TEXT NOT NULL DEFAULT '', created_ts REAL NOT NULL,
            completed_ts REAL)''',
        '''CREATE TABLE IF NOT EXISTS ncs_workbench_stations (
            run_id TEXT NOT NULL REFERENCES ncs_workbench_runs(run_id),
            callsign TEXT NOT NULL, values_json TEXT NOT NULL,
            counted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,callsign))''',
        'CREATE INDEX IF NOT EXISTS ncs_workbench_actions_run ON ncs_workbench_actions(run_id,sequence)',
        'CREATE INDEX IF NOT EXISTS ncs_workbench_submissions_run ON ncs_workbench_submissions(run_id,sequence)',
    )
    for statement in statements:
        conn.execute(statement)
    if 'source_json' not in {row[1] for row in conn.execute('PRAGMA table_info(ncs_workbench_runs)')}:
        conn.execute("ALTER TABLE ncs_workbench_runs ADD COLUMN source_json TEXT NOT NULL DEFAULT '{}'")
    if 'counted' not in {row[1] for row in conn.execute('PRAGMA table_info(ncs_workbench_stations)')}:
        conn.execute('ALTER TABLE ncs_workbench_stations ADD COLUMN counted INTEGER NOT NULL DEFAULT 0')
    if 'created_ts' not in {row[1] for row in conn.execute('PRAGMA table_info(ncs_workbench_runs)')}:
        conn.execute('ALTER TABLE ncs_workbench_runs ADD COLUMN created_ts REAL NOT NULL DEFAULT 0')


class NcsWorkbenchStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _write(self):
        # mode=rw prevents a missing/unavailable profile from becoming an empty DB.
        conn = sqlite3.connect(f'{self.db_path.expanduser().resolve().as_uri()}?mode=rw', uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self):
        conn = connect_sqlite_readonly(self.db_path, row_factory=sqlite3.Row)
        try:
            yield conn
        finally:
            conn.close()

    def create_run(self, *, protocol: str, radio_id: int, instance_id: str,
                   label: str, group_name: str = '', run_id: str | None = None,
                   started_ts: float | None = None, source: Mapping | None = None) -> str:
        ident = _required(run_id or uuid.uuid4().hex, 'Net run')
        protocol = _required(protocol, 'Protocol')
        instance_id = _required(instance_id, 'Instance')
        if int(radio_id) <= 0:
            raise ValueError('An unambiguous radio is required')
        with self._write() as conn:
            conn.execute('''INSERT INTO ncs_workbench_runs
                (run_id,protocol,radio_id,instance_id,label,group_name,started_ts,source_json,created_ts,state)
                VALUES(?,?,?,?,?,?,?,?,?,'active')''',
                (ident, protocol, int(radio_id), instance_id, _required(label, 'Net label'),
                 str(group_name), time.time() if started_ts is None else float(started_ts), _json(source or {}), time.time()))
        return ident

    def get_run(self, run_id: str) -> dict | None:
        with self._read() as conn:
            row = conn.execute('SELECT * FROM ncs_workbench_runs WHERE run_id=?', (run_id,)).fetchone()
            return dict(row) if row else None

    def list_runs(self, *, before: tuple[float, str] | None = None, limit: int = MAX_PAGE) -> list[dict]:
        clause, params = '', []
        if before is not None:
            clause, params = 'WHERE (started_ts,run_id)<(?,?)', list(before)
        with self._read() as conn:
            return [dict(row) for row in conn.execute(
                f'SELECT * FROM ncs_workbench_runs {clause} ORDER BY started_ts DESC,run_id DESC LIMIT ?',
                (*params, min(MAX_PAGE, max(1, int(limit)))))]

    def update_run(self, run_id: str, *, expected_version: int, label: str,
                   draft: Mapping, end: bool = False) -> int:
        encoded = _json(draft)
        with self._write() as conn:
            changed = conn.execute('''UPDATE ncs_workbench_runs SET label=?,draft_json=?,
                state=CASE WHEN ? THEN 'ended' ELSE state END,
                ended_ts=CASE WHEN ? THEN ? ELSE ended_ts END,version=version+1
                WHERE run_id=? AND version=? AND (NOT ? OR state='active')''',
                (_required(label, 'Net label'), encoded, end, end, time.time(), run_id, expected_version, end))
            if changed.rowcount != 1:
                raise StaleWorkbenchWrite('Net changed; reload before saving')
        return expected_version + 1

    def interrupt_active_runs(self, *, before_ts: float | None = None) -> int:
        """Explicit startup recovery only; no send attempts are ever replayed."""
        with self._write() as conn:
            cutoff = float('inf') if before_ts is None else float(before_ts)
            count = conn.execute("UPDATE ncs_workbench_runs SET state='interrupted',version=version+1 WHERE state='active' AND created_ts<=?", (cutoff,)).rowcount
            conn.execute("""UPDATE ncs_workbench_submissions SET outcome='uncertain',
                detail='Interrupted; submission outcome unknown',completed_ts=? WHERE outcome='attempting'
                AND run_id IN (SELECT run_id FROM ncs_workbench_runs WHERE created_ts<=?)""", (time.time(), cutoff))
            return count

    def annotation(self, run_id: str, kind: str, entity_id: str, *, evidence_version: str) -> dict | None:
        with self._read() as conn:
            row = conn.execute('SELECT * FROM ncs_workbench_annotations WHERE run_id=? AND kind=? AND entity_id=? AND evidence_version=?',
                               (run_id, kind, entity_id, evidence_version)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['values'] = json.loads(result.pop('values_json'))
            return result

    def annotate(self, run_id: str, kind: str, entity_id: str, *, evidence_version: str,
                 expected_version: int, values: Mapping, verify_projection: bool = False,
                 expected_run_version: int | None = None) -> int:
        """Compare-and-swap the complete net-local annotation; no source edits.

        A controller must verify the currently published evidence hash before
        submitting a report mutation. The retained evidence hash keeps old review
        state distinguishable when projection content changes.
        """
        if kind not in {'station', 'report'}:
            raise ValueError('Unknown annotation kind')
        entity_id = _required(entity_id, 'Entity')
        evidence_version = _required(evidence_version, 'Evidence version')
        encoded = _json(values)
        version = int(expected_version) + 1
        with self._write() as conn:
            if expected_run_version is not None:
                run = conn.execute('SELECT version FROM ncs_workbench_runs WHERE run_id=?', (run_id,)).fetchone()
                if not run or run['version'] != expected_run_version:
                    raise StaleWorkbenchWrite('Net context changed; pending work retained')
            if verify_projection:
                if kind != 'report':
                    raise ValueError('Evidence verification applies to reports')
                current = conn.execute('SELECT content_hash FROM message_projection WHERE canonical_key=? AND deleted=0', (entity_id,)).fetchone()
                if not current or current['content_hash'] != evidence_version:
                    raise StaleWorkbenchWrite('Report evidence updated; pending work retained')
            if expected_version == 0:
                try:
                    conn.execute('INSERT INTO ncs_workbench_annotations VALUES(?,?,?,?,?,?)',
                                 (run_id, kind, entity_id, evidence_version, version, encoded))
                except sqlite3.IntegrityError as exc:
                    raise StaleWorkbenchWrite('Annotation changed or net is unavailable') from exc
            else:
                changed = conn.execute('''UPDATE ncs_workbench_annotations SET evidence_version=?,version=?,values_json=?
                    WHERE run_id=? AND kind=? AND entity_id=? AND evidence_version=? AND version=?''',
                    (evidence_version, version, encoded, run_id, kind, entity_id, evidence_version, expected_version))
                if changed.rowcount != 1:
                    raise StaleWorkbenchWrite('Annotation changed; reload before saving')
            conn.execute('''INSERT INTO ncs_workbench_actions
                (run_id,kind,entity_id,evidence_version,version,values_json,created_ts) VALUES(?,?,?,?,?,?,?)''',
                (run_id, kind, entity_id, evidence_version, version, encoded, time.time()))
        return version

    def begin_submission(self, run_id: str, *, intent_id: str, action: Mapping) -> None:
        """Persist an explicit intent before the caller invokes guarded send once.

        Reusing an intent raises IntegrityError, never authorizes another send.
        """
        encoded = _json(action)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM ncs_workbench_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row['state'] != 'active':
                raise StaleWorkbenchWrite('This net is no longer active')
            conn.execute('''INSERT INTO ncs_workbench_submissions
                (intent_id,run_id,action_json,outcome,created_ts) VALUES(?,?,?,'attempting',?)''',
                (_required(intent_id, 'Intent'), run_id, encoded, time.time()))

    def finish_submission(self, run_id: str, intent_id: str, *, outcome: str, detail: str = '') -> None:
        if outcome not in {'accepted', 'blocked', 'failed', 'uncertain'}:
            raise ValueError('Unknown send outcome')
        with self._write() as conn:
            changed = conn.execute('''UPDATE ncs_workbench_submissions SET outcome=?,detail=?,completed_ts=?
                WHERE run_id=? AND intent_id=? AND outcome='attempting' ''',
                (outcome, str(detail)[:4096], time.time(), run_id, intent_id))
            if changed.rowcount != 1:
                raise StaleWorkbenchWrite('Submission outcome is already recorded or unavailable')

    def group_action_history(self, run_id: str) -> tuple[tuple[str, ...], dict[str, set[str]]]:
        """All accepted group queries/ACKs, independently of visible history paging."""
        groups, acknowledged, before = [], {}, None
        while True:
            rows = self.submissions(run_id, before=before)
            for row in rows:
                action = row['action']
                target = str(action.get('destination') or '').strip().upper()
                if row['outcome'] != 'accepted' or not target.startswith('@') or len(target) < 2:
                    continue
                if action.get('kind') == 'query' and target not in groups:
                    groups.append(target)
                elif action.get('kind') == 'ack':
                    acknowledged.setdefault(target, set()).update(
                        str(call).upper() for call in action.get('ack_calls', []) if isinstance(call, str))
            if len(rows) < MAX_PAGE:
                return tuple(groups), acknowledged
            before = rows[-1]['sequence']

    def submissions(self, run_id: str, *, before: int | None = None, limit: int = MAX_PAGE) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute('''SELECT * FROM ncs_workbench_submissions WHERE run_id=?
                AND sequence<? ORDER BY sequence DESC LIMIT ?''',
                (run_id, before if before is not None else 9223372036854775807, min(MAX_PAGE, max(1, int(limit)))))
            result = []
            for row in rows:
                item = dict(row)
                item['action'] = json.loads(item.pop('action_json'))
                result.append(item)
            return result

    def annotations(self, run_id: str, *, kind: str, after: tuple[str, str] = ('', ''),
                    limit: int = MAX_PAGE) -> list[dict]:
        """Stable bounded traversal includes older versions with unresolved work."""
        with self._read() as conn:
            rows = conn.execute(
                """SELECT * FROM ncs_workbench_annotations WHERE run_id=? AND kind=?
                   AND (entity_id,evidence_version)>(?,?)
                   ORDER BY entity_id,evidence_version LIMIT ?""",
                (run_id, kind, *after, min(MAX_PAGE, max(1, int(limit)))))
            result = []
            for row in rows:
                item = dict(row)
                item['values'] = json.loads(item.pop('values_json'))
                result.append(item)
            return result


    def observe_stations(self, run_id: str, stations: Mapping) -> None:
        """Retain receiver metadata separately from operator annotations."""
        if len(stations) > MAX_PAGE:
            raise ValueError('Station observation batch exceeds page bound')
        rows = [(run_id, _required(call, 'Callsign').upper(), _json(data))
                for call, data in stations.items()]
        with self._write() as conn:
            conn.executemany("""INSERT INTO ncs_workbench_stations(run_id,callsign,values_json) VALUES(?,?,?)
                ON CONFLICT(run_id,callsign) DO UPDATE SET values_json=excluded.values_json""", rows)

    def stations(self, run_id: str, *, after: str = '', limit: int = MAX_PAGE) -> list[dict]:
        with self._read() as conn:
            return [dict(callsign=row[0], values=json.loads(row[1])) for row in conn.execute(
                'SELECT callsign,values_json FROM ncs_workbench_stations WHERE run_id=? AND callsign>? ORDER BY callsign LIMIT ?',
                (run_id, after, min(MAX_PAGE, max(1, int(limit)))))]


    def count_participants(self, run_id: str, callsigns, *, already_counted=()) -> tuple[str, ...]:
        """Atomically increment legacy counters once per net/base callsign.

        Excluding/restoring a station never clears this durable marker. Metadata
        refreshes update only values_json and cannot reset counted.
        """
        import datetime
        stamp = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        calls = sorted({str(call).strip().upper() for call in callsigns if str(call).strip()})
        previous = {str(call).strip().upper().split('/', 1)[0] for call in already_counted}
        counted = []
        with self._write() as conn:
            run = conn.execute('SELECT state FROM ncs_workbench_runs WHERE run_id=?', (run_id,)).fetchone()
            if not run or run['state'] != 'active':
                raise StaleWorkbenchWrite('Only the active net can record check-in counters')
            for call in calls:
                excluded = conn.execute("""SELECT 1 FROM ncs_workbench_annotations WHERE run_id=?
                    AND kind='station' AND entity_id=?
                    AND evidence_version='station' AND json_extract(values_json,'$.excluded')=1 LIMIT 1""",
                    (run_id, call)).fetchone()
                base = call.split('/', 1)[0]
                if excluded or base in counted:
                    continue
                # Exclusions follow the displayed full callsign. The legacy
                # counter is once per base; a portable alias is not a second
                # participant and does not create a phantom base roster row.
                marked = conn.execute("""SELECT 1 FROM ncs_workbench_stations WHERE run_id=?
                    AND (callsign=? OR callsign LIKE ?) AND counted=1 LIMIT 1""",
                    (run_id, base, base + '/%')).fetchone()
                conn.execute("INSERT OR IGNORE INTO ncs_workbench_stations(run_id,callsign,values_json) VALUES(?,?,'{}')", (run_id, call))
                conn.execute('UPDATE ncs_workbench_stations SET counted=1 WHERE run_id=? AND callsign=?', (run_id, call))
                if not marked and base not in previous:
                    conn.execute("""INSERT INTO operator_checkins
                        (callsign,name,state,grid,group1,group2,group3,group_role,first_seen_utc,last_seen_utc,
                         last_net,last_role,checkin_count,groups_json,trusted)
                        VALUES (?,'','','','','','','',?,?,'','',1,NULL,0)
                        ON CONFLICT(callsign) DO UPDATE SET
                        checkin_count=COALESCE(operator_checkins.checkin_count,0)+1,last_seen_utc=excluded.last_seen_utc""",
                        (base, stamp, stamp))
                counted.append(base)
        return tuple(counted)
