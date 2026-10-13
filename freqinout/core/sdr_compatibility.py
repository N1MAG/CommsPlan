"""Bounded, operator-facing SDR compatibility catalog.

The catalog is deliberately data-only.  It must be safe to import from the
Qt process and from tests without probing hardware, opening a socket, or
loading a vendor driver.  Application compatibility and FIO verification are
separate facts: a receiver can always remain a useful manually tuned target.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Tuple


SDR_COMPATIBILITY_SCHEMA_VERSION = 1
MAX_COMPATIBILITY_RECORDS = 64

OPERATOR_STATES = (
    "FIO tuning ready",
    "Connected; verify tuning",
    "Manual tuning",
    "Receiver unavailable",
)

FIO_IMPLEMENTATION_STATUSES = ("Manual", "Planned", "Experimental", "Verified")
UPSTREAM_SUPPORT_STATUSES = ("working", "beta", "manual", "unknown")


def _text(value: object) -> str:
    return str(value or "").strip()


def _tuple_text(values: Iterable[object]) -> Tuple[str, ...]:
    result: list[str] = []
    for value in values:
        item = _text(value)
        if item and item not in result:
            result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class SdrCompatibilityEntry:
    """One application/hardware compatibility statement.

    ``upstream_support`` describes the receiver application's own support;
    ``fio_status`` describes only FIO's implementation/evidence state.  The
    two values must never be collapsed into a single Supported label.
    """

    key: str
    hardware_family: str
    application: str
    api: str
    upstream_support: str
    fio_status: str
    platforms: Tuple[str, ...] = ()
    hardware_models: Tuple[str, ...] = ()
    capabilities: Tuple[str, ...] = ()
    manual_available: bool = True
    notes: str = ""
    source: str = ""
    source_version: str = ""
    last_verified_date: str = ""

    def __post_init__(self) -> None:
        for field_name in ("key", "hardware_family", "application", "api"):
            value = _text(getattr(self, field_name))
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)
        upstream = _text(self.upstream_support).lower()
        if upstream not in UPSTREAM_SUPPORT_STATUSES:
            raise ValueError("upstream_support is not a known compatibility status")
        object.__setattr__(self, "upstream_support", upstream)
        fio_status = _text(self.fio_status).title()
        if fio_status not in FIO_IMPLEMENTATION_STATUSES:
            raise ValueError("fio_status is not a known implementation status")
        object.__setattr__(self, "fio_status", fio_status)
        for field_name in ("platforms", "hardware_models", "capabilities"):
            object.__setattr__(self, field_name, _tuple_text(getattr(self, field_name)))
        object.__setattr__(self, "manual_available", bool(self.manual_available))
        for field_name in ("notes", "source", "source_version", "last_verified_date"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))

    @property
    def operator_label(self) -> str:
        """Friendly hardware/application label; no opaque IDs are exposed."""

        return f"{self.hardware_family} in {self.application}"

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["schema_version"] = SDR_COMPATIBILITY_SCHEMA_VERSION
        return result


def classify_receiver_state(
    *,
    configured: bool = True,
    manual_only: bool = False,
    api_reachable: bool = False,
    capability_verified: bool = False,
    hardware_verified: bool = False,
) -> str:
    """Return the only truthful operator state for a receiver configuration.

    Manual receivers intentionally do not become unavailable merely because
    FIO cannot probe them: they remain selectable with copy/open guidance.
    Configured API receivers are unavailable when their endpoint is down.
    """

    if manual_only or not configured:
        return "Manual tuning"
    if not api_reachable:
        return "Receiver unavailable"
    if capability_verified and hardware_verified:
        return "FIO tuning ready"
    return "Connected; verify tuning"


class SdrCompatibilityRegistry:
    """Immutable bounded query surface for application/hardware statements."""

    schema_version = SDR_COMPATIBILITY_SCHEMA_VERSION

    def __init__(self, entries: Iterable[SdrCompatibilityEntry] = ()) -> None:
        normalized = tuple(entries)
        if len(normalized) > MAX_COMPATIBILITY_RECORDS:
            raise ValueError("SDR compatibility registry exceeds bounded size")
        keys = [entry.key for entry in normalized]
        if len(set(keys)) != len(keys):
            raise ValueError("SDR compatibility registry keys must be unique")
        self._entries = normalized

    def entries(self) -> Tuple[SdrCompatibilityEntry, ...]:
        return self._entries

    def find(self, key: str) -> SdrCompatibilityEntry | None:
        wanted = _text(key)
        return next((entry for entry in self._entries if entry.key == wanted), None)

    def for_hardware(self, hardware_family: str) -> Tuple[SdrCompatibilityEntry, ...]:
        wanted = _text(hardware_family).casefold()
        return tuple(entry for entry in self._entries if entry.hardware_family.casefold() == wanted)

    def for_application(self, application: str) -> Tuple[SdrCompatibilityEntry, ...]:
        wanted = _text(application).casefold()
        return tuple(entry for entry in self._entries if entry.application.casefold() == wanted)

    def manual_entries(self) -> Tuple[SdrCompatibilityEntry, ...]:
        return tuple(entry for entry in self._entries if entry.manual_available)

    def verified_entries(self) -> Tuple[SdrCompatibilityEntry, ...]:
        return tuple(entry for entry in self._entries if entry.fio_status == "Verified")

    def as_dicts(self) -> Tuple[Mapping[str, object], ...]:
        return tuple(entry.as_dict() for entry in self._entries)


# This is intentionally a small catalog of useful starting points.  It is not
# a claim that every application build contains every source module, nor that
# an application-compatible hardware family is FIO-verified.  RTL-SDR is
# included explicitly because it is the initial acceptance target.
SDR_COMPATIBILITY_REGISTRY = SdrCompatibilityRegistry(
    (
        SdrCompatibilityEntry(
            key="sdrpp-rtl-sdr-rigctl",
            hardware_family="RTL-SDR",
            application="SDR++",
            api="RigCTL server",
            upstream_support="working",
            fio_status="Experimental",
            platforms=("Linux", "macOS", "Windows"),
            capabilities=("frequency", "readback", "optional mode", "manual fallback"),
            notes="CommsPlan's receive-only adapter is implemented. Enable SDR++ RigCTL tuning and select the intended VFO; each exact hardware/application/OS combination remains unverified until its live gate passes.",
            source="SDR++ upstream source/module matrix",
            source_version="8c9f5ee8fe405775bfcd62c8c8f8c0fc928a64af",
        ),
        SdrCompatibilityEntry(
            key="sdrangel-rtl-sdr-rest",
            hardware_family="RTL-SDR",
            application="SDRangel",
            api="REST API",
            upstream_support="working",
            fio_status="Planned",
            platforms=("Linux", "macOS", "Windows"),
            capabilities=("frequency", "mode", "bandwidth", "readback"),
            notes="The live device-set and channel schema must be discovered before control is offered.",
            source="SDRangel receiver source-plugin matrix",
        ),
        SdrCompatibilityEntry(
            key="gqrx-rtl-sdr-rigctl",
            hardware_family="RTL-SDR",
            application="Gqrx",
            api="RigCTL-compatible socket",
            upstream_support="working",
            fio_status="Planned",
            platforms=("Linux", "macOS"),
            capabilities=("frequency", "readback"),
            notes="Gqrx has separate center-frequency and receiver-VFO semantics from SDR++.",
            source="Gqrx upstream hardware statement",
        ),
        SdrCompatibilityEntry(
            key="sdrconnect-rsp-family-websocket",
            hardware_family="SDRplay RSP family",
            application="SDRconnect",
            api="WebSocket API",
            upstream_support="working",
            fio_status="Planned",
            platforms=("Linux", "macOS", "Windows"),
            capabilities=("frequency", "mode", "bandwidth", "readback"),
            notes="Exact RSP model and application version require separate CommsPlan acceptance.",
            source="SDRplay SDRconnect platform and WebSocket documentation",
        ),
        SdrCompatibilityEntry(
            key="manual-other-receiver",
            hardware_family="Other / manual",
            application="Operator-controlled receiver",
            api="None",
            upstream_support="manual",
            fio_status="Manual",
            platforms=("Linux", "macOS", "Windows"),
            capabilities=("copy frequency", "manual mode", "manual bandwidth"),
            notes="Always selectable. CommsPlan provides frequency details and optional safe application launch without claiming control.",
            source="CommsPlan operator contract",
        ),
    )
)


def get_sdr_compatibility_registry() -> SdrCompatibilityRegistry:
    """Return the process-wide immutable catalog without device discovery."""

    return SDR_COMPATIBILITY_REGISTRY


__all__ = [
    "FIO_IMPLEMENTATION_STATUSES",
    "MAX_COMPATIBILITY_RECORDS",
    "OPERATOR_STATES",
    "SDR_COMPATIBILITY_REGISTRY",
    "SDR_COMPATIBILITY_SCHEMA_VERSION",
    "SdrCompatibilityEntry",
    "SdrCompatibilityRegistry",
    "classify_receiver_state",
    "get_sdr_compatibility_registry",
]
