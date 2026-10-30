"""Compatibility shell for the short operator application editor.

This module deliberately keeps the old assistant's host-facing signals while
moving the visible editing surface to :class:`ApplicationSetupEditor`.  It
does not persist drafts, launch applications, or call native preparation code.
Received-file directory checks run in a bounded background worker.
"""

from __future__ import annotations

from copy import deepcopy
from os.path import normcase, normpath
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
from uuid import uuid4

from PySide6.QtCore import Qt, Signal, QEvent, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from freqinout.gui.operator_application_editor import ApplicationSetupEditor, application_task_view_scope
from freqinout.gui.received_files_config import ReceivedFilesCounter
from freqinout.gui.theme import active_app_theme, button_style, label_style, control_height_for_font


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _same_id(left: object, right: object) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def _bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "managed"}
    return bool(value)


def _recovered_edited_fields(base: Mapping[str, Any], values: Mapping[str, Any]) -> set[str]:
    marked = {key for key in values.get('__edited_fields__', ()) if isinstance(key, str)}
    if '__edited_fields__' in values:
        return marked
    for key, value in values.items():
        if key.startswith('_') or key.startswith('operator_'):
            continue
        if key in base and value != base[key]:
            marked.add(key)
        elif key not in base and value not in (None, '', False, (), [], {}):
            marked.add(key)
    return marked


def _radio_id(row: Mapping[str, Any]) -> Any:
    return _first(row, "radio_id", "device_profile_id", "assigned_radio_id", "radio_profile_id", "id")


def _instance_id(row: Mapping[str, Any]) -> Any:
    return _first(
        row,
        "application_instance_id",
        "instance_id",
        "replacement_instance_id",
        "id",
        "assignment_id",
    )


def _family_label(family_key: str) -> str:
    return {
        "js8call": "JS8Call",
        "fast_light": "Fast Light",
        "varac": "VarAC",
    }.get(str(family_key or "application").lower(), str(family_key or "Application").replace("_", " ").title())


class _DraftView(SimpleNamespace):
    """Small attribute/payload view used by old host adapters."""

    def __init__(self, values: Mapping[str, Any], family_key: str) -> None:
        self._values = deepcopy(dict(values))
        namespace_values = dict(self._values)
        namespace_values.pop("family_key", None)
        super().__init__(family_key=family_key, **namespace_values)

    def payload(self) -> dict[str, Any]:
        return deepcopy(self._values)

    def __getattr__(self, name: str) -> Any:
        return self._values.get(name)


class OperatorInstanceAssistant(QWidget):
    """Short-form editor with the legacy ``SoftwareInstanceAssistant`` seams."""

    completed = Signal(object)
    cancelled = Signal()
    create_radio_requested = Signal()
    varac_native_prepare_requested = Signal(object)
    varac_native_apply_requested = Signal(object)
    discover_requested = Signal(str)
    recover_requested = Signal(object)
    discard_unfinished_requested = Signal(object)
    review_subspace_files_requested = Signal()

    # Kept for hosts that connected these optional legacy signals.  They are
    # never emitted by this cache-only editor.
    remove_family_requested = Signal(str)
    validation_requested = Signal(object)

    def __init__(
        self,
        family_key: str = "",
        *,
        radios: Iterable[Mapping[str, Any]] = (),
        existing_instances: Iterable[Mapping[str, Any]] = (),
        varac_clusters: Iterable[Mapping[str, Any]] = (),
        selected_radio_id: object = None,
        radio_role: str = "tx_rx",
        initial_draft: Mapping[str, Any] | object | None = None,
        editing_existing: bool = False,
        replacement_requested: bool = False,
        managed_root: str = "",
        task_scope: str | None = None,
        visible_components: tuple[str, ...] | list[str] | None = None,
        visible_fields: tuple[str, ...] | list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.family_key = _text(family_key).strip().lower()
        self._family_key = self.family_key
        self.radio_role = _text(radio_role).strip().lower() or "tx_rx"
        self.radios = tuple(deepcopy(dict(row)) for row in radios if isinstance(row, Mapping))
        self.existing_instances = tuple(
            deepcopy(dict(row)) for row in existing_instances if isinstance(row, Mapping)
        )
        self.varac_clusters = tuple(
            deepcopy(dict(row)) for row in varac_clusters if isinstance(row, Mapping)
        )
        self.selected_radio_id = selected_radio_id
        self._selected_radio_id = selected_radio_id
        self.managed_root = _text(managed_root)
        self.visible_components = tuple(visible_components or ())
        self.visible_fields = tuple(visible_fields or ())
        self._application_discovery_enabled = True
        self.task_scope = str(task_scope or "").strip().lower()
        self._editing_existing = bool(editing_existing)
        self._replacement_requested = bool(replacement_requested or editing_existing)
        self._existing_instances = self.existing_instances
        self._initial_raw = self._mapping_from_draft(initial_draft)
        self._review_edited_fields = {key for key in self._initial_raw.get('__edited_fields__', ()) if isinstance(key, str)}
        self._raw_fields = deepcopy(self._initial_raw)
        self._discovery_candidates: tuple[dict[str, Any], ...] = ()
        self._discovery_results: tuple[dict[str, Any], ...] = ()
        self._selected_discovery_paths = {}
        self._varac_native_presentation: Any = None
        self._busy = False
        self._status_error = False
        # Suggestions are progressive disclosure.  An empty card otherwise
        # consumes the vertical space needed by the actual form.
        self._discovery_requested = False
        self.operator_session_id = uuid4().hex
        self._unfinished_envelope: dict[str, Any] | None = None
        self._conflict_review_context: dict[str, Any] | None = None
        self._unfinished_token: Any = None
        self._unfinished_revision: Any = None
        self._recovered_from: Any = None
        self.operator_draft_revision: Any = self._initial_raw.get('operator_draft_revision')
        self._replacement_instance_id = self._find_original_instance_id()
        self._initial_instance_id = _first(
            self._initial_raw,
            "instance_id",
            "application_instance_id",
            "replacement_instance_id",
            "id",
        )
        self._initial_system_key = _first(
            self._initial_raw,
            "application_system_key",
            "system_key",
            "imported_system_key",
        )
        self._expected_identity_generation = _first(
            self._initial_raw,
            "expected_identity_generation",
            "identity_generation",
            "generation",
            "revision",
            "identity_revision",
        )
        self._radio_name = self._resolve_radio_name()
        self._nested_unsaved = self.selected_radio_id in (None, "") and bool(
            self._initial_raw.get("owner_draft_key")
        )
        self._allow_close = False

        self.setObjectName("operatorInstanceAssistant")
        self.setAccessibleName("Application setup assistant")
        self._build_ui()
        seeded = self._seed_form_values(self._initial_raw)
        self.editor = ApplicationSetupEditor(
            self.family_key,
            self._radio_name,
            seeded,
            visible_components=(self.visible_components if visible_components is not None else None),
            visible_fields=(self.visible_fields if visible_fields is not None else None),
            parent=self,
        )
        self.application_editor = self.editor
        # Suggestions, validation messages and recovery controls share the
        # existing field-scroll body. Variable-height context above the editor
        # can consume a short Settings viewport and clip its fixed Save footer.
        self.editor.add_form_context(self._context_widget)
        self.editor.full_family_review_requested.connect(self._full_family_review_requested)
        if self.task_scope:
            self.set_task_scope(self.task_scope)
        else:
            self.set_view_scope(visible_components=visible_components, visible_fields=visible_fields)
        # The assistant is embedded in the Settings workspace, whose editor
        # stack may be shorter than this form's natural hint.  Let the local
        # field scroll own that overflow so the editor footer remains inside
        # the assistant viewport instead of being clipped by the host.
        self.editor.setMinimumHeight(0)
        self.editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.save_button = self.editor.save_button
        self.cancel_button = self.editor.cancel_button
        self.find_button = self.editor.find_button
        self.operation_status_label = QLabel()
        self.operation_status_label.setObjectName("operatorInstanceOperationStatus")
        self.operation_status_label.setAccessibleName("Application setup operation status")
        self.operation_status_label.setWordWrap(True)
        self.status_label = self.operation_status_label
        self._context_layout.insertWidget(0, self.operation_status_label)
        self._editor_host_layout.addWidget(self.editor, 1)
        heading = self.editor.findChild(QLabel, 'operatorApplicationHeading')
        if heading is not None:
            heading.hide()
        self._set_received_files_values(self._initial_raw)
        self.operator_base_values = deepcopy(self.values())

        self.editor.value_changed.connect(self._editor_value_changed)
        self.editor.save_requested.connect(self._complete)
        self.editor.cancel_requested.connect(self._cancel)
        self.editor.browse_requested.connect(self._browse_field)
        self.editor.find_requested.connect(self._request_discovery)
        if hasattr(self.editor, "settings_identity_combo"):
            self.editor.settings_identity_combo.currentIndexChanged.connect(self._clear_default_rig_name)
        self.use_discovery_button.clicked.connect(self._use_discovery)
        self.discovery_list.itemSelectionChanged.connect(self._discovery_selection_changed)
        self.create_radio_button.clicked.connect(self.create_radio_requested.emit)
        if self.family_key == "js8call":
            self.review_subspace_files_button.clicked.connect(
                self.review_subspace_files_requested.emit
            )
            self.received_enabled.toggled.connect(self._received_files_edited)
            self.received_path.textChanged.connect(self._received_files_edited)
            self.received_choice.currentIndexChanged.connect(self._received_files_edited)
            self.received_browse.clicked.connect(self._browse_received_files)
            self.received_check_again.clicked.connect(self._received_files_changed)
            self._received_files_changed()
        self.set_operation_status("Saved")
        self.editor.set_discovery_state('ready')
        self.use_discovery_button.setVisible(not self.visible_components)

    # ----- public compatibility surface ----------------------------------

    def set_configuration_attention(self, entry: Mapping[str, Any] | None) -> None:
        self.editor.set_configuration_attention(entry)

    def set_operation_status(self, text: str, *, error: bool = False) -> None:
        self._status_error = bool(error)
        value = _text(text)
        if value.startswith(('Searching', 'Waiting for the current search')):
            self.editor.set_discovery_state('searching')
        elif error or 'could not' in value.lower() or 'failed' in value.lower():
            self.editor.set_discovery_state('failed')
        self.operation_status_label.setText(value)
        self.operation_status_label.setToolTip(value)
        # The editor footer already communicates the normal saved state. Keep
        # this row available for errors, progress, and review messages only.
        self.operation_status_label.setVisible(bool(value) and value.strip().lower() != "saved")
        if error:
            theme = active_app_theme()
            self.operation_status_label.setStyleSheet(label_style("danger", theme, weight=600))
        else:
            self.operation_status_label.setStyleSheet(label_style("muted", active_app_theme()))
        self.editor.set_status(value)
        # This host owns the operation explanation. Keep it once above the
        # form rather than repeating it inside the embedded editor as well.
        self.editor.status_label.hide()

    def apply_theme(self, theme=None):
        palette = dict(theme or active_app_theme())
        heading = self.findChild(QLabel, 'operatorInstanceAssistantHeading')
        if heading is not None:
            heading.setStyleSheet(label_style('text', palette, weight=700))
        self.editor.apply_theme(palette)
        self._size_discovery_list()
        self.operation_status_label.setStyleSheet(label_style('danger' if self._status_error else 'muted',palette))
        for button in (
            self.use_discovery_button,
            self.create_radio_button,
            self.recover_button,
            self.discard_unfinished_button,
            self.conflict_review_button,
            self.review_subspace_files_button,
            self.received_browse,
            self.received_check_again,
        ):
            button.setStyleSheet(button_style('secondary',palette))

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.editor.set_busy(self._busy)
        self.editor.status_label.hide()
        self.use_discovery_button.setEnabled(not self._busy and bool(self.discovery_list.currentItem()))
        self.create_radio_button.setEnabled(not self._busy)
        self.recover_button.setEnabled(not self._busy)
        self.discard_unfinished_button.setEnabled(not self._busy)
        self.conflict_review_button.setEnabled(not self._busy)
        self.received_files_group.setEnabled(not self._busy)

    def _size_discovery_list(self) -> None:
        count = self.discovery_list.count()
        height = control_height_for_font(self.discovery_list, vertical_padding=10, floor=28)
        target = height * max(1, min(count, 4)) + self.discovery_list.frameWidth() * 2
        self.discovery_list.setMinimumHeight(target)
        self.discovery_list.setMaximumHeight(target)

    def set_discovery_results(self, results: Iterable[Mapping[str, Any]]) -> None:
        """Publish candidates without changing the draft until one is selected."""

        self._discovery_candidates = tuple(
            deepcopy(dict(row)) for row in results if isinstance(row, Mapping)
        )
        rows: list[dict[str, Any]] = []
        component_paths_seen = set()
        for raw in self._discovery_candidates if self._application_discovery_enabled else ():
            if isinstance(raw, Mapping):
                row = deepcopy(dict(raw))
                if self.family_key == 'fast_light' and self.visible_components:
                    paths = self._discovery_application_paths(row)
                    if not any(paths.get(f'{component}_application_path') for component in self.visible_components):
                        continue
                    # This picker copies only component executable paths. Saved
                    # profiles and detection may describe the same installation.
                    # Keep profile/source identity rows in the full-family view.
                    identity = tuple((component, normcase(normpath(_text(paths[f'{component}_application_path']))))
                                     for component in self.visible_components if paths.get(f'{component}_application_path'))
                    if identity in component_paths_seen:
                        continue
                    component_paths_seen.add(identity)
                if self._row_has_values(row):
                    rows.append(row)
        self._discovery_results = tuple(rows)
        self.discovery_group.setVisible(self._application_discovery_enabled and (bool(rows) or self._discovery_requested))
        self.discovery_list.clear()
        for row in rows:
            name = _text(_first(row, "name", "instance_name", "label")) or "Unnamed configuration"
            if self.family_key == 'fast_light' and self.visible_components:
                component = self.visible_components[0]
                detail = _text(self._discovery_application_paths(row).get(f'{component}_application_path'))
                name = f"{component.upper()} application"
            else:
                detail = _text(_first(row, "application_path", "install_path", "path"))
            item = QListWidgetItem(f"{name} · {detail}" if detail else name)
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setToolTip("Select to fill this application's path." if self.visible_components else
                            "Select to fill the application path. Use selected values also copies other suggested settings.")
            self.discovery_list.addItem(item)
        visible = bool(rows)
        self._size_discovery_list()
        self.use_discovery_button.setVisible(visible and not self.visible_components)
        selected = self._selected_discovery_paths
        still_selected = selected and all(self.values().get(key) == value for key, value in selected.items())
        self.editor.set_discovery_state('selected' if visible and still_selected else 'detected' if visible else 'empty')
        self.discovery_list.setVisible(visible)
        self._discovery_selection_changed()
        if visible:
            self.discovery_hint.setText(
                "Select the matching application to fill its path. Review the form, then Apply to radio setup."
                if self.visible_components else
                "Select a suggestion to fill the path. Use selected values also copies its settings. Review before applying.")
        else:
            self.discovery_hint.setText("No suggestions are available. Enter values directly or use Browse…")

    def set_varac_native_presentation(self, presentation: Any) -> None:
        """Retain host presentation data for compatibility without applying it."""

        self._varac_native_presentation = deepcopy(presentation)

    def varac_native_presentation(self) -> Any:
        return deepcopy(self._varac_native_presentation)

    def request_varac_native_prepare(self) -> None:
        """Compatibility no-op; native preparation belongs to the host."""

        return None

    def request_varac_native_apply(self) -> None:
        """Compatibility no-op; native apply belongs to the host."""

        return None

    def values(self) -> dict[str, Any]:
        values = self.editor.values()
        if self.family_key == "js8call":
            values.update(received_files_enabled=self.received_enabled.isChecked(),
                          received_files_path=self.received_path.text().strip(),
                          received_files_existing=self.received_choice.currentData())
        return values

    def set_editor_values(self, values: Mapping[str, Any], *, preserve_dirty: bool = True) -> bool:
        """Publish a worker snapshot without overwriting an active draft."""
        values = self._seed_form_values(values)
        if not self.editor.set_values(values, preserve_dirty=preserve_dirty):
            return False
        self._review_edited_fields.clear()
        if self.family_key == "js8call":
            self._set_received_files_values(values)
            self._received_files_changed()
        return True

    def set_view_scope(
        self,
        *,
        visible_components: tuple[str, ...] | list[str] | None = None,
        visible_fields: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Limit the visible editor task while preserving the atomic draft."""
        self.visible_components = tuple(visible_components) if visible_components is not None else ()
        self._selected_discovery_paths = {}
        path_fields = {"application_path", *(f"{c}_application_path" for c in ("flrig", "fldigi", "flmsg", "flamp"))}
        self._application_discovery_enabled = (
            (visible_components is None or bool(visible_components))
            and (visible_fields is None or bool(path_fields.intersection(visible_fields)))
        )
        self.editor.find_button.setVisible(self._application_discovery_enabled)
        self.visible_fields = tuple(visible_fields) if visible_fields is not None else ()
        self.editor.set_view_scope(
            visible_components=(self.visible_components if visible_components is not None else None),
            visible_fields=(self.visible_fields if visible_fields is not None else None),
        )

        self.set_discovery_results(self._discovery_candidates)

    def set_task_scope(self, task_key: str | None) -> None:
        """Show the editor surface for one software task without dropping draft data."""
        self.task_scope = str(task_key or "").strip().lower()
        self.set_view_scope(**application_task_view_scope(task_key))

    def review_all_fast_light_components(self) -> None:
        """Expand a component-scoped Fast Light editor without changing its draft."""
        if self.family_key != "fast_light":
            return
        self._full_family_review_requested()
        self.editor.set_view_scope()

    def _full_family_review_requested(self) -> None:
        if self.family_key != "fast_light":
            return
        # Keep the host's scope bookkeeping aligned with the editor. Future
        # cached set_values publications therefore retain the expanded view.
        self.task_scope = ""
        heading = self.findChild(QLabel, "operatorInstanceAssistantHeading")
        if heading is not None:
            heading.setText("Fast Light setup")
        self.set_view_scope()

    def draft(self) -> SimpleNamespace:
        return _DraftView(self._draft_payload_values(), self.family_key)

    def set_saved_incomplete(self, result: Mapping[str, Any] | object | None) -> None:
        """Publish host acknowledgement for an unfinished save without closing."""

        if isinstance(result, Mapping):
            values = dict(result)
            session_id = _first(values, "session_id", "unfinished_token", "draft_token", "token")
            draft_revision = _first(values, "draft_revision", "revision", "session_revision")
            state = values.get("state")
            issues = values.get("issues") or ()
        else:
            session_id = getattr(result, "session_id", None)
            draft_revision = getattr(result, "draft_revision", None)
            state = getattr(result, "state", None)
            issues = getattr(result, "issues", ()) or ()
        self._unfinished_token = session_id
        self._unfinished_revision = draft_revision
        self.operator_draft_revision = draft_revision
        self.editor.set_dirty(False)
        status = "Saved incomplete"
        if _text(state).lower() in {"saved", "complete", "published"}:
            status = "Saved"
        if issues:
            status = f"{status}: {_text(issues[0])}"
        self.set_operation_status(status)

    def set_unfinished(self, envelope: Mapping[str, Any] | None) -> None:
        """Expose explicit Recover/Discard actions for a retained host draft."""

        self._unfinished_envelope = deepcopy(dict(envelope or {})) if envelope else None
        visible = self._unfinished_envelope is not None
        self.recover_button.setVisible(visible)
        self.discard_unfinished_button.setVisible(visible)
        self.recover_button.setEnabled(not self._busy)
        self.discard_unfinished_button.setEnabled(not self._busy)

    def set_conflict_review(self, context: Mapping[str, Any] | None) -> None:
        self._conflict_review_context = deepcopy(dict(context)) if isinstance(context, Mapping) else None
        self.conflict_review_button.setVisible(self._conflict_review_context is not None)
        self.conflict_review_button.setEnabled(not self._busy)

    def _review_saved_changes(self) -> None:
        if self._busy or self._conflict_review_context is None:
            return
        from freqinout.core.configuration_review import application_review
        review = application_review(self.operator_base_values,
                                    {**self.values(), '__edited_fields__': sorted(self._review_edited_fields)},
                                    self._conflict_review_context)
        box = QMessageBox(self)
        box.setWindowTitle('Review saved application changes')
        box.setTextFormat(Qt.PlainText)
        box.setText('Review your entries alongside the current saved settings.')
        summary = review['summary']
        if len(summary) > 360 or len(summary.splitlines()) > 4:
            box.setInformativeText('Review the changed fields in Show Details. Unedited fields use the current saved settings. Nothing is saved until you choose Save.')
            box.setDetailedText(summary)
        else:
            box.setInformativeText(summary)
        apply = box.addButton('Use reviewed entries', QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton('Keep editing', QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is not apply:
            return
        received_before = (self.received_enabled.isChecked(), self.received_path.text(), self.received_choice.currentData())
        self.editor.set_values(self._seed_form_values(review['values']), preserve_dirty=False)
        if self.family_key == 'js8call':
            self._set_received_files_values(review['values'])
            received_after = (self.received_enabled.isChecked(), self.received_path.text(), self.received_choice.currentData())
            if received_after != received_before:
                self._received_generation += 1
                self._received_timer.stop()
                self._received_accessible = False
                self.received_status.setText('Directory not checked. You can save these choices.' if received_after[0] else 'Monitoring off')
        self.operator_base_values = deepcopy(dict(review['base']))
        context = deepcopy(dict(review['context']))
        self._raw_fields.update(context)
        self._expected_identity_generation = context.get('expected_identity_generation')
        self._replacement_instance_id = context.get('expected_current_instance_id')
        self.operator_draft_revision = context.get('operator_draft_revision')
        self.editor.set_dirty(True)
        self.set_conflict_review(None)
        self.set_operation_status('Reviewed entries are ready. Choose Save to apply them.')

    def _draft_payload_values(self) -> dict[str, Any]:
        values = self.values()
        values['__edited_fields__'] = sorted(self._review_edited_fields)
        # The application editor uses a typed spin box, while the legacy
        # assistant draft contract carries JS8 offset text for atomic payload
        # compatibility.  Keep the editor typed without changing the saved
        # draft shape consumed by existing hosts.
        if self.family_key == "js8call" and isinstance(values.get("offset_hz"), int):
            values["offset_hz"] = str(values["offset_hz"])
        values.update(
            {
                "operator_source_instance_id": self._raw_fields.get("operator_source_instance_id"),
                "operator_source_snapshot": deepcopy(self._raw_fields.get("operator_source_snapshot")),
                "operator_configuration_v1": True,
                "family_key": self.family_key,
                "radio_id": self.selected_radio_id,
                "radio_role": self.radio_role,
                "operator_session_id": self.operator_session_id,
                "expected_current_instance_id": self._replacement_instance_id,
                "replacement_instance_id": self._replacement_instance_id,
                "replace_existing": bool(self._replacement_requested),
                "initial_instance_id": self._initial_instance_id,
                "initial_application_system_key": self._initial_system_key,
                "expected_identity_generation": self._expected_identity_generation,
                "unfinished_token": self._unfinished_token,
                "unfinished_revision": self._unfinished_revision,
                "recovered_from": deepcopy(self._recovered_from),
                "operator_recovered_from": self._recovered_from if isinstance(self._recovered_from, str) else None,
                "operator_draft_revision": self.operator_draft_revision,
            }
        )
        return values

    # ----- setup -----------------------------------------------------------

    def _build_ui(self) -> None:
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(8, 8, 8, 8)
        self._root_layout.setSpacing(6)
        heading = QLabel(f"{_family_label(self.family_key)} setup")
        heading.setObjectName("operatorInstanceAssistantHeading")
        heading.setStyleSheet(label_style("text", active_app_theme(), weight=700))
        self._root_layout.addWidget(heading)

        self._context_widget = QWidget(self)
        self._context_layout = QVBoxLayout(self._context_widget)
        self._context_layout.setContentsMargins(0, 0, 0, 0)
        self._context_layout.setSpacing(6)

        self.discovery_group = QGroupBox("Suggestions")
        self.discovery_group.setObjectName("operatorInstanceDiscoveryGroup")
        discovery_layout = QVBoxLayout(self.discovery_group)
        discovery_layout.setContentsMargins(6, 6, 6, 6)
        self.discovery_hint = QLabel("Find installed… provides optional suggestions. The form remains editable.")
        self.discovery_hint.setObjectName("operatorInstanceDiscoveryHint")
        self.discovery_hint.setAccessibleName("Discovery suggestion guidance")
        self.discovery_hint.setWordWrap(True)
        discovery_layout.addWidget(self.discovery_hint)
        self.discovery_list = QListWidget()
        self.discovery_list.setObjectName("operatorInstanceDiscoveryList")
        self.discovery_list.setAccessibleName("Application setup suggestions")
        self.discovery_list.setVisible(False)
        self.discovery_list.setMaximumHeight(120)
        discovery_layout.addWidget(self.discovery_list)
        controls = QHBoxLayout()
        self.use_discovery_button = QPushButton("Use selected values")
        self.use_discovery_button.setObjectName("operatorInstanceUseDiscovery")
        self.use_discovery_button.setAccessibleName("Use selected application values")
        self.use_discovery_button.setEnabled(False)
        controls.addWidget(self.use_discovery_button)
        controls.addStretch(1)
        discovery_layout.addLayout(controls)
        self._context_layout.addWidget(self.discovery_group)
        self.discovery_group.setVisible(False)

        self.received_files_group = QGroupBox("Received files", self._context_widget)
        received_layout = QVBoxLayout(self.received_files_group)
        self.received_enabled = QCheckBox("Monitor received files")
        received_layout.addWidget(self.received_enabled)
        path_row = QHBoxLayout()
        self.received_path = QLineEdit()
        self.received_path.setAccessibleName("Received files directory")
        self.received_path.setPlaceholderText("Choose an accessible directory")
        path_row.addWidget(self.received_path, 1)
        self.received_browse = QPushButton("Browse…")
        self.received_browse.setAccessibleName("Browse for received files directory")
        path_row.addWidget(self.received_browse)
        received_layout.addLayout(path_row)
        self.received_choice = QComboBox()
        self.received_choice.setAccessibleName("Existing files choice")
        self.received_choice.addItem("Review existing files", "review")
        self.received_choice.addItem("Monitor new files", "new")
        received_layout.addWidget(self.received_choice)
        self.received_status = QLabel("Monitoring off")
        self.received_status.setAccessibleName("Received files status")
        self.received_status.setWordWrap(True)
        self.received_status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        received_layout.addWidget(self.received_status)
        hint = QLabel("Files may be shared by instances; sender and receiving radio remain unverified.")
        hint.setWordWrap(True)
        received_layout.addWidget(hint)
        actions = QHBoxLayout()
        self.review_subspace_files_button = QPushButton("Review received files", self.received_files_group)
        self.review_subspace_files_button.setObjectName("operatorInstanceReviewSubspaceFiles")
        self.review_subspace_files_button.setAccessibleName("Review received files")
        actions.addWidget(self.review_subspace_files_button)
        self.received_check_again = QPushButton("Check directory")
        actions.addWidget(self.received_check_again)
        actions.addStretch()
        received_layout.addLayout(actions)
        self._context_layout.addWidget(self.received_files_group)
        self.received_files_group.setVisible(self.family_key == "js8call")
        self._received_generation = 0
        self._received_accessible = False
        self._received_counter = ReceivedFilesCounter(self)
        self._received_counter.finished.connect(self._received_count_finished)
        self._received_timer = QTimer(self)
        self._received_timer.setSingleShot(True)
        self._received_timer.timeout.connect(self._count_received_files)

        # Parent before any visibility changes. A parentless button would
        # briefly show as a native top-level window,
        # disrupting macOS activation/Spaces even after it is reparented.
        self.create_radio_button = QPushButton("Create a radio…", self._context_widget)
        self.create_radio_button.setObjectName("operatorInstanceCreateRadio")
        self.create_radio_button.setAccessibleName("Create a radio")
        self._context_layout.addWidget(self.create_radio_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.create_radio_button.setVisible(
            self.selected_radio_id in (None, "") and not self._nested_unsaved
        )

        recovery_actions = QHBoxLayout()
        self.recover_button = QPushButton("Recover unfinished")
        self.recover_button.setObjectName("operatorInstanceRecoverUnfinished")
        self.recover_button.setAccessibleName("Recover unfinished application draft")
        self.recover_button.setVisible(False)
        self.recover_button.clicked.connect(self._recover_unfinished)
        self.discard_unfinished_button = QPushButton("Discard unfinished")
        self.discard_unfinished_button.setObjectName("operatorInstanceDiscardUnfinished")
        self.discard_unfinished_button.setAccessibleName("Discard unfinished application draft")
        self.discard_unfinished_button.setVisible(False)
        self.discard_unfinished_button.clicked.connect(self._discard_unfinished)
        recovery_actions.addWidget(self.recover_button)
        recovery_actions.addWidget(self.discard_unfinished_button)
        self.conflict_review_button = QPushButton('Review saved changes…')
        self.conflict_review_button.setObjectName('operatorInstanceReviewSavedChanges')
        self.conflict_review_button.setVisible(False)
        self.conflict_review_button.clicked.connect(self._review_saved_changes)
        recovery_actions.addWidget(self.conflict_review_button)
        recovery_actions.addStretch(1)
        self._context_layout.addLayout(recovery_actions)

        self._editor_host = QWidget()
        self._editor_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._editor_host.setMinimumSize(0, 0)
        self._editor_host_layout = QVBoxLayout(self._editor_host)
        self._editor_host_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.addWidget(self._editor_host, 1)

    # ----- seed/identity ---------------------------------------------------

    @staticmethod
    def _mapping_from_draft(draft: Mapping[str, Any] | object | None) -> dict[str, Any]:
        if draft is None:
            return {}
        payload = getattr(draft, "payload", None)
        if callable(payload):
            try:
                value = payload()
                return deepcopy(dict(value)) if isinstance(value, Mapping) else {}
            except Exception:
                return {}
        return deepcopy(dict(draft)) if isinstance(draft, Mapping) else {}

    def _resolve_radio_name(self) -> str:
        for row in self.radios:
            if _same_id(_radio_id(row), self.selected_radio_id):
                return _text(_first(row, "name", "radio_name", "label")) or "Selected radio"
        return _text(
            _first(self._initial_raw, "owner_label", "radio_name", "radio_label")
        ) or ("Unsaved radio draft" if self.selected_radio_id in (None, "") else "Selected radio")

    def _find_original_instance_id(self) -> Any:
        supplied = _first(
            self._initial_raw,
            "expected_current_instance_id",
            "replacement_instance_id",
            "instance_id",
            "application_instance_id",
        )
        if supplied not in (None, ""):
            return supplied
        if self.selected_radio_id in (None, ""):
            return None
        for row in self.existing_instances:
            if not _same_id(_radio_id(row), self.selected_radio_id):
                continue
            row_family = _text(row.get("family_key") or row.get("family")).lower()
            if row_family and self.family_key and row_family != self.family_key:
                continue
            candidate = _instance_id(row)
            if candidate not in (None, ""):
                return candidate
        return None

    def _seed_form_values(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        values = deepcopy(dict(raw))
        if self.family_key == "js8call":
            values["received_files_enabled"] = _bool(raw.get("received_files_enabled", raw.get("js8_received_files_enabled", False)))
            values["received_files_path"] = _text(raw.get("received_files_path", raw.get("js8_received_files_path", ""))).strip()
            choice = raw.get("received_files_existing", raw.get("js8_received_files_existing", "review"))
            values["received_files_existing"] = choice if choice in {"review", "new"} else "review"
        aliases = {
            "application_path": ("install_path", "path", "js8_application_path", "varac_install_path", "flrig_path"),
            "configuration_path": ("ini_path", "profile_path", "js8_profile_path", "flrig_config_path", "varac_ini_path"),
            "settings_root": ("settings_root",),
            "instance_name": ("name", "label"),
            "launch_command": ("launch_cmd", "launcher_path"),
            "working_directory": ("working_dir",),
            "wine_runner": ("wine", "wine_command"),
            "wine_prefix": ("prefix", "wineprefix"),
            "custom_argv": ("argv", "arguments"),
        }
        if self.family_key == "fast_light":
            # Fast Light has one executable field per component.  Keeping the
            # legacy FLRig alias out of the generic field avoids two writable
            # authorities for the same executable.
            aliases["application_path"] = ("install_path", "path")
        for target, source_keys in aliases.items():
            if target not in values or values[target] in (None, ""):
                value = _first(raw, *source_keys)
                if value not in (None, ""):
                    values[target] = value
        if "host" not in values:
            host = _first(raw, "js8_host", "flrig_host", "fldigi_host")
            values["host"] = "127.0.0.1" if host is None else host
        if "port" not in values:
            port_keys = ("js8_tcp_port", "flrig_port") if self.family_key == "fast_light" else ("js8_tcp_port", "flrig_port", "fldigi_port")
            port = _first(raw, *port_keys)
            values["port"] = "" if port is None else port
        if self.family_key == "fast_light" and "secondary_port" not in values:
            secondary = _first(raw, "fldigi_api_port", "fldigi_port", "secondary_port")
            values["secondary_port"] = "" if secondary is None else secondary
        identity_source = values.get("settings_identity_mode")
        if identity_source in (None, ""):
            identity_source = raw.get("settings_mode")
        if identity_source in (None, "") and raw.get("rig_name") not in (None, ""):
            identity_source = "isolated"
        identity = _text(identity_source).lower()
        if identity in {"named", "rig", "isolated"}:
            values["settings_identity_mode"] = "isolated"
        elif identity in {"launcher", "custom", "custom_launcher"}:
            values["settings_identity_mode"] = "custom_launcher"
        elif self.family_key == "fast_light" and identity in {"separate_folder", "separatefolder"}:
            values["settings_identity_mode"] = "separatefolder"
        elif self.family_key in {"js8call", "fast_light"}:
            values["settings_identity_mode"] = "default"
        if self.family_key == "js8call" and values.get("settings_identity_mode") != "isolated":
            # Default and launcher identities have no native rig selector.
            # Clearing this known field prevents a stale name from leaking
            # into the completed launch recipe; unrelated raw keys remain.
            values["rig_name"] = ""
        if "launch_at_startup" not in values and "mode" in raw:
            values["launch_at_startup"] = _text(raw.get("mode")).lower() == "managed"
        if "mode" not in values:
            values["mode"] = "managed" if _bool(values.get("launch_at_startup")) else "manual"
        if self.family_key == "varac" and "existing_ini" not in values:
            values["existing_ini"] = values.get("configuration_path", "")
        if self.family_key == "fast_light":
            component_paths = dict(values.get("component_paths") or {})
            components = dict(values.get("components") or {})
            for component in ("flrig", "fldigi", "flmsg", "flamp"):
                path = _first(raw, f"{component}_application_path", f"{component}_path")
                if path not in (None, ""):
                    values[f"{component}_application_path"] = path
                    component_paths[component] = path
                flag = _first(raw, f"{component}_enabled", f"use_{component}")
                if flag is not None:
                    values[f"{component}_enabled"] = _bool(flag)
                    components[component] = _bool(flag)
                startup_key = f"{component}_launch_at_startup"
                if startup_key in raw:
                    values[startup_key] = _bool(raw[startup_key])
            values["components"] = components
            values["component_paths"] = component_paths
        return values

    # ----- interaction -----------------------------------------------------

    def _set_received_files_values(self, values: Mapping[str, Any]) -> None:
        enabled = values.get("received_files_enabled", values.get("js8_received_files_enabled", False))
        path = values.get("received_files_path", values.get("js8_received_files_path", ""))
        choice = values.get("received_files_existing", values.get("js8_received_files_existing", "review"))
        for widget in (self.received_enabled, self.received_path, self.received_choice):
            widget.blockSignals(True)
        try:
            self.received_enabled.setChecked(_bool(enabled))
            self.received_path.setText(_text(path))
            self.received_choice.setCurrentIndex(max(0, self.received_choice.findData(choice)))
        finally:
            for widget in (self.received_enabled, self.received_path, self.received_choice):
                widget.blockSignals(False)

    def _browse_received_files(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose received files directory", self.received_path.text())
        if path:
            self.received_path.setText(path)

    def _received_files_edited(self, *_args) -> None:
        self.editor.update_external_fields({
            "received_files_enabled": self.received_enabled.isChecked(),
            "received_files_path": self.received_path.text().strip(),
            "received_files_existing": self.received_choice.currentData(),
        })
        self._received_files_changed()

    def _received_files_changed(self, *_args) -> None:
        self._received_generation += 1
        self._received_accessible = False
        self._received_timer.stop()
        if not self.received_enabled.isChecked():
            self.received_status.setText("Monitoring off")
        elif not self.received_path.text().strip():
            self.received_status.setText("Choose a directory to enable monitoring.")
        else:
            self.received_status.setText("Checking directory… You can save these choices while the check runs.")
            self._received_timer.start(300)

    def _count_received_files(self) -> None:
        self._received_counter.request(self._received_generation, self.received_path.text().strip())

    def _received_count_finished(self, generation, path, count, more, error) -> None:
        if generation != self._received_generation or path != self.received_path.text().strip():
            return
        self._received_accessible = not bool(error)
        self.received_status.setToolTip(str(error or ''))
        if error:
            self.received_status.setText("Directory unavailable. You can save and check again later.")
        elif more and count == 100:
            self.received_status.setText("100 or more existing files; monitoring uses saved settings")
        elif more:
            self.received_status.setText(f"First 100 entries: {count} files; more entries may exist")
        else:
            self.received_status.setText(f"{count} existing files; monitoring uses saved settings")

    def _editor_value_changed(self, key: str, value: object) -> None:
        self._review_edited_fields.add(str(key))
        self._raw_fields[str(key)] = deepcopy(value)

    def _clear_default_rig_name(self, _index: int) -> None:
        if self.family_key != "js8call":
            return
        combo = getattr(self.editor, "settings_identity_combo", None)
        rig = self.editor.field_widget("rig_name")
        if combo is not None and combo.currentData() != "isolated" and isinstance(rig, QLineEdit) and rig.text():
            rig.clear()

    def _request_discovery(self) -> None:
        if not self._application_discovery_enabled:
            return
        self._discovery_requested = True
        self.editor.set_discovery_state('searching')
        self.discovery_group.setVisible(True)
        self.discover_requested.emit(self.family_key)

    def _row_has_values(self, row: Mapping[str, Any]) -> bool:
        keys = (
            "application_path", "install_path", "path", "configuration_path", "profile_path",
            "ini_path", "host", "port", "secondary_port", "fldigi_port", "fldigi_api_port",
            "launch_command", "launch_cmd", "working_directory",
        )
        if any(row.get(key) not in (None, "") for key in keys):
            return True
        return any(
            row.get(key) not in (None, "")
            for component in ("flrig", "fldigi", "flmsg", "flamp")
            for key in (f"{component}_application_path", f"{component}_path", f"{component}_enabled", f"use_{component}")
        )

    def _discovery_selection_changed(self) -> None:
        selected = self.discovery_list.selectedItems()
        self.use_discovery_button.setEnabled(not self._busy and bool(selected))
        if self._busy or not self._application_discovery_enabled or not selected:
            return
        row = selected[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, Mapping):
            return
        paths = self._discovery_application_paths(row)
        if self.family_key == "fast_light" and self.visible_components:
            paths = {key: value for key, value in paths.items()
                     if key.removesuffix("_application_path") in self.visible_components}
        current = self.editor.values()
        if not paths:
            return
        self._selected_discovery_paths = deepcopy(paths)
        self.editor.set_discovery_state('selected')
        if all(current.get(key) == value for key, value in paths.items()):
            return
        self._review_edited_fields.update(key for key, value in paths.items() if current.get(key) != value)
        current.update(paths)
        self.editor.apply_draft_values(current)
        self.set_operation_status("Application path filled. Review the remaining fields before applying.")

    def _discovery_application_paths(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Map executable paths only; selection never adopts another instance."""
        if self.family_key != "fast_light":
            path = _first(row, "application_path", "install_path", "path",
                          "js8_application_path", "varac_install_path")
            return {"application_path": path} if path else {}
        paths = {}
        component_paths = row.get("component_paths")
        component_paths = component_paths if isinstance(component_paths, Mapping) else {}
        for component in ("flrig", "fldigi", "flmsg", "flamp"):
            aliases = ("application_path", "install_path", "path") if component == "flrig" else (
                ("secondary_application_path",) if component == "fldigi" else ())
            path = _first(row, f"{component}_application_path", f"{component}_path", *aliases)
            path = path or component_paths.get(component)
            if path:
                paths[f"{component}_application_path"] = path
        return paths

    def _use_discovery(self) -> None:
        if self._busy or not self._application_discovery_enabled:
            return
        item = self.discovery_list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(value, Mapping):
            return
        row = deepcopy(dict(value))
        if (self.family_key == 'fast_light' and self.visible_components) or row.get("suggestion_kind") == "application":
            self._discovery_selection_changed()
            return
        self._raw_fields.update(row)
        if row.get('id'):
            self._raw_fields['operator_source_instance_id'] = row['id']
            self._raw_fields['operator_source_snapshot'] = deepcopy(row)
        else:
            self._raw_fields.pop('operator_source_instance_id',None)
            self._raw_fields.pop('operator_source_snapshot',None)
        current = self.editor.values()
        suggested = self._explicit_form_values(row)
        self._review_edited_fields.update(key for key, value in suggested.items() if current.get(key) != value)
        current.update(suggested)
        self.editor.apply_draft_values(current, "Suggested values")
        self.editor.set_discovery_state('selected')
        self.set_operation_status("Suggested settings loaded. Review, then Apply to radio setup." if getattr(self, '_radio_setup_context', False)
                                  else "Suggested settings loaded. Review and Save to keep them.")

    def _explicit_form_values(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Return only fields actually supplied by a suggestion row."""

        if "_suggestion_fields" in raw:
            supplied = set(raw["_suggestion_fields"])
            explicit = dict(raw.get("_suggestion_values") or ())
            raw = {key: value for key, value in raw.items() if key in supplied}
            raw.update(explicit)
        values: dict[str, Any] = {}
        aliases = {
            "application_path": ("application_path", "install_path", "path", "js8_application_path", "varac_install_path", "flrig_path"),
            "configuration_path": ("configuration_path", "ini_path", "profile_path", "js8_profile_path", "flrig_config_path", "varac_ini_path", "existing_ini"),
            "settings_root": ("settings_root",),
            "storage_path": ("storage_path", "application_data_root", "db_path", "varac_db_path", "fldigi_log_path"),
            "secondary_storage_path": ("secondary_storage_path", "incoming_path", "fldigi_checkin_dir"),
            "outbox_path": ("outbox_path", "outbox_dir"),
            "directed_path": ("directed_path", "js8_directed_path"),
            "all_path": ("all_path", "js8_all_path"),
            "inbox_path": ("inbox_path", "js8_inbox_path"),
            "instance_name": ("instance_name", "name", "label"),
            "launch_command": ("launch_command", "launch_cmd", "launcher_path"),
            "working_directory": ("working_directory", "working_dir"),
            "wine_runner": ("wine_runner", "wine", "wine_command"),
            "wine_prefix": ("wine_prefix", "prefix", "wineprefix"),
            "custom_argv": ("custom_argv", "argv", "arguments"),
            "host": ("host", "js8_host", "flrig_host", "fldigi_host"),
            "port": ("port", "js8_tcp_port", "flrig_port") if self.family_key == "fast_light" else ("port", "js8_tcp_port", "flrig_port", "fldigi_port"),
            "secondary_port": ("secondary_port", "fldigi_api_port", "fldigi_port"),
            "rig_name": ("rig_name", "js8_rig_name"),
            "settings_identity_mode": ("settings_identity_mode", "settings_mode"),
            "launch_at_startup": ("launch_at_startup",),
            "mode": ("mode",),
        }
        if self.family_key == "fast_light":
            aliases["application_path"] = ("application_path", "install_path", "path")
        for target, keys in aliases.items():
            for key in keys:
                if key in raw:
                    values[target] = raw[key]
                    break
        if "settings_identity_mode" in values:
            identity = _text(values["settings_identity_mode"]).lower()
            values["settings_identity_mode"] = (
                "isolated" if identity in {"named", "rig", "isolated"}
                else "custom_launcher" if identity in {"launcher", "custom", "custom_launcher"}
                else "separatefolder" if identity in {"separate_folder", "separatefolder"}
                else "default"
            )
        if self.family_key == "fast_light":
            for component in ("flrig", "fldigi", "flmsg", "flamp"):
                for key in (f"{component}_application_path", f"{component}_path"):
                    if key in raw:
                        values[key.replace(f"{component}_path", f"{component}_application_path")] = raw[key]
                        break
                for key in (f"{component}_enabled", f"use_{component}"):
                    if key in raw:
                        values[key.replace(f"use_{component}", f"{component}_enabled")] = _bool(raw[key])
                        break
                startup_key = f"{component}_launch_at_startup"
                if startup_key in raw:
                    values[startup_key] = _bool(raw[startup_key])
        values.update(self._discovery_application_paths(raw))
        return values

    def _browse_field(self, key: str) -> None:
        widget = self.editor.field_widget(key)
        current = widget.text() if hasattr(widget, "text") else ""
        title = f"Choose {key.replace('_', ' ')}"
        directory_keys = {"working_directory", "wine_prefix", "settings_root", "outbox_path", "bbs_path", "bbs_archive_path", "flmsg_message_path", "flamp_receive_path"}
        if key in {"storage_path", "secondary_storage_path"} and self.family_key in {"js8call", "fast_light"}:
            directory_keys.add(key)
        if key == "configuration_path" and self.family_key != "varac":
            directory_keys.add(key)
        if key in directory_keys or key.endswith("_configuration_path"):
            path = QFileDialog.getExistingDirectory(self, title, current)
        else:
            path, _filter = QFileDialog.getOpenFileName(self, title, current)
        if path and hasattr(widget, "setText"):
            widget.setText(path)

    def _cancel(self) -> None:
        if self._busy:
            return
        if not self.editor.is_dirty():
            self.cancelled.emit()
            self._allow_close = True
            return
        action = self._dirty_close_action()
        if action == "save":
            self._complete()
        elif action == "discard":
            self.editor.set_dirty(False)
            self.cancelled.emit()

    def _dirty_close_action(self) -> str:
        box = QMessageBox(self)
        nested = getattr(self, '_radio_setup_context', False)
        box.setWindowTitle("Return to radio setup?" if nested else "Save application changes?")
        box.setText("There are application changes in this form." if nested else "There are unsaved application changes.")
        save = box.addButton("Apply to radio setup" if nested else "Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard application changes" if nested else "Discard", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("Keep editing" if nested else "Stay", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.exec()
        if box.clickedButton() is save:
            return "save"
        if box.clickedButton() is discard:
            return "discard"
        return "stay"

    def guard_dialog(self, dialog):
        """Nested window close and Escape use the same draft decision as Cancel."""
        self._guarded_dialog = dialog
        dialog.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is getattr(self, '_guarded_dialog', None):
            if event.type() == QEvent.Type.Close or (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape):
                self._cancel()
                return True
        return super().eventFilter(watched,event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        if self._busy:
            event.ignore()
            return
        if not self.isVisible():
            event.accept()
            return
        if not self.editor.is_dirty():
            self.cancelled.emit()
            event.accept()
            return
        action = self._dirty_close_action()
        if action == "save":
            self._complete()
            event.ignore()
        elif action == "discard":
            self.editor.set_dirty(False)
            self.cancelled.emit()
            event.accept()
        else:
            event.ignore()

    def _recover_unfinished(self) -> None:
        if self._busy:
            return
        envelope = deepcopy(self._unfinished_envelope or {})
        envelope_payload = envelope.get("payload")
        if not isinstance(envelope_payload, Mapping):
            self.set_operation_status("No recoverable unfinished draft is available.", error=True)
            return
        source = envelope_payload.get("values")
        base = envelope_payload.get("base")
        old_session = envelope_payload.get("session_id")
        operator_context = envelope_payload.get("operator_context")
        if not isinstance(source, Mapping) or not isinstance(base, Mapping) or old_session in (None, ""):
            self.set_operation_status("The unfinished draft has an invalid persistence envelope.", error=True)
            return
        if self.editor.is_dirty():
            box = QMessageBox(self)
            box.setWindowTitle("Recover saved application draft?")
            box.setText("Load the previous draft instead of the application entries on screen?")
            box.setInformativeText(
                "Loading replaces your current unsaved application entries. "
                "Choose Keep editing to retain them. Saved settings are unchanged."
            )
            recover = box.addButton("Load previous draft", QMessageBox.ButtonRole.AcceptRole)
            keep = box.addButton("Keep editing", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() is not recover:
                return
        fresh_context = {'operator_launch_snapshot': deepcopy(self._raw_fields.get('operator_launch_snapshot')),
                         'expected_identity_generation': self._expected_identity_generation,
                         'expected_current_instance_id': self._replacement_instance_id,
                         'replacement_instance_id': self._replacement_instance_id,
                         'operator_draft_revision': envelope.get('revision')}
        fresh_values = deepcopy(self.operator_base_values)
        compared_fields = ('expected_identity_generation', 'expected_current_instance_id',
                           'replacement_instance_id', 'operator_launch_snapshot')
        needs_review = (isinstance(operator_context, Mapping) and
                        any(key in operator_context and operator_context[key] != fresh_context.get(key)
                            for key in compared_fields))
        self.operator_session_id = uuid4().hex
        self._recovered_from = _text(old_session)
        self.operator_draft_revision = envelope.get("revision")
        self._unfinished_token = self._recovered_from
        self._unfinished_revision = envelope.get("revision")
        self._raw_fields.update(deepcopy(dict(source)))
        if isinstance(operator_context, Mapping):
            context = deepcopy(dict(operator_context))
            self._raw_fields.update(context)
            expected_generation = _first(
                context,
                "expected_identity_generation",
                "identity_generation",
                "generation",
                "revision",
            )
            if expected_generation is not None:
                self._expected_identity_generation = expected_generation
            original_instance_id = _first(
                context,
                "expected_current_instance_id",
                "replacement_instance_id",
            )
            if original_instance_id not in (None, ""):
                self._replacement_instance_id = original_instance_id
        self._raw_fields["operator_session_id"] = self.operator_session_id
        self._raw_fields["operator_recovered_from"] = self._recovered_from
        self.editor.set_values(self._seed_form_values(source), "Recovered unfinished draft", preserve_dirty=False)
        self._set_received_files_values(source)
        self._received_files_changed()
        self.operator_base_values = deepcopy(dict(base))
        self._review_edited_fields = _recovered_edited_fields(base, source)
        self.editor.set_dirty(True)
        self._unfinished_envelope = None
        self.recover_button.setVisible(False)
        self.discard_unfinished_button.setVisible(False)
        self.set_conflict_review(None)
        self.set_operation_status("Unfinished draft recovered. Review and Save to publish.")
        if needs_review:
            self.set_conflict_review({'current_values': fresh_values, 'context': fresh_context})

    def _discard_unfinished(self) -> None:
        if self._busy:
            return
        envelope = deepcopy(self._unfinished_envelope or {})
        if not envelope:
            return
        if getattr(self, '_radio_setup_context', False):
            previous = envelope.get('payload')
            if not isinstance(previous, Mapping) or previous.get('session_id') in (None, ''):
                self.set_operation_status('The unfinished draft has an invalid persistence envelope.', error=True)
                return
            self._raw_fields['replace_saved_draft'] = {
                'revision': envelope.get('revision'), 'session_id': previous['session_id']}
            self.operator_draft_revision = envelope.get('revision')
            self.set_unfinished(None)
            self.editor.set_dirty(True)
            self.set_operation_status('Current entries selected. The previous draft is replaced only when you Save radio.')
            return
        revision = envelope.get("revision")
        payload = {
            "draft_revision": revision,
            "revision": revision,
            "operator_session_id": (envelope.get("payload") or {}).get("session_id") if isinstance(envelope.get("payload"), Mapping) else None,
        }
        self.discard_unfinished_requested.emit(payload)

    def _complete(self, _editor_values: object = None) -> None:
        operator_values = self.values()
        operator_values['__edited_fields__'] = sorted(self._review_edited_fields)
        raw_fields = deepcopy(self._raw_fields)
        raw_fields.update(deepcopy(operator_values))
        payload = deepcopy(raw_fields)
        payload.update(
            {
                "operator_source_instance_id": self._raw_fields.get("operator_source_instance_id"),
                "operator_source_snapshot": deepcopy(self._raw_fields.get("operator_source_snapshot")),
                "operator_configuration_v1": True,
                "operator_session_id": self.operator_session_id,
                "family_key": self.family_key,
                "radio_id": self.selected_radio_id,
                "radio_role": self.radio_role,
                "operator_base_values": deepcopy(self.operator_base_values),
                "operator_values": deepcopy(operator_values),
                "raw_fields": deepcopy(raw_fields),
                "initial_instance_id": self._initial_instance_id,
                "initial_application_system_key": self._initial_system_key,
                "expected_identity_generation": self._expected_identity_generation,
                "expected_current_instance_id": self._replacement_instance_id,
                "replacement_instance_id": self._replacement_instance_id,
                "replace_existing": bool(self._replacement_requested),
                "editing_existing": self._editing_existing,
                "replacement_requested": self._replacement_requested,
                "unfinished_token": self._unfinished_token,
                "unfinished_revision": self._unfinished_revision,
                "recovered_from": deepcopy(self._recovered_from),
                "operator_recovered_from": self._recovered_from if isinstance(self._recovered_from, str) else None,
                "operator_draft_revision": self.operator_draft_revision,
                "mode": operator_values.get("mode", raw_fields.get("mode", "manual")),
                "settings_identity_mode": operator_values.get(
                    "settings_identity_mode", raw_fields.get("settings_identity_mode", "default")
                ),
            }
        )
        self.completed.emit(payload)

SoftwareInstanceAssistant = OperatorInstanceAssistant

__all__ = ["OperatorInstanceAssistant", "SoftwareInstanceAssistant"]
