"""Bounded worker lanes for FCC reference consumers; workers never touch widgets."""
from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from freqinout.core.call_finder import default_call_finder_store


class CallFinderController(QObject):
    result_ready = Signal(object)
    error_ready = Signal(str)
    refresh_progress = Signal(str)
    refresh_finished = Signal(str)
    _completed = Signal(int, object, str)
    refresh_source_started = Signal(str)
    _import_done = Signal(str, bool)
    _import_progress = Signal(str)

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.store = store or default_call_finder_store()
        self._active = False
        self._disposed = False
        self._generation = 0
        self._running = False
        self._pending = None
        self._refreshing = False
        self._update_queue = []
        self._updating_all = False
        self._cancel = threading.Event()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._dispatch)
        self._completed.connect(self._receive)
        self._import_done.connect(self._receive_import)
        self._import_progress.connect(self._receive_progress)
        # This captures only the event; destruction can safely fence a live worker.
        self.destroyed.connect(lambda *_args, event=self._cancel: event.set())

    @property
    def refreshing(self):
        return self._refreshing

    def set_active(self, active: bool):
        self._active = bool(active) and not self._disposed
        self._generation += 1
        self._pending = None
        self._timer.stop()

    def dispose(self):
        self.set_active(False)
        self._disposed = True
        self._cancel.set()

    def invalidate(self):
        """Fence deferred results when the consumer selects another cached row."""
        self._generation += 1
        self._pending = None
        self._timer.stop()

    def _schedule(self, operation, debounce=False):
        self._generation += 1
        if not self._active or self._disposed:
            return
        self._pending = (self._generation, operation)
        self._timer.stop()
        if debounce:
            self._timer.start()
        else:
            self._dispatch()

    def search(self, prefix, service="", state="", country=""):
        self._schedule(lambda: self.store.search(prefix, service, state, **({"country": country} if country else {})), debounce=True)

    def related(self, record):
        self._schedule(lambda: self.store.related(record))

    def load_status(self):
        self._schedule(self.store.status)

    def _dispatch(self):
        if self._running or self._pending is None or not self._active or self._disposed:
            return
        generation, operation = self._pending
        self._pending = None
        self._running = True
        signal = self._completed
        def run():
            result, error = None, ""
            try:
                result = operation()
            except Exception as exc:
                error = str(exc)
            try:
                signal.emit(generation, result, error)
            except RuntimeError:
                pass  # QObject was destroyed; never resurrect a dead consumer.
        threading.Thread(target=run, daemon=True, name="fcc-reference-query").start()

    def _receive(self, generation, result, error):
        self._running = False
        if self._active and not self._disposed and generation == self._generation:
            if error:
                self.error_ready.emit(error)
            else:
                self.result_ready.emit(result)
        self._dispatch()

    def refresh(self, service: str, source: Path, *, accept_count_drop=False):
        self._start_import(service, Path(source), accept_count_drop=accept_count_drop)

    def download_refresh(self, service: str, *, accept_count_drop=False):
        self._start_import(service, None, accept_count_drop=accept_count_drop)

    def download_refresh_all(self, services):
        """Run each source through the existing import lane, stopping on failure."""
        if self._refreshing or self._disposed:
            return
        services = list(services)
        if not services:
            return
        self._updating_all = True
        self._update_queue = services[1:]
        self._start_import(services[0], None)

    def _start_import(self, service, source, *, accept_count_drop=False):
        if self._refreshing or self._disposed:
            return
        self._refreshing = True
        self._cancel.clear()
        self.refresh_source_started.emit(service)
        store, cancel = self.store, self._cancel
        done, progress = self._import_done, self._import_progress
        from freqinout.gui.reference_source_events import reference_source_events
        published = reference_source_events().changed
        def report(message):
            try:
                progress.emit(message)
            except RuntimeError:
                cancel.set()
        def run():
            succeeded = False
            try:
                if service == "ZIP grids":
                    meta = (store.download_refresh_grid(cancel, report) if source is None
                            else store.refresh_grid(source, cancel, report))
                    message = f"ZIP grids refreshed: {meta.rows:,} Census ZCTAs · {meta.vintage}."
                elif service == "Canada postal grids":
                    meta = (store.download_refresh_canada_grid(cancel, report) if source is None
                            else store.refresh_canada_grid(source, cancel, report))
                    message = f"Canada postal grids refreshed: {meta.rows:,} postal codes · {meta.vintage}."
                else:
                    options = {"accept_count_drop": True} if service == "Canada" and accept_count_drop else {}
                    meta = (store.download_refresh(service, cancel, report, **options) if source is None
                            else store.refresh(service, source, cancel, report, **options))
                    qualifier = "assigned callsigns" if service == "Canada" else "active licenses"
                    message = f"{service} refreshed: {meta.active_rows:,} {qualifier}."
                    if meta.skipped_rows:
                        message += f" {meta.skipped_rows:,} malformed/incomplete source rows skipped."
                succeeded = True
            except Exception as exc:
                message = f"{service} refresh did not complete: {exc}"
            try:
                if succeeded:
                    published.emit(service)
                done.emit(message, succeeded)
            except RuntimeError:
                pass
        threading.Thread(target=run, daemon=True, name="fcc-reference-import").start()

    def cancel_refresh(self):
        self._cancel.set()

    def _receive_progress(self, text):
        if self._active and not self._disposed:
            self.refresh_progress.emit(text)

    def _receive_import(self, text, succeeded):
        self._refreshing = False
        if self._updating_all:
            if succeeded and self._update_queue and not self._cancel.is_set() and not self._disposed:
                self._receive_progress(text)
                self._start_import(self._update_queue.pop(0), None)
                return
            if self._cancel.is_set():
                text = "Update all cancelled; completed datasets were retained."
            elif not succeeded:
                text += " Update all stopped."
            else:
                text = "Update all complete; all datasets refreshed."
            self._update_queue.clear()
            self._updating_all = False
        if self._active and not self._disposed:
            self.refresh_finished.emit(text)
