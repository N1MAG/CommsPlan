"""Explicit station admission and native sync actions for BBS Radio Service."""
from __future__ import annotations

import json
import weakref
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
    QToolButton,
)
from freqinout.core.bbs_access_policy import effective_callsigns
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.varac_bbs_sync import (
    apply_bbs_sync_plan, build_bbs_sync_plan, get_bbs_from_varac, import_bbs_from_varac,
    load_station_access, save_station_access,
)
from freqinout.gui.bbs_access_editor import BbsAccessEditor

_SYNC_JOBS = set()


class _SyncJob(QThread):
    result = Signal(int, str, object)
    error = Signal(int, str)

    def __init__(self, generation, action, db_path, radio_id, value=None, *, panel):
        super().__init__()
        self.generation, self.action = generation, action
        self.db_path, self.radio_id, self.value = Path(db_path), radio_id, value
        self._panel = weakref.ref(panel)
        self.result.connect(self.dispatch_result)
        self.error.connect(self.dispatch_error)

    @Slot(int, str, object)
    def dispatch_result(self, generation, action, value):
        panel = self._panel()
        if panel is not None and isValid(panel):
            panel._result(generation, action, value)

    @Slot(int, str)
    def dispatch_error(self, generation, text):
        panel = self._panel()
        if panel is not None and isValid(panel):
            panel._error(generation, text)

    @Slot()
    def release(self):
        _SYNC_JOBS.discard(self)
        self.deleteLater()

    def run(self):
        try:
            store = MultiRadioStore(self.db_path)
            if self.action == "load":
                result = load_station_access(store)
            elif self.action == "save":
                save_station_access(store, **self.value)
                result = self.value
            elif self.action == "get":
                result = get_bbs_from_varac(store, self.radio_id)
            elif self.action == "import":
                result = import_bbs_from_varac(store, self.value)
            elif self.action == "review":
                result = build_bbs_sync_plan(store, self.radio_id, cluster=bool(self.value))
            elif self.action == "apply":
                result = apply_bbs_sync_plan(store, self.value, backup_root=self.db_path.parent / "backups")
            else:
                raise ValueError("Unknown BBS synchronization action")
            self.result.emit(self.generation, self.action, result)
        except Exception as exc:
            self.error.emit(self.generation, str(exc))


class BbsVaracSyncPanel(QWidget):
    saved = Signal()

    def __init__(self, parent=None, *, db_path, radio_id, radio_dirty=lambda: False):
        super().__init__(parent)
        self.db_path = Path(db_path)
        self._radio_id, self._radio_dirty = radio_id, radio_dirty
        self._generation, self._busy, self._dirty = 0, False, False
        self._loaded = False
        self._mutation_active = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Who may connect to VarAC BBS?")
        layout.addWidget(title)
        self.admission_summary = QLabel("Saved station admission is not loaded.")
        self.admission_summary.setWordWrap(True)
        layout.addWidget(self.admission_summary)
        self.access_toggle = QToolButton()
        self.access_toggle.setText("Edit station connection access…")
        self.access_toggle.setCheckable(True)
        layout.addWidget(self.access_toggle)
        self.access_form = QWidget()
        access_layout = QVBoxLayout(self.access_form)
        access_layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("This admission list applies to all BBS locations served by an instance. "
                      "A restricted list can prevent a visitor from reaching a location even when its location policy allows them. "
                      "Location access and CommsPlan Inbound Guard remain separate.")
        note.setWordWrap(True)
        access_layout.addWidget(note)
        self.limit_check = QCheckBox("Limit native BBS connections to this list")
        access_layout.addWidget(self.limit_check)
        self.access_editor = BbsAccessEditor(operator_db_path=self.db_path.with_name("freqinout_nets.db"))
        access_layout.addWidget(self.access_editor)
        self.save_button = QPushButton("Save CommsPlan access")
        access_layout.addWidget(self.save_button)
        self.access_form.setVisible(False)
        self.access_toggle.toggled.connect(self.access_form.setVisible)
        layout.addWidget(self.access_form)
        guidance = QLabel("Save records CommsPlan intent only. Get reads VarAC into CommsPlan after review. "
                          "Update writes saved CommsPlan BBS settings to the reviewed INIs. Close VarAC before updating; CommsPlan will not stop or restart it.")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("Update selected radio only", False)
        self.scope_combo.addItem("Update enabled members of this radio's cluster", True)
        self.scope_combo.setAccessibleName("Native BBS update scope")
        layout.addWidget(self.scope_combo)
        self.get_button = QPushButton("Get from VarAC…")
        self.update_button = QPushButton("Update VarAC…")
        layout.addWidget(self.get_button)
        layout.addWidget(self.update_button)
        self.status = QLabel("Loading saved station admission…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.limit_check.toggled.connect(self._changed)
        self.access_editor.changed.connect(self._changed)
        self.save_button.clicked.connect(self._save)
        self.get_button.clicked.connect(lambda: self._start("get"))
        self.update_button.clicked.connect(self._review_update)
        self._start("load")

    def _changed(self, *_args):
        self._dirty = True
        self.status.setText("Unsaved CommsPlan access changes. Save before updating VarAC.")

    def radio_changed(self):
        if self._mutation_active:
            return
        self._generation += 1
        self._busy = False
        self._controls(True)

    def hideEvent(self, event):
        # A completed read cannot open a review on top of another workspace.
        if not self._mutation_active:
            self._generation += 1
            self._busy = False
            self._controls(True)
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded and not self._busy:
            self._start("load")

    def _controls(self, enabled):
        for widget in (self.access_editor, self.limit_check, self.scope_combo,
                       self.save_button, self.get_button, self.update_button):
            widget.setEnabled(enabled)
        self.save_button.setEnabled(enabled and self._loaded)
        self.update_button.setEnabled(enabled and self._loaded)

    def _start(self, action, value=None):
        if self._busy:
            return
        self._busy = True
        self._mutation_active = action in {"save", "import", "apply"}
        self._generation += 1
        self._controls(False)
        self.status.setText("Updating reviewed VarAC INIs… Software is not restarted." if action == "apply"
                            else "Working… VarAC operation is unchanged.")
        job = _SyncJob(self._generation, action, self.db_path, int(self._radio_id() or 0), value, panel=self)
        _SYNC_JOBS.add(job)
        job.finished.connect(job.release)
        job.start()

    def _save(self):
        if not self._loaded:
            self.status.setText("Saved station access is not loaded. Get from VarAC or reopen this page before saving.")
            return
        self._start("save", dict(policy=self.access_editor.policy(), limit_access=self.limit_check.isChecked()))

    def _review_update(self):
        if not self._loaded:
            self.status.setText("Load saved CommsPlan station admission before updating VarAC.")
            return
        if self._dirty or self._radio_dirty():
            self.status.setText("Save CommsPlan access and the selected radio's service changes before Update VarAC.")
            return
        self._start("review", self.scope_combo.currentData())

    def _confirm(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText(text)
        layout.addWidget(detail)
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).setText("Import to CommsPlan" if "Get" in title else "Update VarAC")
        buttons.button(QDialogButtonBox.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.Accepted

    def _result(self, generation, action, value):
        if generation != self._generation:
            return
        self._busy = False
        self._mutation_active = False
        self._controls(True)
        if action in {"load", "save"}:
            if action == "load":
                self.access_editor.set_policy(value["policy"])
                self.limit_check.blockSignals(True)
                self.limit_check.setChecked(value["limit_access"])
                self.limit_check.blockSignals(False)
                self._loaded = True
                self._controls(True)
            self._dirty = False
            count = len(effective_callsigns(self.access_editor.policy()))
            self.admission_summary.setText(
                f"Saved CommsPlan admission: limited to {count} callsigns." if self.limit_check.isChecked()
                else "Saved CommsPlan admission: no native callsign restriction. Location policies still apply."
            )
            self.status.setText("Saved in CommsPlan. VarAC INIs are unchanged." if action == "save" else "Saved station admission loaded. VarAC INIs are unchanged.")
            if action == "save":
                self.saved.emit()
        elif action == "get":
            text = (f"Source: {value['radio_name']}\n{value['ini_path']}\n\n"
                    f"BBS: {'on' if value['enable_bbs'] else 'off'}\nFolder: {value['bbs_directory']}\n"
                    f"Announce: {value['announce']}\nLimit access: {value['limit_access']}\n"
                    f"Allowed callsigns: {','.join(value['allowed_callsigns']) or 'none'}\n\n"
                    "Import replaces this radio's CommsPlan BBS service settings and station admission list. "
                    "Group provenance cannot be recovered from VarAC; these callsigns become manual entries. "
                    "Unsaved drafts are replaced. Location and Inbound Guard policies remain unchanged. No INI is written.")
            if self._confirm("Get from VarAC — review import", text):
                self._start("import", value)
            else:
                self.status.setText("Import cancelled. CommsPlan and VarAC remain unchanged.")
        elif action == "import":
            self._dirty = False
            self._loaded = False
            self.saved.emit()
            self._start("load")
        elif action == "review":
            lines = ["Update only the five native BBS adapter settings below. CAT, PTT, audio, cluster identity and databases are unchanged."]
            for target in value.targets:
                before, after = json.loads(target.before_json), json.loads(target.after_json)
                lines.extend([f"\n{target.radio_name}\n{target.path}"])
                for key in ("enable_bbs", "bbs_directory", "limit_access", "allowed_callsigns", "announce"):
                    old, new = before.get(key), after[key]
                    if key == "allowed_callsigns":
                        old, new = ",".join(old), ",".join(new)
                    labels = {"enable_bbs": "BBS enabled", "bbs_directory": "Live BBS folder", "limit_access": "Limit native connections", "allowed_callsigns": "Allowed callsigns", "announce": "Announce BBS"}
                    lines.append(f"{labels[key]}: {old} → {new}")
            lines.append("\nVarAC must be closed. Exact backups and readback protect this update. No software is restarted.")
            if self._confirm("Update VarAC — review target INIs", "\n".join(lines)):
                self._start("apply", value)
            else:
                self.status.setText("Update cancelled. VarAC INIs remain unchanged.")
        elif action == "apply":
            self.status.setText(f"Verified {value['updated']} updated INI(s). " +
                                (f"Backup: {value['backup_dir']}. Start VarAC when ready." if value['backup_dir'] else "Saved native BBS values already match."))

    def _error(self, generation, text):
        if generation == self._generation:
            self._busy = False
            self._mutation_active = False
            self._controls(True)
            self.status.setText(f"Action could not complete: {text}")
