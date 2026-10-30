"""Detached net handoffs and bounded inactive drafts; no UI, I/O or ownership.

Prepared is an association with observed configuration, never an RF-ready claim.
Only explicit Start may create a session. Immutable JSON preserves supplied null,
false and empty values separately from missing values without sharing containers.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

from freqinout.core.net_source_identity import net_source_identity
from freqinout.core.schedule_targeting import SUPPORTED_TARGET_SCOPES


class Protocol(str, Enum):
    JS8 = "JS8"
    FLDIGI = "FLDIGI"


class PreparationError(ValueError):
    pass


class DraftCapacity(PreparationError):
    pass


MAX_HANDOFF_BYTES = 2 * 1024 * 1024


def immutable(value: Any):
    if isinstance(value, (tuple, list)):
        return tuple(immutable(item) for item in value)
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), immutable(item)) for key, item in value.items()))
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float):
        encode_value(value)
        return value
    raise PreparationError("Publication identity must contain detached values")


def bounded(value: str) -> str:
    if len(value.encode()) > MAX_HANDOFF_BYTES:
        raise DraftCapacity("Preparation context exceeds supported size; review its inputs")
    return value


def encode_value(value: Any) -> str:
    """Detach JSON values, rejecting handles, callbacks and non-finite numbers."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class NetValues:
    payload: str

    def __post_init__(self):
        bounded(self.payload)
        if not isinstance(json.loads(self.payload), dict):
            raise PreparationError("Invalid net values")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> NetValues:
        keys = ("net_name", "group_name", "primary_js8call_group", "frequency", "band",
                "mode", "vfo", "fldigi_mode", "fldigi_offset", "auto_tune",
                "fldigi_tuning_json", "early_checkin")
        return cls(encode_value({key: row[key] for key in keys if key in row}))

    def values(self) -> dict[str, Any]:
        return json.loads(self.payload)


@dataclass(frozen=True)
class Target:
    scope: str = "station"
    device_id: int | None = None
    operating_id: int | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Target:
        supplied = row.get("target_scope")
        if supplied is not None and not isinstance(supplied, str):
            raise PreparationError("Saved net target is invalid; review its schedule")
        raw = str(supplied or "station").strip().lower()
        if raw not in SUPPORTED_TARGET_SCOPES:
            raise PreparationError("Saved net target is invalid; review its schedule")
        def required(key):
            value = row.get(key)
            if isinstance(value, bool):
                raise PreparationError("Saved net target is missing or invalid; review its schedule")
            try:
                number = int(str(value))
            except (TypeError, ValueError):
                raise PreparationError("Saved net target is missing or invalid; review its schedule") from None
            if number <= 0:
                raise PreparationError("Saved net target is missing or invalid; review its schedule")
            return number
        return cls(raw,
                   required("target_device_profile_id") if raw == "device_profile" else None,
                   required("target_operating_profile_id") if raw == "operating_profile" else None)

    def accepts(self, radio_id: int, operating_id: int | None) -> bool:
        return (self.scope == "station" or
                self.scope == "device_profile" and self.device_id == radio_id or
                self.scope == "operating_profile" and self.operating_id == operating_id)


def mode_protocols(mode: Any) -> tuple[Protocol, ...]:
    """Inventory-backed allowlist. Generic digital values require a choice."""
    text = str(mode or "").strip().upper().replace(" ", "")
    if text in {"JS8", "JS8CALL"}:
        return (Protocol.JS8,)
    if text in {"FLDIGI", "SSB", "USB", "LSB", "VOICE"}:
        return (Protocol.FLDIGI,)
    if text in {"", "DIGI", "DIGITAL", "TRI"}:
        return (Protocol.JS8, Protocol.FLDIGI)
    return ()


def utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PreparationError("Net occurrence must have an explicit UTC offset")
    return value.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class NetSource:
    row_id: int
    identity: tuple
    revision: tuple
    publication: Any
    occurrence_start: dt.datetime
    occurrence_end: dt.datetime
    target: Target
    values: NetValues
    table: str = "net_schedule_tab"

    def __post_init__(self):
        if type(self.row_id) is not int or self.table != "net_schedule_tab" or self.row_id <= 0:
            raise PreparationError("A saved HF Net source is required")
        object.__setattr__(self, "identity", immutable(self.identity))
        object.__setattr__(self, "revision", immutable(self.revision))
        object.__setattr__(self, "publication", immutable(self.publication))
        object.__setattr__(self, "occurrence_start", utc(self.occurrence_start))
        object.__setattr__(self, "occurrence_end", utc(self.occurrence_end))
        bounded(encode_value((self.identity, self.revision, self.publication)))
        if utc(self.occurrence_end) <= utc(self.occurrence_start):
            raise PreparationError("Net occurrence ends before it starts")

    @classmethod
    def from_row(cls, row, *, revision, publication, start, end):
        target = Target.from_row(row)  # Validate before canonical normalization.
        try:
            ident = int(str(row.get("id")))
        except (TypeError, ValueError):
            raise PreparationError("A saved HF Net source is required") from None
        return cls(ident, net_source_identity(dict(row)), revision,
                   publication, utc(start), utc(end), target, NetValues.from_row(row))

    @property
    def association(self) -> tuple:
        return (self.table, self.row_id, self.identity, self.occurrence_start,
                self.occurrence_end, self.target,
                hashlib.sha256(self.values.payload.encode()).hexdigest())

    def verify_row(self, row: Mapping[str, Any]) -> None:
        if (int(row.get("id") or 0) != self.row_id or
                Target.from_row(row) != self.target or
                NetValues.from_row(row) != self.values or
                net_source_identity(dict(row)) != self.identity):
            raise PreparationError("Source changed; review or refresh the selected net")


@dataclass(frozen=True)
class RadioScope:
    radio_id: int
    radio_name: str
    operating_id: int | None
    protocol: Protocol
    instance_id: str
    settings_payload: str
    endpoint: tuple[str, int] | None = None
    directed_path: str = ""
    all_path: str = ""
    checkin_dir: str = ""
    config_identity: str = ""

    def __post_init__(self):
        object.__setattr__(self, "protocol", Protocol(self.protocol))
        if self.endpoint is not None:
            if (not isinstance(self.endpoint, (tuple, list)) or len(self.endpoint) != 2
                    or not isinstance(self.endpoint[0], str) or not self.endpoint[0].strip()
                    or type(self.endpoint[1]) is not int or not 0 < self.endpoint[1] <= 65535):
                raise PreparationError("Service endpoint must have a host and a valid port")
            object.__setattr__(self, "endpoint", (self.endpoint[0].strip(), self.endpoint[1]))
        bounded(self.settings_payload)
        bounded(encode_value((self.radio_name, self.instance_id, self.directed_path,
                              self.all_path, self.checkin_dir, self.config_identity)))
        if type(self.radio_id) is not int or self.radio_id <= 0 or not self.instance_id:
            raise PreparationError("A configured radio and service instance are required")
        if self.protocol == Protocol.JS8:
            if not self.endpoint or not self.endpoint[0] or not 0 < self.endpoint[1] <= 65535:
                raise PreparationError("JS8 endpoint is unavailable; repair the selected radio")
        if not isinstance(json.loads(self.settings_payload), dict):
            raise PreparationError("Radio settings are unavailable")

    def settings(self) -> dict:
        return json.loads(self.settings_payload)

    @property
    def configuration_identity(self):
        values = self.settings()
        values.pop('net_schedule', None)
        return self.identity[:-1] + (hashlib.sha256(encode_value(values).encode()).hexdigest(),)

    @property
    def identity(self) -> tuple:
        return (self.radio_id, self.operating_id, self.protocol, self.instance_id,
                self.endpoint, self.directed_path, self.all_path, self.checkin_dir,
                self.config_identity, hashlib.sha256(self.settings_payload.encode()).hexdigest())


@dataclass(frozen=True)
class PreparationRequest:
    request_id: str
    lifecycle: int
    navigation: int
    origin: str
    return_context: str
    source: NetSource
    explicit_radio_id: int | None = None
    protocol_choice: Protocol | None = None
    choice_is_explicit: bool = False

    def __post_init__(self):
        if not self.request_id:
            raise PreparationError("Preparation request identity is required")
        bounded(encode_value((self.request_id, self.origin, self.return_context)))

    def allows(self, scope: RadioScope) -> bool:
        if self.explicit_radio_id is not None and self.explicit_radio_id != scope.radio_id:
            return False
        protocols = mode_protocols(self.source.values.values().get("mode"))
        if scope.protocol not in protocols:
            return False
        if len(protocols) > 1 and (not self.choice_is_explicit or self.protocol_choice != scope.protocol):
            return False
        if self.protocol_choice is not None and self.protocol_choice != scope.protocol:
            return False
        if not self.source.target.accepts(scope.radio_id, scope.operating_id):
            return False
        # An untargeted source cannot acquire scope from a display-primary fallback.
        return (self.source.target.scope == "device_profile" or
                self.explicit_radio_id == scope.radio_id)


@dataclass(frozen=True)
class PreparedNet:
    request: PreparationRequest
    scope: RadioScope

    def __post_init__(self):
        if not self.request.allows(self.scope):
            raise PreparationError("Choose an eligible radio and service for the selected net")

    @property
    def association(self):
        return self.request.source.association, self.scope.identity


@dataclass(frozen=True)
class DraftControl:
    name: str
    kind: str
    payload: str

    @classmethod
    def make(cls, name: str, kind: str, value: Any):
        if kind not in {"line", "combo", "text", "check", "spin", "table"}:
            raise PreparationError("Unsupported draft control")
        return cls(name, kind, encode_value(value))

    def __post_init__(self):
        if not self.name or self.kind not in {"line", "combo", "text", "check", "spin", "table"}:
            raise PreparationError("Unsupported draft control")
        json.loads(self.payload)

    def value(self):
        return json.loads(self.payload)


@dataclass(frozen=True)
class InactiveDraft:
    protocol: Protocol
    radio_id: int
    controls: tuple[DraftControl, ...]
    # Explicit adapter allowlists capture editable/derived fields only, never a
    # widget __dict__, active token, callbacks, queues, timers or client handles.
    state_payload: str
    operator_edited: bool
    generation: int
    preparation: PreparedNet | None = None

    def __post_init__(self):
        object.__setattr__(self, "controls", tuple(self.controls))
        if len({control.name for control in self.controls}) != len(self.controls):
            raise PreparationError("Duplicate draft control")
        if not isinstance(json.loads(self.state_payload), dict):
            raise PreparationError("Invalid editable draft state")

    def state(self):
        return json.loads(self.state_payload)

    @property
    def size(self):
        return len(encode_value(_record(self)).encode())


def _record(value):
    if isinstance(value, dt.datetime):
        return utc(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: _record(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _record(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_record(item) for item in value]
    return value


class RetainedDrafts:
    """One inactive slot per protocol/radio. Capacity is explicit, never eviction.

    Current draft and one pending request belong to the receiving coordinator.
    This store is memory only and deliberately cannot store active ownership.
    """
    def __init__(self, *, max_slots: int = 16, max_bytes: int = 16 * 1024 * 1024):
        if max_slots <= 0 or max_bytes <= 0:
            raise ValueError("Draft capacity must be positive")
        self.max_slots, self.max_bytes = max_slots, max_bytes
        self._drafts: dict[tuple, InactiveDraft] = {}

    def retain(self, draft: InactiveDraft):
        key = draft.protocol, draft.radio_id
        if key in self._drafts:
            raise DraftCapacity("A retained draft already occupies this radio; review or restore it first")
        if len(self._drafts) >= self.max_slots or sum(d.size for d in self._drafts.values()) + draft.size > self.max_bytes:
            raise DraftCapacity("Retained draft capacity reached; review or restore existing work first")
        self._drafts[key] = draft

    def get(self, protocol: Protocol, radio_id: int):
        return self._drafts.get((protocol, radio_id))

    def remove(self, draft: InactiveDraft):
        key = draft.protocol, draft.radio_id
        if self._drafts.get(key) is not draft:
            raise PreparationError("Retained draft changed; review again")
        del self._drafts[key]

    def check_exchange(self, incoming: InactiveDraft, current: InactiveDraft):
        key = incoming.protocol, incoming.radio_id
        if self._drafts.get(key) is not incoming:
            raise PreparationError('Retained draft changed; review again')
        other_key = current.protocol, current.radio_id
        if other_key != key and other_key in self._drafts:
            raise DraftCapacity('A retained draft already occupies the current radio; review or restore it first')
        total = sum(d.size for d in self._drafts.values()) - incoming.size + current.size
        if total > self.max_bytes:
            raise DraftCapacity('Retained draft capacity reached; keep the current draft and review existing work')

    def exchange(self, incoming: InactiveDraft, current: InactiveDraft):
        self.check_exchange(incoming, current)
        self.remove(incoming)
        self._drafts[(current.protocol, current.radio_id)] = current

    def values(self):
        return tuple(self._drafts.values())


@dataclass(frozen=True)
class StartIntent:
    serial: int
    association: tuple
    draft_generation: int
    lifecycle: int
    navigation: int
    expires_monotonic: float


class StartIntents:
    """One explicit Start, consumed once; validation retries never replay it."""
    def __init__(self):
        self.serial = 0
        self.current: StartIntent | None = None

    def begin(self, prepared: PreparedNet, generation: int, *, lifecycle: int,
              navigation: int, now: float, timeout: float = 10) -> StartIntent:
        self.serial += 1
        self.current = StartIntent(self.serial, prepared.association, generation,
                                   lifecycle, navigation, now + timeout)
        return self.current

    def cancel(self):
        self.current = None

    def consume(self, intent: StartIntent, prepared: PreparedNet, generation: int,
                *, lifecycle: int, navigation: int, now: float) -> bool:
        valid = (isinstance(intent, StartIntent) and self.current is intent and intent.association == prepared.association
                 and intent.draft_generation == generation and intent.lifecycle == lifecycle
                 and intent.navigation == navigation and now < intent.expires_monotonic)
        if self.current is intent:
            self.current = None
        return valid


@dataclass(frozen=True)
class NetSelection:
    """Published Ops source locator; full values are loaded only on the worker."""
    request_id: str
    row_id: int
    identity: tuple
    when_utc: dt.datetime
    revision: tuple
    publication: Any
    lifecycle: int
    navigation: int
    origin: str = 'Ops Center'
    return_context: str = 'Operating Status'
    explicit_radio_id: int | None = None
    values_identity: str = ''

    def __post_init__(self):
        if type(self.row_id) is not int or self.row_id <= 0:
            raise PreparationError('Select a saved net schedule row')
        object.__setattr__(self, 'when_utc', utc(self.when_utc))
        for key in ('identity', 'revision', 'publication'):
            object.__setattr__(self, key, immutable(getattr(self, key)))
        bounded(encode_value((self.request_id, self.identity, self.revision, self.publication)))
