"""One explicit export job; no database, compression or file work on the GUI thread."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from freqinout.core.diagnostic_export import export_diagnostics


class DiagnosticExportController(QObject):
    finished = Signal(object)
    _done = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='diagnostic-export')
        self._cancel = threading.Event()
        self._closed = False
        self._pending = False
        self._done.connect(self._completed)
        if parent is not None:
            parent.destroyed.connect(self.close)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    def submit(self, output_path, **kwargs):
        if self._closed or self._pending:
            return False
        self._pending = True
        captured = deepcopy(kwargs)
        future = self._executor.submit(export_diagnostics, output_path, cancel_event=self._cancel, **captured)
        def done(result):
            try:
                value = {'result': result.result(), 'error': '', 'settings_db_path': str(captured['settings_db_path'])}
            except Exception as exc:
                value = {'result': None, 'error': str(exc), 'settings_db_path': str(captured['settings_db_path'])}
            if not self._closed:
                try:
                    self._done.emit(value)
                except RuntimeError:
                    pass
        future.add_done_callback(done)
        return True

    def _completed(self, value):
        if self._closed:
            return
        self._pending = False
        self.finished.emit(value)

    def close(self, *_args):
        self._closed = True
        self._cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
