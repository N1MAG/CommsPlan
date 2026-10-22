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

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from freqinout.gui.theme import active_app_theme, button_height_for_font, button_style
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
        "ingest_forms": T("Message ingest", "Review directed-message ingest. CommsPlan Spotter owns form administration.", (
            F("js8_directed_path", "DIRECTED.TXT", "path", "Path to this instance's directed log", browse=True),
        ), action="fio_spotter_forms", action_label="Manage CommsPlan Spotter forms…"),
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
            F("fldigi_host", "FLDigi host", placeholder="127.0.0.1"),
            F("fldigi_port", "FLDigi XML-RPC port", "integer", "7362"),
            F("arq_port", "FLDigi / FLAmp ARQ port", "readonly", help_text="This endpoint is owned by the saved Fast Light application identity and launch recipe. Use Add/Replace instance or the available FLMsg/FLAmp repair action to change it."),
            F("fldigi_log_path", "FLDigi log folder", "path", browse=True),
            F("fldigi_checkin_dir", "Check-in folder", "path", browse=True),
        )),
        "flmsg": T("FLMsg", "The FLMsg application may be shared; this radio keeps its own native NBEMS workspace and message source.", (
            F("path_flmsg", "FLMsg application", "path", browse=True),
            F("message_paths.flmsg", "ICS messages folder", "path", browse=True),
        )),
        "flamp_signing": T("FLAmp & Signing", "Manage FLAmp paths and the paired ARQ endpoint. Signing applies station-wide.", (
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
            F("arq_port", "FLDigi / FLAmp ARQ port", "readonly", help_text="This endpoint is owned by the saved Fast Light application identity and launch recipe. Use Add/Replace instance or the available FLMsg/FLAmp repair action to change it."),
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
        "profile": T("Activity & imports", "Review external activity and import external Spotter data explicitly.", action="external_spotter_activity", action_label="Review external activity…"),
        "forms": T("Form Synchronization", "CommsPlan Spotter owns active form administration; external data can be imported explicitly.", action="fio_spotter_forms", action_label="Manage CommsPlan Spotter forms…"),
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
        "forms": T("Forms Library", "Manage CommsPlan Spotter forms in the shared station workspace.", action="fio_spotter_forms", action_label="Open Forms Library…"),
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
        self._status_identity_text = ""
        self._status_shared_text = ""
        self._runtime_status_text = ""
        self._operation_status_text = ""
        self._embedded_context = False
        self._component_repair_available = False
        self._reference_summary: dict[str, Any] = {}
        self._theme: dict[str, str] = active_app_theme()
        self.setFocusPolicy(Qt.StrongFocus)
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
        self.edit_configuration_button = QPushButton("Edit configuration…")
        self.edit_configuration_button.setAccessibleName("Edit this software configuration")
        self.edit_configuration_button.setToolTip(
            "Open the complete prefilled editor with conflict review and atomic save"
        )
        self.edit_configuration_button.clicked.connect(
            lambda: self.action_requested.emit("edit_canonical_configuration")
        )
        self.edit_configuration_button.setMinimumHeight(button_height_for_font(self.edit_configuration_button))
        self.edit_configuration_button.hide()
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.title_label, 1)
        title_row.addWidget(self.edit_configuration_button)
        root.addLayout(title_row)
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

        self.reference_summary_frame = QFrame()
        self.reference_summary_frame.setObjectName("softwareReferenceSummary")
        self.reference_summary_frame.setMinimumWidth(0)
        self.reference_summary_frame.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        reference_layout = QGridLayout(self.reference_summary_frame)
        reference_layout.setContentsMargins(0, 2, 0, 2)
        reference_layout.setHorizontalSpacing(10)
        reference_layout.setVerticalSpacing(3)
        self.signing_summary_label = QLabel()
        self.signing_summary_label.setWordWrap(True)
        self.signing_summary_label.setAccessibleName("Station-wide signing key summary")
        self.arq_summary_label = QLabel()
        self.arq_summary_label.setWordWrap(True)
        self.arq_summary_label.setAccessibleName("Paired FLDigi ARQ endpoint summary")
        self.edit_flamp_arq_button = QPushButton("Edit FLDigi Modem & Logs…")
        self.edit_flamp_arq_button.setAccessibleName("Edit paired FLDigi ARQ endpoint")
        self.edit_flamp_arq_button.setToolTip("Open the FLDigi Modem & Logs task for this radio")
        self.edit_flamp_arq_button.clicked.connect(
            lambda: self.action_requested.emit("edit_flamp_arq")
        )
        reference_layout.addWidget(QLabel("Signing"), 0, 0)
        reference_layout.addWidget(self.signing_summary_label, 0, 1)
        reference_layout.addWidget(QLabel("FLDigi ARQ"), 1, 0)
        reference_layout.addWidget(self.arq_summary_label, 1, 1)
        reference_layout.addWidget(self.edit_flamp_arq_button, 1, 2)
        reference_layout.setColumnStretch(1, 1)
        self.reference_summary_frame.hide()

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.form_widget = QWidget()
        self.form_widget.setMinimumWidth(0)
        self.form_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.form = QFormLayout(self.form_widget)
        self.form.setContentsMargins(0, 4, 0, 4)
        self.form.setHorizontalSpacing(10)
        self.form.setVerticalSpacing(7)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.reference_summary_host = QWidget(self)
        self.reference_summary_host.setMinimumWidth(0)
        self.reference_summary_host.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        reference_host_layout = QVBoxLayout(self.reference_summary_host)
        reference_host_layout.setContentsMargins(0, 0, 0, 0)
        reference_host_layout.addWidget(self.reference_summary_frame)
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
        self.repair_button = QPushButton("Repair FLMsg / FLAmp components…")
        self.repair_button.setAccessibleName("Repair Fast Light message components")
        self.repair_button.setToolTip("Review the narrow FLMsg and FLAmp component repair")
        self.repair_button.clicked.connect(
            lambda: self.action_requested.emit("repair_fast_light_message_components")
        )
        self.repair_button.hide()
        self.repair_reason_label = QLabel()
        self.repair_reason_label.setWordWrap(True)
        self.repair_reason_label.setAccessibleName("Fast Light component repair reason")
        self.repair_reason_label.hide()
        root.addWidget(self.repair_reason_label)
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
        self._layout_actions(compact=self._action_layout_requires_reflow())
        # The workspace identity banner already carries radio, instance,
        # status, shared-use, and dirty context. At short heights, remove this
        # duplicate line so form rows and their Browse controls remain whole.
        self.status_label.setVisible(
            bool(self._operation_status_text)
            or (not self._embedded_context and event.size().height() >= 240)
        )
        self._fit_action_attention()

    def _fit_action_attention(self) -> None:
        """Publish a font-derived floor so wrapped form warnings stay readable."""
        task = task_definition(self._family_key, self._task_key)
        label = getattr(self, 'configuration_attention_label', None)
        if task is None or label is None:
            return
        margins = self.form.contentsMargins()
        width = max(1, self.scroll.width() - self.scroll.verticalScrollBar().sizeHint().width()
                    - 2 * self.scroll.frameWidth() - margins.left() - margins.right())
        height = max(0, label.heightForWidth(width)) if label.text() else 0
        if label.minimumHeight() != height:
            label.setMinimumHeight(height)
        floor = height + margins.top() + margins.bottom() if height else 0
        if task.fields:
            floor = max(floor, self.form.minimumSize().height())
        if self.form_widget.minimumHeight() != floor:
            self.form_widget.setMinimumHeight(floor)

    def _layout_actions(self, *, compact: bool) -> None:
        self._compact_actions = compact
        self.actions_layout.setVerticalSpacing(2 if compact else 4)
        for widget in (
            self.discover_button,
            self.task_action_button,
            self.repair_button,
            self.save_button,
        ):
            widget.setMinimumHeight(button_height_for_font(widget, vertical_padding=10 if compact else 12))
        for widget in (
            self.discover_button,
            self.task_action_button,
            self.repair_button,
            self.dirty_label,
            self.save_button,
        ):
            self.actions_layout.removeWidget(widget)
        if compact:
            action_widgets = [
                widget for widget in (
                self.discover_button,
                self.task_action_button,
                self.repair_button,
                ) if not widget.isHidden()
            ]
            row = 0
            index = 0
            while index < len(action_widgets):
                widget = action_widgets[index]
                if widget is self.repair_button:
                    self.actions_layout.addWidget(widget, row, 0, 1, 2)
                    row += 1
                    index += 1
                    continue
                partner = action_widgets[index + 1] if index + 1 < len(action_widgets) else None
                pair_width = 0
                if partner is not None and partner is not self.repair_button:
                    pair_width = widget.sizeHint().width() + partner.sizeHint().width() + 6
                if partner is not None and pair_width <= max(1, self.width() - 12):
                    self.actions_layout.addWidget(widget, row, 0)
                    self.actions_layout.addWidget(partner, row, 1)
                    row += 1
                    index += 2
                else:
                    self.actions_layout.addWidget(widget, row, 0, 1, 2)
                    row += 1
                    index += 1
            # Keep hidden controls attached to the layout for a later context
            # publication, without reserving visible space for them.
            for widget in (
                self.discover_button,
                self.task_action_button,
                self.repair_button,
            ):
                if widget.isHidden():
                    self.actions_layout.addWidget(widget, row, 0)
            self.actions_layout.addWidget(self.dirty_label, row, 0)
            self.actions_layout.addWidget(self.save_button, row, 1)
            self.actions_layout.setColumnStretch(0, 1)
            self.actions_layout.setColumnStretch(1, 1)
        else:
            self.actions_layout.addWidget(self.discover_button, 0, 0)
            self.actions_layout.addWidget(self.task_action_button, 0, 1)
            self.actions_layout.addWidget(self.repair_button, 0, 2)
            self.actions_layout.setColumnStretch(3, 1)
            self.actions_layout.addWidget(self.dirty_label, 0, 5)
            self.actions_layout.addWidget(self.save_button, 0, 6)
        self._apply_button_roles()

    def _action_layout_requires_reflow(self) -> bool:
        """Use measured font widths instead of a fixed breakpoint."""
        actions = [
            widget for widget in (
                self.discover_button,
                self.task_action_button,
                self.repair_button,
            ) if not widget.isHidden()
        ]
        if not actions:
            actions = []
        natural = sum(widget.sizeHint().width() for widget in actions)
        if actions:
            natural += 6 * (len(actions) - 1)
        natural += self.dirty_label.sizeHint().width() + self.save_button.sizeHint().width() + 6
        return natural > max(1, self.width() - 12)

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
        self._operation_status_text = ""
        self._reference_summary = {}
        self._component_repair_available = False
        self._canonical_identity_managed = False
        self.identity_notice_label.hide()
        self.edit_configuration_button.hide()
        task = task_definition(self._family_key, self._task_key)
        previous_attention = getattr(self, 'configuration_attention_label', None)
        if previous_attention is not None and self.form.indexOf(previous_attention) < 0:
            previous_attention.hide()
            previous_attention.deleteLater()
        if self.form.rowCount() and self.form.itemAt(0).widget() is self.reference_summary_host:
            # Reparent before removing the row so Qt does not destroy the
            # persistent summary host while switching tasks.
            self.reference_summary_host.setParent(self)
            self.form.removeRow(0)
        self._clear_form(self.form)
        self.form_widget.setMinimumHeight(0)
        self.configuration_attention_label = None
        self._field_widgets = {}
        self._browse_buttons = {}

        if task is None:
            self.title_label.setText("Choose a configuration task")
            self.description_label.setText("")
        else:
            self.title_label.setText(task.title)
            self.description_label.setText(task.description)
            if self._family_key == "fast_light" and self._task_key == "flamp_signing":
                self.reference_summary_host.setParent(self.form_widget)
                self.form.addRow(self.reference_summary_host)
            self.configuration_attention_label = QLabel()
            self.configuration_attention_label.setWordWrap(True)
            self.configuration_attention_label.setTextFormat(Qt.PlainText)
            self.configuration_attention_label.setAccessibleName("Saved configuration attention")
            self.configuration_attention_label.hide()
            # Keep action-only tasks genuinely compact.  The attention row is
            # added lazily if the host publishes a real warning for such a
            # task; a permanently hidden form row otherwise defeats the
            # bounded no-field surface contract.
            if task.fields:
                self.form.addRow(self.configuration_attention_label)
            for field in task.fields:
                self._add_field(field)
        has_fields = bool(task and task.fields)
        has_editable_fields = bool(task and any(field.kind != "readonly" for field in task.fields))
        self.scroll.setVisible(has_fields)
        self.root_layout.setStretchFactor(self.scroll, 1 if has_fields else 0)
        self.root_layout.setAlignment(Qt.Alignment() if has_fields else Qt.AlignTop)
        identity = f"{family_title} for {self._radio_name}" if self._radio_name else f"All radios using {family_title}"
        self._status_identity_text = identity
        others = tuple(name for name in shared_radio_names if name and name != self._radio_name)
        self._status_shared_text = "Shared with: " + ", ".join(others) if others else ""
        self._runtime_status_text = str(status_text or "")
        self._render_runtime_status()
        self.task_action_button.setVisible(bool(task and task.action))
        self.discover_button.setVisible(
            bool(task and self._radio_id is not None and any(field.browse for field in task.fields))
        )
        if task:
            self.task_action_button.setText(task.action_label or "Open")
            self.task_action_button.setProperty("software_action", task.action)
        self._set_reference_summary_visible(self._family_key == "fast_light" and self._task_key == "flamp_signing")
        self.save_button.setText(f"Save {family_title} for {self._radio_name}" if self._radio_name else "Select one radio to save")
        self.dirty_label.setVisible(has_editable_fields)
        self.save_button.setVisible(has_editable_fields)
        self.save_button.setEnabled(self._radio_id is not None and has_editable_fields)
        self._refresh_dirty_label()
        self._update_repair_presentation()
        self._apply_button_roles()

    def set_configuration_attention(self, entry: Mapping[str, object]) -> None:
        """Explain cached saved-configuration evidence inside the content scroll."""
        label = getattr(self, "configuration_attention_label", None)
        if label is None:
            return
        text = f"Configuration: {entry.get('label', '')}. {entry.get('reason', '')}" if entry else ""
        if entry and entry.get("state") == "complete":
            text = "Saved settings complete; see Health for runtime checks."
        label.setToolTip(str(entry.get("reason", "")) if entry else "")
        task = task_definition(self._family_key, self._task_key)
        if text and self.form.indexOf(label) < 0:
            label.setParent(self.form_widget)
            self.form.addRow(label)
        if task and not task.fields:
            # Empty action pages stay compact. A real explanation gets the
            # bounded scroll owner so long warnings cannot hide the action.
            self.scroll.setVisible(bool(text))
            self.root_layout.setStretchFactor(self.scroll, 1 if text else 0)
            self.root_layout.setAlignment(Qt.Alignment() if text else Qt.AlignTop)
        if label.text() != text:
            label.setText(text)
        label.setVisible(bool(text))
        # Overview pages are read-only summaries.  When saved evidence says
        # review is required, place the canonical edit route beside the title
        # so the operator can act immediately without hunting through tasks.
        state = str(entry.get("state", "") or "").strip().lower() if entry else ""
        overview_edit = (
            bool(text)
            and state in {"review", "unsaved", "error", "unknown"}
            and self._task_key == "overview"
            and self._family_key in {"js8call", "fast_light", "varac"}
        )
        if not self._canonical_identity_managed:
            self.edit_configuration_button.setVisible(overview_edit)
        self._fit_action_attention()

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

        from freqinout.core.configuration_attention import REFERENCE_TASKS
        self._canonical_identity_managed = (
            bool(managed)
            and self._task_key not in REFERENCE_TASKS.get(self._family_key, ())
            and not (self._family_key == "varac" and self._task_key == "inbound_guard")
        )
        self._component_repair_available = bool(component_repair_available)
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
            self.identity_notice_label.setText(
                f"Saved settings for {self._radio_name or 'this radio'}. "
                "Choose Edit configuration… to make changes."
            )
            self.identity_notice_label.setToolTip("")
            self.discover_button.hide()
            self.save_button.hide()
            self.dirty_label.hide()
        else:
            self.edit_configuration_button.hide()
            task = task_definition(self._family_key, self._task_key)
            has_editable_fields = bool(
                task and any(field.kind != "readonly" for field in task.fields)
            )
            self.dirty_label.setVisible(has_editable_fields)
            self.save_button.setVisible(has_editable_fields)
            self.save_button.setEnabled(self._radio_id is not None and has_editable_fields)
        self._update_repair_presentation()
        self._apply_button_roles()

    def _update_repair_presentation(self) -> None:
        """Update the independent repair affordance on every state transition."""
        available = bool(self._component_repair_available)
        self.repair_button.setVisible(available)
        self.repair_reason_label.setVisible(available)
        if available:
            self.repair_reason_label.setText(
                "Repair available: FLMsg/FLAmp wiring is incomplete; saved identities stay unchanged."
            )
            self.repair_reason_label.setToolTip("")
            if not self._canonical_identity_managed and self._task_key == "flamp":
                self.identity_notice_label.setText(
                    "This radio has an older or incomplete Fast Light message-component identity. "
                    "Repair only FLMsg/FLAmp wiring while retaining the saved FLRig and FLDigi identity."
                )
                self.identity_notice_label.setToolTip("")
                self.identity_notice_label.show()
            # ``flamp`` was the pre-OCS-9 task alias. Keep its historical
            # repair action for callers that still address that alias while
            # the canonical ``flamp_signing`` task keeps signing management
            # and repair as independent actions.
            if self._task_key == "flamp":
                if self._canonical_identity_managed:
                    self.identity_notice_label.setText(
                        self.identity_notice_label.text()
                        + " A component-only repair is available below; it preserves the saved "
                        "FLRig and FLDigi identity."
                    )
                self.task_action_button.setText("Repair FLMsg / FLAmp components…")
                self.task_action_button.setProperty(
                    "software_action", "repair_fast_light_message_components"
                )
                self.task_action_button.setVisible(True)
        elif self._task_key == "flamp":
            # Retain the old alias route's visibility behavior, but clear its
            # repair action payload so a later click cannot emit stale repair.
            self.task_action_button.setText("")
            self.task_action_button.setProperty("software_action", "")
            self.task_action_button.setVisible(False)
            self.identity_notice_label.clear()
            self.identity_notice_label.hide()
        elif not self._canonical_identity_managed:
            self.identity_notice_label.clear()
            self.identity_notice_label.hide()
        self._layout_actions(compact=self._action_layout_requires_reflow())

    def _apply_button_roles(self) -> None:
        """Apply shared theme roles without introducing screen-local styling."""
        theme = self._theme
        for button, role in (
            # Save is the one affirmative action for every editable task.
            # Its enabled state still follows the existing radio/field policy;
            # the role must not turn gray merely because the draft is clean.
            (self.save_button, "primary"),
            (self.discover_button, "secondary"),
            (self.task_action_button, "secondary"),
            (self.repair_button, "secondary"),
            (self.edit_configuration_button, "secondary"),
            (self.edit_flamp_arq_button, "secondary"),
        ):
            button.setProperty("button_role", role)
            button.setStyleSheet(button_style(role, theme))
        for button in self._browse_buttons.values():
            button.setProperty("button_role", "secondary")
            button.setStyleSheet(button_style("secondary", theme))

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        """Publish shared theme tokens without changing draft or dirty state."""
        palette = active_app_theme()
        if theme:
            palette.update(dict(theme))
        self._theme = palette
        self._apply_button_roles()
        self._fit_action_attention()

    def set_theme(self, theme: Mapping[str, str] | None = None) -> None:
        """Compatibility alias for hosts that publish themes by setter name."""
        self.apply_theme(theme)

    def _set_reference_summary_visible(self, visible: bool) -> None:
        self.reference_summary_host.setVisible(bool(visible))
        self.reference_summary_frame.setVisible(bool(visible))
        if visible:
            self._render_reference_summary()

    @staticmethod
    def _display_reference(value: Any) -> str:
        if isinstance(value, Mapping):
            for key in ("display", "summary", "value", "label", "name"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return str(candidate)
            return "Unknown"
        text = str(value or "").strip()
        return text or "Unknown"

    def _render_reference_summary(self) -> None:
        summary = self._reference_summary
        self.signing_summary_label.setText(
            self._display_reference(summary.get("signing_identity", "Unknown"))
        )
        self.arq_summary_label.setText(
            self._display_reference(summary.get("arq_endpoint", "Unknown"))
        )

    def set_reference_summary(self, mapping: Mapping[str, Any] | None) -> None:
        """Publish host-owned signing/ARQ display facts without changing the draft."""
        self._reference_summary = deepcopy(dict(mapping or {}))
        self._set_reference_summary_visible(
            self._family_key == "fast_light" and self._task_key == "flamp_signing"
        )

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
        self._apply_button_roles()
        self.value_changed.emit(key, value)

    def _refresh_dirty_label(self) -> None:
        self.dirty_label.setText("Unsaved changes" if self._dirty else "No unsaved changes")
        self.dirty_label.setProperty("dirty", self._dirty)

    def set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._refresh_dirty_label()
        self._apply_button_roles()

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
        """Show an operation/validation result even when embedded context is hidden."""
        self._operation_status_text = str(text or "")
        self._render_runtime_status()
        self.status_label.setVisible(bool(self._operation_status_text) or not self._embedded_context)

    def _render_runtime_status(self) -> None:
        if self._operation_status_text:
            self.status_label.setText(self._operation_status_text)
            self.status_label.setVisible(True)
            return
        if self._embedded_context:
            self.status_label.clear()
            self.status_label.hide()
            return
        parts = [self._status_identity_text]
        if self._runtime_status_text:
            parts.append(f"Status: {self._runtime_status_text}")
        if self._status_shared_text:
            parts.append(self._status_shared_text)
        self.status_label.setText(" · ".join(part for part in parts if part))

    def set_runtime_status(self, text: str) -> None:
        """Refresh cached runtime status without touching draft widgets."""
        self._runtime_status_text = str(text or "")
        self._render_runtime_status()

    def set_embedded_context(self, embedded: bool) -> None:
        """Hide duplicate identity/runtime context when a workspace owns it.

        Operation and validation results remain visible because they explain the
        focused editor's current action. Standalone editors retain the existing
        identity and cached runtime summary.
        """
        self._embedded_context = bool(embedded)
        self._render_runtime_status()
        self.status_label.setVisible(
            bool(self._operation_status_text)
            or (not self._embedded_context and self.height() >= 240)
        )

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


class FastLightOverview(QWidget):
    """Cache-only overview of the four Fast Light components.

    The Settings host publishes the rows through :meth:`set_component_summary`.
    Rendering and task navigation only consume that bounded snapshot; this
    widget never discovers files, opens endpoints, reads keyrings, or reloads a
    store.
    """

    task_navigation_requested = Signal(str)
    details_requested = Signal(str, object)
    _COMPONENT_LABELS = ("FLRig", "FLDigi", "FLMsg", "FLAmp")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._component_rows: tuple[dict[str, Any], ...] = ()
        self._component_details: dict[str, object] = {}
        self._theme: dict[str, str] = active_app_theme()
        self.title_label = QLabel("Fast Light overview")
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.description_label = QLabel(
            "Review the saved component identity and published status for this radio."
        )
        self.description_label.setWordWrap(True)
        self.attention_label = QLabel()
        self.attention_label.setObjectName("fastLightOverviewAttention")
        self.attention_label.setWordWrap(True)
        self.attention_label.setAccessibleName("Fast Light configuration attention")
        self.attention_label.hide()
        self.component_table = QTableWidget(0, 6)
        self.component_table.setHorizontalHeaderLabels(
            ("Component", "Use", "Settings folder / launch", "Status", "Configure", "Details")
        )
        self.component_table.setObjectName("fastLightComponentTable")
        self.component_table.setAccessibleName("Fast Light component summary")
        self.component_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.component_table.setSelectionMode(QTableWidget.NoSelection)
        self.component_table.setFocusPolicy(Qt.NoFocus)
        self.component_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.component_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.component_table.setWordWrap(True)
        self.component_table.setTextElideMode(Qt.ElideNone)
        self.component_table.verticalHeader().setVisible(False)
        self._initial_scroll_reset_timer = QTimer(self)
        self._initial_scroll_reset_timer.setSingleShot(True)
        self._initial_scroll_reset_timer.timeout.connect(self._reset_horizontal_scroll)
        header = self.component_table.horizontalHeader()
        header.setStretchLastSection(False)
        # Keep the overview readable in the narrow Settings detail pane. The
        # table owns horizontal overflow locally, so identity/status text and
        # both actions retain usable widths without widening the page. Actual
        # widths are derived from the active font and content below.
        header.setSectionResizeMode(QHeaderView.Interactive)
        self._apply_column_metrics()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.addWidget(self.title_label, 1)
        self.review_configuration_button = QPushButton("Review setup…")
        self.review_configuration_button.setAccessibleName("Review Fast Light launch choices")
        self.review_configuration_button.setStyleSheet(button_style("secondary", self._theme))
        self.review_configuration_button.clicked.connect(lambda: self.task_navigation_requested.emit("launch"))
        self.review_configuration_button.hide()
        heading.addWidget(self.review_configuration_button)
        layout.addLayout(heading)
        layout.addWidget(self.description_label)
        layout.addWidget(self.attention_label)
        layout.addWidget(self.component_table)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.set_component_summary(())

    def _apply_column_metrics(self) -> None:
        """Size summary columns from active font/content metrics only."""
        header = self.component_table.horizontalHeader()
        metrics = QFontMetrics(self.component_table.font())

        def text_width(values: tuple[str, ...], padding: int = 24) -> int:
            return max(metrics.horizontalAdvance(value) for value in values) + padding

        widths = (
            text_width(("Component", *self._COMPONENT_LABELS)),
            text_width(("Use", "Unknown", "Not selected")),
            text_width(("Settings folder / launch", "Default settings / CommsPlan"), 28),
            text_width(("Status", "Ready", "Needs review", "Unavailable")),
            max(text_width(("Configure",)), self._action_width("Configure")),
            max(text_width(("Details",)), self._action_width("Details")),
        )
        header.setMinimumSectionSize(metrics.horizontalAdvance("WW") + 16)
        for column, width in enumerate(widths):
            header.resizeSection(column, width)

    def _action_width(self, text: str) -> int:
        button = self.component_table.cellWidget(0, 4 if text == "Configure" else 5)
        if button is not None:
            return button.sizeHint().width() + 16
        return QFontMetrics(self.component_table.font()).horizontalAdvance(text) + 32

    @staticmethod
    def _row_value(row: Mapping[str, Any], key: str, fallback: Any = "") -> Any:
        value = row.get(key, fallback)
        return fallback if value is None else value

    def set_component_summary(self, rows: Any) -> None:
        """Replace the bounded published component table without side effects."""
        by_label: dict[str, Mapping[str, Any]] = {}
        if isinstance(rows, Mapping):
            mapped_rows: list[Mapping[str, Any]] = []
            for key, value in rows.items():
                if isinstance(value, Mapping):
                    row = dict(value)
                    row.setdefault("label", key)
                    mapped_rows.append(row)
            iterable = mapped_rows
        elif isinstance(rows, (list, tuple)):
            iterable = rows
        else:
            iterable = ()
        for raw in iterable:
            if not isinstance(raw, Mapping):
                continue
            label = str(raw.get("label", "")).strip()
            if label:
                by_label[label.casefold()] = raw

        normalized: list[dict[str, Any]] = []
        for label in self._COMPONENT_LABELS:
            raw = by_label.get(label.casefold(), {})
            enabled_raw = raw.get("enabled", None)
            enabled = None if enabled_raw is None else bool(enabled_raw)
            identity = self._display_value(self._row_value(raw, "identity", "Unknown"))
            status = self._display_value(self._row_value(raw, "status", "Unknown"))
            task_key = str(self._row_value(raw, "task_key", "")).strip()
            identity_detail = self._display_value(self._row_value(raw, "identity_detail", ""))
            item = {
                "label": label,
                "enabled": enabled,
                "identity": identity,
                "status": status,
                "task_key": task_key,
                "identity_detail": identity_detail,
                "launch_owner": str(raw.get("launch_owner") or ""),
            }
            normalized.append(item)

        normalized_rows = tuple(normalized)
        if normalized_rows == self._component_rows:
            return

        for row_index, item in enumerate(normalized_rows):
            label = item["label"]
            enabled = item["enabled"]
            identity = item["identity"]
            status = item["status"]
            task_key = item["task_key"]
            identity_detail = item["identity_detail"]
            availability = "Unknown" if enabled is None else ("Use" if enabled else "Not selected")
            if self.component_table.rowCount() <= row_index:
                self.component_table.insertRow(row_index)
                self.component_table.setItem(row_index, 0, QTableWidgetItem(label))
                self.component_table.setItem(row_index, 1, QTableWidgetItem())
                self.component_table.setItem(row_index, 2, QTableWidgetItem())
                self.component_table.setItem(row_index, 3, QTableWidgetItem())
                configure = QPushButton("Configure")
                configure.setAccessibleName(f"Configure {label}")
                configure.setProperty("button_role", "secondary")
                configure.setStyleSheet(button_style("secondary", self._theme))
                configure.clicked.connect(
                    lambda _checked=False, index=row_index: self._emit_component_navigation(index)
                )
                self.component_table.setCellWidget(row_index, 4, configure)
                details = QPushButton("Details")
                details.setAccessibleName(f"Show {label} saved identity details")
                details.setProperty("button_role", "secondary")
                details.setStyleSheet(button_style("secondary", self._theme))
                details.clicked.connect(
                    lambda _checked=False, index=row_index: self._emit_component_details(index)
                )
                self.component_table.setCellWidget(row_index, 5, details)
            self.component_table.item(row_index, 1).setText(availability)
            self.component_table.item(row_index, 2).setText("\n".join(part for part in (identity, item["launch_owner"]) if part))
            self.component_table.item(row_index, 2).setToolTip(identity_detail or identity)
            self.component_table.item(row_index, 3).setText(status)
            self.component_table.item(row_index, 3).setToolTip(status)
            configure = self.component_table.cellWidget(row_index, 4)
            if configure is not None:
                configure.setEnabled(bool(task_key))
                configure.setMinimumHeight(button_height_for_font(configure))
            details = self.component_table.cellWidget(row_index, 5)
            if details is not None:
                details.setEnabled(bool(identity_detail and identity_detail != "Unknown"))
                details.setMinimumHeight(button_height_for_font(details))
        # Keep the row/widget identities stable across cache publications.
        while self.component_table.rowCount() > len(self._COMPONENT_LABELS):
            self.component_table.removeRow(self.component_table.rowCount() - 1)
        self._component_rows = normalized_rows
        self._component_details = {
            str(item["label"]): item["identity_detail"] for item in normalized_rows
        }
        self._apply_column_metrics()
        self.component_table.resizeRowsToContents()
        self._apply_font_derived_height()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.setFocus(Qt.OtherFocusReason)
        # QTableWidget may auto-reveal the last Details button while its
        # child controls are polished. Start each newly shown overview at the
        # component/name columns; operators can still use the local scrollbar
        # to reach Configure and Details.
        self._initial_scroll_reset_timer.start(0)

    def _reset_horizontal_scroll(self) -> None:
        """Return a newly shown overview to its readable leading columns."""
        if not self.isVisible():
            return
        self.component_table.horizontalScrollBar().setValue(0)

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, Mapping):
            for key in ("display", "summary", "value", "label", "name"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return str(candidate)
            return "Unknown"
        text = str(value or "").strip()
        return text or "Unknown"

    def component_summary(self) -> tuple[dict[str, Any], ...]:
        return deepcopy(self._component_rows)

    def _apply_font_derived_height(self) -> None:
        # The table owns its local vertical overflow. Do not force all four
        # rows into the parent page's minimum height on compact windows.
        for row in range(self.component_table.rowCount()):
            button = self.component_table.cellWidget(row, 4)
            if button is not None:
                button.setMinimumHeight(button_height_for_font(button))
        self.component_table.setMinimumHeight(0)

    def _emit_component_navigation(self, row_index: int) -> None:
        if 0 <= row_index < len(self._component_rows):
            task_key = str(self._component_rows[row_index].get("task_key", "")).strip()
            if task_key:
                self.task_navigation_requested.emit(task_key)

    def _emit_component_details(self, row_index: int) -> None:
        if 0 <= row_index < len(self._component_rows):
            item = self._component_rows[row_index]
            self.details_requested.emit(str(item.get("label", "")), item.get("identity_detail", ""))

    def set_configuration_attention(self, entry: Mapping[str, object] | None) -> None:
        """Show a compact saved-state reason without exposing raw paths."""
        self.review_configuration_button.setVisible(bool(entry) and str(entry.get("state", "")) in {"unknown", "review", "error"})
        if not entry:
            self.attention_label.clear()
            self.attention_label.hide()
            return
        label = str(entry.get("label", "Review")).strip() or "Review"
        reason = str(entry.get("reason", "")).strip()
        self.attention_label.setText(f"{label}: {reason}" if reason else label)
        self.attention_label.setToolTip(reason)
        self.attention_label.show()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Four rows are bounded and this synchronous recalculation does not
        # recurse into the parent layout. It keeps wrapped identities and
        # Configure hit targets aligned after a real viewport-width change.
        self.component_table.resizeRowsToContents()
        self._apply_font_derived_height()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._apply_column_metrics()
            self.component_table.resizeRowsToContents()
            self._apply_font_derived_height()

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        """Refresh shared button roles and font-derived rows cache-only."""
        palette = active_app_theme()
        if theme:
            palette.update(dict(theme))
        self._theme = palette
        self.review_configuration_button.setStyleSheet(button_style("secondary", palette))
        for row in range(self.component_table.rowCount()):
            button = self.component_table.cellWidget(row, 4)
            if button is not None:
                button.setStyleSheet(button_style("secondary", palette))
            details = self.component_table.cellWidget(row, 5)
            if details is not None:
                details.setStyleSheet(button_style("secondary", palette))
        self._apply_column_metrics()
        self.component_table.resizeRowsToContents()
        self._apply_font_derived_height()

    def set_theme(self, theme: Mapping[str, str] | None = None) -> None:
        """Compatibility alias used by hosts that name theme publication directly."""
        self.apply_theme(theme)


__all__ = [
    "SOFTWARE_EDITOR_TASKS",
    "SoftwareEditorField",
    "SoftwareEditorTask",
    "FastLightOverview",
    "SoftwareTaskEditor",
    "merge_draft_value",
    "task_definition",
]
