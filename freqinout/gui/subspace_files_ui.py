"""Bounded, explicit review actions for Subspace file occurrences."""
from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QObject, Signal, Qt, QEvent
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget


class _Signals(QObject):
    finished = Signal(object, object)


_REVIEW_LANES = threading.BoundedSemaphore(2)


class _Job:
    def __init__(self, action, occurrence, args, expected_root, expected_generation, cancel):
        self.action, self.occurrence, self.args = action, occurrence, args
        self.expected_root, self.expected_generation, self.cancel = expected_root, expected_generation, cancel
        self.signals = _Signals()

    def run(self):
        try:
            from freqinout.core.subspace_files import default_file_store
            store = default_file_store()
            self.store = store
            root = str(store.root)
            if self.cancel.is_set() or (self.expected_root is not None and self.expected_root != root):
                raise RuntimeError("The file source changed; reopen the item.")
            generation = store.generation
            if self.expected_generation is not None and generation != self.expected_generation:
                raise RuntimeError("The file source changed; reopen the item.")
            result = getattr(store, self.action)(self.occurrence, generation=generation, cancel_event=self.cancel, **self.args)
            if self.cancel.is_set():
                self.signals.finished.emit(None, "Cancelled")
                return
            if self.action in {"mark_reviewed", "retry"}:
                warning = ''
                try:
                    store.project_pending(store.root.parent.parent / "config" / "freqinout_nets.db", generation=generation, cancel_event=self.cancel)
                except Exception as exc:
                    if self.cancel.is_set() or generation != store.generation:
                        raise
                    warning = 'Saved; Messages update pending: ' + str(exc)
                result = store.detail(self.occurrence, generation=generation, cancel_event=self.cancel)
                if warning:
                    result['sync_warning'] = warning
            if self.cancel.is_set():
                self.signals.finished.emit(None, "Cancelled")
            else:
                self.signals.finished.emit((root, generation, result), None)
        except Exception as exc:
            self.signals.finished.emit(None, str(exc))
        finally:
            _REVIEW_LANES.release()


class SubspaceFileReview(QWidget):
    """Reader controls; jobs return to the GUI and stale selections are fenced."""
    manage_source_requested = Signal(str)
    occurrence_changed = Signal(str)
    def __init__(self, parent, viewer, info_label):
        super().__init__(parent)
        self.host, self.viewer, self.info_label = parent, viewer, info_label
        self._occurrence = None
        self._drafts = {}
        self._jobs = set()
        self._serial = 0
        self._detail = {}
        self._root = None
        self._generation = None
        self._cancel = threading.Event()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.info_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("Review notes"))
        self.notes = QPlainTextEdit()
        self._size_notes()
        self.notes.textChanged.connect(self._remember_notes)
        layout.addWidget(self.notes)
        actions = QHBoxLayout()
        self.review = QPushButton("Mark reviewed")
        self.retry = QPushButton("Retry")
        self.export = QPushButton("Export…")
        self.open = QPushButton("Open text file")
        self.manage = QPushButton("Manage source")
        for button in (self.review, self.retry, self.export, self.open, self.manage):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.review.clicked.connect(lambda: self._start("mark_reviewed", notes=self.notes.toPlainText()))
        self.retry.clicked.connect(self._retry)
        self.export.clicked.connect(self._export)
        self.open.clicked.connect(lambda: self._start("open_text_path"))
        self.manage.clicked.connect(lambda: self.manage_source_requested.emit(str(self._detail.get("source_id") or "")))
        self.hide()

    def _size_notes(self):
        self.notes.setMaximumHeight(self.notes.fontMetrics().lineSpacing() * 4 + 2 * self.notes.frameWidth() + self.notes.contentsMargins().top() + self.notes.contentsMargins().bottom())

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange):
            if hasattr(self, "notes"):
                self._size_notes()

    def shutdown(self):
        """Cancel queued work and fence late callbacks during teardown."""
        self.clear_selection()

    def _remember_notes(self):
        if self._occurrence:
            self._drafts[self._occurrence] = self.notes.toPlainText()

    def clear_selection(self):
        self._cancel.set()
        self._cancel = threading.Event()
        self._remember_notes()
        self._serial += 1
        self._occurrence = None
        self._root = None
        self._generation = None
        self.hide()

    def show_occurrence(self, occurrence):
        self.clear_selection()
        self._occurrence = str(occurrence)
        self._detail = {}
        self.notes.blockSignals(True)
        self.notes.setPlainText(self._drafts.get(self._occurrence, ""))
        self.notes.blockSignals(False)
        from freqinout.gui.help_registry import resolve_help_host
        host = resolve_help_host(self.host)
        self.manage.setVisible(callable(getattr(host, "open_settings_section", None)))
        self.show()
        self.status.setText("Loading file details…")
        self.viewer.setPlainText("File found · transfer completion unverified\n\nLoading preview…")
        self._set_actions(False)
        self._start("detail")

    def _set_actions(self, enabled):
        for button in (self.review, self.retry, self.export, self.open, self.manage):
            button.setEnabled(enabled)

    def _start(self, action, **args):
        if not self._occurrence:
            return
        occurrence, serial = self._occurrence, self._serial
        if not _REVIEW_LANES.acquire(blocking=False):
            self.status.setText("File review is busy. Please retry shortly.")
            if not self._detail:
                self.retry.setText("Retry loading")
                self.retry.setEnabled(True)
            return
        self._set_actions(False)
        job = _Job(action, occurrence, args, self._root, self._generation, self._cancel)
        self._jobs.add(job)
        def finished(result, error):
            self._jobs.discard(job)
            if serial != self._serial or occurrence != self._occurrence:
                return
            self._set_actions(True)
            if self._detail:
                self._display(self._detail)
            else:
                self._set_actions(False)
            if error:
                self.status.setText(f"{action.replace('_', ' ').capitalize()} failed: {error}")
                return
            root, generation, result = result
            if job.cancel.is_set() or getattr(job,"store",None) is None or job.store.generation != generation:
                self.status.setText("The file source changed; reopen the item. Notes retained.")
                return
            self._root = root
            self._generation = generation
            if action == "export":
                self.status.setText("Exported file.")
                return
            if action == "open_text_path":
                # Only a positively identified, strictly decoded text preview is
                # eligible. Other files remain available through Export.
                self.host._set_open_external_path(Path(result), label="Open text file")
                self.host._open_external_file()
                self.host._set_open_external_path(None)
                return
            if action in {"mark_reviewed", "retry"}:
                self.occurrence_changed.emit(occurrence)
            self._display(dict(result or {}))
        job.signals.finished.connect(finished)
        try:
            threading.Thread(target=job.run, name="subspace-file-review", daemon=True).start()
        except RuntimeError as exc:
            self._jobs.discard(job)
            _REVIEW_LANES.release()
            self.retry.setText("Retry loading" if not self._detail else "Retry")
            self.retry.setEnabled(True)
            self.status.setText(f"File review could not start: {exc}")

    def _display(self, detail):
        self._detail = detail
        occurrence = self._occurrence
        if occurrence not in self._drafts:
            self.notes.blockSignals(True)
            self.notes.setPlainText(str(detail.get("notes") or ""))
            self.notes.blockSignals(False)
        name = str(detail.get("filename") or "File")
        state = str(detail.get("state") or "")
        state_label = {"waiting":"Waiting for stable file","attention":"Needs attention","capturing":"Preserving copy","available":"Ready for review"}.get(state,state)
        self.info_label.setText(f"{name} · Sender unknown")
        self.status.setText(f"File found · transfer completion unverified · {state_label} · {'Reviewed' if detail.get('reviewed') else 'Not reviewed'}")
        from datetime import datetime, timezone
        found = detail.get('found_ts')
        try:
            found = datetime.fromtimestamp(float(found),timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        except (TypeError,ValueError,OverflowError,OSError):
            found = 'Unknown'
        preview = str(detail.get('preview') or '')
        preview_state = detail.get('preview_state')
        if preview_state != 'text':
            preview = {'too_large':'Preview exceeds the 64 KiB limit. Export the preserved file to inspect it.',
                       'binary':'No plain-text preview. Export the preserved file to inspect it.'}.get(preview_state,
                       'A protected preview is not available yet. Retry after the source settles.')
        if detail.get('sync_warning'):
            self.status.setText(self.status.text() + ' · ' + str(detail['sync_warning']))
        lines = ['File contents', preview or '(Empty file)', '',
                 'Source details', f"Found: {found}", f"Source: {detail.get('source_label', '')}",
                 'Receiving radio and sender unverified', f"State: {state_label}",
                 str(detail.get('detail') or ''), f"Size: {detail.get('size', '')} bytes",
                 f"Source directory: {detail.get('source_path', '')}",
                 f"SHA-256: {detail.get('sha256', '')}", f"Protected copy: {detail.get('archive_path', '')}"]
        self.viewer.setAcceptRichText(False)
        self.viewer.setPlainText("\n".join(lines))
        available = bool(detail.get("archive_path"))
        self.export.setEnabled(available)
        self.open.setEnabled(available and Path(name).suffix.lower() in {'.txt', '.csv'} and str(detail.get('preview_state', '')).lower() in {'text', 'available', 'ok'})
        self.retry.setEnabled(state.lower() not in {'available'})
        empty = not available and detail.get("size") == 0
        self.retry.setText("Capture empty file" if empty else "Retry")

    def _retry(self):
        if not self._detail:
            self._start("detail")
            return
        empty = not self._detail.get("archive_path") and self._detail.get("size") == 0
        self._start("retry", allow_empty=bool(empty))

    def _export(self):
        name = str(self._detail.get('filename') or 'subspace-file')
        # The suggested name is display-only; drop any path components.
        name = name.replace('\\', '/').split('/')[-1]
        destination, _ = QFileDialog.getSaveFileName(self, "Export file", name)
        if destination:
            self._start("export", destination=destination)
