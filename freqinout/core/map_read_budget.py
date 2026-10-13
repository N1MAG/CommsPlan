"""Worker-scoped read limits; optional legacy tables remain optional.

A failed optional loader must not hide lock/corruption/timeout failures from the
snapshot boundary merely because its legacy API returns an empty collection.
"""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
import sqlite3
import time

_current = ContextVar('map_read_budget', default=None)


class MapReadBudget:
    def __init__(self, seconds=5.0, row_limit=20000):
        self.deadline = time.monotonic() + seconds
        self.row_limit = row_limit
        self.errors = []
        self.connections = []
        self.capped = set()

    def check(self):
        if time.monotonic() > self.deadline:
            raise sqlite3.OperationalError('Map read time budget exceeded')

    def record(self, exc):
        # Legacy transports are optional in a profile. Their absent tables and
        # compatibility columns are handled by the existing fallback readers.
        text = str(exc)
        if not text.startswith(('no such table:', 'no such column:')):
            self.errors.append(text)

    def wrap(self, conn):
        self.connections.append(conn)
        conn.set_progress_handler(lambda: int(time.monotonic() > self.deadline), 1000)
        return _Connection(conn, self)

    def raise_if_failed(self):
        self.check()
        if self.errors:
            raise sqlite3.OperationalError(self.errors[0])


@contextmanager
def map_read_budget(seconds=5.0, row_limit=20000):
    budget = MapReadBudget(seconds, row_limit)
    token = _current.set(budget)
    try:
        yield budget
    finally:
        for conn in budget.connections:
            conn.close()
        _current.reset(token)


def bound_map_connection(conn):
    budget = _current.get()
    return budget.wrap(conn) if budget is not None else conn


def record_map_read_error(exc):
    budget = _current.get()
    if budget is not None:
        budget.record(exc)


def note_map_read_cap(source, count, limit):
    budget = _current.get()
    if budget is not None and count >= limit:
        budget.capped.add(source)


class _Connection:
    def __init__(self, conn, budget):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_budget', budget)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def cursor(self):
        return _Cursor(self._conn.cursor(), self._budget)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            return self._conn.__exit__(*args)
        finally:
            self._conn.close()


class _Cursor:
    def __init__(self, cursor, budget):
        self._cursor = cursor
        self._budget = budget
        self._rows = 0

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, sql, parameters=()):
        try:
            self._budget.check()
            self._cursor.execute(sql, parameters)
            self._rows = 0
            return self
        except sqlite3.Error as exc:
            self._budget.record(exc)
            raise

    def fetchmany(self, size=None):
        try:
            self._budget.check()
            rows = self._cursor.fetchmany(min(size or self._cursor.arraysize, self._budget.row_limit + 1))
            self._rows += len(rows)
            if self._rows > self._budget.row_limit:
                raise sqlite3.OperationalError('Map query row budget exceeded')
            return rows
        except sqlite3.Error as exc:
            self._budget.record(exc)
            raise

    def fetchall(self):
        return self.fetchmany(self._budget.row_limit + 1)

    def fetchone(self):
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row
