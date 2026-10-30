"""Bounded, resumable radio/modem apply; no Qt, database, sleeps or timers."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event, Lock
import time
from typing import Callable


def _mode(value: object) -> str:
    text = str(value or "").strip().upper().replace("_", "-")
    return {"DIGI": "DATA-U", "DIGITAL": "DATA-U", "DATA": "DATA-U", "DATAU": "DATA-U",
            "PKTUSB": "DATA-U", "USB-D": "DATA-U", "DATA-USB": "DATA-U",
            "DATAL": "DATA-L", "PKTLSB": "DATA-L", "LSB-D": "DATA-L"}.get(text, text)


@dataclass
class RadioApplyOperation:
    identity: tuple
    radio_id: int | None
    frequency_hz: int
    vfo: str | None
    radio_mode: str | None
    fldigi_mode: str | None
    fldigi_offset: int | None
    apply_radio: bool
    auto_tune: bool
    started_at: float
    timeout_s: float = 60.0
    frequency_tolerance_hz: int = 5
    _cancelled: Event = field(default_factory=Event, init=False, repr=False)
    _step_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _radio_verified: bool = field(default=False, init=False)
    _tune_attempted: bool = field(default=False, init=False)
    _tune_time: float | None = field(default=None, init=False)
    _receive_time: float | None = field(default=None, init=False)
    _observed: dict = field(default_factory=dict, init=False)
    _progress: dict = field(default_factory=lambda: dict(phase="radio", complete=False,
                                                       blocked=False, detail="Applying radio settings", tune_attempted=False), init=False)

    def snapshot(self) -> dict:
        # One atomic reference replacement publishes immutable progress. Never
        # hold a UI-thread lock while a worker is making a bounded endpoint RPC.
        return dict(self._progress)

    def cancel(self) -> None:
        self._cancelled.set()

    def defer_completion(self, detail: str) -> dict:
        return self._result("companion", detail)

    def check_safety(self, preflight, now=time.monotonic) -> str | None:
        if self._cancelled.is_set():
            return "operation_cancelled"
        if now() - self.started_at >= self.timeout_s:
            return "operation_deadline"
        return preflight()

    def _result(self, phase: str, detail: str, *, complete=False, blocked=False) -> dict:
        self._progress = dict(phase=phase, detail=detail, complete=complete,
                              blocked=blocked, tune_attempted=self._tune_attempted)
        return {"ok": True, "actual_state": {**self._observed, "radio_apply": self.snapshot()}}

    def advance(self, rig, command, preflight: Callable[[], str | None],
                now: Callable[[], float] = time.monotonic) -> dict:
        """Perform at most one bounded attempt; caller schedules later attempts.

        The caller owns endpoint serialization and shared-resource reservation.
        Each mutation has a fresh safety/authority fence. Pending is a successful
        *attempt*, never evidence that the complete target has been applied.
        """
        if not self._step_lock.acquire(blocking=False):
            return {"ok": True, "actual_state": {"radio_apply": self.snapshot()}}
        try:
            if self._cancelled.is_set():
                return self._result("cancelled", "Operation cancelled", blocked=True)
            if self._progress["blocked"] or self._progress["complete"]:
                return {"ok": True, "actual_state": {**self._observed, "radio_apply": self.snapshot()}}
            if now() - self.started_at >= self.timeout_s:
                return self._result("blocked", "Radio/modem apply timed out; review receive state and FLDigi settings, then retry", blocked=True)

            def guard():
                reason = self.check_safety(preflight, now)
                if reason:
                    self._receive_time = None
                if reason in {"command_authority_changed", "schedule_occurrence_expired", "operator_schedule_approval_changed"}:
                    self._cancelled.set()
                return reason

            reason = guard()
            if reason:
                self._receive_time = None
                if self._cancelled.is_set():
                    return self._result("cancelled", "Operation no longer owns this radio target", blocked=True)
                return self._result("waiting_receive", "Waiting for verified receive/safety: " + str(reason))

            if not self._radio_verified:
                matches = self._verify_radio(rig)
                mode_mismatch = (self._observed.get("radio_mode") is not None
                                 and _mode(self._observed["radio_mode"]) != _mode(self.radio_mode))
                if not self.apply_radio and not matches:
                    actual_hz = self._observed.get("flrig_freq_hz")
                    actual_vfo = self._observed.get("flrig_vfo")
                    if ((actual_hz is not None and abs(actual_hz - self.frequency_hz) > self.frequency_tolerance_hz)
                            or (self.vfo and actual_vfo and actual_vfo.upper() != self.vfo.upper())):
                        return self._result("blocked", "Radio moved before modem correction; review target and resume", blocked=True)
                    if actual_hz is None or (self.vfo and not actual_vfo) or (self.radio_mode and not self._observed.get("radio_mode")):
                        return self._result("radio", "Radio readback unavailable; modem correction pending")
                if self.apply_radio or (not matches and mode_mismatch):
                    if guard():
                        return self._result("radio", "Radio apply waiting for safety")
                    setter = getattr(rig, "set_frequency_guarded", None)
                    applied = setter(command, guard) if callable(setter) else rig.set_frequency(command)
                    if not applied:
                        return self._result("radio", "Radio settings could not be applied; retry pending")
                reason = guard()
                if reason:
                    return self._result("radio", "Radio verification pending: " + str(reason))
                if not self._verify_radio(rig):
                    return self._result("radio", "Radio frequency, VFO or mode readback does not match; retry pending")
                self._radio_verified = True

            if self.auto_tune and not self._tune_attempted:
                reason = guard()
                if reason:
                    return self._result("waiting_receive", "Tuning waiting for verified receive: " + str(reason))
                # Persist BEFORE the RPC: a lost response must never re-key RF.
                self._tune_attempted = True
                self._tune_time = now()
                if guard():
                    return self._result("cancelled", "Authority changed before tuning; retry explicitly", blocked=True)
                try:
                    if not rig.tune():
                        return self._result("blocked", "Antenna tuning outcome is unknown; inspect radio before retrying", blocked=True)
                except Exception:
                    return self._result("blocked", "Antenna tuning outcome is unknown; inspect radio before retrying", blocked=True)
                return self._result("waiting_receive", "Radio settings verified; waiting for tuning to finish and verified receive")

            reason = guard()
            if reason:
                self._receive_time = None
                return self._result("waiting_receive", "Radio settings verified; waiting for receive: " + str(reason))
            if self._tune_time is not None:
                # tune() returning does not prove that PTT has asserted/cleared.
                # Wait out initial assertion latency, then require separated,
                # fresh receive observations. No sleep or held worker lane.
                if now() - self._tune_time < 2.0:
                    return self._result("waiting_receive", "Waiting for antenna tuning to settle")
                if self._receive_time is None:
                    self._receive_time = now()
                    return self._result("waiting_receive", "Confirming stable receive after tuning")
                if now() - self._receive_time < 1.0:
                    return self._result("waiting_receive", "Confirming stable receive after tuning")

            if not self._verify_radio(rig):
                return self._result("blocked", "Radio moved from the target during modem apply; review tuning and retry", blocked=True)
            if not rig.is_fldigi_available():
                return self._result("modem", "Radio settings verified; FLDigi is unavailable, modem settings pending")
            current_mode = rig.get_fldigi_mode()
            current_offset = rig.get_fldigi_offset()
            self._observed.update(fldigi_mode=current_mode, fldigi_offset_hz=current_offset)
            if self.fldigi_mode and str(current_mode or "").strip().upper() != self.fldigi_mode.strip().upper():
                reason = guard()
                if reason:
                    return self._result("modem", "FLDigi mode pending: " + str(reason))
                if not rig.set_fldigi_mode_offset(self.fldigi_mode, None):
                    return self._result("modem", "FLDigi mode apply failed; retry pending")
                # Changing modem can alter carrier; read it again before apply.
                current_offset = rig.get_fldigi_offset()
            if self.fldigi_offset is not None and current_offset != self.fldigi_offset:
                reason = guard()
                if reason:
                    return self._result("modem", "FLDigi offset pending: " + str(reason))
                if not rig.set_fldigi_mode_offset(None, self.fldigi_offset):
                    return self._result("modem", "FLDigi offset apply failed; retry pending")
            current_mode = rig.get_fldigi_mode()
            current_offset = rig.get_fldigi_offset()
            self._observed.update(fldigi_mode=current_mode, fldigi_offset_hz=current_offset)
            reason = guard()
            if reason:
                return self._result("modem", "FLDigi verification pending: " + str(reason))
            if (self.fldigi_mode and str(current_mode or "").strip().upper() != self.fldigi_mode.strip().upper()) or (
                self.fldigi_offset is not None and current_offset != self.fldigi_offset
            ):
                return self._result("modem", "FLDigi mode/offset readback does not match; retry pending")
            self._observed.update(checked_ts=time.time(), flrig_ptt_known=True,
                                  flrig_ptt_active=False, fldigi_available=True)
            return self._result("complete", "Radio and FLDigi settings verified", complete=True)
        except Exception as exc:
            return self._result("modem" if self._radio_verified else "radio", "Readback/apply unavailable; retry pending: " + str(exc))
        finally:
            self._step_lock.release()

    def _verify_radio(self, rig) -> bool:
        frequency = rig.get_vfo_frequency()
        vfo = rig.get_active_vfo() if self.vfo else None
        mode = rig.get_radio_mode() if self.radio_mode else None
        self._observed.update(flrig_freq_hz=frequency, flrig_vfo=vfo, radio_mode=mode)
        return bool(frequency is not None and abs(int(frequency) - self.frequency_hz) <= self.frequency_tolerance_hz
                    and (not self.vfo or str(vfo or "").upper() == self.vfo.upper())
                    and (not self.radio_mode or _mode(mode) == _mode(self.radio_mode)))
