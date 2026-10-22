"""Cache-only operator edits and revisioned unfinished work, never runtime authority.

Persistence functions run on a worker's existing MultiRadioStore. Canonical reads,
validation and typed publication are supplied by the owning radio/app adapter;
they must not discover, launch, tune, or write native application files.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
from uuid import uuid4


DRAFT_VERSION = 1
MAX_DRAFT_BYTES = 64 * 1024
_DRAFT_PREFIX = 'operator_configuration_draft_v1:'


class ConfigurationConflict(ValueError):
    """The same fields or saved unfinished work changed after this edit began."""


@dataclass(frozen=True)
class SuggestionToken:
    session_id: str
    generation: int
    fields: tuple[tuple[str, int], ...]


class ConfigEditSession:
    """One editor owns its values; cached suggestions have no write precedence."""

    def __init__(self, values: Mapping[str, Any], *, watch_fields=(), draft_revision: int = 0):
        self.session_id = uuid4().hex
        self.base = deepcopy(dict(values))
        self.values = deepcopy(dict(values))
        self._saved_values = deepcopy(dict(values))
        self.watch_fields = tuple(watch_fields)
        self.draft_revision = int(draft_revision)
        self.revision = 0
        self.generation = 0
        self.closed = False
        self.recovered_from: str | None = None
        self._field_revisions: dict[str, int] = {}

    @property
    def has_unsaved_changes(self) -> bool:
        return self.values != self._saved_values

    @property
    def dirty_fields(self) -> tuple[str, ...]:
        return tuple(key for key in self.values if key not in self.base or self.values[key] != self.base[key])

    def edit(self, key: str, value: Any) -> None:
        if self.closed:
            raise RuntimeError('This configuration editor is closed.')
        self.values[key] = deepcopy(value)
        self.revision += 1
        self._field_revisions[key] = self._field_revisions.get(key, 0) + 1

    def suggestion_token(self, fields) -> SuggestionToken:
        return SuggestionToken(self.session_id, self.generation,
                               tuple((key, self._field_revisions.get(key, 0)) for key in fields))

    def accept_suggestion(self, token: SuggestionToken, values: Mapping[str, Any]) -> bool:
        """Explicit acceptance only; reject the whole bundle if any field changed."""
        versions = dict(token.fields)
        if (self.closed or token.session_id != self.session_id or token.generation != self.generation
                or set(values) != set(versions)
                or any(self._field_revisions.get(key, 0) != version for key, version in token.fields)):
            return False
        for key, value in values.items():
            self.edit(key, value)
        return True

    def cancel(self) -> None:
        self.closed = True
        self.generation += 1

    def snapshot(self) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError('This configuration editor is closed.')
        return deepcopy(dict(version=DRAFT_VERSION, session_id=self.session_id,
                             revision=self.revision, base=self.base, values=self.values,
                             watch_fields=list(self.watch_fields), draft_revision=self.draft_revision,
                             recovered_from=self.recovered_from))

    @classmethod
    def recover(cls, envelope: Mapping[str, Any]) -> 'ConfigEditSession':
        """Called only after the operator explicitly chooses Recover unfinished work."""
        payload = envelope.get('payload')
        if not isinstance(payload, Mapping) or payload.get('version') != DRAFT_VERSION:
            raise ValueError('No supported unfinished configuration is available.')
        session = cls(payload['base'], watch_fields=payload.get('watch_fields', ()),
                      draft_revision=envelope['revision'])
        session.values = deepcopy(dict(payload['values']))
        session._saved_values = deepcopy(session.values)
        session.recovered_from = payload['session_id']
        return session

    def acknowledge(self, result: 'ConfigurationSaveResult', requested_revision: int) -> bool:
        # A worker result must not clear later keystrokes, resurrect cancellation,
        # or replace the draft that the operator has since started editing.
        if self.closed or self.revision != requested_revision or result.session_id != self.session_id:
            return False
        self.draft_revision = result.draft_revision
        self._saved_values = deepcopy(self.values)
        if result.state == 'saved':
            self.base = deepcopy(result.values)
            self.values = deepcopy(result.values)
            self._saved_values = deepcopy(result.values)
        return True


@dataclass(frozen=True)
class ConfigurationSaveResult:
    session_id: str
    state: str
    draft_revision: int
    values: dict[str, Any]
    issues: tuple[str, ...] = ()
    unfinished_retained: bool = False


def _key(scope: str) -> str:
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 180:
        raise ValueError('A bounded configuration owner scope is required.')
    return _DRAFT_PREFIX + scope


def _encode(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(encoded.encode('utf-8')) > MAX_DRAFT_BYTES:
        raise ValueError('The unfinished configuration exceeds the size limit.')
    return encoded


def _read_envelope(conn, key: str) -> dict[str, Any]:
    row = conn.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    if row is None:
        return dict(version=DRAFT_VERSION, revision=0, payload=None)
    if len(row[0].encode('utf-8')) > MAX_DRAFT_BYTES:
        raise ValueError('The saved unfinished configuration exceeds the size limit.')
    value = json.loads(row[0])
    if (not isinstance(value, dict) or value.get('version') != DRAFT_VERSION
            or not isinstance(value.get('revision'), int) or value['revision'] < 0):
        raise ValueError('The saved unfinished configuration has an unsupported format.')
    return value


def load_unfinished_configuration(store, scope: str) -> dict[str, Any]:
    """Read only. Loading neither restores editor values nor publishes configuration."""
    with store.connect_readonly() as conn:
        return _read_envelope(conn, _key(scope))


def open_configuration_edit(store, scope: str, *, read_current, watch_fields=()):
    """Worker-side coherent open: retain unfinished evidence without adopting it."""
    with store.connect_readonly() as conn:
        conn.execute('BEGIN')
        envelope = _read_envelope(conn, _key(scope))
        session = ConfigEditSession(read_current(conn), watch_fields=watch_fields,
                                    draft_revision=envelope['revision'])
    return session, envelope


def save_configuration_edit(
    store, scope: str, request: Mapping[str, Any], *,
    read_current: Callable[[Any], Mapping[str, Any]],
    validate: Callable[[Mapping[str, Any]], tuple[str, ...]],
    publish: Callable[[Any, Mapping[str, Any]], None],
) -> ConfigurationSaveResult:
    """Atomic CP-only save; invalid raw values are kept outside typed runtime rows.

    read_current/publish run within the existing guided transaction. The adapter
    owns typed records, association/resource claims and coherent readback. This
    helper does not invent another configuration schema or activation engine.
    """
    key = _key(scope)
    request = deepcopy(dict(request))  # caller passes a detached worker snapshot
    if request.get('version') != DRAFT_VERSION:
        raise ValueError('Unsupported edit request version.')
    _encode(request)
    base, proposed = request['base'], request['values']
    changed = {name: value for name, value in proposed.items() if name not in base or base[name] != value}
    expected = request['draft_revision']
    with store.configuration_save_transaction() as transaction:
        with store.connect() as conn:
            envelope = _read_envelope(conn, key)
            if envelope['revision'] != expected:
                raise ConfigurationConflict('This saved unfinished configuration changed; review it again.')
            saved_payload = envelope.get('payload')
            owns_draft = (saved_payload is None
                          or saved_payload.get('session_id') == request['session_id']
                          or saved_payload.get('session_id') == request.get('recovered_from'))
            current = deepcopy(dict(read_current(conn)))
            merged = {**current, **changed}
            issues = tuple(validate(merged))
            if not owns_draft and (changed or issues):
                raise ConfigurationConflict('Unfinished work exists; recover or explicitly discard it before replacing it.')
            next_revision = expected + 1
            if issues:
                # Raw/invalid values are durable but do not replace active typed
                # rows. Keep the original base for later conflict review.
                payload = request
                state, result_values = 'incomplete', deepcopy(proposed)
            else:
                for name in set(changed) | set(request.get('watch_fields', ())):
                    if (name in base) != (name in current) or base.get(name) != current.get(name):
                        raise ConfigurationConflict(f'{name} changed after this editor opened; review both values.')
                if changed:
                    publish(conn, deepcopy(merged))
                result_values = deepcopy(dict(read_current(conn)))
                # A typed adapter must acknowledge every changed value. A lossy
                # normalizer or incomplete bundle must not be called Saved.
                mismatches = [name for name,value in changed.items() if name not in result_values or result_values[name] != value]
                if mismatches:
                    raise ValueError('Saved configuration readback does not match the reviewed changes: ' + ', '.join(sorted(mismatches)))
                payload, state = None, 'saved'
            if not owns_draft:
                next_revision = expected
            elif changed or issues or envelope.get('payload') is not None:
                encoded = _encode(dict(version=DRAFT_VERSION, revision=next_revision, payload=payload))
                conn.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (key, encoded))
            else:
                next_revision = expected

            transaction.complete()
    return ConfigurationSaveResult(request['session_id'], state, next_revision, result_values, issues,
                                   unfinished_retained=not owns_draft)


def discard_unfinished_configuration(store, scope: str, *, expected_revision: int) -> int:
    """Explicit discard only; canonical configuration is never changed."""
    key = _key(scope)
    with store.configuration_save_transaction() as transaction:
        with store.connect() as conn:
            envelope = _read_envelope(conn, key)
            if envelope['revision'] != expected_revision:
                raise ConfigurationConflict('Unfinished work changed; review it before discarding.')
            revision = expected_revision
            if envelope.get('payload') is not None:
                revision += 1
                conn.execute('UPDATE kv SET value=? WHERE key=?',
                             (_encode(dict(version=DRAFT_VERSION, revision=revision, payload=None)), key))
            transaction.complete()
    return revision
