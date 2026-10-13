"""Qt-free, receive-only control contract for SDR application adapters.

This is intentionally an adapter boundary, not an SDR driver.  Implementations
must honour each caller-supplied monotonic deadline, check ``cancel`` before
starting any blocking step, and return a bounded result.  The contract has no
PTT, transmit, or hardware-ownership operation: a receiver adapter can tune a
selected receive target only after a later scheduler lane authorizes it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable, Mapping, Optional, Protocol, Tuple, runtime_checkable


CancelCheck = Callable[[], bool]


def _clean(value: object) -> str:
    return str(value or "").strip()


def _positive_int(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("frequency and bandwidth must be positive integers")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("frequency and bandwidth must be positive integers") from exc
    if number <= 0:
        raise ValueError("frequency and bandwidth must be positive integers")
    return number


@dataclass(frozen=True)
class ReceiverCapabilities:
    """Capabilities reported by a live receiver application, never inferred."""

    can_list_targets: bool = False
    can_read_state: bool = False
    can_set_receive_frequency: bool = False
    can_set_receive_mode: bool = False
    can_set_receive_bandwidth: bool = False
    can_verify_state: bool = False
    readback_tolerance_hz: int = 0
    manual_only: bool = True
    detail: str = ""

    def __post_init__(self) -> None:
        tolerance = int(self.readback_tolerance_hz or 0)
        if tolerance < 0:
            raise ValueError("readback_tolerance_hz cannot be negative")
        object.__setattr__(self, "readback_tolerance_hz", tolerance)
        object.__setattr__(self, "detail", _clean(self.detail))


@dataclass(frozen=True)
class ReceiverIdentity:
    """Operator-readable identity for one receiver application target."""

    adapter_id: str
    receiver_id: str
    display_name: str
    application_name: str = ""
    hardware_family: str = ""
    target_id: str = ""
    endpoint_label: str = ""

    def __post_init__(self) -> None:
        adapter_id = _clean(self.adapter_id)
        receiver_id = _clean(self.receiver_id)
        display_name = _clean(self.display_name)
        if not adapter_id or not receiver_id or not display_name:
            raise ValueError("receiver identity requires adapter_id, receiver_id, and display_name")
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "receiver_id", receiver_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "application_name", _clean(self.application_name))
        object.__setattr__(self, "hardware_family", _clean(self.hardware_family))
        object.__setattr__(self, "target_id", _clean(self.target_id) or receiver_id)
        object.__setattr__(self, "endpoint_label", _clean(self.endpoint_label))


def receiver_identity_from_profile(profile: Mapping[str, object]) -> ReceiverIdentity:
    """Build persisted target identity without discovery or endpoint I/O."""

    try:
        profile_id = int(profile.get("id") or profile.get("device_profile_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("receiver profile requires a positive ID") from exc
    if profile_id <= 0:
        raise ValueError("receiver profile requires a positive ID")
    target = _clean(profile.get("sdr_target")) or f"device-profile:{profile_id}"
    name = _clean(profile.get("name")) or f"Receiver {profile_id}"
    application = _clean(profile.get("sdr_application")) or "Other / manual"
    adapter = _clean(profile.get("sdr_adapter")) or "manual"
    host = _clean(profile.get("sdr_host"))
    port = profile.get("sdr_port")
    endpoint = host
    if host and port not in (None, ""):
        endpoint = f"{host}:{int(port)}"
    return ReceiverIdentity(
        adapter_id=adapter,
        receiver_id=f"profile-{profile_id}:{target}",
        display_name=name,
        application_name=application,
        hardware_family=_clean(profile.get("radio_model")),
        target_id=target,
        endpoint_label=endpoint,
    )


def receiver_verification_evidence(profile: Mapping[str, object]) -> Mapping[str, object]:
    """Return bounded persisted receiver evidence, or an empty mapping."""

    raw = profile.get("sdr_verification")
    if raw in (None, ""):
        raw = profile.get("sdr_verification_json", "{}")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or len(raw) > 16_384:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def receiver_control_verification_matches(profile: Mapping[str, object]) -> bool:
    """Return whether successful evidence belongs to this exact endpoint.

    A stale or hand-edited ``verified`` flag must never authorize tuning. Any
    adapter, host, port, or application-target change invalidates the proof and
    safely returns the receiver to manual operation.
    """

    state = _clean(profile.get("sdr_verification_state")).lower()
    adapter = _clean(profile.get("sdr_adapter")).lower().replace("-", "_")
    host = _clean(profile.get("sdr_host")).casefold()
    target = _clean(profile.get("sdr_target")).casefold()
    try:
        port = int(profile.get("sdr_port"))
    except (TypeError, ValueError):
        return False
    if state != "verified" or adapter in {"", "manual", "none"} or not host or not target:
        return False
    evidence = receiver_verification_evidence(profile)
    try:
        evidence_schema = int(evidence.get("schema_version", 0) or 0)
        evidence_port = int(evidence.get("port"))
    except (TypeError, ValueError):
        return False
    return bool(
        evidence_schema >= 1
        and _clean(evidence.get("tested_at_utc"))
        and _clean(evidence.get("adapter")).lower().replace("-", "_") == adapter
        and _clean(evidence.get("host")).casefold() == host
        and evidence_port == port
        and _clean(evidence.get("target")).casefold() == target
        and evidence.get("tune_readback_verified") is True
        and evidence.get("restore_readback_verified") is True
    )


@dataclass(frozen=True)
class ReceiverState:
    """Readback or manual-fallback state for one receive-only target."""

    identity: ReceiverIdentity
    available: bool = False
    running: bool = False
    frequency_hz: Optional[int] = None
    mode: str = ""
    bandwidth_hz: Optional[int] = None
    control_owner: str = ""
    verified: bool = False
    manual: bool = True
    cancelled: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "frequency_hz", _positive_int(self.frequency_hz))
        object.__setattr__(self, "bandwidth_hz", _positive_int(self.bandwidth_hz))
        object.__setattr__(self, "mode", _clean(self.mode))
        object.__setattr__(self, "control_owner", _clean(self.control_owner))
        object.__setattr__(self, "detail", _clean(self.detail))
        if self.manual and self.verified:
            raise ValueError("a manual receiver state cannot claim API verification")


@dataclass(frozen=True)
class ReceiverCommand:
    """Immutable desired receive setting used for command and readback checks."""

    target_id: str
    frequency_hz: Optional[int] = None
    mode: str = ""
    bandwidth_hz: Optional[int] = None
    request_id: str = ""

    def __post_init__(self) -> None:
        target_id = _clean(self.target_id)
        if not target_id:
            raise ValueError("receiver command requires target_id")
        frequency_hz = _positive_int(self.frequency_hz)
        bandwidth_hz = _positive_int(self.bandwidth_hz)
        if frequency_hz is None and not _clean(self.mode) and bandwidth_hz is None:
            raise ValueError("receiver command requires a frequency, mode, or bandwidth")
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "frequency_hz", frequency_hz)
        object.__setattr__(self, "mode", _clean(self.mode))
        object.__setattr__(self, "bandwidth_hz", bandwidth_hz)
        object.__setattr__(self, "request_id", _clean(self.request_id))


@runtime_checkable
class ReceiverControlClient(Protocol):
    """Bounded receive-only application bridge; deliberately no PTT surface."""

    def probe(self, *, deadline: float, cancel: CancelCheck) -> Tuple[ReceiverIdentity, ReceiverCapabilities]:
        ...

    def list_targets(self, *, deadline: float, cancel: CancelCheck) -> Tuple[ReceiverIdentity, ...]:
        ...

    def read_state(self, target: ReceiverIdentity, *, deadline: float, cancel: CancelCheck) -> ReceiverState:
        ...

    def set_receive_frequency(
        self,
        target: ReceiverIdentity,
        frequency_hz: int,
        *,
        deadline: float,
        cancel: CancelCheck,
    ) -> ReceiverState:
        ...

    def set_receive_mode(
        self,
        target: ReceiverIdentity,
        mode: str,
        bandwidth_hz: Optional[int] = None,
        *,
        deadline: float,
        cancel: CancelCheck,
    ) -> ReceiverState:
        """Optional capability; unsupported adapters return an unverified state."""
        ...

    def verify_state(
        self,
        target: ReceiverIdentity,
        expected: ReceiverCommand,
        *,
        tolerance_hz: int,
        deadline: float,
        cancel: CancelCheck,
    ) -> ReceiverState:
        ...

    def close(self) -> None:
        ...


class ManualReceiverControl:
    """Explicit fallback for manually tuned receivers; every method is zero-I/O."""

    def __init__(
        self,
        identity: ReceiverIdentity,
        *,
        initial_frequency_hz: Optional[int] = None,
        initial_mode: str = "",
        initial_bandwidth_hz: Optional[int] = None,
        guidance: str = "Tune this receiver manually, then confirm the displayed frequency.",
    ) -> None:
        self._identity = identity
        self._initial_frequency_hz = _positive_int(initial_frequency_hz)
        self._initial_mode = _clean(initial_mode)
        self._initial_bandwidth_hz = _positive_int(initial_bandwidth_hz)
        self._guidance = _clean(guidance) or "Tune this receiver manually."
        self._closed = False

    @property
    def identity(self) -> ReceiverIdentity:
        return self._identity

    def _state(self, *, cancelled: bool = False, detail: str = "") -> ReceiverState:
        if self._closed:
            return ReceiverState(
                identity=self._identity,
                available=False,
                running=False,
                frequency_hz=self._initial_frequency_hz,
                mode=self._initial_mode,
                bandwidth_hz=self._initial_bandwidth_hz,
                manual=True,
                cancelled=cancelled,
                detail=detail or "Manual receiver control is closed.",
            )
        return ReceiverState(
            identity=self._identity,
            available=True,
            running=False,
            frequency_hz=self._initial_frequency_hz,
            mode=self._initial_mode,
            bandwidth_hz=self._initial_bandwidth_hz,
            manual=True,
            cancelled=cancelled,
            detail=detail or self._guidance,
        )

    @staticmethod
    def _cancelled(cancel: CancelCheck) -> bool:
        try:
            return bool(cancel())
        except Exception:
            # A broken cancellation callback is treated as cancellation so a
            # manual fallback never encourages an unbounded caller to proceed.
            return True

    def probe(self, *, deadline: float, cancel: CancelCheck) -> Tuple[ReceiverIdentity, ReceiverCapabilities]:
        del deadline
        if self._cancelled(cancel):
            return self._identity, ReceiverCapabilities(detail="Manual receiver probe cancelled.")
        return self._identity, ReceiverCapabilities(detail=self._guidance)

    def list_targets(self, *, deadline: float, cancel: CancelCheck) -> Tuple[ReceiverIdentity, ...]:
        del deadline
        return () if self._cancelled(cancel) or self._closed else (self._identity,)

    def read_state(self, target: ReceiverIdentity, *, deadline: float, cancel: CancelCheck) -> ReceiverState:
        del deadline
        self._require_target(target)
        cancelled = self._cancelled(cancel)
        return self._state(
            cancelled=cancelled,
            detail="Manual receiver read cancelled." if cancelled else "",
        )

    def set_receive_frequency(
        self,
        target: ReceiverIdentity,
        frequency_hz: int,
        *,
        deadline: float,
        cancel: CancelCheck,
    ) -> ReceiverState:
        del deadline
        self._require_target(target)
        _positive_int(frequency_hz)
        cancelled = self._cancelled(cancel)
        return self._state(
            cancelled=cancelled,
            detail=(
                "Manual receiver tune cancelled."
                if cancelled
                else "CommsPlan did not tune this manual receiver; tune it manually and confirm the frequency."
            ),
        )

    def set_receive_mode(
        self,
        target: ReceiverIdentity,
        mode: str,
        bandwidth_hz: Optional[int] = None,
        *,
        deadline: float,
        cancel: CancelCheck,
    ) -> ReceiverState:
        del deadline
        self._require_target(target)
        _clean(mode)
        _positive_int(bandwidth_hz)
        cancelled = self._cancelled(cancel)
        return self._state(
            cancelled=cancelled,
            detail=(
                "Manual receiver mode change cancelled."
                if cancelled
                else "CommsPlan did not change this manual receiver; set mode and bandwidth manually."
            ),
        )

    def verify_state(
        self,
        target: ReceiverIdentity,
        expected: ReceiverCommand,
        *,
        tolerance_hz: int,
        deadline: float,
        cancel: CancelCheck,
    ) -> ReceiverState:
        del deadline
        self._require_target(target)
        if expected.target_id != target.target_id:
            raise ValueError("receiver command target does not match receiver identity")
        if int(tolerance_hz or 0) < 0:
            raise ValueError("tolerance_hz cannot be negative")
        cancelled = self._cancelled(cancel)
        return self._state(
            cancelled=cancelled,
            detail=(
                "Manual receiver verification cancelled."
                if cancelled
                else "Manual receiver state cannot be verified by CommsPlan; confirm it at the receiver."
            ),
        )

    def close(self) -> None:
        self._closed = True

    def _require_target(self, target: ReceiverIdentity) -> None:
        if target != self._identity:
            raise ValueError("receiver target does not belong to this manual receiver")
