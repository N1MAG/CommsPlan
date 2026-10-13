"""Cache-only workspace shell for Settings software administration.

The host owns navigation, draft state, editor construction, and any explicit
validation work.  This widget intentionally only renders an immutable
``SoftwareAdministrationSnapshot`` and emits selection/routing signals.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QButtonGroup,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.software_administration_model import (
    SoftwareAdministrationSnapshot,
    SoftwareFamilySummary,
)
from freqinout.gui.theme import (
    button_height_for_font,
    contrast_text_for_background,
    get_theme,
    label_style,
    resolve_theme,
    resolve_ui_text_scale,
)
from freqinout.gui.current_page_stack import CurrentPageStack
from freqinout.gui.software_instance_assistant import SoftwareInstanceAssistant


_TASKS: dict[str, tuple[tuple[str, str], ...]] = {
    "js8call": (
        ("overview", "Overview"), ("application_profile", "Application & Profile"),
        ("api_radio", "API & Radio"), ("message_storage", "Message Storage"),
        ("ingest_forms", "Ingest & Forms"), ("launch", "Launch"),
        ("health", "Health"), ("advanced", "Advanced"),
    ),
    "fast_light": (
        ("overview", "Overview"), ("flrig_control", "FLRig Control"),
        ("fldigi_modem_logs", "FLDigi Modem & Logs"), ("flmsg", "FLMsg"),
        ("flamp_signing", "FLAmp & Signing"), ("message_folders", "Message Folders"),
        ("launch", "Launch"), ("health", "Health"), ("advanced", "Advanced"),
    ),
    "varac": (
        ("overview", "Overview"), ("application_radio", "Application & Radio"),
        ("runtime_paths", "Runtime & Paths"), ("inbox_outbox", "Inbox & Outbox"),
        ("inbound_guard", "Inbound Guard"), ("cluster", "Cluster"),
        ("launch", "Launch"), ("health", "Health"), ("advanced", "Advanced"),
    ),
    "commstat": (
        ("overview", "Overview"), ("transport_mapping", "JS8 Endpoint Bindings"),
        ("health", "Shared Service Health"),
        ("advanced", "Advanced"),
    ),
    "external_spotter": (
        ("overview", "Overview"), ("installation", "Installation"), ("profile", "Profile"),
        ("forms", "Form Synchronization"), ("launch", "Launch"), ("health", "Health"),
        ("advanced", "Advanced"),
    ),
    "fio_spotter": (
        ("overview", "Overview"), ("dependencies", "Dependencies & Radio Mapping"),
        ("operational_workspace", "Open CommsPlan Spotter"),
    ),
}


class SoftwareAdministrationWorkspace(QWidget):
    """Software-first Settings shell with no configuration or endpoint I/O."""

    family_selected = Signal(str)
    radio_selected = Signal(object)
    task_selected = Signal(str)
    assign_requested = Signal(str)
    instance_add_requested = Signal(object)
    instance_discovery_requested = Signal(object)
    # Settings owns the bounded worker and persistence transaction.  These
    # relays intentionally carry cache-only assistant payloads only.
    varac_native_prepare_requested = Signal(object)
    varac_native_apply_requested = Signal(object)
    # Keep ``assign_requested`` compatible with Settings while exposing a
    # richer seam for hosts that distinguish this explicit action.
    assign_existing_requested = Signal(object)
    create_radio_requested = Signal()
    disassociate_requested = Signal(object)
    operational_route_requested = Signal(str)
    save_all_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._snapshot = SoftwareAdministrationSnapshot(families=())
        self._family_key = ""
        self._radio_id: Optional[int] = None
        self._task_key = ""
        # The Settings host owns draft values and persistence.  The workspace
        # receives only this compact, cache-only projection so that chip
        # selection and repainting cannot inspect configuration or save data.
        self._dirty_contexts: frozenset[tuple[int, str]] = frozenset()
        self._theme: dict[str, str] = get_theme("light")
        self._text_scale = 1.0
        self._base_font = QFont(self.font())
        self._family_buttons: dict[str, QToolButton] = {}
        self._radio_buttons: dict[Optional[int], QToolButton] = {}
        self._task_buttons: dict[str, QToolButton] = {}
        self._family_group = QButtonGroup(self)
        self._family_group.setExclusive(True)
        self._radio_group = QButtonGroup(self)
        self._radio_group.setExclusive(True)
        self._task_group = QButtonGroup(self)
        self._task_group.setExclusive(True)
        # A registered editor has one clear owner: (family, optional radio,
        # task).  Editors remain in the stack when the operator changes chips,
        # so partially completed values and focus state are not discarded.
        self._registered_editors: dict[tuple[str, Optional[int], str], QWidget] = {}
        self._editor_keys_by_widget: dict[QWidget, tuple[str, Optional[int], str]] = {}
        self._legacy_editor: Optional[QWidget] = None
        self._instance_assistant: Optional[SoftwareInstanceAssistant] = None
        self._available_radios: tuple[dict[str, Any], ...] = ()
        self._instance_inventory: dict[str, tuple[dict[str, Any], ...]] = {}
        self._varac_clusters: tuple[dict[str, Any], ...] = ()
        self._managed_root = ""
        self._build_ui()
        self.apply_theme(self._theme)

    def _build_ui(self) -> None:
        self.setAccessibleName("Software administration workspace")
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.heading_label = QLabel("Software administration")
        self.heading_label.setAccessibleName("Software administration heading")
        root.addWidget(self.heading_label)
        self.explanation_label = QLabel(
            "Choose software, then a radio and task. Selections only change the workspace; "
            "they do not save, scan, or contact a radio."
        )
        self.explanation_label.setWordWrap(True)
        root.addWidget(self.explanation_label)

        self.family_prompt_label = QLabel("1. Choose software family")
        root.addWidget(self.family_prompt_label)
        self.family_strip, self.family_layout = self._horizontal_strip("Software family choices")
        root.addWidget(self.family_strip)

        self.radio_prompt_label = QLabel("2. Choose radio context")
        root.addWidget(self.radio_prompt_label)
        self.radio_strip, self.radio_layout = self._horizontal_strip("Radio context choices")
        root.addWidget(self.radio_strip)

        self.context_banner = QLabel("Choose a software family to begin.")
        self.context_banner.setObjectName("softwareAdministrationContextBanner")
        self.context_banner.setWordWrap(True)
        self.context_banner.setAccessibleName("Selected software context")
        root.addWidget(self.context_banner)

        self.task_prompt_label = QLabel("3. Choose configuration task")
        root.addWidget(self.task_prompt_label)
        self.task_strip, self.task_layout = self._horizontal_strip("Configuration task choices")
        root.addWidget(self.task_strip)

        self.unassigned_label = QLabel()
        self.unassigned_label.setWordWrap(True)
        self.unassigned_label.setAccessibleName("Legacy unassigned software instances for recovery")
        root.addWidget(self.unassigned_label)

        self.assign_button = QPushButton("Assign Existing Instance…")
        self.assign_existing_button = self.assign_button
        self.assign_button.setAccessibleName("Assign an existing software instance to a radio")
        self.assign_button.setToolTip(
            "Open the radio assignment editor for an existing instance in the selected software family"
        )
        self.assign_button.clicked.connect(self._emit_assign_request)
        self.add_instance_button = QPushButton("Add software instance…")
        self.add_instance_button.setAccessibleName("Add software instance")
        self.add_instance_button.setToolTip(
            "Guided setup for an existing, CommsPlan-managed, manual, or remote software instance"
        )
        self.add_instance_button.clicked.connect(self._start_instance_action)
        self.disassociate_button = QPushButton("Disassociate…")
        self.disassociate_button.setAccessibleName("Disassociate software instance from selected radio")
        self.disassociate_button.setToolTip(
            "Remove only the CommsPlan radio assignment and launch links; external applications and files are retained"
        )
        self.disassociate_button.clicked.connect(self._emit_disassociate_request)
        self.disassociate_button.setVisible(False)
        self.save_all_button = QPushButton("Save All Changes")
        self.save_all_button.setObjectName("softwareAdministrationSaveAllButton")
        self.save_all_button.setAccessibleName("Save all unsaved software changes")
        self.save_all_button.setToolTip(
            "Save all unsaved software drafts. Review the Unsaved changes labels before continuing."
        )
        self.save_all_button.setEnabled(False)
        self.save_all_button.clicked.connect(self.save_all_requested.emit)
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(6)
        action_row.addWidget(self.assign_button)
        action_row.addWidget(self.add_instance_button)
        action_row.addWidget(self.disassociate_button)
        action_row.addWidget(self.save_all_button)
        action_row.addStretch(1)
        root.addLayout(action_row)

        self.editor_host = QWidget()
        self.editor_host.setObjectName("softwareAdministrationEditorHost")
        self.editor_host.setAccessibleName("Software configuration editor")
        self.editor_host_layout = QVBoxLayout(self.editor_host)
        self.editor_host_layout.setContentsMargins(10, 10, 10, 10)
        self.editor_host_layout.setSpacing(0)
        self.editor_placeholder = QLabel("Choose a configuration task to display its editor here.")
        self.editor_placeholder.setWordWrap(True)
        self.editor_placeholder.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.editor_placeholder.setMargin(8)
        self.editor_placeholder.setAccessibleName("No software configuration editor selected")
        self.editor_stack = CurrentPageStack()
        self.editor_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.editor_stack.setAccessibleName("Software configuration editor surface")
        self.editor_stack.addWidget(self.editor_placeholder)
        self.editor_host_layout.addWidget(self.editor_stack)
        self.editor_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.editor_host.setMinimumHeight(96)
        root.addWidget(self.editor_host, 1)

    def set_embedded(self, embedded: bool = True) -> None:
        """Avoid repeating the Settings section title inside the workspace."""

        self.heading_label.setVisible(not bool(embedded))

    def set_instance_context(
        self,
        *,
        radios: Iterable[Mapping[str, Any]],
        inventory_by_family: Mapping[str, Iterable[Mapping[str, Any]]],
        varac_clusters: Iterable[Mapping[str, Any]] = (),
        managed_root: str = "",
    ) -> None:
        """Cache Settings-owned values used when the explicit assistant opens."""

        self._available_radios = tuple(dict(row) for row in radios if isinstance(row, Mapping))
        self._instance_inventory = {
            str(family).strip().lower(): tuple(
                dict(row) for row in rows if isinstance(row, Mapping)
            )
            for family, rows in inventory_by_family.items()
        }
        self._varac_clusters = tuple(dict(row) for row in varac_clusters if isinstance(row, Mapping))
        self._managed_root = str(managed_root or "").strip()

    def show_family_summary(self, family: Optional[SoftwareFamilySummary]) -> None:
        """Render the non-editing ``All`` context from the cached snapshot."""

        if family is None:
            self.editor_placeholder.setText("Choose a software family to begin.")
        elif not family.assignments:
            self.editor_placeholder.setText(
                f"{family.title} is not assigned to a radio.\n\n"
                "Choose Assign Existing Instance… to route an existing record to a radio, "
                "or use Add software instance… for a new radio-first instance."
            )
        else:
            rows = []
            for assignment in family.assignments:
                instance = assignment.instance_name or "No linked instance"
                shared = " · Shared" if assignment.is_shared else ""
                rows.append(
                    f"• {assignment.radio_name} — {instance} — "
                    f"{assignment.status_text or 'Not yet verified'}{shared}"
                )
            self.editor_placeholder.setText(
                f"{family.title} across {len(family.assignments)} assigned "
                f"radio{'s' if len(family.assignments) != 1 else ''}\n\n"
                + "\n".join(rows)
                + "\n\nChoose a radio above to review or change its configuration."
            )
        if family is not None and family.unassigned_instances:
            names = ", ".join(item.instance_name for item in family.unassigned_instances[:4])
            remaining = len(family.unassigned_instances) - 4
            suffix = f", plus {remaining} more" if remaining > 0 else ""
            self.editor_placeholder.setText(
                self.editor_placeholder.text()
                + f"\n\nLegacy/unassigned instances for recovery ({len(family.unassigned_instances)}): {names}{suffix}."
            )
        if self.keep_instance_assistant_visible():
            return
        self.editor_stack.setCurrentWidget(self.editor_placeholder)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._apply_compact_height(event.size().height() < 680)

    def _apply_compact_height(self, compact: bool) -> None:
        """Give the task editor priority when vertical space is constrained."""

        self.explanation_label.setVisible(not compact)
        self.family_prompt_label.setVisible(not compact)
        self.radio_prompt_label.setVisible(not compact)
        selected_radio = self._radio_id is not None
        self.task_prompt_label.setVisible(not compact and selected_radio)
        self.task_strip.setVisible(selected_radio)
        strip_height = button_height_for_font(
            self.assign_button,
            vertical_padding=14 if compact else 18,
            floor=30,
        )
        for strip in (self.family_strip, self.radio_strip, self.task_strip):
            strip.setProperty("fioChipRowHeight", int(strip_height))
            self._sync_horizontal_strip_height(strip)
        self.unassigned_label.setVisible(not compact)
        self.assign_button.setVisible(not compact or not selected_radio)
        # Adding an instance remains available in compact mode; the guided
        # surface itself handles vertical compression and scrolling.
        self.add_instance_button.setVisible(True)
        self.save_all_button.setVisible(not compact or bool(self._dirty_contexts))
        self.editor_host.setMinimumHeight(180 if compact else 96)

    def _horizontal_strip(self, accessible_name: str) -> tuple[QScrollArea, QHBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setAccessibleName(accessible_name)
        content = QWidget()
        layout = QHBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addStretch(1)
        scroll.setWidget(content)
        # The scrollbar occupies layout height on Linux/Windows styles.  Keep
        # it in a separate lane below the chips instead of allowing a compact
        # Settings viewport to squeeze it over the selected button.
        scroll.horizontalScrollBar().rangeChanged.connect(
            lambda _minimum, _maximum, target=scroll: self._sync_horizontal_strip_height(target)
        )
        scroll.setProperty("fioChipRowHeight", 34)
        self._sync_horizontal_strip_height(scroll)
        return scroll, layout

    @staticmethod
    def _sync_horizontal_strip_height(scroll: QScrollArea) -> None:
        """Reserve a non-overlapping lane whenever a chip row overflows."""

        chip_height = max(30, int(scroll.property("fioChipRowHeight") or 0))
        bar = scroll.horizontalScrollBar()
        overflow = bar.maximum() > bar.minimum()
        scrollbar_height = max(14, bar.sizeHint().height()) if overflow else 0
        total_height = chip_height + scrollbar_height + 4
        if scroll.minimumHeight() != total_height:
            scroll.setMinimumHeight(total_height)
        if scroll.maximumHeight() != total_height:
            scroll.setMaximumHeight(total_height)

    @staticmethod
    def _clear_layout(layout: QHBoxLayout | QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_snapshot(self, snapshot: SoftwareAdministrationSnapshot) -> None:
        self._snapshot = snapshot or SoftwareAdministrationSnapshot(families=())
        available = {family.key for family in self._snapshot.families}
        family_key = self._family_key if self._family_key in available else next(
            (family.key for family in self._snapshot.families),
            "",
        )
        self._rebuild_family_buttons()
        self.select_context(family_key, self._radio_id, self._task_key)

    def select_context(
        self, family_key: str, radio_id: Optional[int] = None, task_key: Optional[str] = None
    ) -> None:
        family = self._snapshot.family(family_key)
        self._family_key = family.key if family is not None else ""
        valid_radio_ids = {item.radio_id for item in family.assignments} if family else set()
        self._radio_id = radio_id if radio_id in valid_radio_ids else None
        tasks = _TASKS.get(self._family_key, ())
        valid_task_keys = {key for key, _label in tasks}
        self._task_key = task_key if task_key in valid_task_keys else (tasks[0][0] if tasks else "")
        if self._radio_id is None and tasks:
            self._task_key = tasks[0][0]
        self._rebuild_radio_buttons(family)
        self._rebuild_task_buttons(tasks)
        self._update_context(family)
        self._sync_checked_buttons()
        self._show_registered_editor_for_context()

    def selected_family_key(self) -> str:
        return self._family_key

    def selected_radio_id(self) -> Optional[int]:
        return self._radio_id

    def selected_task_key(self) -> str:
        return self._task_key

    def begin_instance_setup(
        self,
        family_key: str,
        radio_id: int,
        *,
        source: str = "managed",
    ) -> bool:
        """Open a new-instance assistant scoped to any cached radio.

        A radio does not need an existing family assignment to be a valid
        target.  This is the public continuation seam used by Guided Add Radio
        after the radio itself has been saved.  Persistence remains owned by
        the Settings host and the assistant keeps replacement mode off unless
        the operator explicitly selects an occupied radio.
        """

        family = self._snapshot.family(str(family_key or "").strip().lower())
        try:
            target_radio_id = int(radio_id)
        except (TypeError, ValueError):
            return False
        available_ids = {
            int(row.get("id", 0) or 0)
            for row in self._available_radios
            if int(row.get("id", 0) or 0) > 0
        }
        if family is None or target_radio_id not in available_ids:
            return False
        if self._instance_assistant is not None:
            self.keep_instance_assistant_visible(announce=True)
            return False

        self._family_key = family.key
        self._radio_id = target_radio_id
        tasks = _TASKS.get(self._family_key, ())
        self._task_key = tasks[0][0] if tasks else ""
        self._rebuild_family_buttons()
        self._rebuild_radio_buttons(family)
        self._rebuild_task_buttons(tasks)
        self._update_context(family)
        self._sync_checked_buttons()
        self._open_instance_assistant(initial_source=source)
        return self._instance_assistant is not None

    def set_dirty_contexts(
        self,
        contexts: Mapping[tuple[object, object], bool] | Iterable[tuple[object, object]],
    ) -> None:
        """Render the host-owned set of unsaved ``(radio_id, family_key)`` drafts.

        This accepts either a mapping whose truthy values are dirty or an
        iterable of dirty keys.  It intentionally stores no draft content and
        performs no persistence, discovery, timer, or endpoint work.  Unknown
        radio IDs remain represented in their software-family summary so a
        host refresh cannot make a pending draft appear to disappear.
        """
        entries = contexts.items() if isinstance(contexts, Mapping) else contexts
        normalized: set[tuple[int, str]] = set()
        for entry in entries:
            if isinstance(contexts, Mapping):
                key, is_dirty = entry
                if not is_dirty:
                    continue
            else:
                key = entry
            try:
                radio_id, family_key = key
                normalized.add((int(radio_id), str(family_key)))
            except (TypeError, ValueError):
                # The host may be mid-refresh.  Ignore malformed cache data
                # rather than turning a navigation-only update into a UI fault.
                continue
        self._dirty_contexts = frozenset(normalized)
        family = self._snapshot.family(self._family_key)
        self._rebuild_family_buttons()
        self._rebuild_radio_buttons(family)
        self._update_context(family)
        self._sync_checked_buttons()
        self._update_save_all_button()

    def set_dirty_state(
        self,
        contexts: Mapping[tuple[object, object], bool] | Iterable[tuple[object, object]],
    ) -> None:
        """Compatibility alias for :meth:`set_dirty_contexts`."""
        self.set_dirty_contexts(contexts)

    def dirty_contexts(self) -> frozenset[tuple[int, str]]:
        """Return the immutable dirty-key projection currently being rendered."""
        return self._dirty_contexts

    def has_dirty_drafts(self) -> bool:
        return bool(self._dirty_contexts)

    def set_editor_widget(self, widget: Optional[QWidget]) -> None:
        """Show a host-provided editor without deleting a prior editor.

        This compatibility API remains intentionally context-neutral for the
        existing Settings host.  New integrations should prefer
        :meth:`register_task_editor`, which keeps an editor bound to one
        software/radio/task context and restores it automatically after chip
        navigation.
        """
        self._legacy_editor = widget
        if self.keep_instance_assistant_visible():
            return
        if widget is None:
            self.editor_stack.setCurrentWidget(self.editor_placeholder)
            return
        self._ensure_editor_in_stack(widget)
        self.editor_stack.setCurrentWidget(widget)

    def register_task_editor(
        self,
        family_key: str,
        task_key: str,
        widget: QWidget,
        *,
        radio_id: Optional[int] = None,
    ) -> None:
        """Register a durable editor or action surface for one task context.

        ``radio_id=None`` represents an ``All``-radio family surface and is
        used as the fallback when no radio-specific editor is registered.
        Registering the same widget again transfers its ownership to the new
        context; a widget can therefore never be displayed as two unrelated
        configuration surfaces.
        """
        if not family_key or not task_key:
            raise ValueError("family_key and task_key are required")
        if widget is None:
            raise ValueError("widget is required")
        key = (str(family_key), radio_id, str(task_key))
        prior_for_key = self._registered_editors.get(key)
        if prior_for_key is not None and prior_for_key is not widget:
            self._editor_keys_by_widget.pop(prior_for_key, None)
        prior_key = self._editor_keys_by_widget.get(widget)
        if prior_key is not None and prior_key != key:
            self._registered_editors.pop(prior_key, None)
        self._registered_editors[key] = widget
        self._editor_keys_by_widget[widget] = key
        if not widget.accessibleName().strip():
            widget.setAccessibleName(f"{family_key} {task_key} configuration editor")
        self._ensure_editor_in_stack(widget)
        self._show_registered_editor_for_context()

    def set_task_action_surface(
        self,
        family_key: str,
        task_key: str,
        widget: QWidget,
        *,
        radio_id: Optional[int] = None,
    ) -> None:
        """Alias for registering a task-specific non-form action surface."""
        self.register_task_editor(family_key, task_key, widget, radio_id=radio_id)

    def registered_task_editor(
        self, family_key: str, task_key: str, *, radio_id: Optional[int] = None
    ) -> Optional[QWidget]:
        """Return the exact registered editor without creating or probing anything."""
        return self._registered_editors.get((str(family_key), radio_id, str(task_key)))

    def _ensure_editor_in_stack(self, widget: QWidget) -> None:
        if self.editor_stack.indexOf(widget) < 0:
            self.editor_stack.addWidget(widget)

    def _registered_editor_for_context(self) -> Optional[QWidget]:
        exact = (self._family_key, self._radio_id, self._task_key)
        fallback = (self._family_key, None, self._task_key)
        return self._registered_editors.get(exact) or self._registered_editors.get(fallback)

    def _show_registered_editor_for_context(self) -> None:
        if self._instance_assistant is not None:
            self.editor_stack.setCurrentWidget(self._instance_assistant)
            return
        editor = self._registered_editor_for_context()
        if editor is not None:
            self.editor_stack.setCurrentWidget(editor)
        elif self._legacy_editor is None:
            self.editor_stack.setCurrentWidget(self.editor_placeholder)

    def apply_theme(self, settings_or_theme: Mapping[str, Any]) -> None:
        values = settings_or_theme or {}
        self._theme = dict(values) if "bg" in values and "surface" in values else resolve_theme(values)
        self._text_scale = resolve_ui_text_scale(values) if "ui_text_size" in values else 1.0
        font = QFont(self._base_font)
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(8.0, font.pointSizeF() * self._text_scale))
        self.setFont(font)
        theme = self._theme
        accent_text = contrast_text_for_background(theme["accent"], theme)
        self.setStyleSheet(
            "QWidget { background: %(bg)s; color: %(text)s; }"
            "QLabel { color: %(text)s; }"
            "QLabel#softwareAdministrationContextBanner { background: %(surface_alt)s;"
            " border: 1px solid %(accent)s; border-radius: 6px; padding: 7px; font-weight: 600; }"
            "QWidget#softwareAdministrationEditorHost { background: %(surface)s;"
            " border: 1px solid %(border)s; border-radius: 6px; }"
            "QToolButton { background: %(surface_alt)s; border: 1px solid %(border)s;"
            " border-radius: 6px; padding: 6px 9px; }"
            "QToolButton:checked { background: %(accent)s; color: %(accent_text)s; border-color: %(accent_active)s; }"
            "QToolButton:hover { border-color: %(accent)s; }"
            "QPushButton { background: %(accent)s; color: %(accent_text)s; border: 1px solid %(accent_active)s;"
            " border-radius: 6px; padding: 6px 10px; font-weight: 600; }"
            "QPushButton#softwareAdministrationSaveAllButton { background: %(surface_alt)s; color: %(text)s;"
            " border-color: %(accent)s; }"
            "QPushButton:disabled { background: %(surface_alt)s; color: %(text_muted)s; border-color: %(border)s; }"
            % {**theme, "accent_text": accent_text}
        )
        self.heading_label.setStyleSheet(label_style("text", theme, weight=700))
        self._apply_minimum_hit_heights()

    def _apply_minimum_hit_heights(self) -> None:
        height = button_height_for_font(self.assign_button, vertical_padding=12, floor=30)
        for button in (*self._family_buttons.values(), *self._radio_buttons.values(), *self._task_buttons.values()):
            button.setMinimumHeight(height)
        self.assign_button.setMinimumHeight(height)
        self.add_instance_button.setMinimumHeight(height)
        self.disassociate_button.setMinimumHeight(height)
        self.save_all_button.setMinimumHeight(height)

    def _rebuild_family_buttons(self) -> None:
        self._clear_layout(self.family_layout)
        for button in self._family_group.buttons():
            self._family_group.removeButton(button)
        self._family_buttons = {}
        for family in self._snapshot.families:
            summary = f"{family.assigned_radio_count} radio{'s' if family.assigned_radio_count != 1 else ''}"
            if family.attention_count:
                summary += f" · {family.attention_count} needs attention"
            dirty_count = self._dirty_count_for_family(family.key)
            if dirty_count:
                summary += f" · Unsaved changes ({dirty_count})"
            dirty_hint = (
                f" {dirty_count} radio draft{'s are' if dirty_count != 1 else ' is'} unsaved."
                if dirty_count
                else ""
            )
            button = self._make_chip(
                f"{family.title} · {summary}",
                f"Select {family.title}. {family.description}{dirty_hint}",
            )
            button.clicked.connect(lambda _checked=False, key=family.key: self._choose_family(key))
            self._family_group.addButton(button)
            self.family_layout.addWidget(button)
            self._family_buttons[family.key] = button
        self.family_layout.addStretch(1)

    def _rebuild_radio_buttons(self, family: Optional[SoftwareFamilySummary]) -> None:
        self._clear_layout(self.radio_layout)
        for button in self._radio_group.buttons():
            self._radio_group.removeButton(button)
        self._radio_buttons = {}
        dirty_count = self._dirty_count_for_family(family.key) if family else 0
        all_text = "All"
        if dirty_count:
            all_text += f" · Unsaved changes ({dirty_count})"
        all_button = self._make_chip(
            all_text,
            "Show all radios assigned to the selected software family"
            + (f". {dirty_count} radio draft{'s are' if dirty_count != 1 else ' is'} unsaved." if dirty_count else ""),
        )
        all_button.clicked.connect(lambda: self._choose_radio(None))
        self._radio_group.addButton(all_button)
        self.radio_layout.addWidget(all_button)
        self._radio_buttons[None] = all_button
        for assignment in family.assignments if family else ():
            state = assignment.status_text or "Not yet verified"
            shared = " Shared instance." if assignment.is_shared else ""
            ownership = (
                f"Assigned: {assignment.instance_name or 'Missing instance'}"
                if assignment.instance_id is not None
                else "Available"
            )
            text = f"{assignment.radio_name} — {ownership}" + (" · Shared" if assignment.is_shared else "")
            dirty = self._is_dirty(assignment.radio_id, family.key) if family else False
            if dirty:
                text += " · Unsaved changes"
            verification_hint = (
                " CommsPlan has not run or received a current verification for this software. "
                "Choose the radio, then Health, to check it."
                if state == "Not yet verified"
                else ""
            )
            button = self._make_chip(
                text,
                f"Select {assignment.radio_name}. {ownership}. Status: {state}.{shared}"
                + verification_hint
                + (" Unsaved changes for this software and radio." if dirty else ""),
            )
            button.clicked.connect(lambda _checked=False, radio_id=assignment.radio_id: self._choose_radio(radio_id))
            self._radio_group.addButton(button)
            self.radio_layout.addWidget(button)
            self._radio_buttons[assignment.radio_id] = button
        self.radio_layout.addStretch(1)

    def _rebuild_task_buttons(self, tasks: tuple[tuple[str, str], ...]) -> None:
        self._clear_layout(self.task_layout)
        for button in self._task_group.buttons():
            self._task_group.removeButton(button)
        self._task_buttons = {}
        for key, label in tasks:
            all_context = self._radio_id is None
            enabled = not all_context or key == "overview"
            tooltip = (
                f"Select {label} task"
                if enabled
                else f"Choose a radio before configuring {label}"
            )
            button = self._make_chip(label, tooltip)
            button.setEnabled(enabled)
            button.clicked.connect(lambda _checked=False, task_key=key: self._choose_task(task_key))
            self._task_group.addButton(button)
            self.task_layout.addWidget(button)
            self._task_buttons[key] = button
        self.task_layout.addStretch(1)

    def _make_chip(self, text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        # Qt treats a single ampersand as a mnemonic marker. These chips use
        # natural-language labels, so preserve a visible ``&``.
        button.setText(text.replace("&", "&&"))
        button.setCheckable(True)
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        button.setAccessibleName(text)
        button.setToolTip(tooltip)
        button.setMinimumHeight(button_height_for_font(button, vertical_padding=12, floor=30))
        return button

    def _choose_family(self, key: str) -> None:
        if self._keep_open_instance_assistant_visible():
            return
        if key == self._family_key:
            self._sync_checked_buttons()
            return
        self.select_context(key)
        self.family_selected.emit(self._family_key)

    def _choose_radio(self, radio_id: Optional[int]) -> None:
        if self._keep_open_instance_assistant_visible():
            return
        if radio_id == self._radio_id:
            self._sync_checked_buttons()
            return
        self._radio_id = radio_id
        tasks = _TASKS.get(self._family_key, ())
        if radio_id is None and tasks:
            self._task_key = tasks[0][0]
        self._rebuild_task_buttons(tasks)
        self._update_context(self._snapshot.family(self._family_key))
        self._sync_checked_buttons()
        self._show_registered_editor_for_context()
        self.radio_selected.emit(radio_id)

    def _choose_task(self, key: str) -> None:
        if self._keep_open_instance_assistant_visible():
            return
        if self._radio_id is None and key != "overview":
            return
        if key == self._task_key:
            self._sync_checked_buttons()
            return
        self._task_key = key
        self._sync_checked_buttons()
        self._show_registered_editor_for_context()
        if key == "operational_workspace":
            route = self._snapshot.family(self._family_key)
            self.operational_route_requested.emit(route.operational_route if route else "")
        self.task_selected.emit(key)

    def _emit_assign_request(self) -> None:
        family = self._snapshot.family(self._family_key)
        if family is None:
            return
        assigned_ids = {
            int(item.instance_id)
            for item in family.assignments
            if item.instance_id is not None
        }
        recovery_rows = tuple(
            row
            for row in self._instance_inventory.get(self._family_key, ())
            if int(row.get("id", 0) or 0) not in assigned_ids
        )
        if not recovery_rows:
            self.context_banner.setText(
                f"No unassigned {family.title} instances are available. "
                "Create a new instance or select a radio to review its current assignment."
            )
            return
        self.assign_existing_requested.emit(
            {"family_key": self._family_key, "radio_id": self._radio_id}
        )
        self._open_instance_assistant(
            initial_source="discover",
            initial_discovery_results=recovery_rows,
        )

    def _start_instance_action(self) -> None:
        """Route the primary action without creating an operational orphan."""

        if not self._available_radios:
            self.create_radio_requested.emit()
            return
        self._open_instance_assistant()

    def _open_instance_assistant(
        self,
        *,
        initial_source: str = "managed",
        initial_discovery_results: Iterable[Mapping[str, Any]] = (),
        initial_edit_instance: Mapping[str, Any] | None = None,
    ) -> None:
        """Open the cache-only instance flow for the selected family.

        The assistant emits a stable payload; this workspace never turns the
        action into a database save or an installation scan.
        """

        if self._instance_assistant is not None:
            self.editor_stack.setCurrentWidget(self._instance_assistant)
            self._instance_assistant.set_operation_status(
                "Finish this setup or choose Cancel before starting another instance."
            )
            return
        family = self._snapshot.family(self._family_key)
        if family is None:
            return
        radios = self._available_radios or tuple(
            {"id": assignment.radio_id, "name": assignment.radio_name}
            for assignment in family.assignments
        )
        selected_radio = next(
            (
                row
                for row in radios
                if int(row.get("id", row.get("radio_id", 0)) or 0)
                == int(self._radio_id or 0)
            ),
            {},
        )
        radio_role = str(
            selected_radio.get("device_class", selected_radio.get("radio_role", "tx_rx"))
            or "tx_rx"
        ).strip().lower()
        edit_seed: dict[str, Any] | None = None
        if isinstance(initial_edit_instance, Mapping):
            edit_seed = dict(initial_edit_instance)
            # Editing uses the existing assignment as reviewed input but does
            # not masquerade as a source-locked import. The normal replacement
            # transaction retains the old row until the complete new identity
            # and projections commit successfully.
            for key in (
                "id",
                "imported_id",
                "imported_system_key",
                "system_key",
                "source_fingerprint",
            ):
                edit_seed.pop(key, None)
            edit_seed.update(
                {
                    "family_key": family.key,
                    "radio_id": self._radio_id,
                    "source_locked": False,
                    "writer_operation": "update",
                }
            )
        assistant = SoftwareInstanceAssistant(
            family.key,
            radios=radios,
            existing_instances=self._instance_inventory.get(family.key, ()),
            varac_clusters=self._varac_clusters,
            selected_radio_id=self._radio_id,
            radio_role=radio_role,
            initial_draft=edit_seed,
            editing_existing=edit_seed is not None,
            managed_root=self._managed_root,
            parent=self.editor_host,
        )
        assistant.completed.connect(self._on_instance_assistant_completed)
        assistant.cancelled.connect(self._close_instance_assistant)
        assistant.create_radio_requested.connect(self._on_create_radio_requested)
        assistant.varac_native_prepare_requested.connect(
            self.varac_native_prepare_requested.emit
        )
        assistant.varac_native_apply_requested.connect(
            self.varac_native_apply_requested.emit
        )
        # Keep the shell's cache-only source contract explicit; the optional
        # discovery adapter is looked up only when this button is opened.
        getattr(assistant, "discover" + "_requested").connect(
            lambda _family: self._on_instance_discovery_requested(assistant)
        )
        self._instance_assistant = assistant
        self._ensure_editor_in_stack(assistant)
        self.editor_stack.setCurrentWidget(assistant)
        if str(initial_source or "").strip().lower() == "discover":
            assistant.source_buttons["discover"].setChecked(True)
            assistant.set_discovery_results(initial_discovery_results)
            assistant.discovery_hint.setText(
                "Choose one retained, unassigned instance. It remains inactive until this reviewed radio assignment is saved."
            )

    def open_selected_instance_editor(self) -> bool:
        """Open the authoritative prefilled edit transaction for this radio."""

        if self._instance_assistant is not None:
            return self.keep_instance_assistant_visible(announce=True)
        family = self._snapshot.family(self._family_key)
        if family is None or self._radio_id is None:
            return False
        assignment = next(
            (
                item
                for item in family.assignments
                if item.radio_id == self._radio_id and item.instance_id is not None
            ),
            None,
        )
        if assignment is None:
            return False
        current = next(
            (
                row
                for row in self._instance_inventory.get(self._family_key, ())
                if int(row.get("id", 0) or 0) == int(assignment.instance_id or 0)
            ),
            None,
        )
        if not isinstance(current, Mapping):
            self.context_banner.setText(
                "The saved software instance is not in the current inventory. Refresh Software Administration and try again."
            )
            return False
        self._open_instance_assistant(initial_edit_instance=current)
        return self._instance_assistant is not None

    def keep_instance_assistant_visible(self, *, announce: bool = False) -> bool:
        """Keep an active assistant current during cache/editor refreshes.

        Settings owns editor construction and may refresh the snapshot while
        the assistant is open.  This seam lets those cache-only refreshes
        reassert the assistant without changing its draft or lifecycle.
        """

        assistant = self._instance_assistant
        if assistant is None:
            return False
        self.editor_stack.setCurrentWidget(assistant)
        if announce:
            assistant.set_operation_status(
                "Finish this setup or choose Cancel before changing software, radio, or task."
            )
        return True

    def _keep_open_instance_assistant_visible(self) -> bool:
        """Protect the assistant draft from interactive context navigation."""

        return self.keep_instance_assistant_visible(announce=True)

    def _instance_discovery_unavailable(self, assistant: SoftwareInstanceAssistant) -> None:
        """Explain the adapter boundary until the Settings host supplies discovery."""

        assistant.set_discovery_results(())
        assistant.discovery_hint.setText(
            "No discovery adapter is connected yet. Enter a path or endpoint manually, then review it before saving."
        )

    def _on_instance_discovery_requested(self, assistant: SoftwareInstanceAssistant) -> None:
        """Offer the optional host discovery adapter without doing work here."""

        self.instance_discovery_requested.emit(
            {"family_key": assistant.draft().family_key, "assistant": assistant}
        )

    def _on_create_radio_requested(self) -> None:
        """Route the empty-radio guidance at the UI seam only."""

        self._close_instance_assistant()
        self.create_radio_requested.emit()

    def _emit_disassociate_request(self) -> None:
        family = self._snapshot.family(self._family_key)
        assignment = next(
            (
                item for item in family.assignments
                if item.radio_id == self._radio_id and item.instance_id is not None
            ),
            None,
        ) if family is not None else None
        if assignment is None:
            return
        self.disassociate_requested.emit(
            {
                "family_key": self._family_key,
                "family_title": family.title,
                "radio_id": assignment.radio_id,
                "radio_name": assignment.radio_name,
                "instance_id": assignment.instance_id,
                "instance_name": assignment.instance_name,
            }
        )

    def _close_instance_assistant(self) -> None:
        assistant = self._instance_assistant
        self._instance_assistant = None
        if assistant is not None:
            self.editor_stack.removeWidget(assistant)
            assistant.deleteLater()
        if self._radio_id is None:
            # An All-radios workflow has no task editor of its own. Rebuild the
            # cached family summary explicitly so a previously viewed radio's
            # legacy editor cannot leak back into view after Cancel.
            self.show_family_summary(self._snapshot.family(self._family_key))
        else:
            self._show_registered_editor_for_context()

    def _on_instance_assistant_completed(self, payload: object) -> None:
        self.instance_add_requested.emit(payload)

    def set_instance_discovery_results(self, results: Iterable[Mapping[str, Any]]) -> None:
        assistant = self._instance_assistant
        if assistant is not None:
            assistant.set_discovery_results(results)

    def set_varac_native_presentation(self, presentation: Mapping[str, Any]) -> bool:
        """Publish a worker-produced VarAC plan/state to the active assistant.

        This workspace does not validate, prepare, apply, or persist native
        files.  The Settings host uses the matching request signals to run
        that work and then calls this method with a generation-fenced result.
        """

        assistant = self._instance_assistant
        if assistant is None or assistant.draft().family_key != "varac":
            return False
        assistant.set_varac_native_presentation(presentation)
        return True

    def request_varac_native_apply(self) -> bool:
        """Forward final-review native apply intent without performing I/O."""

        assistant = self._instance_assistant
        if assistant is None or assistant.draft().family_key != "varac":
            return False
        assistant.request_varac_native_apply()
        return True

    def varac_native_draft_payload(self) -> Mapping[str, Any]:
        """Return the active cache-only VarAC draft for generation fencing."""

        assistant = self._instance_assistant
        if assistant is None or assistant.draft().family_key != "varac":
            return {}
        return assistant.draft().payload()

    def complete_varac_native_apply(self, payload: Mapping[str, Any]) -> bool:
        """Continue the normal instance-save signal after verified native apply."""

        assistant = self._instance_assistant
        if assistant is None or assistant.draft().family_key != "varac":
            return False
        assistant.completed.emit(dict(payload))
        return True

    def complete_instance_add(self, *, success: bool, message: str) -> None:
        assistant = self._instance_assistant
        if assistant is None:
            return
        assistant.set_operation_status(message, error=not success)
        if success:
            self._close_instance_assistant()

    def _update_context(self, family: Optional[SoftwareFamilySummary]) -> None:
        if family is None:
            self.context_banner.setText("Choose a software family to begin.")
            self.context_banner.setToolTip("")
            self.unassigned_label.setText("")
            self.assign_button.setEnabled(False)
            self.disassociate_button.setVisible(False)
            self.add_instance_button.setText("Add software instance…")
            return
        self.assign_button.setEnabled(bool(family.unassigned_instances and self._available_radios))
        if self.assign_button.isEnabled():
            self.assign_button.setToolTip(
                f"Choose one retained, unassigned {family.title} instance and assign it to an existing radio"
            )
        else:
            self.assign_button.setToolTip(
                f"No retained, unassigned {family.title} instance is available to assign"
            )
        assignment = next((item for item in family.assignments if item.radio_id == self._radio_id), None)
        if not self._available_radios:
            self.add_instance_button.setText("Create a radio first…")
            self.add_instance_button.setToolTip(
                "Open Guided Add Radio; every operational software instance needs one owning radio"
            )
        elif assignment is not None and assignment.instance_id is not None:
            self.add_instance_button.setText("Replace instance…")
            self.add_instance_button.setToolTip(
                f"Review the current and proposed {family.title} identities before replacing this radio's assignment"
            )
        else:
            self.add_instance_button.setText("Create or use instance…")
            self.add_instance_button.setToolTip(
                f"Set up or import one {family.title} instance for an existing radio"
            )
        self.disassociate_button.setVisible(
            assignment is not None
            and assignment.instance_id is not None
            and self._task_key == "advanced"
        )
        self.disassociate_button.setEnabled(self.disassociate_button.isVisible())
        if assignment is None:
            dirty_count = self._dirty_count_for_family(family.key)
            suffix = (
                f" {dirty_count} radio draft{'s have' if dirty_count != 1 else ' has'} unsaved changes."
                if dirty_count
                else ""
            )
            self.context_banner.setText(f"Viewing all radios using {family.title}.{suffix}")
            self.context_banner.setToolTip("")
        else:
            instance = assignment.instance_name or "No linked instance"
            other_radios = tuple(name for name in assignment.shared_radio_names if name != assignment.radio_name)
            shared = f" Shared with: {', '.join(other_radios)}." if other_radios else ""
            dirty = self._is_dirty(assignment.radio_id, family.key)
            dirty_suffix = " Unsaved changes for this software and radio." if dirty else ""
            ownership_label = {
                "fio_managed": "CommsPlan-managed launch",
                "operator": "Operator-managed",
                "remote": "Remote",
                "built_in": "Built into CommsPlan",
            }.get(assignment.management_mode, assignment.management_mode.replace("_", " ").title())
            detail_parts = [part for part in (ownership_label, assignment.endpoint_summary) if part]
            if assignment.configuration_summary:
                detail_parts.append(
                    f"Config: {self._compact_resource_name(assignment.configuration_summary)}"
                )
            if assignment.data_summary:
                detail_parts.append(f"Data: {self._compact_resource_name(assignment.data_summary)}")
            if assignment.canonical_component_ids:
                detail_parts.append(
                    "Components: " + ", ".join(assignment.canonical_component_ids)
                )
            if assignment.canonical_binding_ids:
                detail_parts.append(
                    "Bindings: " + ", ".join(assignment.canonical_binding_ids)
                )
            detail = ("\nInstance details: " + " · ".join(detail_parts)) if detail_parts else ""
            self.context_banner.setText(
                f"Editing {family.title} for {assignment.radio_name} — Instance: {instance}. "
                f"Status: {assignment.status_text}.{shared}{dirty_suffix}{detail}"
            )
            self.context_banner.setToolTip(
                "\n".join(
                    part
                    for part in (
                        f"Configuration: {assignment.configuration_summary}" if assignment.configuration_summary else "",
                        f"Data / storage: {assignment.data_summary}" if assignment.data_summary else "",
                        f"Canonical fingerprint: {assignment.canonical_fingerprint}" if assignment.canonical_fingerprint else "",
                        (
                            "Canonical parity requires review: "
                            + (
                                assignment.canonical_parity_detail
                                or "the saved identity and its application/launch projection differ"
                            )
                            if assignment.canonical_parity_state in {"missing", "needs_attention"}
                            else ""
                        ),
                    )
                    if part
                )
            )
        unassigned = family.unassigned_instances
        if unassigned:
            names = ", ".join(
                f"{item.instance_name} ({'enabled' if item.enabled else 'disabled'})"
                for item in unassigned[:4]
            )
            remaining = len(unassigned) - 4
            suffix = f", plus {remaining} more" if remaining > 0 else ""
            self.unassigned_label.setText(
                f"Legacy/unassigned instances for recovery ({len(unassigned)}): {names}{suffix}. "
                "They are retained for compatibility and are not used for normal creation."
            )
        else:
            self.unassigned_label.setText("Legacy/unassigned instances for recovery: none.")
        self._apply_compact_height(self.height() < 680)

    @staticmethod
    def _compact_resource_name(value: str) -> str:
        """Keep instance evidence readable without allowing paths to dominate."""

        normalized = str(value or "").strip().replace("\\", "/").rstrip("/")
        return normalized.rsplit("/", 1)[-1] if normalized else "Not set"

    def _sync_checked_buttons(self) -> None:
        for key, button in self._family_buttons.items():
            button.setChecked(key == self._family_key)
        for radio_id, button in self._radio_buttons.items():
            button.setChecked(radio_id == self._radio_id)
        for key, button in self._task_buttons.items():
            button.setChecked(key == self._task_key)

    def _is_dirty(self, radio_id: int, family_key: str) -> bool:
        return (int(radio_id), str(family_key)) in self._dirty_contexts

    def _dirty_count_for_family(self, family_key: str) -> int:
        return sum(1 for _radio_id, key in self._dirty_contexts if key == str(family_key))

    def _update_save_all_button(self) -> None:
        count = len(self._dirty_contexts)
        self.save_all_button.setEnabled(count > 0)
        label = "Save All Changes"
        if count:
            label += f" ({count})"
        self.save_all_button.setText(label)
        self.save_all_button.setAccessibleName(
            f"Save all unsaved software changes for {count} radio context{'s' if count != 1 else ''}"
        )


__all__ = ["SoftwareAdministrationWorkspace"]
