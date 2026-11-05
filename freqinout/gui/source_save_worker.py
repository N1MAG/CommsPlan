"""Publish one detached source save operation at a time to its owning editor."""
from __future__ import annotations

from concurrent.futures import Future

from PySide6.QtCore import QObject, Qt, Signal

from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


_executor = DaemonSerialExecutor(thread_name_prefix="fio-source-save")


class SourceSaveWorker(QObject):
    _finished = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self._future = None
        self._callback = None
        self._closed = False
        self._finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self):
        return self._callback is not None

    def submit(self, function, args, on_done):
        """Queue detached inputs. Deliver the result or exception on the GUI thread."""
        if self._closed or self.busy:
            return False
        self._callback = on_done
        self._future = _executor.submit(function, *args)
        self._future.add_done_callback(self._complete)
        return True

    def _complete(self, future: Future):
        try:
            outcome = (future.result(), None)
        except Exception as exc:
            outcome = (None, exc)
        try:
            self._finished.emit(outcome)
        except RuntimeError:
            pass  # The editor was destroyed while storage work was finishing.

    def _deliver(self, outcome):
        self._future = None
        callback, self._callback = self._callback, None
        if callback is not None and not self._closed:
            callback(*outcome)

    def close(self):
        self._closed = True
        self._callback = None
        if self._future is not None:
            self._future.cancel()
            self._future = None
