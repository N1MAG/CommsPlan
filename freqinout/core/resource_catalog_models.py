"""Qt-free immutable models for the Local Nets resource catalog.

The catalog deliberately uses text keys at its service boundary.  SQLite row
ids remain an implementation detail and are never represented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Tuple


class CatalogValidationError(ValueError):
    """Raised when a catalog model violates the stable storage contract."""


class ReadOnlyResourceError(PermissionError):
    """Raised when a caller attempts to change bundled/imported content."""


class ReferencedResourceError(ValueError):
    """Raised when hard deletion is requested for an in-use resource."""


def _text(value: object, field_name: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise CatalogValidationError(f"{field_name} is required")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _hz(value: int | None, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CatalogValidationError(f"{field_name} must be integer Hz")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(f"{field_name} must be integer Hz") from exc
    if result < 0:
        raise CatalogValidationError(f"{field_name} must not be negative")
    return result


@dataclass(frozen=True, slots=True)
class CatalogSource:
    source_key: str
    label: str
    source_kind: str
    jurisdiction: str | None = None
    version: str | None = None
    effective_date: str | None = None
    source_uri: str | None = None
    last_verified_utc: str | None = None
    content_hash: str | None = None
    read_only: bool = False
    enabled: bool = True
    created_utc: str | None = None
    updated_utc: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _text(self.source_key, "source_key", required=True))
        object.__setattr__(self, "label", _text(self.label, "label", required=True))
        kind = _text(self.source_kind, "source_kind", required=True).lower()
        if kind not in {"bundled", "station", "imported"}:
            raise CatalogValidationError("source_kind must be bundled, station, or imported")
        object.__setattr__(self, "source_kind", kind)
        object.__setattr__(self, "read_only", bool(self.read_only))
        object.__setattr__(self, "enabled", bool(self.enabled))


@dataclass(frozen=True, slots=True)
class FrequencyResource:
    frequency_resource_key: str
    source_key: str
    resource_kind: str
    service: str
    label: str
    jurisdiction: str | None = None
    band: str | None = None
    channel: str | None = None
    lower_hz: int | None = None
    upper_hz: int | None = None
    receive_hz: int | None = None
    transmit_hz: int | None = None
    center_hz: int | None = None
    offset_hz: int | None = None
    mode: str | None = None
    bandwidth_hz: int | None = None
    tone: str | None = None
    locality: str | None = None
    grid: str | None = None
    coverage: str | None = None
    notes: str | None = None
    provenance: str | None = None
    content_version: str | None = None
    content_hash: str | None = None
    version_hash: str | None = None
    active: bool = True
    retired: bool = False
    replacement_frequency_resource_key: str | None = None
    created_utc: str | None = None
    updated_utc: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "frequency_resource_key", _text(self.frequency_resource_key, "frequency_resource_key", required=True))
        object.__setattr__(self, "source_key", _text(self.source_key, "source_key", required=True))
        kind = _text(self.resource_kind, "resource_kind", required=True).lower()
        if kind not in {"band_range", "channel", "simplex", "repeater"}:
            raise CatalogValidationError("invalid resource_kind")
        object.__setattr__(self, "resource_kind", kind)
        object.__setattr__(self, "service", _text(self.service, "service", required=True).upper())
        object.__setattr__(self, "label", _text(self.label, "label", required=True))
        for name in ("lower_hz", "upper_hz", "receive_hz", "transmit_hz", "center_hz", "offset_hz", "bandwidth_hz"):
            object.__setattr__(self, name, _hz(getattr(self, name), name))
        if self.lower_hz is not None and self.upper_hz is not None and self.lower_hz > self.upper_hz:
            raise CatalogValidationError("lower_hz must not exceed upper_hz")
        if kind == "band_range" and (self.lower_hz is None or self.upper_hz is None):
            raise CatalogValidationError("band_range requires lower_hz and upper_hz")
        if kind != "band_range" and all(value is None for value in (self.receive_hz, self.center_hz)):
            raise CatalogValidationError("operational resource requires receive_hz or center_hz")
        object.__setattr__(self, "active", bool(self.active))
        object.__setattr__(self, "retired", bool(self.retired))


@dataclass(frozen=True, slots=True)
class NetDirectoryEntry:
    net_entry_key: str
    source_key: str
    name: str
    description: str | None = None
    scope: str | None = None
    contact_info: str | None = None
    last_verified_utc: str | None = None
    content_hash: str | None = None
    version_hash: str | None = None
    active: bool = True
    retired: bool = False
    replacement_net_entry_key: str | None = None
    created_utc: str | None = None
    updated_utc: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "net_entry_key", _text(self.net_entry_key, "net_entry_key", required=True))
        object.__setattr__(self, "source_key", _text(self.source_key, "source_key", required=True))
        object.__setattr__(self, "name", _text(self.name, "name", required=True))
        object.__setattr__(self, "active", bool(self.active))
        object.__setattr__(self, "retired", bool(self.retired))


@dataclass(frozen=True, slots=True)
class NetDirectorySession:
    net_session_key: str
    net_entry_key: str
    source_key: str
    service: str
    frequency_resource_key: str | None = None
    recurrence: str | None = None
    local_start_time: str | None = None
    duration_minutes: int | None = None
    timezone: str | None = None
    effective_start_date: str | None = None
    effective_end_date: str | None = None
    exception_dates: Tuple[str, ...] = field(default_factory=tuple)
    reminder_minutes: int | None = None
    mode: str | None = None
    mode_details: str | None = None
    day_utc: str | None = None
    content_hash: str | None = None
    version_hash: str | None = None
    active: bool = True
    retired: bool = False
    replacement_net_session_key: str | None = None
    created_utc: str | None = None
    updated_utc: str | None = None
    fldigi_tuning_json: str = "{}"

    def __post_init__(self) -> None:
        object.__setattr__(self, "net_session_key", _text(self.net_session_key, "net_session_key", required=True))
        object.__setattr__(self, "net_entry_key", _text(self.net_entry_key, "net_entry_key", required=True))
        object.__setattr__(self, "source_key", _text(self.source_key, "source_key", required=True))
        object.__setattr__(self, "service", _text(self.service, "service", required=True).upper())
        object.__setattr__(self, "exception_dates", tuple(_text(item, "exception_date", required=True) for item in self.exception_dates))
        for name in ("duration_minutes", "reminder_minutes"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or int(value) < 0):
                raise CatalogValidationError(f"{name} must be a non-negative integer")
            object.__setattr__(self, name, None if value is None else int(value))
        object.__setattr__(self, "active", bool(self.active))
        object.__setattr__(self, "retired", bool(self.retired))


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    resource_key: str
    total_references: int
    by_kind: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_key", _text(self.resource_key, "resource_key", required=True))
        total = int(self.total_references)
        if total < 0:
            raise CatalogValidationError("total_references must not be negative")
        object.__setattr__(self, "total_references", total)
        object.__setattr__(self, "by_kind", MappingProxyType({str(key): int(value) for key, value in self.by_kind.items()}))

    @property
    def is_referenced(self) -> bool:
        return self.total_references > 0


@dataclass(frozen=True, slots=True)
class FieldDiff:
    field_name: str
    accepted_value: Any
    current_value: Any


@dataclass(frozen=True, slots=True)
class VersionComparison:
    object_key: str
    accepted_version_hash: str | None
    current_version_hash: str | None
    update_available: bool
    diffs: Tuple[FieldDiff, ...] = field(default_factory=tuple)
