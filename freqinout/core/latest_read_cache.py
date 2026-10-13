"""Single-flight, latest-input cache for blocking reads with GUI-side polling."""
from __future__ import annotations

import time

from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


class LatestReadCache:
    def __init__(self, name: str):
        self._executor = DaemonSerialExecutor(thread_name_prefix=name)
        self._future = None
        self._wanted = object()
        self._running = None
        self._reader = None
        self._value = None
        self._closed = False
        self.failed = False
        self._retry_at = 0.0
        self._published_at = 0.0

    def read(self, signature, reader_factory, *, refresh_after_s=None):
        if self._closed:
            return None
        if signature != self._wanted:
            self._wanted = signature
            self._reader = reader_factory()
            self._value = None
            self.failed = False
            self._retry_at = 0.0
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            try:
                value = future.result()
            except Exception:
                if self._running == self._wanted:
                    self._value = None
                    self.failed = True
                    self._retry_at = time.monotonic() + 30.0
            else:
                if self._running == self._wanted:
                    self._value = value
                    self.failed = False
                    self._published_at = time.monotonic()
        refresh_due = (refresh_after_s is not None
                       and time.monotonic() - self._published_at >= refresh_after_s)
        if (self._value is None or refresh_due) and self._future is None and time.monotonic() >= self._retry_at:
            if refresh_due and self._value is not None:
                self._reader = reader_factory()
            self._running = self._wanted
            self._future = self._executor.submit(self._reader)
        return self._value

    def invalidate(self):
        self._wanted = object()
        self._running = None
        self._value = None

    def close(self):
        self._closed = True
        self._value = None
        self._executor.shutdown(wait=False, cancel_futures=True)
