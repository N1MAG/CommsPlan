"""Shared nonmodal callsign picker. It never transmits or commits a check-in."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QMessageBox


def reviewed_draft(record, callsign, name, state, known=None):
    """Preserve manual/managed values; refuse to attach them to a different call."""
    known = known or {}
    if (name or state) and callsign != record.callsign:
        raise ValueError("The draft has details for another callsign. Clear those details before selecting a different license.")
    local_name = str(known.get("name") or "").strip() or " ".join(
        str(known.get(key) or "").strip() for key in ("first_name", "last_name")
    ).strip()
    return record.callsign, name or local_name or record.name, state or known.get("state") or record.state


class _ReferenceDialog(QDialog):
    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Hide:
            self.reject()
        return super().eventFilter(watched, event)

    def done(self, result):
        self.finder.controller.dispose()
        if self.parent() is not None:
            self.parent().removeEventFilter(self)
            self.parent()._fcc_reference_dialog = None
        super().done(result)


def open_call_finder(owner, initial="", apply=None, *, store=None):
    existing = getattr(owner, "_fcc_reference_dialog", None)
    if existing is not None:
        existing.raise_()
        existing.activateWindow()
        return existing
    from freqinout.gui.call_finder_tab import CallFinderTab
    dialog = _ReferenceDialog(owner)
    dialog.setAttribute(Qt.WA_DeleteOnClose)
    dialog.setWindowTitle("Call Finder — offline reference")
    layout = QVBoxLayout(dialog)
    note = QLabel("Review the licensee name and mailing region before use. GMRS family members may use the same callsign.")
    note.setWordWrap(True)
    layout.addWidget(note)
    finder = CallFinderTab(dialog, store=store, picker=apply is not None)
    dialog.finder = finder
    if apply is None:
        finder.add_btn.hide()
    layout.addWidget(finder)
    close = QPushButton("Close", dialog)
    close.clicked.connect(dialog.reject)
    layout.addWidget(close)
    if apply is not None:
        def selected(record):
            try:
                apply(record)
            except ValueError as exc:
                QMessageBox.information(dialog, "Review draft", str(exc))
                return
            dialog.accept()
        finder.selected_license.connect(selected)
    owner._fcc_reference_dialog = dialog
    owner.installEventFilter(dialog)
    dialog.resize(dialog.fontMetrics().horizontalAdvance("M") * 85,
                  dialog.fontMetrics().lineSpacing() * 35)
    dialog.show()
    finder.search_edit.setText(initial)
    finder.search_edit.setFocus()
    return dialog


def open_ncs_draft_picker(owner, field, parse, format_entry, known_rows, *, store=None):
    """Fence against draft and NCS session changes while the picker is open."""
    original = field.text()
    session = (getattr(owner, "_ncs_session_id", ""), getattr(owner, "_ncs_session_radio_id", None),
               getattr(owner, "_net_in_progress", None))
    parts = parse(original)
    initial = str(parts[0] or "")
    def apply(record):
        current_session = (getattr(owner, "_ncs_session_id", ""), getattr(owner, "_ncs_session_radio_id", None),
                           getattr(owner, "_net_in_progress", None))
        if field.text() != original or current_session != session:
            raise ValueError("The draft or NCS session changed. Close this lookup and open it again for the current draft.")
        cs, name, state = parts[:3]
        known = known_rows().get(record.callsign, {})
        cs, name, state = reviewed_draft(record, cs, name, state, known)
        text = format_entry(cs, name, state)
        if len(parts) > 3 and parts[3]:
            text += " " + str(parts[3])
        field.setText(text)
        field.setFocus()
    return open_call_finder(owner, initial, apply, store=store)
