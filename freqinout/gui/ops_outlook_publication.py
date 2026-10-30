"""Qt publication owner for one serial outlook reader and one latest intent."""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from freqinout.core.ops_outlook_model import OutlookWorker
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


class OpsOutlookPublication(QObject):
    completed = Signal(object, object)

    def __init__(self, tab, config_dir: Path):
        super().__init__(tab)
        self.tab = tab
        self.worker = OutlookWorker(config_dir)
        self.reader = self.worker.reader
        self.executor = DaemonSerialExecutor(thread_name_prefix='fio-ops-outlook')
        self.pending = False
        self.latest = None
        self.publication = None
        self.card_publication = None
        self.state = 'checking'
        self.error = ''
        self.generation = 0
        self.key = None
        self.observed_revision = None
        self.failures = 0
        self.probe_failures = 0
        self.raw_failures = 0
        self.next_raw_retry = float('inf')
        self.raw_usable = False
        self.next_retry = float('inf')
        self.next_probe = 0.
        self.closed = False
        self.clock = time.monotonic
        self.utcnow = lambda: dt.datetime.now(dt.timezone.utc)
        self.completed.connect(self._completed)
        tab.destroyed.connect(self.close)

    def _context(self):
        context = self.tab._outlook_context()
        context['now'] = self.utcnow()
        context['key'] = tuple(context[k] for k in (
            'target', 'target_error', 'config_root', 'show_local', 'timezone', 'group', 'search', 'card', 'propagation')) + (int(context['now'].timestamp()//60),)
        return context

    def demanded(self):
        return (not self.closed and self.tab._active and not getattr(self.tab, '_closing', False)
                and getattr(self.tab, '_startup_shell_ready', True)
                and (self.tab._dashboard_card_enabled('schedule') or self.tab._dashboard_card_enabled('propagation')))

    def _status(self, state, error=''):
        changed = (self.state, self.error) != (state, str(error))
        self.state, self.error = state, str(error)
        if changed and not self.closed:
            self.tab._outlook_status_changed()

    def invalidate(self):
        self.generation += 1
        self.latest = None
        self.key = None
        self.failures = self.probe_failures = 0
        self.raw_failures = 0
        self.next_raw_retry = float('inf')
        self.raw_usable = False
        self.next_retry = float('inf')
        self.next_probe = 0.
        self._status('stale' if self.publication else 'checking')
        if self.demanded():
            self.request()

    def deactivate(self):
        self.generation += 1
        self.latest = None
        self.key = None
        self.raw_usable = False
        self._status('stale' if self.publication else 'checking')

    def retry(self):
        self.failures = self.probe_failures = 0
        self.raw_failures = 0
        self.next_raw_retry = 0.
        self.next_retry = 0.
        self.next_probe = 0.
        self.request(force=True)

    def current(self, entry=None):
        pub = self.publication
        if (not self.demanded() or self.state != 'ready' or pub is None or
                pub.context_key != self._context()['key'] or self.utcnow() >= pub.valid_until):
            return False
        return entry is None or entry.get('_outlook_generation') == pub.action_token

    def raw_current(self):
        pub = self.publication
        return bool(self.demanded() and self.raw_usable and pub is not None and
                    pub.context_key == self._context()['key'] and
                    pub.action_token[0] == self.generation and
                    self.utcnow() < pub.evaluated_at.replace(second=0, microsecond=0) + dt.timedelta(minutes=1))

    def tick(self):
        if not self.demanded():
            return
        context = self._context()
        if self.key != context['key']:
            self.request()
            return
        if self.publication and self.utcnow() >= self.publication.valid_until and self.state == 'ready':
            self._status('stale')
        if self.clock() >= min(self.next_probe, self.next_retry, self.next_raw_retry):
            self.request()

    def request(self, *, force=False):
        if not self.demanded():
            return
        context = self._context()
        changed = self.key != context['key']
        if changed:
            self.generation += 1
            self.key = context['key']
            self.raw_usable = False
            # A filter or minute change is not evidence a failed data source
            # recovered. Ordinary consumers never bypass the failed-read backoff.
            self._status('stale' if self.publication else 'checking', self.error)
        context['generation'] = self.generation
        if self.pending:
            if changed or force:
                self.latest = (context, bool(force))
            return
        now = self.clock()
        if not force and self.probe_failures and now < self.next_probe:
            return
        if not force and not changed and now < min(self.next_probe, self.next_retry, self.next_raw_retry):
            return
        self._start(context, force)

    def _start(self, context, force):
        self.pending = True
        self.latest = None
        now = self.clock()
        raw_allowed = bool(force or not self.raw_failures or now >= self.next_raw_retry)
        allowed = bool(force or raw_allowed and (not self.failures or now >= self.next_retry))
        if allowed and not self.current():
            self._status('stale' if self.publication else 'checking')
        observed = self.observed_revision
        worker = self.worker

        def read():
            try:
                revision = worker.reader.revision()
            except Exception as exc:
                return (None, None, exc, 'probe')
            if not allowed and revision == observed:
                if context['propagation'] and raw_allowed:
                    try:
                        raw = worker.load(dict(context, card=False), force=False)
                        return (revision, raw, None, 'raw')
                    except Exception as exc:
                        return (revision, None, exc, 'raw')
                return (revision, None, None, 'probe-only')
            try:
                return (revision, worker.load(context, force=force), None, 'full')
            except Exception as exc:
                raw = None
                if context['card'] and context['propagation']:
                    try:
                        raw = worker.load(dict(context, card=False), force=False)
                    except Exception:
                        pass
                return (revision, raw, exc, 'card' if raw is not None else 'full')

        future = self.executor.submit(read)
        def done(future):
            try:
                value = future.result()
                self.completed.emit(context, value)
            except RuntimeError:
                pass  # receiver has been destroyed; owner cleanup is queued
        future.add_done_callback(done)

    @staticmethod
    def _delay(count):
        return (2., 4., 8.)[count-1] if count <= 3 else 30.

    def render_failed(self, error):
        self.publication = getattr(self.tab, '_outlook_last_render_publication', None)
        self.raw_usable = False
        self.failures += 1
        self.next_retry = self.clock() + self._delay(self.failures)
        self._status('unavailable', f'Schedule display could not be updated: {error}')

    def _completed(self, context, result):
        self.pending = False
        if self.closed:
            return
        if (not self.demanded() or context['generation'] != self.generation or
                context['key'] != self._context()['key']):
            latest, self.latest = self.latest, None
            if self.demanded():
                if latest:
                    self._start(*latest)
                else:
                    self.request()
            return
        revision, pub, error, kind = result
        now = self.clock()
        if kind == 'probe':
            self.raw_usable = False
            self.probe_failures += 1
            self.next_probe = now + self._delay(self.probe_failures)
            # No ordinary tick may skip a failed probe's deadline.
            self.next_retry = max(self.next_probe, self.next_retry) if self.failures else float('inf')
            self._status('unavailable', error)
        else:
            self.probe_failures = 0
            # Renew before the five-second evidence deadline. A healthy read
            # must not disable/re-enable every visible action merely to poll.
            # Slow or failed reads still expire through tick/current normally.
            self.next_probe = now + 4.
            changed = revision != self.observed_revision
            self.observed_revision = revision
            if changed:
                self.failures = 0
                self.raw_failures = 0
            if kind in {'full', 'raw', 'card'} and error is not None:
                self.failures += 1
                self.next_retry = now + self._delay(self.failures)
                if kind != 'card':
                    self.raw_usable = False
                    self.raw_failures += 1
                    self.next_raw_retry = now + self._delay(self.raw_failures)
                self._status('unavailable', error)
            elif kind == 'full' and pub is not None:
                if self.utcnow() >= pub.valid_until:
                    self.failures += 1
                    self.next_retry = now + self._delay(self.failures)
                    self._status('unavailable', 'Schedule evidence expired while loading; retry')
                    pub = None
                else:
                    self.tab.settings._data.update(pub.inputs.catalog.settings)
                    if self._context()['key'] != context['key']:
                        # A committed display preference discovered by the
                        # worker needs a projection in that exact display basis.
                        self.invalidate()
                        return
                    self.raw_failures = 0
                    self.raw_usable = True
                    self.next_raw_retry = float('inf')
                    # The UI clock ticks once per second, and the worker has
                    # already consumed part of this publication's lifetime.
                    # Schedule from its evidence deadline, with enough slack
                    # for the next tick, rather than completion + 4 seconds.
                    remaining = (pub.valid_until - self.utcnow()).total_seconds()
                    self.next_probe = min(self.next_probe, now + max(0., remaining - 1.5))
                    self.publication = pub
                    self._status('ready')
                    self.tab._apply_outlook_publication(pub)
                    if self.state == 'ready':
                        if context['card']:
                            self.card_publication = pub
                        self.failures = 0
                        self.next_retry = float('inf')
            if pub is not None and (kind == 'raw' or error is not None):
                self.tab.settings._data.update(pub.inputs.catalog.settings)
                if self._context()['key'] != context['key']:
                    self.invalidate()
                    return
                self.publication = pub
                self.raw_usable = True
                self.raw_failures = 0
                self.next_raw_retry = float('inf')
                self.tab._outlook_raw_published()
            # A successful unchanged probe leaves any failed stream unavailable.
        latest, self.latest = self.latest, None
        if latest and self.demanded():
            self._start(*latest)

    def close(self, *_args):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        self.latest = None
        self.executor.submit(self.worker.close)
        self.executor.shutdown(wait=False)
