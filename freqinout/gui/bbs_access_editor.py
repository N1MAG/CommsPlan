"""Reviewed, draft-only access policy editor shared by BBS and inbound Guard."""
from __future__ import annotations

from pathlib import Path
import weakref

from shiboken6 import isValid

from PySide6.QtCore import QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from freqinout.core.bbs_access_policy import (
    effective_callsigns, group_refresh_preview, load_access_families, normalize_access_policy, refresh_access_policy,
)
from freqinout.core.config_paths import get_config_dir


# A running loader outlives its editor safely. Queued results target typed
# slots on the GUI-affine loader, which checks a weak editor reference before
# publishing. Direct queued Python-object delivery to a deleted QWidget can
# crash the PySide QtQml error-handler path; do not reconnect that receiver.
_ROSTER_JOBS: set[QThread] = set()


class _RosterLoader(QThread):
    loaded = Signal(int, str, object)
    failed = Signal(int, str)

    def __init__(self, generation: int, action: str, path: Path, editor):
        super().__init__()
        self.generation, self.action, self.path = generation, action, path
        self._editor = weakref.ref(editor)
        self.loaded.connect(self.dispatch_loaded)
        self.failed.connect(self.dispatch_failed)

    @Slot(int, str, object)
    def dispatch_loaded(self, generation, action, families):
        editor = self._editor()
        if editor is not None and isValid(editor):
            editor._roster_loaded(generation, action, families)

    @Slot(int, str)
    def dispatch_failed(self, generation, error):
        editor = self._editor()
        if editor is not None and isValid(editor):
            editor._roster_failed(generation, error)

    def run(self):
        try:
            families = load_access_families(self.path)
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))
            return
        self.loaded.emit(self.generation, self.action, families)

    @Slot()
    def release(self):
        _ROSTER_JOBS.discard(self)
        self.deleteLater()


class BbsAccessEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, operator_db_path=None):
        super().__init__(parent)
        self._operator_db_path = Path(operator_db_path) if operator_db_path else get_config_dir() / "config" / "freqinout_nets.db"
        self._policy = normalize_access_policy(None)
        self._generation = 0
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Manually allowed callsigns"))
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("Callsign; add more with commas")
        self.manual_edit.setAccessibleName("Manually allowed callsigns")
        layout.addWidget(self.manual_edit)
        self.source_summary = QLabel()
        self.source_summary.setWordWrap(True)
        layout.addWidget(self.source_summary)
        self.derived_summary = QLineEdit()
        self.derived_summary.setReadOnly(True)
        self.derived_summary.setAccessibleName("Reviewed group callsigns")
        layout.addWidget(self.derived_summary)
        self.choose_button = QPushButton("Choose groups…")
        self.refresh_button = QPushButton("Refresh from HF Operators…")
        layout.addWidget(self.choose_button)
        layout.addWidget(self.refresh_button)
        self.status_label = QLabel("Group membership changes apply only after review.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.manual_edit.textChanged.connect(self._manual_changed)
        self.choose_button.clicked.connect(lambda: self._load_roster("choose"))
        self.refresh_button.clicked.connect(lambda: self._load_roster("refresh"))
        self._render()

    def set_policy(self, value, legacy_callsigns=()):
        self._generation += 1
        self._policy = normalize_access_policy(value, legacy_callsigns=legacy_callsigns)
        self.manual_edit.blockSignals(True)
        self.manual_edit.setText(", ".join(self._policy["manual_callsigns"]))
        self.manual_edit.blockSignals(False)
        self._busy = False
        self.choose_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self._render()

    def policy(self):
        return normalize_access_policy(self._policy)

    def hideEvent(self, event):
        # A delayed roster read must never open a review over another workspace.
        self._generation += 1
        self._busy = False
        self.choose_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        super().hideEvent(event)

    def _manual_changed(self):
        policy = dict(self._policy)
        policy["manual_callsigns"] = self.manual_edit.text()
        self._policy = normalize_access_policy(policy)
        self.changed.emit()

    def _render(self):
        sources = self._policy["group_sources"]
        self.source_summary.setText("Selected groups: " + (
            "; ".join(f"{row['family']} / {', '.join(row['groups'])}" for row in sources) or "none"
        ))
        resolved = self._policy["resolved_group_callsigns"]
        self.derived_summary.setText(f"Reviewed group callsigns ({len(resolved)}): " + (", ".join(resolved) or "none"))

    def _load_roster(self, action):
        if self._busy:
            return
        self._busy = True
        self._generation += 1
        self.choose_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status_label.setText("Loading HF Operators…")
        job = _RosterLoader(self._generation, action, self._operator_db_path, self)
        _ROSTER_JOBS.add(job)
        job.finished.connect(job.release)
        job.start()

    @Slot(int, str)
    def _roster_failed(self, generation, error):
        if generation != self._generation:
            return
        self._busy = False
        self.choose_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.status_label.setText("Could not load HF Operators; saved access remains unchanged. " + error)

    @Slot(int, str, object)
    def _roster_loaded(self, generation, action, families):
        if generation != self._generation:
            return
        self._busy = False
        self.choose_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        if not families and action == "choose":
            self.status_label.setText("No HF Operator groups are available. Add group assignments in HF Operators, or refresh saved sources to review removals.")
            return
        candidate = self.policy()
        if action == "choose":
            sources = self._choose_sources(families)
            if sources is None:
                self.status_label.setText("Group selection cancelled; access remains unchanged.")
                return
            candidate["group_sources"] = sources
        candidate = refresh_access_policy(candidate, families)
        self._review_missing_families = sorted({row["family"] for row in candidate["group_sources"] if row["family"] not in families})
        if self._review_policy(candidate):
            self._policy = candidate
            self._render()
            self.changed.emit()
            self.status_label.setText("Reviewed access draft updated. Save to apply.")
        else:
            self.status_label.setText("Review cancelled; access remains unchanged.")

    def _choose_sources(self, families):
        dialog = QDialog(self)
        dialog.setWindowTitle("Choose trusted HF Operator groups")
        layout = QVBoxLayout(dialog)
        hint = QLabel("Select a whole family or individual subgroups. Only trusted operators are included.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Group", "Trusted callsigns"])
        selected = {(row["family"], group) for row in self._policy["group_sources"] for group in row["groups"]}
        for parent, family in sorted(families.items()):
            counts = dict(family.trusted_callsigns_by_group)
            root = QTreeWidgetItem(tree, [parent + " (family)", str(len(family.trusted_callsigns))])
            root.setData(0, Qt.UserRole, (parent, parent))
            root.setCheckState(0, Qt.Checked if (parent, parent) in selected else Qt.Unchecked)
            for group in family.members:
                if group == parent:
                    continue
                item = QTreeWidgetItem(root, [group, str(len(counts.get(group, ())))])
                item.setData(0, Qt.UserRole, (parent, group))
                item.setCheckState(0, Qt.Checked if (parent, group) in selected else Qt.Unchecked)
            root.setExpanded(True)
        layout.addWidget(tree)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        sources = []
        for index in range(tree.topLevelItemCount()):
            root = tree.topLevelItem(index)
            items = [root] + [root.child(i) for i in range(root.childCount())]
            groups = [item.data(0, Qt.UserRole)[1] for item in items if item.checkState(0) == Qt.Checked]
            if groups:
                sources.append({"family": root.data(0, Qt.UserRole)[0], "groups": groups})
        return sources

    def _review_policy(self, candidate):
        preview = group_refresh_preview(self._policy, candidate)
        dialog = QDialog(self)
        dialog.setWindowTitle("Review access changes")
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n\n".join([
            "Unavailable selected families: " + (", ".join(getattr(self, "_review_missing_families", ())) or "none") + ". Their derived callsigns will be removed.",
            "Added: " + (", ".join(preview["added"]) or "none"),
            "Removed: " + (", ".join(preview["removed"]) or "none"),
            "Unchanged: " + (", ".join(preview["unchanged"]) or "none"),
            "Manually allowed (retained): " + (", ".join(candidate["manual_callsigns"]) or "none"),
            "Resulting allowed callsigns: " + (", ".join(effective_callsigns(candidate)) or "none"),
        ]))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.Accepted
