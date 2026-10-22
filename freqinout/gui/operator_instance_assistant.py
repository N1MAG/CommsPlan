"""Compatibility shell for the short operator application editor.

This module deliberately keeps the old assistant's host-facing signals while
moving the visible editing surface to :class:`ApplicationSetupEditor`.  It
does not persist drafts, inspect the filesystem, launch applications, or call
native preparation code.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
from uuid import uuid4

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
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

from freqinout.gui.operator_application_editor import ApplicationSetupEditor
from freqinout.gui.theme import active_app_theme, button_style, label_style


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
        self.task_scope = str(task_scope or "").strip().lower()
        self._editing_existing = bool(editing_existing)
        self._replacement_requested = bool(replacement_requested or editing_existing)
        self._existing_instances = self.existing_instances
        self._initial_raw = self._mapping_from_draft(initial_draft)
        self._raw_fields = deepcopy(self._initial_raw)
        self._discovery_results: tuple[dict[str, Any], ...] = ()
        self._varac_native_presentation: Any = None
        self._busy = False
        self._status_error = False
        # Suggestions are progressive disclosure.  An empty card otherwise
        # consumes the vertical space needed by the actual form.
        self._discovery_requested = False
        self.operator_session_id = uuid4().hex
        self._unfinished_envelope: dict[str, Any] | None = None
        self._unfinished_token: Any = None
        self._unfinished_revision: Any = None
        self._recovered_from: Any = None
        self.operator_draft_revision: Any = None
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
            self.editor.set_task_scope(self.task_scope)
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
        self.operator_base_values = deepcopy(self.editor.values())

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
        self.set_operation_status("Saved")

    # ----- public compatibility surface ----------------------------------

    def set_configuration_attention(self, entry: Mapping[str, Any] | None) -> None:
        self.editor.set_configuration_attention(entry)

    def set_operation_status(self, text: str, *, error: bool = False) -> None:
        self._status_error = bool(error)
        value = _text(text)
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
        self.operation_status_label.setStyleSheet(label_style('danger' if self._status_error else 'muted',palette))
        for button in (self.use_discovery_button,self.create_radio_button,self.recover_button,self.discard_unfinished_button):
            button.setStyleSheet(button_style('secondary',palette))

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.editor.set_busy(self._busy)
        self.editor.status_label.hide()
        self.use_discovery_button.setEnabled(not self._busy and bool(self.discovery_list.currentItem()))
        self.create_radio_button.setEnabled(not self._busy)

    def set_discovery_results(self, results: Iterable[Mapping[str, Any]]) -> None:
        """Publish candidates without changing the draft until Use is clicked."""

        rows: list[dict[str, Any]] = []
        for raw in results:
            if isinstance(raw, Mapping):
                row = deepcopy(dict(raw))
                if self._row_has_values(row):
                    rows.append(row)
        self._discovery_results = tuple(rows)
        self.discovery_group.setVisible(bool(rows) or self._discovery_requested)
        self.discovery_list.clear()
        for row in rows:
            name = _text(_first(row, "name", "instance_name", "label")) or "Unnamed configuration"
            detail = _text(_first(row, "application_path", "install_path", "path"))
            item = QListWidgetItem(f"{name} · {detail}" if detail else name)
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setToolTip("Choose Use selected values to copy this suggestion into the form.")
            self.discovery_list.addItem(item)
        visible = bool(rows)
        self.discovery_list.setVisible(visible)
        self._discovery_selection_changed()
        if visible:
            self.discovery_hint.setText("Select a suggestion, then choose Use selected values. Nothing changes before that action.")
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
        return self.editor.values()

    def set_view_scope(
        self,
        *,
        visible_components: tuple[str, ...] | list[str] | None = None,
        visible_fields: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Limit the visible editor task while preserving the atomic draft."""
        self.visible_components = tuple(visible_components) if visible_components is not None else ()
        self.visible_fields = tuple(visible_fields) if visible_fields is not None else ()
        self.editor.set_view_scope(
            visible_components=(self.visible_components if visible_components is not None else None),
            visible_fields=(self.visible_fields if visible_fields is not None else None),
        )

    def set_task_scope(self, task_key: str | None) -> None:
        """Show the editor surface for one software task without dropping draft data."""
        self.task_scope = str(task_key or "").strip().lower()
        self.editor.set_task_scope(task_key)

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
        self.visible_components = ()
        self.visible_fields = ()

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

    def _draft_payload_values(self) -> dict[str, Any]:
        values = self.editor.values()
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

        self._context_widget = QWidget()
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

        self.create_radio_button = QPushButton("Create a radio…")
        self.create_radio_button.setObjectName("operatorInstanceCreateRadio")
        self.create_radio_button.setAccessibleName("Create a radio")
        self.create_radio_button.setVisible(
            self.selected_radio_id in (None, "") and not self._nested_unsaved
        )
        self._context_layout.addWidget(self.create_radio_button, 0, Qt.AlignmentFlag.AlignLeft)

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
        aliases = {
            "application_path": ("install_path", "path", "js8_application_path", "varac_install_path", "flrig_path"),
            "configuration_path": ("ini_path", "profile_path", "js8_profile_path", "flrig_config_path", "varac_ini_path"),
            "settings_root": ("storage_path", "application_data_root", "data_path"),
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

    def _editor_value_changed(self, key: str, value: object) -> None:
        self._raw_fields[str(key)] = deepcopy(value)

    def _clear_default_rig_name(self, _index: int) -> None:
        if self.family_key != "js8call":
            return
        combo = getattr(self.editor, "settings_identity_combo", None)
        rig = self.editor.field_widget("rig_name")
        if combo is not None and combo.currentData() != "isolated" and isinstance(rig, QLineEdit) and rig.text():
            rig.clear()

    def _request_discovery(self) -> None:
        self._discovery_requested = True
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
        self.use_discovery_button.setEnabled(
            not self._busy and self.discovery_list.currentItem() is not None
        )

    def _use_discovery(self) -> None:
        item = self.discovery_list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(value, Mapping):
            return
        row = deepcopy(dict(value))
        self._raw_fields.update(row)
        if row.get('id'):
            self._raw_fields['operator_source_instance_id'] = row['id']
            self._raw_fields['operator_source_snapshot'] = deepcopy(row)
        else:
            self._raw_fields.pop('operator_source_instance_id',None)
            self._raw_fields.pop('operator_source_snapshot',None)
        current = self.editor.values()
        current.update(self._explicit_form_values(row))
        self.editor.set_values(current, "Suggested values", preserve_dirty=False)
        self.editor.set_dirty(True)
        self.set_operation_status("Suggested values loaded. Review and Save to publish them.")

    def _explicit_form_values(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Return only fields actually supplied by a suggestion row."""

        values: dict[str, Any] = {}
        aliases = {
            "application_path": ("application_path", "install_path", "path", "js8_application_path", "varac_install_path", "flrig_path"),
            "configuration_path": ("configuration_path", "ini_path", "profile_path", "js8_profile_path", "flrig_config_path", "varac_ini_path", "existing_ini"),
            "settings_root": ("settings_root", "storage_path", "application_data_root", "data_path"),
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
        box.setWindowTitle("Save application changes?")
        box.setText("There are unsaved application changes.")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("Stay", QMessageBox.ButtonRole.RejectRole)
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
        self.operator_base_values = deepcopy(dict(base))
        self.editor.set_dirty(True)
        self._unfinished_envelope = None
        self.recover_button.setVisible(False)
        self.discard_unfinished_button.setVisible(False)
        self.set_operation_status("Unfinished draft recovered. Review and Save to publish.")

    def _discard_unfinished(self) -> None:
        envelope = deepcopy(self._unfinished_envelope or {})
        if not envelope:
            return
        revision = envelope.get("revision")
        payload = {
            "draft_revision": revision,
            "revision": revision,
            "operator_session_id": (envelope.get("payload") or {}).get("session_id") if isinstance(envelope.get("payload"), Mapping) else None,
        }
        self.discard_unfinished_requested.emit(payload)

    def _complete(self, _editor_values: object = None) -> None:
        operator_values = self.editor.values()
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
