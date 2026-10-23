"""Detached, cancellable latest-request lane for bounded value-only jobs."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal



class DetachedRequestLane(QObject):
    ready = Signal(int, object)
    _settled = Signal(int, object)

    def __init__(self, reader, parent=None, *, name="detached-read"):
        super().__init__(parent)
        self._reader = reader
        self._name = name
        self._generation = 0
        self._closed = False
        self._running = False
        self._pending = None
        self._cancel = threading.Event()
        self._settled.connect(self._receive)
        self.destroyed.connect(lambda *_: self.shutdown())

    def submit(self, value) -> int:
        if self._closed:
            return self._generation
        self.invalidate()
        self._pending = (self._generation, value)
        if not self._running:
            self._dispatch()
        return self._generation

    def invalidate(self) -> None:
        self._generation += 1
        self._cancel.set()
        self._pending = None

    def shutdown(self) -> None:
        self._closed = True
        self.invalidate()

    def _dispatch(self) -> None:
        if self._closed or self._pending is None:
            return
        generation, value = self._pending
        self._pending = None
        self._running = True
        cancelled = threading.Event()
        self._cancel = cancelled
        settled, reader = self._settled, self._reader
        # Only scalar request values, an Event and a signal cross the boundary.
        # No widget, settings manager, connection or bound GUI reader is used.
        def run():
            try:
                result = reader(value, cancelled.is_set)
            except Exception:
                result = None
            try:
                settled.emit(generation, result)
            except RuntimeError:
                pass  # The owning GUI object was deleted during a bounded read.
        threading.Thread(target=run, daemon=True, name=self._name).start()

    def _receive(self, generation, result):
        self._running = False
        if self._closed:
            return
        if generation == self._generation:
            self.ready.emit(generation, result)
        if self._pending is not None:
            self._dispatch()
