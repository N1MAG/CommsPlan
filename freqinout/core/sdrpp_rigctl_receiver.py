"""Bounded, receive-only SDR++ RigCTL receiver adapter.

This adapter talks to SDR++'s *application* RigCTL server.  It deliberately
does not open an RTL-SDR (or any other hardware) driver and has no PTT or
transmit API.  SDR++ chooses which VFO its RigCTL server controls; the saved
``sdr_target`` is therefore an operator label for that selected VFO, not a
claim that FIO can enumerate or switch SDR++ VFOs.

SDR++ currently accepts one RigCTL client at a time.  Every FIO operation
opens a short-lived TCP connection, completes one bounded command, and closes
it.  This prevents a scheduler lane from monopolising the server or an
operator's manual RigCTL client.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from typing import Callable, Mapping, Optional, Sequence, Tuple

from freqinout.core.receiver_control import (
    CancelCheck,
    ManualReceiverControl,
    ReceiverCapabilities,
    ReceiverCommand,
    ReceiverControlClient,
    ReceiverIdentity,
    ReceiverState,
    receiver_identity_from_profile,
)


SDRPP_RIGCTL_ADAPTER_ID = "sdrpp_rigctl"
SDRPP_RIGCTL_DEFAULT_PORT = 4532
_MAX_RESPONSE_BYTES = 4096
_MAX_RESPONSE_LINES = 8
_MAX_LINE_BYTES = 1024
_SOCKET_WAIT_SLICE_SECONDS = 0.20


class ReceiverControlError(RuntimeError):
    """A bounded SDR++ receiver operation failed before it was verified."""


class _ReceiverOperationCancelled(ReceiverControlError):
    pass


def _clean(value: object) -> str:
    return str(value or "").strip()


def _cancelled(cancel: CancelCheck) -> bool:
    try:
        return bool(cancel())
    except Exception:
        # A faulty callback must fail safe rather than permit additional I/O.
        return True


def _port(value: object) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("SDR++ RigCTL requires a port between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SDR++ RigCTL requires a port between 1 and 65535")
    return port


class SdrppRigctlReceiverControl:
    """RigCTL bridge for one already-selected SDR++ receive VFO.

    The object is Qt-free and safe for one endpoint lane.  Its lock only
    serializes one adapter's own short-lived exchanges; other endpoint lanes
    and other receivers remain independent.  ``close`` closes the currently
    active socket without waiting for that lane to finish.
    """

    def __init__(
        self,
        identity: ReceiverIdentity,
        *,
        host: str,
        port: int = SDRPP_RIGCTL_DEFAULT_PORT,
        readback_tolerance_hz: int = 25,
        timeout_s: float = 0.75,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if identity.adapter_id != SDRPP_RIGCTL_ADAPTER_ID:
            raise ValueError("SDR++ RigCTL receiver identity has the wrong adapter ID")
        host_value = _clean(host)
        if not host_value or len(host_value) > 255:
            raise ValueError("SDR++ RigCTL requires a host name or address")
        tolerance = int(readback_tolerance_hz)
        if tolerance < 0:
            raise ValueError("readback_tolerance_hz cannot be negative")
        operation_timeout = float(timeout_s)
        if operation_timeout <= 0 or operation_timeout > 10.0:
            raise ValueError("timeout_s must be greater than zero and no more than 10 seconds")
        self._identity = identity
        self._host = host_value
        self._port = _port(port)
        self._readback_tolerance_hz = tolerance
        self._operation_timeout_s = operation_timeout
        self._socket_factory = socket_factory
        self._monotonic = monotonic
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_socket: Optional[socket.socket] = None
        self._closed = False
        self._mode_supported: Optional[bool] = None
        self._supported_modes: Tuple[str, ...] = ()

    @property
    def identity(self) -> ReceiverIdentity:
        return self._identity

    def _is_closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _remaining(self, deadline: float) -> float:
        return float(deadline) - float(self._monotonic())

    def _bounded_deadline(self, deadline: float) -> float:
        """Respect the caller's one deadline while capping a single exchange."""

        return min(float(deadline), float(self._monotonic()) + self._operation_timeout_s)

    def _check(self, *, deadline: float, cancel: CancelCheck) -> None:
        if _cancelled(cancel):
            raise _ReceiverOperationCancelled("SDR++ receiver operation cancelled.")
        if self._is_closed():
            raise ReceiverControlError("SDR++ receiver control is closed; manual tuning remains available.")
        if self._remaining(deadline) <= 0:
            raise ReceiverControlError("SDR++ receiver operation timed out; manual tuning remains available.")

    def _lock_io(self, *, deadline: float, cancel: CancelCheck) -> None:
        while True:
            self._check(deadline=deadline, cancel=cancel)
            if self._io_lock.acquire(timeout=min(_SOCKET_WAIT_SLICE_SECONDS, self._remaining(deadline))):
                return

    def _socket_timeout(self, deadline: float) -> float:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise ReceiverControlError("SDR++ receiver operation timed out; manual tuning remains available.")
        return min(_SOCKET_WAIT_SLICE_SECONDS, remaining)

    @staticmethod
    def _close_socket(sock: Optional[socket.socket]) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    def _register_socket(self, sock: socket.socket) -> None:
        with self._state_lock:
            if self._closed:
                self._close_socket(sock)
                raise ReceiverControlError("SDR++ receiver control is closed; manual tuning remains available.")
            self._active_socket = sock

    def _unregister_socket(self, sock: socket.socket) -> None:
        with self._state_lock:
            if self._active_socket is sock:
                self._active_socket = None

    def _send_all(self, sock: socket.socket, payload: bytes, *, deadline: float, cancel: CancelCheck) -> None:
        offset = 0
        while offset < len(payload):
            self._check(deadline=deadline, cancel=cancel)
            try:
                sock.settimeout(self._socket_timeout(deadline))
                sent = sock.send(payload[offset:])
            except socket.timeout:
                continue
            except OSError as exc:
                if self._is_closed():
                    raise ReceiverControlError("SDR++ receiver control closed during command.") from exc
                raise ReceiverControlError(f"SDR++ RigCTL command failed: {exc}") from exc
            if sent <= 0:
                raise ReceiverControlError("SDR++ RigCTL closed while CommsPlan was sending a receive-only command.")
            offset += sent

    def _read_lines(
        self,
        sock: socket.socket,
        *,
        minimum_lines: int,
        maximum_lines: int,
        deadline: float,
        cancel: CancelCheck,
    ) -> Tuple[str, ...]:
        if not 1 <= minimum_lines <= maximum_lines <= _MAX_RESPONSE_LINES:
            raise ValueError("invalid bounded RigCTL response limits")
        data = bytearray()
        lines: list[str] = []
        while True:
            self._check(deadline=deadline, cancel=cancel)
            while b"\n" in data:
                raw, _, remainder = data.partition(b"\n")
                data = bytearray(remainder)
                if len(raw) > _MAX_LINE_BYTES:
                    raise ReceiverControlError("SDR++ RigCTL returned an overlong response line.")
                line = raw.rstrip(b"\r").decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                lines.append(line)
                if line.upper().startswith("RPRT") or len(lines) >= maximum_lines:
                    return tuple(lines)
                if len(lines) >= minimum_lines and maximum_lines == minimum_lines:
                    return tuple(lines)
            if len(data) >= _MAX_LINE_BYTES or len(data) + sum(len(item) for item in lines) >= _MAX_RESPONSE_BYTES:
                raise ReceiverControlError("SDR++ RigCTL response exceeded CommsPlan's safe size limit.")
            try:
                sock.settimeout(self._socket_timeout(deadline))
                chunk = sock.recv(min(1024, _MAX_RESPONSE_BYTES - len(data)))
            except socket.timeout:
                continue
            except OSError as exc:
                if self._is_closed():
                    raise ReceiverControlError("SDR++ receiver control closed during readback.") from exc
                raise ReceiverControlError(f"SDR++ RigCTL readback failed: {exc}") from exc
            if not chunk:
                if lines and len(lines) >= minimum_lines:
                    return tuple(lines)
                raise ReceiverControlError("SDR++ RigCTL closed before returning a complete response.")
            data.extend(chunk)
            if len(data) > _MAX_RESPONSE_BYTES:
                raise ReceiverControlError("SDR++ RigCTL response exceeded CommsPlan's safe size limit.")

    @staticmethod
    def _require_rprt_ok(lines: Sequence[str], operation: str) -> None:
        if not lines:
            raise ReceiverControlError(f"SDR++ RigCTL returned no response to {operation}.")
        first = lines[0].strip()
        if not first.upper().startswith("RPRT"):
            raise ReceiverControlError(f"SDR++ RigCTL returned an unexpected response to {operation}: {first[:120]}")
        parts = first.split()
        try:
            code = int(parts[1])
        except (IndexError, ValueError) as exc:
            raise ReceiverControlError(f"SDR++ RigCTL returned an invalid RPRT response to {operation}.") from exc
        if code != 0:
            raise ReceiverControlError(f"SDR++ RigCTL rejected {operation} (RPRT {code}).")

    @staticmethod
    def _frequency_from_lines(lines: Sequence[str]) -> int:
        if not lines:
            raise ReceiverControlError("SDR++ RigCTL returned no frequency readback.")
        value = lines[0].strip()
        if value.upper().startswith("RPRT"):
            raise ReceiverControlError(f"SDR++ RigCTL could not read frequency ({value[:120]}).")
        try:
            frequency = int(float(value))
        except (TypeError, ValueError) as exc:
            raise ReceiverControlError(f"SDR++ RigCTL returned an invalid frequency: {value[:120]}") from exc
        if frequency <= 0:
            raise ReceiverControlError("SDR++ RigCTL returned a non-positive frequency.")
        return frequency

    @staticmethod
    def _mode_from_lines(lines: Sequence[str]) -> Tuple[str, Optional[int]]:
        if not lines or lines[0].upper().startswith("RPRT"):
            raise ReceiverControlError("SDR++ RigCTL did not report mode readback.")
        first_parts = lines[0].split()
        mode = _clean(first_parts[0] if first_parts else "")
        if not mode or len(mode) > 64:
            raise ReceiverControlError("SDR++ RigCTL returned an invalid mode.")
        bandwidth: Optional[int] = None
        bandwidth_text = first_parts[1] if len(first_parts) > 1 else ""
        if not bandwidth_text and len(lines) > 1 and not lines[1].upper().startswith("RPRT"):
            bandwidth_text = lines[1]
        if bandwidth_text:
            try:
                parsed = int(float(bandwidth_text))
            except (TypeError, ValueError) as exc:
                raise ReceiverControlError("SDR++ RigCTL returned an invalid mode bandwidth.") from exc
            if parsed > 0:
                bandwidth = parsed
        return mode, bandwidth

    def _exchange(
        self,
        command: str,
        *,
        minimum_lines: int = 1,
        maximum_lines: int = 1,
        deadline: float,
        cancel: CancelCheck,
    ) -> Tuple[str, ...]:
        payload = (str(command).strip() + "\n").encode("ascii", errors="strict")
        if len(payload) > 256:
            raise ValueError("RigCTL command exceeds the receiver safety limit")
        self._lock_io(deadline=deadline, cancel=cancel)
        sock: Optional[socket.socket] = None
        try:
            self._check(deadline=deadline, cancel=cancel)
            try:
                sock = self._socket_factory((self._host, self._port), timeout=self._socket_timeout(deadline))
            except socket.timeout as exc:
                raise ReceiverControlError("SDR++ RigCTL connection timed out; manual tuning remains available.") from exc
            except OSError as exc:
                if self._is_closed():
                    raise ReceiverControlError("SDR++ receiver control is closed; manual tuning remains available.") from exc
                raise ReceiverControlError(f"Could not connect to SDR++ RigCTL at {self._host}:{self._port}: {exc}") from exc
            self._register_socket(sock)
            self._send_all(sock, payload, deadline=deadline, cancel=cancel)
            return self._read_lines(
                sock,
                minimum_lines=minimum_lines,
                maximum_lines=maximum_lines,
                deadline=deadline,
                cancel=cancel,
            )
        finally:
            self._unregister_socket(sock) if sock is not None else None
            self._close_socket(sock)
            self._io_lock.release()

    def _cancelled_state(self, *, detail: str = "SDR++ receiver operation cancelled.") -> ReceiverState:
        return ReceiverState(
            identity=self._identity,
            available=not self._is_closed(),
            running=not self._is_closed(),
            manual=False,
            cancelled=True,
            detail=detail,
        )

    def _closed_state(self) -> ReceiverState:
        return ReceiverState(
            identity=self._identity,
            available=False,
            running=False,
            manual=False,
            detail="SDR++ receiver control is closed; manual tuning remains available.",
        )

    def _require_target(self, target: ReceiverIdentity) -> None:
        if (
            target.adapter_id != self._identity.adapter_id
            or target.receiver_id != self._identity.receiver_id
            or target.target_id != self._identity.target_id
        ):
            raise ValueError("receiver target does not belong to this SDR++ RigCTL adapter")

    def _read_mode(self, *, deadline: float, cancel: CancelCheck) -> Tuple[str, Optional[int]]:
        # SDR++ uses the basic Hamlib ``m`` reply (mode + passband).  A failed
        # query only disables optional mode control; frequency control remains
        # governed by its own successful readback.
        lines = self._exchange("m", minimum_lines=2, maximum_lines=2, deadline=deadline, cancel=cancel)
        return self._mode_from_lines(lines)

    def _read_supported_modes(self, *, deadline: float, cancel: CancelCheck) -> Tuple[str, ...]:
        lines = self._exchange("M ?", minimum_lines=1, maximum_lines=1, deadline=deadline, cancel=cancel)
        if not lines or lines[0].upper().startswith("RPRT"):
            raise ReceiverControlError("SDR++ RigCTL did not advertise writable modes.")
        modes = tuple(
            token.upper()
            for token in lines[0].replace(",", " ").split()
            if token and len(token) <= 32 and token.replace("-", "").isalnum()
        )
        if not modes:
            raise ReceiverControlError("SDR++ RigCTL returned an empty writable-mode list.")
        return modes

    def probe(self, *, deadline: float, cancel: CancelCheck) -> Tuple[ReceiverIdentity, ReceiverCapabilities]:
        if _cancelled(cancel):
            return self._identity, ReceiverCapabilities(detail="SDR++ receiver probe cancelled.")
        if self._is_closed():
            return self._identity, ReceiverCapabilities(detail="SDR++ receiver control is closed; manual tuning remains available.")
        operation_deadline = self._bounded_deadline(deadline)
        try:
            self._frequency_from_lines(
                self._exchange("f", deadline=operation_deadline, cancel=cancel)
            )
        except _ReceiverOperationCancelled:
            return self._identity, ReceiverCapabilities(detail="SDR++ receiver probe cancelled.")
        except ReceiverControlError as exc:
            return self._identity, ReceiverCapabilities(
                detail=f"SDR++ RigCTL is unavailable ({str(exc)[:180]}); tune this receiver manually."
            )
        mode_detail = "frequency readback available"
        try:
            self._supported_modes = self._read_supported_modes(
                deadline=operation_deadline,
                cancel=cancel,
            )
            self._read_mode(deadline=operation_deadline, cancel=cancel)
            self._mode_supported = True
            mode_detail = "frequency and advertised mode control available"
        except _ReceiverOperationCancelled:
            return self._identity, ReceiverCapabilities(detail="SDR++ receiver probe cancelled.")
        except ReceiverControlError:
            self._mode_supported = False
            self._supported_modes = ()
            mode_detail = "frequency readback available; mode control was not advertised by this SDR++ RigCTL server"
        return self._identity, ReceiverCapabilities(
            can_list_targets=False,
            can_read_state=True,
            can_set_receive_frequency=True,
            can_set_receive_mode=bool(self._mode_supported),
            can_set_receive_bandwidth=bool(self._mode_supported),
            can_verify_state=True,
            readback_tolerance_hz=self._readback_tolerance_hz,
            manual_only=False,
            detail=(
                f"SDR++ RigCTL controls SDR++'s selected VFO ({self._identity.target_id}); {mode_detail}."
            ),
        )

    def list_targets(self, *, deadline: float, cancel: CancelCheck) -> Tuple[ReceiverIdentity, ...]:
        # SDR++ RigCTL does not disclose a named VFO list.  Return only the
        # operator-configured target label after the normal bounded guards.
        try:
            self._check(deadline=deadline, cancel=cancel)
        except _ReceiverOperationCancelled:
            return ()
        except ReceiverControlError:
            return ()
        return (self._identity,)

    def read_state(self, target: ReceiverIdentity, *, deadline: float, cancel: CancelCheck) -> ReceiverState:
        self._require_target(target)
        if _cancelled(cancel):
            return self._cancelled_state()
        if self._is_closed():
            return self._closed_state()
        operation_deadline = self._bounded_deadline(deadline)
        try:
            frequency = self._frequency_from_lines(self._exchange("f", deadline=operation_deadline, cancel=cancel))
            mode = ""
            bandwidth: Optional[int] = None
            if self._mode_supported is True:
                try:
                    mode, bandwidth = self._read_mode(deadline=operation_deadline, cancel=cancel)
                    self._mode_supported = True
                except _ReceiverOperationCancelled:
                    return self._cancelled_state()
                except ReceiverControlError:
                    self._mode_supported = False
            return ReceiverState(
                identity=self._identity,
                available=True,
                running=True,
                frequency_hz=frequency,
                mode=mode,
                bandwidth_hz=bandwidth,
                manual=False,
                verified=False,
                detail="SDR++ selected-VFO readback.",
            )
        except _ReceiverOperationCancelled:
            return self._cancelled_state()
        except ReceiverControlError as exc:
            return ReceiverState(
                identity=self._identity,
                available=False,
                running=False,
                manual=True,
                verified=False,
                detail=f"SDR++ RigCTL readback failed ({str(exc)[:180]}); tune this receiver manually.",
            )

    def set_receive_frequency(
        self,
        target: ReceiverIdentity,
        frequency_hz: int,
        *,
        deadline: float,
        cancel: CancelCheck,
    ) -> ReceiverState:
        self._require_target(target)
        try:
            frequency = int(frequency_hz)
        except (TypeError, ValueError) as exc:
            raise ValueError("frequency_hz must be a positive integer") from exc
        if frequency <= 0:
            raise ValueError("frequency_hz must be a positive integer")
        if _cancelled(cancel):
            return self._cancelled_state()
        if self._is_closed():
            return self._closed_state()
        operation_deadline = self._bounded_deadline(deadline)
        try:
            self._require_rprt_ok(
                self._exchange(f"F {frequency}", deadline=operation_deadline, cancel=cancel),
                "receive frequency set",
            )
        except _ReceiverOperationCancelled:
            return self._cancelled_state()
        except ReceiverControlError as exc:
            return ReceiverState(
                identity=self._identity,
                available=False,
                running=False,
                manual=True,
                verified=False,
                detail=f"SDR++ RigCTL tuning failed ({str(exc)[:180]}); tune this receiver manually.",
            )
        return ReceiverState(
            identity=self._identity,
            available=True,
            running=True,
            frequency_hz=frequency,
            manual=False,
            verified=False,
            detail="SDR++ RigCTL accepted the receive-frequency command; readback verification is still required.",
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
        self._require_target(target)
        mode_value = _clean(mode).upper()
        if not mode_value or len(mode_value) > 32 or any(char.isspace() for char in mode_value):
            raise ValueError("mode must be a short RigCTL mode token")
        if bandwidth_hz is None:
            bandwidth = 0
        else:
            try:
                bandwidth = int(bandwidth_hz)
            except (TypeError, ValueError) as exc:
                raise ValueError("bandwidth_hz must be a non-negative integer") from exc
            if bandwidth < 0:
                raise ValueError("bandwidth_hz must be a non-negative integer")
        if _cancelled(cancel):
            return self._cancelled_state()
        if self._is_closed():
            return self._closed_state()
        if self._mode_supported is False:
            return ReceiverState(
                identity=self._identity,
                available=True,
                running=True,
                mode=mode_value,
                bandwidth_hz=bandwidth or None,
                manual=True,
                verified=False,
                detail="This SDR++ RigCTL server did not advertise verified mode control; set mode manually.",
            )
        if self._mode_supported is not True or mode_value not in self._supported_modes:
            return ReceiverState(
                identity=self._identity,
                available=True,
                running=True,
                mode=mode_value,
                bandwidth_hz=bandwidth or None,
                manual=True,
                verified=False,
                detail="The requested mode was not advertised by SDR++ RigCTL; set mode manually.",
            )
        operation_deadline = self._bounded_deadline(deadline)
        try:
            self._require_rprt_ok(
                self._exchange(f"M {mode_value} {bandwidth}", deadline=operation_deadline, cancel=cancel),
                "receive mode set",
            )
            self._mode_supported = True
        except _ReceiverOperationCancelled:
            return self._cancelled_state()
        except ReceiverControlError as exc:
            self._mode_supported = False
            return ReceiverState(
                identity=self._identity,
                available=False,
                running=False,
                manual=True,
                verified=False,
                detail=f"SDR++ RigCTL mode change failed ({str(exc)[:180]}); set mode manually.",
            )
        return ReceiverState(
            identity=self._identity,
            available=True,
            running=True,
            mode=mode_value,
            bandwidth_hz=bandwidth or None,
            manual=False,
            verified=False,
            detail="SDR++ RigCTL accepted the receive-mode command; readback verification is still required.",
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
        self._require_target(target)
        if expected.target_id != target.target_id:
            raise ValueError("receiver command target does not match receiver identity")
        tolerance = int(tolerance_hz)
        if tolerance < 0:
            raise ValueError("tolerance_hz cannot be negative")
        state = self.read_state(target, deadline=deadline, cancel=cancel)
        if state.cancelled or not state.available:
            return state
        frequency_ok = (
            expected.frequency_hz is None
            or (state.frequency_hz is not None and abs(state.frequency_hz - expected.frequency_hz) <= tolerance)
        )
        mode_ok = not expected.mode or state.mode.upper() == expected.mode.upper()
        bandwidth_ok = (
            expected.bandwidth_hz is None
            or state.bandwidth_hz is not None and state.bandwidth_hz == expected.bandwidth_hz
        )
        verified = bool(frequency_ok and mode_ok and bandwidth_ok)
        return replace(
            state,
            verified=verified,
            detail=("SDR++ receive state verified by readback." if verified else "SDR++ receive readback did not match the requested state."),
        )

    def close(self) -> None:
        """Fence future calls and interrupt the active bounded socket immediately."""

        with self._state_lock:
            self._closed = True
            sock = self._active_socket
            self._active_socket = None
        self._close_socket(sock)


def receiver_control_client_from_profile(
    profile: Mapping[str, object],
    *,
    socket_factory: Callable[..., socket.socket] = socket.create_connection,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReceiverControlClient:
    """Build an SDR++ adapter only for a complete saved observer profile.

    This factory performs no endpoint I/O.  Incomplete, incompatible, or
    manually configured profiles receive the explicit zero-I/O manual client.
    Runtime activation still independently requires the persisted verified and
    enabled gates in :mod:`station_runtime_manager`.
    """

    identity = receiver_identity_from_profile(profile)
    adapter = _clean(profile.get("sdr_adapter")).lower().replace("-", "_") or "manual"
    device_class = _clean(profile.get("device_class")).lower() or "tx_rx"
    if device_class != "observer":
        return ManualReceiverControl(identity, guidance="Only an observer / SDR profile can use receive-only application control.")
    if adapter != SDRPP_RIGCTL_ADAPTER_ID:
        return ManualReceiverControl(identity, guidance="CommsPlan has no enabled receive-only adapter for this SDR configuration; tune it manually.")
    host = _clean(profile.get("sdr_host"))
    target = _clean(profile.get("sdr_target"))
    try:
        port = _port(profile.get("sdr_port"))
    except ValueError:
        port = 0
    if not host or not target or not port:
        return ManualReceiverControl(
            identity,
            guidance="Configure SDR++ RigCTL host, port, and the selected SDR++ VFO label before enabling CommsPlan tuning.",
        )
    return SdrppRigctlReceiverControl(
        identity,
        host=host,
        port=port,
        socket_factory=socket_factory,
        monotonic=monotonic,
    )


__all__ = [
    "ReceiverControlError",
    "SDRPP_RIGCTL_ADAPTER_ID",
    "SDRPP_RIGCTL_DEFAULT_PORT",
    "SdrppRigctlReceiverControl",
    "receiver_control_client_from_profile",
]
