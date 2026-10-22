"""Bounded low-priority FCC enrichment shared by NCS and future decoder feeds.

request()/identity() are memory-only. Disk work never runs on receive/entry paths.
"""
from __future__ import annotations

from collections import OrderedDict
import os
import re
import threading
import time

from PySide6.QtCore import QObject, QEvent, QTimer, Signal
from PySide6.QtWidgets import QApplication

from freqinout.core.call_finder import default_call_finder_store, EXACT_BATCH_LIMIT


_BROKERS = {}
_CALL = re.compile(r"[A-Z0-9]{3,12}\Z", re.ASCII)


class FccIdentityBroker(QObject):
    identities_ready = Signal(object)
    _completed = Signal(object, object)
    QUEUE_LIMIT = 512

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.store = store or default_call_finder_store()
        self._pending = OrderedDict()
        self._inflight = set()
        self._running = False
        self._closed = False
        self._retry_after = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._dispatch)
        self._completed.connect(self._receive)

    def identity(self, callsign):
        rows = self.store.cached_exact(str(callsign).strip().upper())
        return rows[0] if rows and len(rows) == 1 else None

    def request(self, callsign, *, interactive=False):
        call = str(callsign).strip().upper()
        if (self._closed or not _CALL.fullmatch(call) or call in self._inflight
                or self.store.cached_exact(call) is not None or time.monotonic() < self._retry_after):
            return
        if call in self._pending:
            self._pending[call] = self._pending[call] or interactive
        else:
            if len(self._pending) >= self.QUEUE_LIMIT:
                passive = next((key for key, urgent in self._pending.items() if not urgent), None)
                if passive is None:
                    return
                del self._pending[passive]
            self._pending[call] = interactive
        if not self._running and not self._timer.isActive():
            self._timer.start()

    def _dispatch(self):
        if self._closed or self._running or not self._pending:
            return
        # Interactive check-in drafts go ahead of passive repeated observations.
        calls = tuple(sorted(self._pending, key=lambda call: not self._pending[call])[:EXACT_BATCH_LIMIT])
        for call in calls:
            del self._pending[call]
        self._inflight.update(calls)
        self._running = True
        store, done = self.store, self._completed
        def run():
            try:
                result = store.exact_many(calls)
            except Exception:
                result = None
            try:
                done.emit(calls, result)
            except RuntimeError:
                pass
        threading.Thread(target=run, daemon=True, name="fcc-identity-enrichment").start()

    def _receive(self, calls, result):
        self._inflight.difference_update(calls)
        self._running = False
        if self._closed:
            return
        if getattr(result, "kind", "") == "exact_retry":
            for call in calls:
                self.request(call)
            return
        if result is None or (result.errors and not result.sources):
            self._retry_after = time.monotonic() + 30
            self._pending.clear()
        if result is not None and result.rows and getattr(result, "complete", True):
            self.identities_ready.emit(result.rows)
        if self._pending:
            self._timer.start()

    def shutdown(self):
        self._closed = True
        self._pending.clear()
        self._timer.stop()


def identity_broker():
    key = os.environ.get("FREQINOUT_CONFIG_DIR", "")
    broker = _BROKERS.get(key)
    if broker is None:
        app = QApplication.instance()
        broker = FccIdentityBroker(app)
        _BROKERS[key] = broker
        if app is not None:
            app.aboutToQuit.connect(broker.shutdown)
    return broker


class NcsIdentityPrefetch(QObject):
    """Warm exact identities while typing; Add consumes only ready memory data."""
    def __init__(self, owner, field, parse, *, known=None, broker=None):
        super().__init__(owner)
        self._owner, self._field, self._parse = owner, field, parse
        self._known = known or (lambda: {})
        self.broker = broker or identity_broker()
        field.textChanged.connect(self._prefetch)
        owner.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self._owner and event.type() == QEvent.Show:
            self._prefetch()
        return super().eventFilter(watched, event)

    def _prefetch(self, *_args):
        if self._owner.isVisible() and self._field.isEnabled():
            call, name, state = self._parse(self._field.text())[:3]
            local = self._known().get(call, {})
            local_name = local.get("name") or local.get("first_name") or local.get("last_name")
            if (name or local_name) and (state or local.get("state")):
                return
            self.broker.request(call, interactive=True)

    def fill(self, callsign, name, state, known=None):
        known = known or {}
        local_name = str(known.get("name") or "").strip() or " ".join(
            str(known.get(key) or "").strip() for key in ("first_name", "last_name")
        ).strip()
        name = str(name or "").strip() or local_name
        state = str(state or "").strip() or str(known.get("state") or "").strip()
        if not name or not state:
            record = self.broker.identity(callsign)
            if record is not None:
                fcc_name = (" ".join(part for part in (record.first_name, record.last_name) if part)
                            if record.kind == "Individual" else "") or record.name
                name = name or fcc_name
                state = state or record.state
        return name, state.upper()
