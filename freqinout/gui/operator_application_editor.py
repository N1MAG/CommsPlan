"""Small, cache-only application configuration editor.

The editor deliberately owns no persistence or discovery work.  Host code can
connect the browse/find signals to an adapter and can publish a saved draft
back with :meth:`set_values` after its own transaction has completed.
"""

from __future__ import annotations

import shlex
from copy import deepcopy
from typing import Any, Mapping

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from freqinout.gui.theme import (
    active_app_theme,
    ThemedCheckBox,
    button_height_for_font,
    button_style,
    control_height_for_font,
    fit_child_combo_boxes,
    label_style,
)
from freqinout.core.operator_application_configuration import (
    operator_launch_recipe, js8_profile_path_review_token, js8_profile_paths_need_review,
    JS8_REVIEW_PATHS,
)


_COMPONENTS = ("flrig", "fldigi", "flmsg", "flamp")

_TASK_SCOPE_FIELDS: dict[str, tuple[str, ...]] = {
    "flrig_control": (
        "settings_identity_mode", "host", "port", "flrig_application_path",
        "flrig_configuration_path", "flrig_launch_control", "flrig_launch_at_startup",
    ),
    "fldigi_modem_logs": (
        "settings_identity_mode", "fldigi_host", "secondary_port", "storage_path",
        "secondary_storage_path", "fldigi_application_path", "fldigi_configuration_path",
        "fldigi_launch_control", "fldigi_launch_at_startup",
    ),
    "flmsg": (
        "settings_identity_mode", "flmsg_application_path", "flmsg_message_path",
        "flmsg_configuration_path", "flmsg_launch_control", "flmsg_launch_at_startup",
    ),
    "flamp_signing": (
        "settings_identity_mode", "flamp_application_path", "flamp_receive_path",
        "flamp_configuration_path", "flamp_launch_control", "flamp_launch_at_startup",
    ),
}
_TASK_SCOPE_COMPONENTS = {
    "flrig_control": ("flrig",),
    "fldigi_modem_logs": ("fldigi",),
    "flmsg": ("flmsg",),
    "flamp_signing": ("flamp",),
}


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _family_label(family_key: str) -> str:
    labels = {
        "js8call": "JS8Call",
        "fast_light": "Fast Light",
        "varac": "VarAC",
        "flrig": "FLRig",
        "fldigi": "FLDigi",
        "flmsg": "FLMsg",
        "flamp": "FLAmp",
    }
    key = str(family_key or "application").strip().lower()
    return labels.get(key, key.replace("_", " ").title())


class ApplicationSetupEditor(QWidget):
    """Edit one application's operator-facing configuration draft.

    ``family_key`` is normally ``js8call``, ``fast_light`` or ``varac``.  The
    widget accepts arbitrary extra values and carries them through unchanged;
    this lets an owner add fields without making this UI an authority for
    storage schema or launch policy.
    """

    value_changed = Signal(str, object)
    save_requested = Signal(object)
    cancel_requested = Signal()
    browse_requested = Signal(str)
    find_requested = Signal()
    full_family_review_requested = Signal()

    def __init__(
        self,
        family_key: str,
        radio_name: str,
        initial_values: Mapping[str, Any] | None = None,
        *,
        visible_components: tuple[str, ...] | list[str] | None = None,
        visible_fields: tuple[str, ...] | list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.family_key = str(family_key or "application").strip().lower()
        self.radio_name = str(radio_name or "").strip()
        self._visible_components = (
            None if visible_components is None else {
                str(value).strip().lower() for value in visible_components if str(value).strip()
            }
        )
        self._visible_fields = (
            None if visible_fields is None else {
                str(value).strip() for value in visible_fields if str(value).strip()
            }
        )
        self._loading = False
        self._busy = False
        self._dirty = False
        self._field_widgets: dict[str, QWidget] = {}
        self._field_labels: dict[str, QLabel] = {}
        self._browse_buttons: dict[str, QPushButton] = {}
        self._component_checks: dict[str, QCheckBox] = {}
        self._component_startup_checks: dict[str, QCheckBox] = {}
        self._component_paths: dict[str, QLineEdit] = {}
        self._component_blocks: dict[str, QWidget] = {}
        self._component_grids: dict[str, QGridLayout] = {}
        self._component_policy_widgets: dict[str, tuple[QLabel, QComboBox, QCheckBox]] = {}
        self._data: dict[str, Any] = {}
        self._invalid_spin_values: dict[str, object] = {}
        self._baseline: dict[str, Any] = {}
        self._status_before_busy = "Saved"
        self._component_layout_timer = QTimer(self)
        self._component_layout_timer.setSingleShot(True)
        self._component_layout_timer.timeout.connect(self._update_fast_light_component_layouts)

        self.setObjectName("operatorApplicationEditor")
        self.setAccessibleName("Application setup editor")
        self.setMinimumSize(360, 220)
        self._build_ui()
        self._apply_field_scope()
        self._apply_component_scope()
        self._update_fast_light_scope_ui()
        self.set_values(initial_values or {}, status="Saved", preserve_dirty=False)
        self.apply_theme()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._sync_form_scroll_geometry()
        self._update_fast_light_component_layouts()
        self._component_layout_timer.start(0)

    # ----- public draft/status interface ---------------------------------

    def values(self) -> dict[str, Any]:
        """Return a detached snapshot of the current visible draft."""

        self._sync_controls_to_data()
        return deepcopy(self._data)

    def add_form_context(self, widget: QWidget) -> None:
        """Keep optional context in the field scroll, above the fixed footer."""
        self._content_layout.insertWidget(0, widget)

    def set_values(
        self,
        values: Mapping[str, Any] | None,
        status: str = "Saved",
        *,
        preserve_dirty: bool = True,
        force: bool = False,
    ) -> bool:
        """Publish values from an owner without trampling an active draft.

        The default protects local edits.  Callers that have explicitly
        resolved the replacement may use ``preserve_dirty=False`` or
        ``force=True``.  No signal is emitted for a host publication.
        """

        if self._dirty and preserve_dirty and not force:
            return False
        incoming = deepcopy(dict(values or {}))
        self._data = self._with_defaults(incoming)
        self._baseline = deepcopy(self._data)
        self._invalid_spin_values.clear()
        self._loading = True
        try:
            self._load_controls_from_data()
        finally:
            self._loading = False
        self._dirty = False
        self.set_status(status)
        self._refresh_dirty_label()
        return True

    def set_status(self, text: str) -> None:
        self.status_label.setText(_text(text))
        self.status_label.setToolTip(_text(text))
        self.status_label.setVisible(bool(_text(text).strip()) and _text(text).strip().casefold() != 'saved')

    def set_busy(self, busy: bool) -> None:
        """Reflect an owner operation while keeping the draft intact."""

        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if hasattr(self, 'profile_paths_ack'):
            self.profile_paths_ack.setEnabled(not busy)
        if self._busy:
            self._status_before_busy = self.status_label.text()
            self.set_status("Working…")
        else:
            self.set_status(self._status_before_busy)
        for widget in (*self._field_widgets.values(), *self._component_checks.values(), self.more_button):
            widget.setEnabled(not self._busy)
        for button in (*self._browse_buttons.values(), self.find_button, self.save_button, self.cancel_button):
            button.setEnabled(not self._busy)
        self._update_identity_visibility()

    def is_busy(self) -> bool:
        return self._busy

    def is_dirty(self) -> bool:
        return self._dirty

    def set_configuration_attention(self, entry: Mapping[str, Any] | None) -> None:
        """Publish the saved review reason separately from Save/progress errors."""
        entry = entry or {}
        reason = str(entry.get("reason") or "").strip()
        state = str(entry.get("state") or "")
        visible = bool(reason) and state in {"review", "error", "unknown"}
        self.configuration_attention_label.setText(reason)
        self.configuration_attention_label.setVisible(visible)

    def set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._refresh_dirty_label()

    def field_widget(self, key: str) -> QWidget | None:
        return self._field_widgets.get(str(key))

    def field_keys(self) -> tuple[str, ...]:
        return tuple(self._field_widgets)

    def set_view_scope(
        self,
        *,
        visible_components: tuple[str, ...] | list[str] | None = None,
        visible_fields: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Set the presentation scope without dropping any draft values.

        Hosts can use this for a component editor (for example FLRig) while
        still receiving the complete atomic payload from :meth:`values`.
        The scope only affects visibility; it never clears or rewrites data.
        Call it before opening the editor for the cleanest layout transition.
        """
        self._visible_components = (
            None if visible_components is None else {
                str(value).strip().lower() for value in visible_components if str(value).strip()
            }
        )
        self._visible_fields = (
            None if visible_fields is None else {
                str(value).strip() for value in visible_fields if str(value).strip()
            }
        )
        self._apply_field_scope()
        self._apply_component_scope()
        self._update_fast_light_scope_ui()

    def set_task_scope(self, task_key: str | None) -> None:
        """Show the smallest useful editor surface for a named task.

        This is a presentation-only scope.  The complete draft remains in
        :meth:`values`, so a component editor can be used without losing the
        other Fast Light components or their launch identities.
        """
        key = str(task_key or "").strip().lower()
        self.set_view_scope(
            visible_components=_TASK_SCOPE_COMPONENTS.get(key),
            visible_fields=_TASK_SCOPE_FIELDS.get(key),
        )

    def _set_field_visible(self, key: str, visible: bool) -> None:
        widget = self._field_widgets.get(key)
        if widget is None:
            return
        browse = self._browse_buttons.get(key)
        row = browse.parentWidget() if browse is not None else widget.parentWidget()
        if browse is not None and row is not None:
            row.setVisible(bool(visible))
            browse.setVisible(bool(visible))
        widget.setVisible(bool(visible))
        label = self._field_labels.get(key)
        if label is not None:
            label.setVisible(bool(visible))

    def _apply_component_scope(self) -> None:
        if self.family_key != "fast_light":
            return
        visible = self._visible_components if self._visible_components is not None else set(_COMPONENTS)
        for component, block in self._component_blocks.items():
            block.setVisible(component in visible)
        for component in _COMPONENTS:
            allowed = component in visible
            path_key = f"{component}_configuration_path"
            if not allowed or self._visible_fields is None:
                self._set_field_visible(path_key, allowed)

    def _is_fast_light_component_scope(self) -> bool:
        if self.family_key != "fast_light" or self._visible_components is None:
            return False
        return self._visible_components != set(_COMPONENTS)

    def _update_fast_light_scope_ui(self) -> None:
        """Guard the family-wide settings-folder mode in a component view."""
        combo = getattr(self, "settings_identity_combo", None)
        note = getattr(self, "fast_light_scope_note", None)
        review = getattr(self, "review_all_components_button", None)
        if combo is None or note is None or review is None:
            return
        mode_visible = self._visible_fields is None or "settings_identity_mode" in self._visible_fields
        scoped = self._is_fast_light_component_scope() and mode_visible
        combo.setEnabled(not scoped and not self._busy)
        note.setVisible(scoped)
        review.setVisible(scoped)
        if scoped:
            note.setText(
                "Settings-folder mode applies to all Fast Light components. "
                "The selected component folder is used only in Separate folder mode."
            )
            combo.setToolTip("Review all Fast Light components before changing this shared setting.")
        else:
            combo.setToolTip("Choose the settings-folder mode for the Fast Light family.")

    def _apply_field_scope(self) -> None:
        if self._visible_fields is None:
            for key in self._field_widgets:
                self._set_field_visible(key, True)
            return
        for key, widget in self._field_widgets.items():
            visible = key in self._visible_fields
            browse = self._browse_buttons.get(key)
            row = browse.parentWidget() if browse is not None else widget.parentWidget()
            # Browse rows own the field and button; hiding the row preserves
            # their pairing and keeps the full values in the draft.
            if browse is not None and row is not None:
                row.setVisible(visible)
            widget.setVisible(visible)
            if browse is not None:
                browse.setVisible(visible)
            label = self._field_labels.get(key)
            if label is not None:
                label.setVisible(visible)

    # ----- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        heading = QLabel(f"{_family_label(self.family_key)} · {self.radio_name or 'Selected radio'}")
        heading.setObjectName("operatorApplicationHeading")
        heading.setAccessibleName("Application and radio context")
        heading.setStyleSheet(label_style("text", active_app_theme(), weight=700))
        outer.addWidget(heading)

        self.status_label = QLabel("Saved")
        self.status_label.setObjectName("operatorApplicationStatus")
        self.status_label.setAccessibleName("Application setup status")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setObjectName("operatorApplicationScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        scroll.setAccessibleName("Application configuration fields")
        scroll.setMinimumSize(0, 0)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content = QWidget()
        content.setObjectName("operatorApplicationFormContent")
        content.setMinimumSize(0, 0)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(2, 2, 2, 2)
        self._content_layout.setSpacing(8)
        scroll.setWidget(content)
        # Keep a named handle for host/layout tests and for future focus
        # routing.  The form scroll owns only the fields; the footer remains
        # reachable while the operator reviews a long form.
        self.form_scroll = scroll
        outer.addWidget(scroll, 1)

        self.configuration_attention_label = QLabel()
        self.configuration_attention_label.setObjectName("applicationConfigurationAttention")
        self.configuration_attention_label.setWordWrap(True)
        self.configuration_attention_label.setTextFormat(Qt.PlainText)
        self.configuration_attention_label.setAccessibleName("Saved configuration review reason")
        self.configuration_attention_label.hide()
        self._content_layout.addWidget(self.configuration_attention_label)

        self._normal_group = QGroupBox("Application")
        self._normal_group.setObjectName("operatorApplicationNormalGroup")
        self._normal_form = QFormLayout(self._normal_group)
        self._normal_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._normal_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._normal_form.setHorizontalSpacing(10)
        self._normal_form.setVerticalSpacing(6)
        self._content_layout.addWidget(self._normal_group)
        self._build_normal_form()

        self.more_button = QPushButton("More settings…")
        self.more_button.setObjectName("operatorApplicationMoreButton")
        self.more_button.setAccessibleName("Show more application settings")
        self.more_button.setCheckable(True)
        self.more_button.toggled.connect(self._set_more_visible)
        self._content_layout.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.more_group = QGroupBox("More settings")
        self.more_group.setObjectName("operatorApplicationMoreGroup")
        self.more_form = QFormLayout(self.more_group)
        self.more_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.more_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.more_form.setHorizontalSpacing(10)
        self.more_form.setVerticalSpacing(6)
        self.more_group.setVisible(False)
        self._content_layout.addWidget(self.more_group)
        self._build_more_form()
        self._content_layout.addStretch(1)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.dirty_label = QLabel("Saved")
        self.dirty_label.setObjectName("operatorApplicationDirty")
        self.dirty_label.setAccessibleName("Application setup edit state")
        footer.addWidget(self.dirty_label, 1)
        self.find_button = QPushButton("Find installed…")
        self.find_button.setObjectName("operatorApplicationFindButton")
        self.find_button.setAccessibleName("Find installed applications")
        self.find_button.clicked.connect(self.find_requested.emit)
        footer.addWidget(self.find_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("operatorApplicationCancelButton")
        self.cancel_button.setAccessibleName("Cancel application setup")
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        footer.addWidget(self.cancel_button)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("operatorApplicationSaveButton")
        self.save_button.setAccessibleName("Save application setup")
        self.save_button.clicked.connect(self._emit_save)
        footer.addWidget(self.save_button)
        outer.addLayout(footer)

    def _sync_form_scroll_geometry(self) -> None:
        """Keep the field viewport shrinkable without clipping its content.

        The editor is embedded both in the Settings workspace and in the
        instance assistant.  Those hosts can reduce its height substantially
        at compact sizes.  A zero floor lets the local scroll area own the
        overflow instead of propagating a stale child hint to the host.
        """

        scroll = getattr(self, "form_scroll", None)
        if scroll is None:
            return
        scroll.setMinimumHeight(0)
        content = scroll.widget()
        if content is not None:
            # Let widgetResizable shrink the form to the viewport.  Mirroring
            # even the current viewport into a child minimum can resurrect a
            # larger stale form hint and create hidden horizontal overflow.
            content.setMinimumWidth(0)
            content.setMinimumHeight(0)
            content.updateGeometry()
        scroll.updateGeometry()

    def _build_normal_form(self) -> None:
        family = self.family_key
        if family != "fast_light":
            self._add_path(self._normal_form, "application_path", "Application / launcher", "Choose an executable or launcher")
        if family == "js8call":
            self.settings_identity_combo = self._add_combo(
                self._normal_form,
                "settings_identity_mode",
                "Launches as",
                (("Default profile", "default"), ("Named profile…", "isolated"), ("Use existing launcher", "custom_launcher")),
            )
            self.settings_identity_combo.currentIndexChanged.connect(self._identity_changed)
            launch_combo = self._add_combo(
                self._normal_form,
                "launch_control",
                "Launch control",
                (("CommsPlan launches", "cp"), ("I start the application", "operator"), ("Remote application", "remote")),
            )
            launch_combo.currentIndexChanged.connect(lambda _index, widget=launch_combo: self._launch_control_changed(widget.currentData()))
            # A native profile name is part of the launch identity, so it is
            # revealed beside the selector instead of being hidden in the
            # advanced section.  The CP display label remains independent.
            self._add_path(self._normal_form, "rig_name", "Named profile", "Actual JS8 profile name", browse=False)
            self._add_path(self._normal_form, "host", "Host", "127.0.0.1", browse=False)
            self._add_path(self._normal_form, "port", "API port", "Port", browse=False)
            self.launch_preview_label = QLabel()
            self.launch_preview_label.setWordWrap(True)
            self.launch_preview_label.setObjectName("operatorApplicationLaunchPreview")
            self.launch_preview_label.setAccessibleName("JS8Call launch and message paths")
            self._normal_form.addRow(self.launch_preview_label)
            self.profile_paths_ack = ThemedCheckBox("Keep reviewed paths")
            self.profile_paths_ack.setObjectName("js8ProfilePathsAcknowledged")
            self.profile_paths_ack.setAccessibleName("Use the reviewed message and readback paths with this native profile")
            self.profile_paths_ack.setToolTip("Keep the message and readback paths shown above when this native JS8Call profile changes.")
            self.profile_paths_ack.toggled.connect(self._acknowledge_profile_paths)
            self._normal_form.addRow(self.profile_paths_ack)
        elif family == "fast_light":
            self.settings_identity_combo = self._add_combo(
                self._normal_form,
                "settings_identity_mode",
                "Settings folder",
                (("Default settings", "default"), ("Separate folder", "separatefolder")),
            )
            self.fast_light_scope_note = QLabel()
            self.fast_light_scope_note.setObjectName("fastLightSharedSettingsNote")
            self.fast_light_scope_note.setWordWrap(True)
            self.fast_light_scope_note.setAccessibleName("Fast Light shared settings-folder explanation")
            self._normal_form.addRow(self.fast_light_scope_note)
            self.review_all_components_button = QPushButton("Review all components…")
            self.review_all_components_button.setObjectName("fastLightReviewAllComponents")
            self.review_all_components_button.setAccessibleName("Review all Fast Light components")
            self.review_all_components_button.setToolTip("Expand this component editor to the complete Fast Light family.")
            self.review_all_components_button.clicked.connect(self._review_all_fast_light_components)
            self._normal_form.addRow(self.review_all_components_button)
            self._build_fast_light_components()
            self._add_path(self._normal_form, "host", "FLRig host", "127.0.0.1", browse=False)
            self._add_path(self._normal_form, "fldigi_host", "FLDigi host", "127.0.0.1", browse=False)
            self._add_path(self._normal_form, "port", "Control port", "Port", browse=False)
            self._add_path(self._normal_form, "secondary_port", "FLDigi API port", "Port", browse=False)
        elif family == "varac":
            launch_combo = self._add_combo(
                self._normal_form,
                "launch_control",
                "Launch control",
                (("CommsPlan launches", "cp"), ("I start the application", "operator"), ("Remote application", "remote")),
            )
            launch_combo.currentIndexChanged.connect(lambda _index, widget=launch_combo: self._launch_control_changed(widget.currentData()))
            self._add_path(self._normal_form, "configuration_path", "Existing VarAC INI", "Choose an existing INI file")
            self.launch_preview_label = QLabel()
            self.launch_preview_label.setObjectName("operatorApplicationLaunchPreview")
            self.launch_preview_label.setAccessibleName("VarAC launch preview")
            self.launch_preview_label.setWordWrap(True)
            self._normal_form.addRow(QLabel("Launches as"), self.launch_preview_label)
        else:
            self._add_path(self._normal_form, "port", "Port", "Port", browse=False)
        if family != "fast_light":
            self._add_startup_row(self._normal_form)

    def _build_fast_light_components(self) -> None:
        for component in _COMPONENTS:
            # Keep the common path action and launch policy in two compact
            # rows.  The policy row is reflowed below the normal width
            # breakpoint so the full startup action never competes with the
            # launch-control selector for a narrow cell.
            block = QWidget()
            block.setObjectName(f"operatorApplicationComponentBlock_{component}")
            block_layout = QGridLayout(block)
            block_layout.setContentsMargins(0, 4, 0, 4)
            block_layout.setHorizontalSpacing(6)
            block_layout.setVerticalSpacing(4)
            block_layout.setColumnStretch(1, 1)
            self._component_blocks[component] = block
            self._component_grids[component] = block_layout

            check = ThemedCheckBox(f"Use {component.upper()}")
            check.setObjectName(f"operatorApplicationComponent_{component}")
            check.setAccessibleName(f"Use {component.upper()} component")
            check.toggled.connect(lambda checked, name=component: self._component_changed(name, checked))
            self._component_checks[component] = check
            check.setMinimumHeight(control_height_for_font(self, vertical_padding=8, floor=28))
            block_layout.addWidget(check, 0, 0)

            key = f"{component}_application_path"
            edit = QLineEdit()
            edit.setObjectName("operatorApplicationField_" + key)
            edit.setAccessibleName(f"{component.upper()} executable")
            edit.setPlaceholderText(f"Path to {component.upper()}")
            edit.textChanged.connect(lambda value, field_key=key: self._line_changed(field_key, value))
            self._field_widgets[key] = edit
            self._component_paths[component] = edit
            block_layout.addWidget(edit, 0, 1)
            button = QPushButton("Browse…")
            button.setObjectName("operatorApplicationBrowse_" + key)
            button.setAccessibleName(f"Browse for {component.upper()} executable")
            button.clicked.connect(lambda _checked=False, field_key=key: self.browse_requested.emit(field_key))
            self._browse_buttons[key] = button
            block_layout.addWidget(button, 0, 2)

            control_label = QLabel("Launch control")
            control_label.setObjectName(f"operatorApplicationLaunchControlLabel_{component}")
            control_label.setAccessibleName(f"Launch control for {component.upper()}")
            startup_key = f"{component}_launch_at_startup"
            startup = ThemedCheckBox("Start when CommsPlan opens")
            startup.setObjectName("operatorApplicationField_" + startup_key)
            startup.setAccessibleName(f"Start {component.upper()} when CommsPlan opens")
            startup.toggled.connect(
                lambda checked, field_key=startup_key: self._component_startup_changed(field_key, checked)
            )
            self._field_widgets[startup_key] = startup
            self._component_startup_checks[component] = startup
            startup.setMinimumHeight(control_height_for_font(self, vertical_padding=8, floor=28))
            control_key = f"{component}_launch_control"
            control = QComboBox()
            control.setObjectName("operatorApplicationField_" + control_key)
            control.setAccessibleName(f"{component.upper()} launch control")
            control.addItem("CommsPlan", "cp")
            control.addItem("Operator", "operator")
            control.currentIndexChanged.connect(
                lambda _index, field_key=control_key, widget=control: self._component_launch_control_changed(field_key, widget.currentData())
            )
            self._field_widgets[control_key] = control
            block_layout.addWidget(control_label, 1, 0)
            block_layout.addWidget(control, 1, 1)
            block_layout.addWidget(startup, 1, 2)
            self._component_policy_widgets[component] = (control_label, control, startup)
            self._normal_form.addRow(block)

    def _update_fast_light_component_layouts(self) -> None:
        """Reflow component launch policy before controls can clip."""

        if self.family_key != "fast_light":
            return
        for component, block in self._component_blocks.items():
            layout = self._component_grids.get(component)
            policy = self._component_policy_widgets.get(component)
            if layout is None or policy is None:
                continue
            _control_label, _control, startup = policy
            layout.removeWidget(startup)
            if block.width() < 700:
                layout.addWidget(startup, 2, 0, 1, 3)
            else:
                layout.addWidget(startup, 1, 2)
            layout.invalidate()
            block.updateGeometry()

    def _build_more_form(self) -> None:
        family = self.family_key
        if family == "js8call":
            self._add_path(self.more_form, "configuration_path", "Profile readback folder", "Existing JS8 profile folder")
            # JS8Call's native settings root is intentionally retained in the
            # draft for round-trip compatibility, but it is not an operator
            # choice in this editor.  The profile and explicit message paths
            # below are the actionable settings.
            self._add_path(self.more_form, "storage_path", "Read JS8Call messages from", "Native JS8Call application-data folder containing message files")
            self._add_path(self.more_form, "directed_path", "Native DIRECTED.TXT", "Existing JS8Call DIRECTED.TXT")
            self._add_path(self.more_form, "all_path", "Native ALL.TXT", "Existing JS8Call ALL.TXT")
            self._add_path(self.more_form, "inbox_path", "Native inbox.db3", "Existing JS8Call inbox.db3 file")
            self._add_integer(self.more_form, "offset_hz", "Audio offset (Hz)", minimum=0, maximum=100000)
        elif family == "fast_light":
            self._add_path(self.more_form, "configuration_path", "Settings folder", "Fast Light settings folder")
            self._add_path(self.more_form, "settings_root", "Settings root", "Optional settings root")
            self._add_path(self.more_form, "storage_path", "FLDigi logs", "Optional FLDigi logs folder")
            self._add_path(self.more_form, "secondary_storage_path", "FLDigi check-ins", "Optional check-in folder")
            self._add_path(self.more_form, "flmsg_message_path", "FLMsg messages folder", "Native FLMsg messages folder")
            self._add_path(self.more_form, "flamp_receive_path", "FLAmp receive folder", "Native FLAmp receive folder")
            for component in _COMPONENTS:
                self._add_path(
                    self.more_form,
                    f"{component}_configuration_path",
                    f"{component.upper()} settings",
                    f"Optional {component.upper()} settings folder",
                )
        elif family == "varac":
            self._add_path(self.more_form, "launch_command", "Launcher / command", "Existing launcher or command")
            self._add_path(self.more_form, "process_application_path", "VarAC executable for status", "Select VarAC.exe when using a launcher")
            self._add_path(self.more_form, "wine_runner", "Wine runner", "wine, wine64, or selected runner")
            self._add_path(self.more_form, "wine_prefix", "Wine prefix", "Existing Wine prefix")
            self._add_path(self.more_form, "storage_path", "VarAC database", "Optional database file")
            self._add_path(self.more_form, "secondary_storage_path", "VarAC incoming", "Optional incoming file")
            self._add_path(self.more_form, "vara_runtime_path", "VARA runtime", "Optional runtime file")
            self._add_path(self.more_form, "vara_ini_path", "VARA INI", "Optional INI file")
            self._add_path(self.more_form, "bbs_archive_path", "BBS archive folder", "Optional archive folder")
            self._add_path(self.more_form, "settings_root", "Settings root", "Optional VarAC settings root")
            self._add_path(self.more_form, "outbox_path", "Outbox folder", "Optional VarAC outbox folder")
            self._add_path(self.more_form, "bbs_path", "BBS folder", "Optional VarAC BBS folder")
        else:
            self._add_path(self.more_form, "configuration_path", "Configuration", "Configuration location")
            self._add_path(self.more_form, "settings_root", "Settings root", "Optional settings root")

        self._add_path(self.more_form, "instance_name", "CommsPlan display name", "Label shown in CommsPlan", browse=False)
        if "launch_command" not in self._field_widgets:
            self._add_path(self.more_form, "launch_command", "Custom command", "Optional structured launcher command", browse=False)
        self._add_path(self.more_form, "custom_argv", "Custom arguments", "Optional launcher arguments", browse=False)
        if "working_directory" not in self._field_widgets:
            self._add_path(self.more_form, "working_directory", "Working directory", "Optional working directory")
        if family != "fast_light":
            self._add_startup_row(self.more_form, duplicate=False)

    def _add_startup_row(self, form: QFormLayout, *, duplicate: bool = True) -> None:
        if not duplicate and "launch_at_startup" in self._field_widgets:
            return
        check = ThemedCheckBox("Start this application when CommsPlan opens")
        check.setObjectName("operatorApplicationLaunchAtStartup")
        check.setAccessibleName("Start this application when CommsPlan opens")
        check.toggled.connect(self._startup_changed)
        self._field_widgets["launch_at_startup"] = check
        form.addRow(QLabel("Startup"), check)

    def _add_combo(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        items: tuple[tuple[str, str], ...],
    ) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName("operatorApplicationField_" + key)
        combo.setAccessibleName(label)
        for text, data in items:
            combo.addItem(text, data)
        self._field_widgets[key] = combo
        self._field_labels[key] = QLabel(label)
        form.addRow(self._field_labels[key], combo)
        return combo

    def _add_path(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        placeholder: str,
        *,
        browse: bool = True,
    ) -> QLineEdit:
        edit = QLineEdit()
        edit.setObjectName("operatorApplicationField_" + key)
        edit.setAccessibleName(label)
        edit.setPlaceholderText(placeholder)
        edit.textChanged.connect(lambda value, field_key=key: self._line_changed(field_key, value))
        self._field_widgets[key] = edit
        self._field_labels[key] = QLabel(label)
        if not browse:
            form.addRow(self._field_labels[key], edit)
            return edit
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(edit, 1)
        button = QPushButton("Browse…")
        button.setObjectName("operatorApplicationBrowse_" + key)
        button.setAccessibleName(f"Browse for {label}")
        button.clicked.connect(lambda _checked=False, field_key=key: self.browse_requested.emit(field_key))
        self._browse_buttons[key] = button
        row_layout.addWidget(button)
        form.addRow(self._field_labels[key], row)
        return edit

    def _add_integer(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        *,
        minimum: int,
        maximum: int,
    ) -> QSpinBox:
        spin = QSpinBox()
        spin.setObjectName("operatorApplicationField_" + key)
        spin.setAccessibleName(label)
        spin.setRange(int(minimum), int(maximum))
        spin.valueChanged.connect(lambda value, field_key=key: self._spin_changed(field_key, value))
        self._field_widgets[key] = spin
        self._field_labels[key] = QLabel(label)
        form.addRow(self._field_labels[key], spin)
        return spin

    # ----- draft synchronization ------------------------------------------

    def _with_defaults(self, incoming: dict[str, Any]) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "application_path": "",
            "configuration_path": "",
            "settings_root": "",
            "host": "127.0.0.1",
            "port": "",
            "launch_at_startup": False,
            "launch_control": "operator",
            "mode": "manual",
            "instance_name": "",
            "launch_command": "",
            "custom_argv": "",
            "working_directory": "",
            "wine_runner": "",
            "wine_prefix": "",
        }
        if self.family_key == "js8call":
            defaults["settings_identity_mode"] = "default"
            defaults["rig_name"] = ""
        if self.family_key == "varac":
            defaults["existing_ini"] = ""
            defaults["wine_runner"] = ""
            defaults["wine_prefix"] = ""
        if self.family_key == "fast_light":
            defaults["secondary_port"] = ""
            defaults["settings_identity_mode"] = "default"
            defaults["components"] = {name: False for name in _COMPONENTS}
            defaults["component_paths"] = {name: "" for name in _COMPONENTS}
            for component in _COMPONENTS:
                defaults[f"{component}_enabled"] = False
                defaults[f"{component}_application_path"] = ""
                defaults[f"{component}_configuration_path"] = ""
                defaults[f"{component}_launch_at_startup"] = False
                defaults[f"{component}_launch_control"] = "operator"
        defaults.update(incoming)
        if self.family_key == "fast_light":
            components = dict(defaults.get("components") or {})
            component_paths = dict(defaults.get("component_paths") or {})
            incoming_components = incoming.get("components") or {}
            incoming_paths = incoming.get("component_paths") or {}
            for component in _COMPONENTS:
                if f"{component}_enabled" in incoming:
                    components[component] = bool(incoming[f"{component}_enabled"])
                elif component in incoming_components:
                    components[component] = bool(incoming_components[component])
                path_key = f"{component}_application_path"
                if path_key in incoming:
                    component_paths[component] = _text(incoming[path_key])
                    defaults[path_key] = _text(incoming[path_key])
                elif component in incoming_paths:
                    component_paths[component] = _text(incoming_paths[component])
                    defaults[path_key] = _text(incoming_paths[component])
            defaults["components"] = components
            defaults["component_paths"] = component_paths
        if self.family_key == "varac":
            if "configuration_path" not in incoming:
                defaults["configuration_path"] = _text(
                    incoming.get("existing_ini") or incoming.get("varac_ini_path") or ""
                )
            if "existing_ini" not in incoming and defaults.get("configuration_path"):
                defaults["existing_ini"] = _text(defaults["configuration_path"])
        if self.family_key == "js8call":
            identity = _text(defaults.get("settings_identity_mode")).lower()
            if identity in {"named", "isolated"}:
                defaults["settings_identity_mode"] = "isolated"
            elif identity not in {"default", "custom_launcher"}:
                defaults["settings_identity_mode"] = "default"
        if self.family_key in {"js8call", "varac"} and "launch_control" not in incoming:
            if _text(incoming.get("mode")).lower() == "managed" or bool(incoming.get("launch_at_startup")):
                defaults["launch_control"] = "cp"
        if self.family_key in {"js8call", "varac"} and defaults.get("launch_control") != "cp" and "launch_at_startup" in incoming:
            defaults["launch_at_startup"] = False
        if self.family_key == "fast_light":
            for component in _COMPONENTS:
                control_key = f"{component}_launch_control"
                startup_key = f"{component}_launch_at_startup"
                if control_key not in incoming and bool(incoming.get(startup_key)):
                    defaults[control_key] = "cp"
                if _text(defaults.get(control_key)).lower() not in {"cp", "operator"}:
                    defaults[control_key] = "operator"
                if defaults.get(control_key) != "cp" and startup_key in incoming:
                    defaults[startup_key] = False
        if self.family_key == "fast_light":
            # Fast Light launch policy belongs to each component.  The legacy
            # global flag is intentionally not projected into this form.
            defaults.pop("launch_at_startup", None)
            defaults.pop("launch_control", None)
            defaults["mode"] = self._mode_from_values(defaults, self.family_key)
        else:
            defaults["mode"] = self._mode_from_values(defaults, self.family_key)
        return defaults

    @staticmethod
    def _mode_from_values(values: Mapping[str, Any], family_key: str | None = None) -> str:
        control = _text(values.get("launch_control") or "operator").lower()
        if family_key == "fast_light":
            controls = [_text(values.get(f"{component}_launch_control") or "operator").lower() for component in _COMPONENTS]
            if "cp" in controls:
                return "managed"
            if "remote" in controls:
                return "remote"
            return "manual"
        if control == "cp":
            return "managed"
        if control == "remote":
            return "remote"
        return "manual"

    def _load_controls_from_data(self) -> None:
        for key, widget in self._field_widgets.items():
            value = self._data.get(key, "")
            blocked = widget.blockSignals(True)
            try:
                if isinstance(widget, QLineEdit):
                    widget.setText(_text(value))
                elif isinstance(widget, QSpinBox):
                    self._invalid_spin_values.pop(key, None)
                    try:
                        parsed = int(value)
                        if parsed < widget.minimum() or parsed > widget.maximum():
                            self._invalid_spin_values[key] = value
                            widget.setValue(max(widget.minimum(), min(widget.maximum(), parsed)))
                        else:
                            widget.setValue(parsed)
                    except (TypeError, ValueError):
                        if value not in (None, ""):
                            self._invalid_spin_values[key] = value
                        widget.setValue(0)
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    widget.setCurrentIndex(max(0, index))
            finally:
                widget.blockSignals(blocked)
        self._sync_component_controls()
        self._update_identity_visibility()
        self._update_launch_preview()

    def _sync_component_controls(self) -> None:
        components = self._data.get("components") or {}
        paths = self._data.get("component_paths") or {}
        for component, check in self._component_checks.items():
            blocked = check.blockSignals(True)
            check.setChecked(bool(components.get(component, self._data.get(f"{component}_enabled", False))))
            check.blockSignals(blocked)
        for component, edit in self._component_paths.items():
            value = self._data.get(f"{component}_application_path", paths.get(component, ""))
            blocked = edit.blockSignals(True)
            edit.setText(_text(value))
            edit.blockSignals(blocked)
        for component, startup in self._component_startup_checks.items():
            blocked = startup.blockSignals(True)
            startup.setChecked(bool(self._data.get(f"{component}_launch_at_startup", False)))
            startup.blockSignals(blocked)

    def _sync_controls_to_data(self) -> None:
        for key, widget in self._field_widgets.items():
            if isinstance(widget, QLineEdit):
                self._data[key] = widget.text()
            elif isinstance(widget, QSpinBox):
                # A numeric editor needs a visible zero when no value was
                # supplied, but that UI placeholder must not become a new
                # persisted default.  Keep absent optional values absent until
                # the operator actually changes the control.
                if key in self._invalid_spin_values:
                    continue
                if key in self._data or widget.value() != 0:
                    self._data[key] = widget.value()
            elif isinstance(widget, QCheckBox):
                self._data[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                self._data[key] = widget.currentData()
        if self.family_key == "fast_light":
            self._data["mode"] = self._mode_from_values(self._data, self.family_key)
        else:
            self._data["mode"] = self._mode_from_values(self._data, self.family_key)
        if self.family_key == "varac":
            self._data["existing_ini"] = self._data.get("configuration_path", "")
        if self.family_key == "fast_light":
            components = dict(self._data.get("components") or {})
            paths = dict(self._data.get("component_paths") or {})
            for component, check in self._component_checks.items():
                components[component] = check.isChecked()
                self._data[f"{component}_enabled"] = check.isChecked()
            for component, edit in self._component_paths.items():
                path = edit.text()
                self._data[f"{component}_application_path"] = path
                paths[component] = path
            self._data["components"] = components
            self._data["component_paths"] = paths

    def _line_changed(self, key: str, value: str) -> None:
        if self._loading:
            return
        self._data[key] = value
        if self.family_key == "varac" and key == "configuration_path":
            self._data["existing_ini"] = value
        self._mark_changed(key, value)

    def _spin_changed(self, key: str, value: int) -> None:
        if self._loading:
            return
        self._invalid_spin_values.pop(key, None)
        self._data[key] = int(value)
        self._mark_changed(key, int(value))

    def _component_changed(self, component: str, checked: bool) -> None:
        if self._loading:
            return
        components = dict(self._data.get("components") or {})
        components[component] = bool(checked)
        self._data["components"] = components
        self._data[f"{component}_enabled"] = bool(checked)
        self._mark_changed("components", deepcopy(components))

    def _component_startup_changed(self, key: str, checked: bool) -> None:
        if self._loading:
            return
        component = key.removesuffix("_launch_at_startup")
        control_key = f"{component}_launch_control"
        control = self._field_widgets.get(control_key)
        if checked and isinstance(control, QComboBox) and control.currentData() != "cp":
            blocked = control.blockSignals(True)
            control.setCurrentIndex(max(0, control.findData("cp")))
            control.blockSignals(blocked)
            self._data[control_key] = "cp"
        self._data[key] = bool(checked)
        self._data["mode"] = self._mode_from_values(self._data, self.family_key)
        self._mark_changed(key, bool(checked))

    def _component_launch_control_changed(self, key: str, value: object) -> None:
        if self._loading:
            return
        control = _text(value).lower() or "operator"
        self._data[key] = control
        component = key.removesuffix("_launch_control")
        if control != "cp":
            startup_key = f"{component}_launch_at_startup"
            startup = self._component_startup_checks.get(component)
            if isinstance(startup, QCheckBox) and startup.isChecked():
                blocked = startup.blockSignals(True)
                startup.setChecked(False)
                startup.blockSignals(blocked)
                self._data[startup_key] = False
        self._data["mode"] = self._mode_from_values(self._data, self.family_key)
        self._mark_changed(key, control)

    def _startup_changed(self, checked: bool) -> None:
        if self._loading:
            return
        self._data["launch_at_startup"] = bool(checked)
        self._data["mode"] = self._mode_from_values(self._data, self.family_key)
        self._mark_changed("launch_at_startup", bool(checked))

    def _launch_control_changed(self, value: object) -> None:
        if self._loading:
            return
        control = _text(value).lower() or "operator"
        self._data["launch_control"] = control
        if control != "cp":
            startup = self._field_widgets.get("launch_at_startup")
            if isinstance(startup, QCheckBox) and startup.isChecked():
                blocked = startup.blockSignals(True)
                startup.setChecked(False)
                startup.blockSignals(blocked)
                self._data["launch_at_startup"] = False
        self._data["mode"] = self._mode_from_values(self._data, self.family_key)
        self._mark_changed("launch_control", control)

    def _identity_changed(self, _index: int) -> None:
        if self._loading:
            return
        value = self.settings_identity_combo.currentData()
        self._data["settings_identity_mode"] = value
        self._update_identity_visibility()
        self._mark_changed("settings_identity_mode", value)

    def _review_all_fast_light_components(self) -> None:
        if self.family_key != "fast_light":
            return
        # The atomic draft remains intact; this only expands the presentation
        # so the shared settings-folder mode can be reviewed safely.
        self.set_view_scope()
        self.full_family_review_requested.emit()

    def _update_identity_visibility(self) -> None:
        self._update_fast_light_scope_ui()
        rig = self._field_widgets.get("rig_name")
        if rig is None or not hasattr(self, "settings_identity_combo"):
            return
        label = self._field_labels.get("rig_name")
        visible = self.family_key == "js8call" and self.settings_identity_combo.currentData() == "isolated"
        rig.setVisible(visible)
        if label is not None:
            label.setVisible(visible)
        config = self._field_widgets.get('configuration_path')
        if config is not None:
            config.setToolTip('Location retained for configuration readback. Launches as selects the native JS8Call profile.')
        launcher = self._field_widgets.get('launch_command')
        if self.family_key == 'js8call' and launcher is not None:
            custom = self.settings_identity_combo.currentData() == 'custom_launcher'
            launcher.parentWidget().setEnabled(custom and not self._busy)
            launcher.setToolTip('Used only when Launches as is Existing launcher. Default and named profiles use the selected application.')

    def _mark_changed(self, key: str, value: object) -> None:
        self._dirty = self._data != self._baseline
        self._update_launch_preview()
        self._refresh_dirty_label()
        self.value_changed.emit(key,deepcopy(value))

    def _acknowledge_profile_paths(self, checked):
        if self._loading:
            return
        value = js8_profile_path_review_token(self._data) if checked else ''
        self._data['profile_paths_acknowledged'] = value
        self._mark_changed('profile_paths_acknowledged', value)

    def _update_launch_preview(self) -> None:
        """Render the pure launch recipe without probing paths or processes."""

        label = getattr(self, "launch_preview_label", None)
        if label is None or self.family_key not in {"varac", "js8call"}:
            return
        control_labels = {
            "cp": "CommsPlan launches",
            "operator": "I start the application",
            "remote": "Remote application",
        }
        control = _text(self._data.get("launch_control") or "operator").lower()
        prefix = control_labels.get(control, "Launch selection")
        try:
            recipe = operator_launch_recipe(self.family_key, self._data)
            component = (recipe.get("components") or [{}])[0]
            argv = [
                str(component.get("executable") or ""),
                *(str(value) for value in component.get("arguments", ()) or ()),
            ]
            if argv and argv[0]:
                preview = " ".join(shlex.quote(value) for value in argv)
                label.setText(f"{prefix} · {preview}")
            elif control == "cp":
                label.setText(f"{prefix} · Choose an existing launcher or VarAC INI")
            else:
                label.setText(f"{prefix} · No CommsPlan launch command")
        except (TypeError, ValueError) as exc:
            label.setText(f"{prefix} · {exc}")
        if self.family_key == 'js8call':
            paths = '\n'.join(f"{self._field_labels[key].text() if key in self._field_labels else key.replace('_', ' ').capitalize()}: {self._data[key]}"
                              for key in JS8_REVIEW_PATHS if self._data.get(key))
            label.setText(label.text() + '\nProfile folder is for CommsPlan readback; it does not relocate JS8Call data.' + ('\n' + paths if paths else ''))
            review = js8_profile_paths_need_review(self._baseline, self._data)
            self.profile_paths_ack.setVisible(review)
            acknowledged = self._data.get('profile_paths_acknowledged') == js8_profile_path_review_token(self._data)
            blocked = self.profile_paths_ack.blockSignals(True)
            self.profile_paths_ack.setChecked(acknowledged)
            self.profile_paths_ack.blockSignals(blocked)
            if review and not acknowledged:
                label.setText(label.text() + '\nProfile changed: review retained paths under More settings before saving.')

    def _refresh_dirty_label(self) -> None:
        # Clean values do not prove that the last save attempt succeeded.
        self.dirty_label.setText('Unsaved changes' if self._dirty else 'No unsaved changes')

    def _emit_save(self) -> None:
        if not self._busy:
            self.save_requested.emit(self.values())

    def _set_more_visible(self, visible: bool) -> None:
        self.more_group.setVisible(bool(visible))
        self.more_button.setText('Hide more settings' if visible else 'More settings…')

    # ----- theme -----------------------------------------------------------

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        palette = dict(theme or active_app_theme())
        heading = self.findChild(QLabel, 'operatorApplicationHeading')
        if heading is not None:
            heading.setStyleSheet(label_style('text', palette, weight=700))
        self.status_label.setStyleSheet(label_style("muted", palette))
        self.dirty_label.setStyleSheet(label_style("muted", palette))
        if hasattr(self, "launch_preview_label"):
            self.launch_preview_label.setStyleSheet(label_style("muted", palette))
        self.more_button.setStyleSheet(button_style("muted", palette))
        if hasattr(self, "fast_light_scope_note"):
            self.fast_light_scope_note.setStyleSheet(label_style("muted", palette))
        if hasattr(self, "review_all_components_button"):
            self.review_all_components_button.setStyleSheet(button_style("secondary", palette))
        self.find_button.setStyleSheet(button_style("muted", palette))
        self.cancel_button.setStyleSheet(button_style("secondary", palette))
        self.save_button.setStyleSheet(button_style("primary", palette))
        for button in self._browse_buttons.values():
            button.setStyleSheet(button_style('secondary', palette))
        height = control_height_for_font(self, vertical_padding=8, floor=28)
        button_height = button_height_for_font(self, vertical_padding=10, floor=30)
        for check in self._component_checks.values():
            check.setMinimumHeight(height)
        for widget in self._field_widgets.values():
            if isinstance(widget, (QLineEdit, QComboBox, QCheckBox, QSpinBox)):
                widget.setMinimumHeight(height)
        extra_buttons = ((self.review_all_components_button,) if hasattr(self, "review_all_components_button") else ())
        for button in (*self._browse_buttons.values(), self.more_button, self.find_button, self.cancel_button, self.save_button, *extra_buttons):
            button.setMinimumHeight(button_height)
        self._update_fast_light_scope_ui()
        fit_child_combo_boxes(self)


__all__ = ["ApplicationSetupEditor"]
