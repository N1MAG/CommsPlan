"""Bounded detached local pin operations for the existing management surface."""
from __future__ import annotations

from dataclasses import dataclass
import threading

from PySide6.QtCore import QObject, Signal

from freqinout.core.js8_pin_review import review_received_pin
from freqinout.core.observation_store import list_observations
from freqinout.core.rf_pins import delete_rf_pins, save_rf_pin


@dataclass(frozen=True)
class PinManagementResult:
    pins: tuple = ()
    changed: bool = False
    error: str = ""


def _operate(db_path: str, operation: str, value) -> PinManagementResult:
    changed = False
    if operation == "review":
        observation_id, accept = value
        changed = review_received_pin(db_path, observation_id, accept=bool(accept))
        if not changed:
            return PinManagementResult(error="The pending pin changed or could not be reviewed. Refresh and try again.")
    elif operation == "save":
        save_rf_pin(db_path, value)
        changed = True
    elif operation == "delete":
        changed = bool(delete_rf_pins(db_path, value))
    elif operation != "load":
        raise ValueError("Unknown pin operation")
    try:
        rows = tuple(list_observations(db_path, source_family="rf_pin", limit=500, readonly=True))
    except Exception:
        if changed:
            return PinManagementResult(changed=True, error="Local change saved; refresh to reload the pin list.")
        raise
    return PinManagementResult(pins=rows, changed=changed)


class PinManagementLane(QObject):
    ready = Signal(object)
    _settled = Signal(object)

    def __init__(self, db_path, parent=None):
        super().__init__(parent)
        self._db_path = str(db_path)
        self._busy = False
        self._closed = False
        self._cancelled = threading.Event()
        self._settled.connect(self._receive)
        self.destroyed.connect(lambda *_: self.shutdown())

    @property
    def busy(self):
        return self._busy

    def request(self, operation="load", value=None) -> bool:
        if self._closed or self._busy:
            return False
        # No QWidget/connection enters the worker. Copies are bounded by the
        # owning pin form and the at-most-500-row management selection.
        if operation == "save":
            value = dict(value)
        elif operation == "delete":
            value = tuple(value)[:500]
        elif operation == "review":
            value = (str(value[0]), bool(value[1]))
        db_path, cancelled, settled = self._db_path, self._cancelled, self._settled
        self._busy = True
        def run():
            if cancelled.is_set():
                return
            try:
                result = _operate(db_path, operation, value)
            except Exception:
                result = PinManagementResult(error="Pin operation unavailable. Refresh to retry.")
            if not cancelled.is_set():
                try:
                    settled.emit(result)
                except RuntimeError:
                    pass
        threading.Thread(target=run, daemon=True, name="pin-management").start()
        return True

    def _receive(self, result):
        self._busy = False
        if not self._closed:
            self.ready.emit(result)

    def shutdown(self):
        # An already-started explicit local transaction may commit. Closing
        # the dialog suppresses publication and never blocks the GUI waiting.
        self._closed = True
        self._cancelled.set()
