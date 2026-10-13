"""Task-oriented, I/O-free editors for Software administration.

The editor owns presentation and a local copy of a radio/software draft.  The
Settings host remains the only persistence authority and supplies all values,
status text, browse actions, and explicit validation actions.
"""

from __future__ import annotations

from copy import deepcopy
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from freqinout.gui.theme import button_height_for_font
from freqinout.gui.bbs_access_editor import BbsAccessEditor


@dataclass(frozen=True)
class SoftwareEditorField:
    key: str
    label: str
    kind: str = "text"
    placeholder: str = ""
    help_text: str = ""
    browse: bool = False


@dataclass(frozen=True)
class SoftwareEditorTask:
    title: str
    description: str
    fields: tuple[SoftwareEditorField, ...] = ()
    action: str = ""
    action_label: str = ""


F = SoftwareEditorField
T = SoftwareEditorTask


SOFTWARE_EDITOR_TASKS: dict[str, dict[str, SoftwareEditorTask]] = {
    "js8call": {
        "overview": T("JS8Call overview", "Review the application, API, profile, and message-storage assignment for this radio."),
        "application_profile": T("Application & Profile", "Choose the JS8Call installation and this radio's saved-files profile.", (
            F("path_js8call", "JS8Call application", "path", "Application or executable path", browse=True),
            F("js8_profile_path", "Save folder", "path", "Folder for operator-saved and received transfers", browse=True),
        )),
        "api_radio": T("API & Radio", "The TCP endpoint and audio offset must identify this radio's JS8Call instance.", (
            F("js8_host", "TCP host", placeholder="127.0.0.1"),
            F("js8_port", "TCP port", "integer", "2442"),
            F("js8_offset_hz", "Audio offset (Hz)", "integer", "0"),
        )),
        "message_storage": T("Message Storage", "DIRECTED.TXT is an ingest source. The Save folder does not relocate JS8Call message storage.", (
            F("js8_directed_path", "DIRECTED.TXT", "path", "Path to this instance's directed log", browse=True),
        )),
        "ingest_forms": T("Ingest & Forms", "Configure directed-message ingest. CommsPlan Spotter owns form administration.", (
            F("js8_directed_path", "DIRECTED.TXT", "path", "Path to this instance's directed log", browse=True),
        ), action="fio_spotter", action_label="Open CommsPlan Spotter"),
        "launch": T("Launch", "Configure the application CommsPlan launches for this radio.", (
            F("path_js8call", "JS8Call application", "path", "Application or executable path", browse=True),
        )),
        "health": T("Health", "Use an explicit check to validate the configured application, storage, and API endpoint.", action="validate", action_label="Check JS8Call"),
        "advanced": T("Advanced", "Advanced instance identity stays radio-scoped. Ordinary use should not require changes here.", (
            F("js8_host", "TCP host", placeholder="127.0.0.1"),
            F("js8_port", "TCP port", "integer", "2442"),
            F("js8_offset_hz", "Audio offset (Hz)", "integer", "0"),
            F("js8_profile_path", "Save folder", "path", browse=True),
            F("js8_directed_path", "DIRECTED.TXT", "path", browse=True),
        )),
    },
    "fast_light": {
        "overview": T("Fast Light overview", "Review FLRig, FLDigi, FLMsg, and FLAmp for this radio workflow."),
        "flrig_control": T("FLRig Control", "Configure the radio-control application and its radio-specific XML-RPC port.", (
            F("path_flrig", "FLRig application", "path", browse=True),
            F("flrig_port", "FLRig XML-RPC port", "integer", "12345"),
        )),
        "fldigi_modem_logs": T("FLDigi Modem & Logs", "Configure the modem endpoint and the folders used for logs and check-ins.", (
            F("path_fldigi", "FLDigi application", "path", browse=True),
            F("fldigi_host", "FLDigi XML-RPC host", placeholder="127.0.0.1"),
            F("fldigi_port", "FLDigi XML-RPC port", "integer", "7362"),
            F("arq_port", "FLDigi / FLAmp ARQ port", "integer", "7322"),
            F("fldigi_log_path", "FLDigi log folder", "path", browse=True),
            F("fldigi_checkin_dir", "Check-in folder", "path", browse=True),
        )),
        "flmsg": T("FLMsg", "The FLMsg application may be shared; this radio keeps its own native NBEMS workspace and message source.", (
            F("path_flmsg", "FLMsg application", "path", browse=True),
            F("message_paths.flmsg", "ICS messages folder", "path", browse=True),
        )),
        "flamp_signing": T("FLAmp & Signing", "Review this radio's FLAmp application, native receive source, and paired FLDigi ARQ endpoint. Signing identities are managed centrally.", (
            F("path_flamp", "FLAmp application", "path", browse=True),
            F("message_paths.flamp", "FLAMP receive folder", "path", browse=True),
        ), action="message_signing", action_label="Manage signing identities"),
        "message_folders": T("Message Folders", "Review the folders CommsPlan watches for FLMsg and FLAmp traffic.", (
            F("message_paths.flmsg", "FLMsg messages", "path", browse=True),
            F("message_paths.flamp", "FLAmp received files", "path", browse=True),
        )),
        "launch": T("Launch", "Choose the Fast Light applications available to this radio workflow.", (
            F("path_flrig", "FLRig application", "path", browse=True),
            F("path_fldigi", "FLDigi application", "path", browse=True),
            F("path_flmsg", "FLMsg application", "path", browse=True),
            F("path_flamp", "FLAmp application", "path", browse=True),
        )),
        "health": T("Health", "Run explicit checks for the configured Fast Light applications and endpoints.", action="validate", action_label="Check Fast Light"),
        "advanced": T("Advanced", "Review all radio-specific Fast Light endpoints without changing shared application ownership.", (
            F("flrig_port", "FLRig XML-RPC port", "integer", "12345"),
            F("fldigi_host", "FLDigi XML-RPC host", placeholder="127.0.0.1"),
            F("fldigi_port", "FLDigi XML-RPC port", "integer", "7362"),
            F("arq_port", "FLDigi / FLAmp ARQ port", "integer", "7322"),
            F("fldigi_log_path", "FLDigi log folder", "path", browse=True),
            F("fldigi_checkin_dir", "Check-in folder", "path", browse=True),
        )),
    },
    "varac": {
        "overview": T("VarAC overview", "Review this radio's VarAC application, runtime folders, guard, and cluster assignment."),
        "application_radio": T("Application & Radio", "Choose the VarAC installation and configuration used with this radio.", (
            F("varac_path", "VarAC application folder", "path", browse=True),
            F("varac_ini_path", "VarAC configuration", "path", browse=True),
        )),
        "runtime_paths": T("Runtime & Paths", "Configure radio-specific VarAC runtime and launch paths.", (
            F("varac_path", "VarAC application folder", "path", browse=True),
            F("varac_ini_path", "VarAC configuration", "path", browse=True),
            F("varac_launch_cmd", "Launch command", "path", browse=True),
        )),
        "inbox_outbox": T("Inbox & Outbox", "These are native VarAC paths for this radio, not shared BBS administration.", (
            F("message_paths.varac", "Incoming folder", "path", browse=True),
            F("varac_outbox_dir", "Outbox folder", "path", browse=True),
        )),
        "inbound_guard": T("Inbound Guard", "CommsPlan-only inbound safety: choose who may pass this radio's inbound file guard. Either enabled source may authorize. Selecting groups does not narrow the BBS access list. Save applies this policy in CommsPlan. Native Get/Update is in BBS > Radio Service.", (
            F("varac_guard_allow_bbs_allowed_callsigns", "Allow BBS access list", "boolean"),
            F("varac_guard_allow_operator_trusted", "Allow trusted operators", "boolean"),
            F("varac_guard_access_policy_v1", "HF Operator access scope", "access_policy"),
        )),
        "cluster": T("Cluster", "Open the existing radio-specific VarAC cluster coordinator.", action="varac_cluster", action_label="Open cluster settings"),
        "launch": T("Launch", "Configure the VarAC command CommsPlan launches for this radio.", (
            F("varac_launch_cmd", "Launch command", "path", browse=True),
        )),
        "health": T("Health", "Run an explicit check of the VarAC application and runtime paths.", action="validate", action_label="Check VarAC"),
        "advanced": T("Advanced", "Shared BBS publication remains in the top-level BBS service.", (
            F("varac_bbs_dir", "Live BBS folder", "path", browse=True),
            F("varac_bbs_archive_dir", "BBS archive folder", "path", browse=True),
        ), action="bbs", action_label="Open BBS service"),
    },
    "commstat": {
        "overview": T(
            "CommStat overview",
            "CommStat is one station-shared process. Each selected radio contributes only a binding to its JS8Call endpoint.",
        ),
        "transport_mapping": T("JS8 Endpoint Bindings", "Review the selected radio's binding to the station-shared CommStat process.", (
            F("js8_host", "JS8 transport host", "readonly"),
            F("js8_port", "JS8 transport port", "readonly"),
        )),
        "health": T("Shared Service Health", "Run an explicit station-shared health check without probing during navigation.", action="validate", action_label="Check CommStat"),
        "advanced": T("Advanced", "The shared process is configured once at station scope; this radio owns only its JS8 endpoint binding.", (
            F("js8_host", "JS8 transport host", "readonly"),
            F("js8_port", "JS8 transport port", "readonly"),
        )),
    },
    "external_spotter": {
        "overview": T("External Spotter overview", "This optional external application is separate from built-in CommsPlan Spotter."),
        "installation": T("Installation", "Choose the optional external Spotter launcher or shortcut.", (
            F("path_js8spotter", "External Spotter application", "path", browse=True),
        )),
        "profile": T("Profile", "Importing an external Spotter profile is an explicit operation.", action="external_spotter_import", action_label="Import external Spotter data"),
        "forms": T("Form Synchronization", "CommsPlan Spotter owns active form administration; external data can be imported explicitly.", action="fio_spotter", action_label="Open CommsPlan Spotter forms"),
        "launch": T("Launch", "Choose the external Spotter application CommsPlan may launch.", (
            F("path_js8spotter", "External Spotter application", "path", browse=True),
        )),
        "health": T("Health", "Check the configured external application only when requested.", action="validate", action_label="Check External Spotter"),
        "advanced": T("Advanced", "External Spotter is optional and does not control CommsPlan Spotter.", (
            F("path_js8spotter", "External Spotter application", "path", browse=True),
        )),
    },
    "fio_spotter": {
        "overview": T("CommsPlan Spotter overview", "CommsPlan Spotter is built into CommsPlan. Rules, Expect queries, watches, forms, and activity live in its top-level workspace.", action="fio_spotter", action_label="Open CommsPlan Spotter"),
        "dependencies": T("Dependencies & Radio Mapping", "Review the JS8 transport selected for this radio. Built-in forms require no per-radio MCF folder.", (
            F("js8_host", "JS8 transport host", "readonly"),
            F("js8_port", "JS8 transport port", "readonly"),
        ), action="fio_spotter", action_label="Open CommsPlan Spotter"),
        "operational_workspace": T("CommsPlan Spotter workspace", "Manage rules, Expect queries, watches, forms, and activity in the operational workspace.", action="fio_spotter", action_label="Open CommsPlan Spotter"),
    },
}


def task_definition(family_key: str, task_key: str) -> Optional[SoftwareEditorTask]:
    return SOFTWARE_EDITOR_TASKS.get(str(family_key or "").strip().lower(), {}).get(
        str(task_key or "").strip().lower()
    )


def _value_at(state: Mapping[str, Any], key: str) -> Any:
    current: Any = state
    for part in key.split("."):
        if not isinstance(current, Mapping):
            return ""
        current = current.get(part, "")
    return current


def merge_draft_value(state: Mapping[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Return a copied state with one dotted field updated."""
    result = deepcopy(dict(state))
    parts = key.split(".")
    target = result
    for part in parts[:-1]:
        child = target.get(part)
        child_copy = dict(child) if isinstance(child, Mapping) else {}
        target[part] = child_copy
        target = child_copy
    target[parts[-1]] = value
    return result


class SoftwareTaskEditor(QWidget):
    """Render one selected software task from an already-loaded draft."""

    value_changed = Signal(str, object)
    browse_requested = Signal(str)
    action_requested = Signal(str)
    save_requested = Signal(str, object)

    def __init__(self, parent: Optional[QWidget] = None, *, operator_db_path=None) -> None:
        super().__init__(parent)
        self._family_key = ""
        self._task_key = ""
        self._radio_id: Optional[int] = None
        self._radio_name = ""
        self._state: dict[str, Any] = {}
        self._dirty = False
        self._field_widgets: dict[str, QWidget] = {}
        self._browse_buttons: dict[str, QPushButton] = {}
        self._canonical_identity_managed = False
        self._operator_db_path = operator_db_path
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.root_layout = root
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self.title_label = QLabel("Choose a radio and task")
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        self.title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        root.addWidget(self.title_label)
        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        self.description_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        root.addWidget(self.description_label)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.status_label.setAccessibleName("Software task status")
        root.addWidget(self.status_label)
        self.identity_notice_label = QLabel()
        self.identity_notice_label.setWordWrap(True)
        self.identity_notice_label.setAccessibleName("Canonical software identity editing route")
        self.identity_notice_label.hide()
        root.addWidget(self.identity_notice_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.form_widget = QWidget()
        self.form = QFormLayout(self.form_widget)
        self.form.setContentsMargins(0, 4, 0, 4)
        self.form.setHorizontalSpacing(10)
        self.form.setVerticalSpacing(7)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.scroll.setWidget(self.form_widget)
        root.addWidget(self.scroll, 1)

        actions = QGridLayout()
        self.actions_layout = actions
        self._compact_actions: Optional[bool] = None
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(4)
        self.discover_button = QPushButton("Find installed software")
        self.discover_button.setAccessibleName("Find installed software for this task")
        self.discover_button.setToolTip("Search common local installation and data locations only when requested")
        self.discover_button.clicked.connect(lambda: self.action_requested.emit("discover"))
        self.discover_button.setMinimumHeight(button_height_for_font(self.discover_button))
        self.discover_button.hide()
        self.task_action_button = QPushButton()
        self.task_action_button.clicked.connect(self._emit_action)
        self.task_action_button.setMinimumHeight(button_height_for_font(self.task_action_button))
        self.task_action_button.hide()
        self.edit_configuration_button = QPushButton("Edit configuration…")
        self.edit_configuration_button.setAccessibleName("Edit this software configuration")
        self.edit_configuration_button.setToolTip(
            "Open the complete prefilled editor with conflict review and atomic save"
        )
        self.edit_configuration_button.clicked.connect(
            lambda: self.action_requested.emit("edit_canonical_configuration")
        )
        self.edit_configuration_button.setMinimumHeight(
            button_height_for_font(self.edit_configuration_button)
        )
        self.edit_configuration_button.hide()
        self.dirty_label = QLabel("No unsaved changes")
        self.dirty_label.setAccessibleName("Software draft state")
        self.save_button = QPushButton("Save selected software")
        self.save_button.clicked.connect(self._emit_save)
        self.save_button.setMinimumHeight(button_height_for_font(self.save_button))
        self._layout_actions(compact=True)
        root.addLayout(actions)
        self.setAccessibleName("Software task editor")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._layout_actions(compact=event.size().width() < 700)
        # The workspace identity banner already carries radio, instance,
        # status, shared-use, and dirty context. At short heights, remove this
        # duplicate line so form rows and their Browse controls remain whole.
        self.status_label.setVisible(event.size().height() >= 240)

    def _layout_actions(self, *, compact: bool) -> None:
        if self._compact_actions is compact:
            return
        self._compact_actions = compact
        for widget in (
            self.discover_button,
            self.task_action_button,
            self.edit_configuration_button,
            self.save_button,
        ):
            widget.setMinimumHeight(button_height_for_font(widget, vertical_padding=10 if compact else 12))
        for widget in (
            self.discover_button,
            self.task_action_button,
            self.edit_configuration_button,
            self.dirty_label,
            self.save_button,
        ):
            self.actions_layout.removeWidget(widget)
        if compact:
            self.actions_layout.addWidget(self.discover_button, 0, 0)
            self.actions_layout.addWidget(self.task_action_button, 0, 1)
            self.actions_layout.addWidget(self.edit_configuration_button, 0, 2)
            self.actions_layout.addWidget(self.dirty_label, 1, 0)
            self.actions_layout.addWidget(self.save_button, 1, 1, 1, 2)
            self.actions_layout.setColumnStretch(3, 1)
        else:
            self.actions_layout.addWidget(self.discover_button, 0, 0)
            self.actions_layout.addWidget(self.task_action_button, 0, 1)
            self.actions_layout.addWidget(self.edit_configuration_button, 0, 2)
            self.actions_layout.setColumnStretch(3, 1)
            self.actions_layout.addWidget(self.dirty_label, 0, 4)
            self.actions_layout.addWidget(self.save_button, 0, 5)

    @staticmethod
    def _clear_form(form: QFormLayout) -> None:
        while form.rowCount():
            form.removeRow(0)

    def set_context(
        self,
        *,
        family_key: str,
        family_title: str,
        task_key: str,
        radio_id: Optional[int],
        radio_name: str,
        state: Mapping[str, Any],
        status_text: str = "",
        shared_radio_names: tuple[str, ...] = (),
    ) -> None:
        self._family_key = str(family_key or "").strip().lower()
        self._task_key = str(task_key or "").strip().lower()
        self._radio_id = int(radio_id) if radio_id else None
        self._radio_name = str(radio_name or "").strip()
        self._state = deepcopy(dict(state or {}))
        self._dirty = False
        task = task_definition(self._family_key, self._task_key)
        self._clear_form(self.form)
        self._field_widgets = {}
        self._browse_buttons = {}

        if task is None:
            self.title_label.setText("Choose a configuration task")
            self.description_label.setText("")
        else:
            self.title_label.setText(task.title)
            self.description_label.setText(task.description)
            for field in task.fields:
                self._add_field(field)
        has_fields = bool(task and task.fields)
        has_editable_fields = bool(task and any(field.kind != "readonly" for field in task.fields))
        self.scroll.setVisible(has_fields)
        self.root_layout.setStretchFactor(self.scroll, 1 if has_fields else 0)
        self.root_layout.setAlignment(Qt.Alignment() if has_fields else Qt.AlignTop)
        identity = f"{family_title} for {self._radio_name}" if self._radio_name else f"All radios using {family_title}"
        status_parts = [identity]
        if status_text:
            status_parts.append(f"Status: {status_text}")
        others = tuple(name for name in shared_radio_names if name and name != self._radio_name)
        if others:
            status_parts.append("Shared with: " + ", ".join(others))
        self.status_label.setText(" · ".join(status_parts))
        self.task_action_button.setVisible(bool(task and task.action))
        self.discover_button.setVisible(
            bool(task and self._radio_id is not None and any(field.browse for field in task.fields))
        )
        if task:
            self.task_action_button.setText(task.action_label or "Open")
            self.task_action_button.setProperty("software_action", task.action)
        self.save_button.setText(f"Save {family_title} for {self._radio_name}" if self._radio_name else "Select one radio to save")
        self.dirty_label.setVisible(has_editable_fields)
        self.save_button.setVisible(has_editable_fields)
        self.save_button.setEnabled(self._radio_id is not None and has_editable_fields)
        self._refresh_dirty_label()

    def set_canonical_identity_managed(
        self,
        managed: bool,
        *,
        identity_key: str = "",
        component_repair_available: bool = False,
    ) -> None:
        """Present canonical values as facts with one authoritative edit route.

        The guided instance assistant is the sole editor for a canonical
        identity because it can update the application row, manifest, launch
        projection, and identity generation atomically.  Allowing this compact
        legacy field projection to save independently would create split state.
        """

        self._canonical_identity_managed = bool(managed) and not (
            self._family_key == "varac" and self._task_key == "inbound_guard"
        )
        self.identity_notice_label.setVisible(self._canonical_identity_managed)
        self.edit_configuration_button.setVisible(self._canonical_identity_managed)
        for widget in self._field_widgets.values():
            if isinstance(widget, QLineEdit):
                widget.setReadOnly(self._canonical_identity_managed)
                widget.setFrame(not self._canonical_identity_managed)
                widget.setFocusPolicy(
                    Qt.NoFocus if self._canonical_identity_managed else Qt.StrongFocus
                )
                widget.setStyleSheet(
                    "background: transparent; border: none; padding: 2px 0;"
                    if self._canonical_identity_managed
                    else ""
                )
            elif isinstance(widget, QCheckBox):
                widget.setEnabled(not self._canonical_identity_managed)
        for button in self._browse_buttons.values():
            button.setVisible(not self._canonical_identity_managed)
        if self._canonical_identity_managed:
            repair_note = (
                " A component-only FLMsg/FLAmp repair is available below; it preserves the "
                "existing FLRig profile and the FLDigi profile/XML-RPC identity."
                if component_repair_available
                else ""
            )
            self.identity_notice_label.setText(
                f"These are the saved {self._radio_name or 'radio'} software values. "
                "Choose Edit configuration… to change paths, endpoints, defaults, or launch details "
                f"in one reviewed transaction.{repair_note}"
            )
            self.identity_notice_label.setToolTip("")
            self.discover_button.hide()
            self.save_button.hide()
            self.dirty_label.hide()
            if component_repair_available:
                self.task_action_button.setText("Repair FLMsg / FLAmp components…")
                self.task_action_button.setProperty(
                    "software_action", "repair_fast_light_message_components"
                )
                self.task_action_button.show()
            else:
                task = task_definition(self._family_key, self._task_key)
                self.task_action_button.setVisible(bool(task and task.action))
                if task and task.action:
                    self.task_action_button.setText(task.action_label or "Open")
                    self.task_action_button.setProperty("software_action", task.action)
        else:
            self.edit_configuration_button.hide()
            task = task_definition(self._family_key, self._task_key)
            has_editable_fields = bool(
                task and any(field.kind != "readonly" for field in task.fields)
            )
            self.dirty_label.setVisible(has_editable_fields)
            self.save_button.setVisible(has_editable_fields)
            self.save_button.setEnabled(self._radio_id is not None and has_editable_fields)
            if component_repair_available:
                self.identity_notice_label.setText(
                    "This radio uses an older or incomplete Fast Light launch identity. "
                    "Repair only FLMsg/FLAmp wiring while retaining the saved FLRig and "
                    "FLDigi identity, endpoints, and unrelated launch choices."
                )
                self.identity_notice_label.setToolTip("")
                self.identity_notice_label.show()
                self.task_action_button.setText("Repair FLMsg / FLAmp components…")
                self.task_action_button.setProperty(
                    "software_action", "repair_fast_light_message_components"
                )
                self.task_action_button.show()

    def _add_field(self, field: SoftwareEditorField) -> None:
        value = _value_at(self._state, field.key)
        if field.kind == "access_policy":
            control = QWidget()
            layout = QVBoxLayout(control)
            layout.setContentsMargins(0, 0, 0, 0)
            self.guard_scope_combo = QComboBox()
            self.guard_scope_combo.addItems([
                "All trusted HF Operators (legacy)", "Only selected groups and callsigns",
            ])
            self.guard_scope_combo.setAccessibleName("Inbound Guard HF Operator access scope")
            self.guard_access_editor = BbsAccessEditor(control, operator_db_path=self._operator_db_path)
            layout.addWidget(self.guard_scope_combo)
            layout.addWidget(self.guard_access_editor)
            self._set_guard_policy(value)
            self.guard_scope_combo.currentIndexChanged.connect(self._guard_policy_changed)
            self.guard_access_editor.changed.connect(self._guard_policy_changed)
        elif field.kind == "boolean":
            control: QWidget = QCheckBox()
            control.setChecked(bool(value))
            control.stateChanged.connect(
                lambda _state, key=field.key, widget=control: self._on_value_changed(key, widget.isChecked())
            )
        else:
            edit = QLineEdit()
            edit.setText(str(value if value is not None else ""))
            edit.setPlaceholderText(field.placeholder)
            edit.setReadOnly(field.kind == "readonly")
            edit.setAccessibleName(field.label)
            if field.help_text:
                edit.setToolTip(field.help_text)
            edit.textEdited.connect(lambda text, key=field.key: self._on_value_changed(key, text))
            control = edit
        control.setObjectName("softwareField_" + field.key.replace(".", "_"))
        self._field_widgets[field.key] = control
        if field.browse and field.kind != "readonly":
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            row_layout.addWidget(control, 1)
            browse = QPushButton("Browse")
            browse.setAccessibleName(f"Browse for {field.label}")
            browse.clicked.connect(lambda _checked=False, key=field.key: self.browse_requested.emit(key))
            self._browse_buttons[field.key] = browse
            row_layout.addWidget(browse)
            self.form.addRow(field.label, row)
        else:
            self.form.addRow(field.label, control)

    def _set_guard_policy(self, value):
        blocked = self.guard_scope_combo.blockSignals(True)
        self.guard_scope_combo.setCurrentIndex(1 if str(value or "").strip() else 0)
        self.guard_scope_combo.blockSignals(blocked)
        self.guard_access_editor.set_policy(value)
        self._update_guard_scope()

    def _update_guard_scope(self):
        selected = self.guard_scope_combo.currentIndex() == 1
        self.guard_access_editor.setVisible(selected)
        flag = self._field_widgets.get("varac_guard_allow_operator_trusted")
        label = self.form.labelForField(flag) if flag is not None else None
        if isinstance(label, QLabel):
            label.setText("Allow selected access list" if selected else "Allow trusted operators")

    def _guard_policy_changed(self, *_args):
        self._update_guard_scope()
        value = "" if self.guard_scope_combo.currentIndex() == 0 else json.dumps(
            self.guard_access_editor.policy(), sort_keys=True, separators=(",", ":")
        )
        self._on_value_changed("varac_guard_access_policy_v1", value)

    def _on_value_changed(self, key: str, value: Any) -> None:
        self._state = merge_draft_value(self._state, key, value)
        self._dirty = True
        self._refresh_dirty_label()
        self.value_changed.emit(key, value)

    def _refresh_dirty_label(self) -> None:
        self.dirty_label.setText("Unsaved changes" if self._dirty else "No unsaved changes")
        self.dirty_label.setProperty("dirty", self._dirty)

    def set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._refresh_dirty_label()

    def set_state(self, state: Mapping[str, Any], *, dirty: Optional[bool] = None) -> None:
        """Refresh visible values from a host-owned draft without emitting edits."""
        self._state = deepcopy(dict(state or {}))
        for key, widget in self._field_widgets.items():
            value = _value_at(self._state, key)
            if key == "varac_guard_access_policy_v1":
                self._set_guard_policy(value)
                continue
            blocked = widget.blockSignals(True)
            if isinstance(widget, QLineEdit):
                widget.setText(str(value if value is not None else ""))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            widget.blockSignals(blocked)
        if dirty is not None:
            self.set_dirty(dirty)

    def set_operation_status(self, text: str) -> None:
        self.status_label.setText(str(text or ""))

    def is_dirty(self) -> bool:
        return self._dirty

    def state(self) -> dict[str, Any]:
        return deepcopy(self._state)

    def field_widget(self, key: str) -> Optional[QWidget]:
        return self._field_widgets.get(key)

    def field_keys(self) -> tuple[str, ...]:
        """Return the visible task fields without exposing the widget registry."""

        return tuple(self._field_widgets)

    def apply_value(self, key: str, value: Any) -> None:
        """Apply a host-selected value through the normal draft-change path."""
        widget = self._field_widgets.get(key)
        if key == "varac_guard_access_policy_v1" and widget is not None:
            self._set_guard_policy(value)
        if isinstance(widget, QLineEdit) and widget.text() != str(value or ""):
            blocked = widget.blockSignals(True)
            widget.setText(str(value or ""))
            widget.blockSignals(blocked)
        elif isinstance(widget, QCheckBox) and widget.isChecked() != bool(value):
            blocked = widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(blocked)
        self._on_value_changed(key, value)

    def _emit_action(self) -> None:
        self.action_requested.emit(str(self.task_action_button.property("software_action") or ""))

    def _emit_save(self) -> None:
        if self._radio_id is not None:
            self.save_requested.emit(self._family_key, self._radio_id)


__all__ = [
    "SOFTWARE_EDITOR_TASKS",
    "SoftwareEditorField",
    "SoftwareEditorTask",
    "SoftwareTaskEditor",
    "merge_draft_value",
    "task_definition",
]
