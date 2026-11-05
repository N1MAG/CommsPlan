"""Serial worker boundary for the Plan Builder assignment panel."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from freqinout.core.plan_assignment_service import PlanAssignmentService
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor


class PlanAssignmentController(QObject):
    finished = Signal(object)
    result_ready = Signal(object)

    def __init__(self, config_dir, parent=None):
        super().__init__(parent)
        self.config_dir = Path(config_dir)
        self._executor = DaemonSerialExecutor(thread_name_prefix='fio-plan-assignment')
        self._service = None
        self._generation = 0
        self._active = True
        self._closed = False
        self._sequence = 0
        self._latest_read = 0
        self._write_pending = False
        self._deferred_context = None
        self.finished.connect(self._accept)

    def submit(self, operation, payload):
        if self._closed or not self._active:
            return False
        write = operation in {'commit', 'save_draft', 'commit_refresh', 'discard_draft'}
        if self._write_pending:
            if operation == 'context':
                self._deferred_context = deepcopy(payload)
            return False
        self._sequence += 1
        sequence = self._sequence
        self._latest_read = sequence
        if write:
            self._write_pending = True
        generation = self._generation
        clean = deepcopy(payload)

        def work():
            if self._service is None:
                self._service = PlanAssignmentService(self.config_dir)
            if operation == 'context':
                return self._service.context(**clean)
            if operation == 'relink_context':
                return self._service.relink_context(clean['plan_id'])
            if operation == 'preview_relink':
                return self._service.preview_relink(clean)
            if operation == 'review':
                return self._service.review(clean)
            if operation == 'schedule_page':
                return self._service.schedule_page(clean)
            if operation == 'commit':
                return self._service.commit(clean)
            if operation == 'save_draft':
                return self._service.save_draft(clean['payload'], expected=clean.get('expected'),
                                                expected_draft_version=clean.get('expected_draft_version'))
            if operation == 'preview_refresh':
                return self._service.preview_refresh(clean['plan_ids'])
            if operation == 'commit_refresh':
                return self._service.commit_refresh(clean)
            if operation == 'discard_draft':
                return self._service.discard_draft(clean['plan_id'], expected_version=clean['expected_version'])
            raise ValueError('Unknown assignment operation')

        future = self._executor.submit(work)

        def done(completed):
            if completed.cancelled() or self._closed:
                return
            try:
                result = {'operation': operation, 'generation': generation, 'sequence': sequence, 'write': write, 'payload': clean,
                          'result': completed.result(), 'error': ''}
            except Exception as exc:
                result = {'operation': operation, 'generation': generation, 'sequence': sequence, 'write': write, 'payload': clean,
                          'result': None, 'error': str(exc)}
            try:
                self.finished.emit(result)
            except RuntimeError:
                pass  # A closed/deleted tab has no UI publication authority.

        future.add_done_callback(done)
        return True

    def _accept(self, result):
        write = result.get('write', False)
        if write:
            self._write_pending = False
        before = self._sequence
        valid = not self._closed and self._active and result['generation'] == self._generation
        if valid and (write or result['sequence'] == self._latest_read):
            self.result_ready.emit(result)
        if write:
            deferred, self._deferred_context = self._deferred_context, None
            if deferred is not None and self._active and not self._closed and before == self._sequence:
                self.submit('context', deferred)

    def set_active(self, active):
        self._active = bool(active)
        if not active:
            self._generation += 1

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._executor.shutdown(wait=False, cancel_futures=True)
