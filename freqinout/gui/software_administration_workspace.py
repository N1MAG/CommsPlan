"""Cache-only workspace shell for Settings software administration.

The host owns navigation, draft state, editor construction, and any explicit
validation work.  This widget intentionally only renders an immutable
``SoftwareAdministrationSnapshot`` and emits selection/routing signals.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping, Optional

from PySide6.QtCore import QSignalBlocker, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QButtonGroup,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.software_administration_model import (
    SoftwareAdministrationSnapshot,
    SoftwareFamilySummary,
)
from freqinout.core.configuration_attention import (
    LABELS as _CONFIGURATION_ATTENTION_LABELS,
    SYMBOLS as _CONFIGURATION_ATTENTION_SYMBOLS,
)
from freqinout.gui.theme import (
    button_height_for_font,
    contrast_text_for_background,
    fit_combo_box_to_contents,
    get_theme,
    label_style,
    resolve_theme,
    resolve_ui_text_scale,
)
from freqinout.gui.current_page_stack import CurrentPageStack
from freqinout.gui.operator_instance_assistant import SoftwareInstanceAssistant


_TASKS: dict[str, tuple[tuple[str, str], ...]] = {
    "js8call": (
        ("overview", "Overview"), ("application_profile", "Application & Profile"),
        ("api_radio", "API & Radio"), ("message_storage", "Message Storage"),
        ("ingest_forms", "Message ingest"), ("launch", "Launch"),
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
        ("overview", "Overview"), ("installation", "Installation"), ("profile", "Activity & imports"),
        ("forms", "CP Forms Library"), ("launch", "Launch"), ("health", "Health"),
        ("advanced", "Advanced"),
    ),
    "fio_spotter": (
        ("overview", "Overview"), ("dependencies", "Dependencies & Radio Mapping"),
        ("forms", "Forms Library"), ("operational_workspace", "Open CommsPlan Spotter"),
    ),
}


# Configuration attention is deliberately a separate projection from runtime
# status and from the selected navigation item.  The host publishes these
# values after its own validation/read-model work; this widget never derives
# them from an editor, dirty context, or a status probe.
_TASK_CONFIGURATION_STATES = frozenset(
    {"complete", "review", "unknown", "optional", "unsaved", "error"}
)
_TASK_CONFIGURATION_PRESENTATION: dict[str, tuple[str, str]] = {
    state: (
        _CONFIGURATION_ATTENTION_SYMBOLS[state],
        _CONFIGURATION_ATTENTION_LABELS[state],
    )
    for state in _TASK_CONFIGURATION_STATES
}
_TASK_CONFIGURATION_STATE_ROLE = Qt.UserRole + 1
_TASK_CONFIGURATION_REASON_ROLE = Qt.UserRole + 2


class _TaskNavigationDelegate(QStyledItemDelegate):
    """Paint task rows without Qt's single-line item elision.

    The task panel is a bounded navigation surface.  Its rows can contain a
    second line for an explicit configuration badge, so the default item
    delegate's eliding path would hide the very attention signal the row is
    meant to expose.  Selection/focus backgrounds remain owned by the shared
    QListWidget stylesheet; this delegate only supplies wrapped text.
    """

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        base = super().sizeHint(option, index)
        text = str(index.data(Qt.DisplayRole) or "")
        width = max(120, int(option.rect.width() or 0) - 22)
        metrics = QFontMetrics(option.font)
        bounds = metrics.boundingRect(
            QRect(0, 0, width, 1_000_000),
            Qt.TextWordWrap,
            text,
        )
        return QSize(base.width(), max(base.height(), bounds.height() + 12))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        widget = option.widget
        selected = bool(option.state & QStyle.State_Selected)
        enabled = bool(option.state & QStyle.State_Enabled)
        hovered = bool(option.state & QStyle.State_MouseOver)
        def theme_color(name: str, fallback_role: QPalette.ColorRole) -> QColor:
            value = widget.property(name) if widget is not None else None
            if value:
                return QColor(str(value))
            group = QPalette.Active if enabled else QPalette.Disabled
            return option.palette.color(group, fallback_role)

        if selected:
            background = theme_color("fioTaskSelectedSurface", QPalette.Highlight)
            border = theme_color("fioTaskSelectedBorder", QPalette.Highlight)
            foreground = theme_color("fioTaskSelectedText", QPalette.HighlightedText)
        else:
            background = theme_color("fioTaskSurface", QPalette.Base)
            border = theme_color("fioTaskHoverBorder" if hovered else "fioTaskBorder", QPalette.Mid)
            foreground = theme_color("fioTaskText" if enabled else "fioTaskMuted", QPalette.Text)
        item_rect = option.rect.adjusted(1, 1, -1, -1)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(background)
        painter.setPen(border)
        painter.drawRoundedRect(item_rect, 6, 6)
        text_rect = item_rect.adjusted(7, 2, -7, -2)
        painter.setPen(foreground)
        painter.drawText(
            text_rect,
            int(Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap),
            str(index.data(Qt.DisplayRole) or ""),
        )
        painter.restore()


class SoftwareAdministrationWorkspace(QWidget):
    """Software-first Settings shell with no configuration or endpoint I/O."""

    family_selected = Signal(str)
    radio_selected = Signal(object)
    task_selected = Signal(str)
    assign_requested = Signal(str)
    instance_add_requested = Signal(object)
    operator_edit_opened = Signal(object)
    operator_unfinished_discard_requested = Signal(object)
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
    # Settings owns the status worker and existing ``validate`` route.  This
    # signal is intentionally a cache-only intent with the selected context;
    # the workspace never probes, reloads, or persists on its own.
    status_check_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._snapshot = SoftwareAdministrationSnapshot(families=())
        self._family_key = ""
        self._radio_id: Optional[int] = None
        self._radio_context_explicit = False
        self._radio_inventory_known = False
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
        # The compact controls below are the authoritative visible route.
        # The legacy button strips remain as hidden compatibility surfaces for
        # older Settings integrations and tests; they are never part of the
        # operator scan path.
        self._context_controls_loading = False
        self._task_list_items: dict[str, QListWidgetItem] = {}
        self._task_configuration_states: dict[str, dict[str, str]] = {}
        self._manage_menu: Optional[QMenu] = None
        self._manage_summary_action: Optional[QAction] = None
        self._assign_action: Optional[QAction] = None
        self._add_instance_action: Optional[QAction] = None
        self._disassociate_action: Optional[QAction] = None
        self._save_all_action: Optional[QAction] = None
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
        self._embedded = False
        self._observation_summary = ""
        self._context_row_layout: Optional[QGridLayout] = None
        self._context_row_compact: Optional[bool] = None
        self._context_widgets: tuple[QWidget, ...] = ()
        self._build_ui()
        self.apply_theme(self._theme)

    def _build_manage_instances_menu(self) -> None:
        """Create the secondary instance-maintenance menu once.

        The menu is a presentation surface over existing signal seams.  It
        never discovers, validates, or persists anything itself.
        """
        menu = QMenu(self.manage_instances_button)
        menu.setObjectName("softwareAdministrationManageInstancesMenu")
        summary = QAction("No recovery items", menu)
        summary.setEnabled(False)
        menu.addAction(summary)
        menu.addSeparator()
        assign = QAction("Assign existing instance…", menu)
        assign.triggered.connect(self._emit_assign_request)
        menu.addAction(assign)
        add = QAction("Create or use instance…", menu)
        add.triggered.connect(self._start_instance_action)
        menu.addAction(add)
        disassociate = QAction("Disassociate instance…", menu)
        disassociate.triggered.connect(self._emit_disassociate_request)
        menu.addAction(disassociate)
        menu.addSeparator()
        save_all = QAction("Save all drafts", menu)
        save_all.triggered.connect(self.save_all_requested.emit)
        menu.addAction(save_all)
        self._manage_menu = menu
        self._manage_summary_action = summary
        self._assign_action = assign
        self._add_instance_action = add
        self._disassociate_action = disassociate
        self._save_all_action = save_all
        self.manage_instances_button.setMenu(menu)

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

        # Compact, labeled context selectors are the only visible family and
        # radio navigation.  Each label keeps its natural width while the
        # adjacent selector takes the remaining space.  The layout reflows at
        # compact widths so actions remain visible and the page never needs a
        # horizontal scrollbar for this row.
        context_row = QGridLayout()
        context_row.setContentsMargins(0, 0, 0, 0)
        context_row.setHorizontalSpacing(6)
        context_row.setVerticalSpacing(4)
        software_label = QLabel("Software")
        software_label.setObjectName("softwareAdministrationSoftwareLabel")
        software_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.software_combo = QComboBox()
        self.software_combo.setObjectName("softwareAdministrationSoftwareCombo")
        self.software_combo.setAccessibleName("Software family")
        self.software_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.software_combo.currentIndexChanged.connect(self._on_software_combo_changed)
        self.software_family_combo = self.software_combo
        self.family_combo = self.software_combo
        radio_label = QLabel("Radio")
        radio_label.setObjectName("softwareAdministrationRadioLabel")
        radio_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.radio_combo = QComboBox()
        self.radio_combo.setObjectName("softwareAdministrationRadioCombo")
        self.radio_combo.setAccessibleName("Radio context")
        self.radio_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.radio_combo.currentIndexChanged.connect(self._on_radio_combo_changed)
        self.radio_context_combo = self.radio_combo
        self.manage_instances_button = QToolButton()
        self.manage_instances_button.setObjectName("softwareAdministrationManageInstancesButton")
        self.manage_instances_button.setText("Manage instances")
        self.manage_instances_button.setAccessibleName("Manage software instances")
        self.manage_instances_button.setToolTip(
            "Assign, replace, disassociate, or recover software instances; save all drafts as a secondary action."
        )
        self.manage_instances_button.setPopupMode(QToolButton.InstantPopup)
        self.manage_instances_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.status_check_button = QToolButton()
        self.status_check_button.setObjectName("softwareAdministrationStatusCheckButton")
        self.status_check_button.setText("Check status")
        self.status_check_button.setAccessibleName("Check software status")
        self.status_check_button.setToolTip(
            "Request a fresh status check for the selected software context. This does not save changes."
        )
        self.status_check_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.status_check_button.clicked.connect(self.request_status_check)
        self._build_manage_instances_menu()
        self._context_widgets = (
            software_label,
            self.software_combo,
            radio_label,
            self.radio_combo,
            self.manage_instances_button,
            self.status_check_button,
        )
        self._context_row_layout = context_row
        self._apply_context_row_layout(compact=False)
        root.addLayout(context_row)

        # Legacy strips are retained for old host seams, then hidden after
        # construction.  New code must use the combo/list route above.
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

        self.task_navigation_label = QLabel("Configuration tasks")
        self.task_navigation_label.setObjectName("softwareAdministrationTaskNavigationLabel")
        self.task_navigation_label.setAccessibleName("Configuration task navigation")
        self.task_navigation = QListWidget()
        self.task_navigation.setObjectName("softwareAdministrationTaskNavigation")
        self.task_navigation.setAccessibleName("Configuration task navigation")
        self.task_navigation.setSelectionMode(QListWidget.SingleSelection)
        self.task_navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_navigation.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.task_navigation.setWordWrap(True)
        self.task_navigation.setTextElideMode(Qt.ElideNone)
        self.task_navigation.setItemDelegate(_TaskNavigationDelegate(self.task_navigation))
        self.task_navigation.currentItemChanged.connect(self._on_task_list_changed)
        # Compatibility aliases make the intended route discoverable to host
        # code while avoiding a second parallel navigation implementation.
        self.task_list = self.task_navigation
        self.task_navigation_list = self.task_navigation
        self.task_list_widget = self.task_navigation

        self.unassigned_label = QLabel()
        self.unassigned_label.setWordWrap(True)
        self.unassigned_label.setAccessibleName("Legacy unassigned software instances for recovery")
        root.addWidget(self.unassigned_label)

        self.assign_button = QPushButton("Assign Existing Instance…")
        self.assign_button.setObjectName("softwareAdministrationSecondaryAction")
        self.assign_existing_button = self.assign_button
        self.assign_button.setAccessibleName("Assign an existing software instance to a radio")
        self.assign_button.setToolTip(
            "Open the radio assignment editor for an existing instance in the selected software family"
        )
        self.assign_button.clicked.connect(self._emit_assign_request)
        self.add_instance_button = QPushButton("Add software instance…")
        self.add_instance_button.setObjectName("softwareAdministrationSecondaryAction")
        self.add_instance_button.setAccessibleName("Add software instance")
        self.add_instance_button.setToolTip(
            "Guided setup for an existing, CommsPlan-managed, manual, or remote software instance"
        )
        self.add_instance_button.clicked.connect(self._start_instance_action)
        self.disassociate_button = QPushButton("Disassociate…")
        self.disassociate_button.setObjectName("softwareAdministrationSecondaryAction")
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
        # Actions are exposed by Manage instances.  Keep the old widgets
        # alive but out of the normal layout for host/test compatibility.
        for legacy_action in (
            self.assign_button,
            self.add_instance_button,
            self.disassociate_button,
            self.save_all_button,
        ):
            legacy_action.setVisible(False)

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
        # The Settings host owns the outer page geometry.  Active editors own
        # their own inner scroll area, so this stack must accept a smaller
        # height than an editor's natural form hint at compact viewports.
        self.editor_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.editor_stack.setMinimumSize(0, 0)
        self.editor_stack.setAccessibleName("Software configuration editor surface")
        self.editor_stack.addWidget(self.editor_placeholder)
        self.editor_host_layout.addWidget(self.editor_stack)
        self.editor_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.editor_host.setMinimumWidth(0)
        self.editor_host.setMinimumHeight(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        task_panel = QWidget()
        task_panel.setObjectName("softwareAdministrationTaskPanel")
        task_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        task_panel_layout = QVBoxLayout(task_panel)
        task_panel_layout.setContentsMargins(0, 0, 0, 0)
        task_panel_layout.setSpacing(4)
        self.task_navigation_label.hide()
        task_panel_layout.addWidget(self.task_navigation, 1)
        task_panel.setMinimumWidth(176)
        task_panel.setMaximumWidth(270)
        body.addWidget(task_panel, 0)
        body.addWidget(self.editor_host, 1)
        root.addLayout(body, 1)

        # The old strips and numbered prompts are compatibility objects only.
        # Hiding them after all references are established prevents accidental
        # reintroduction of the former three-row layout.
        for legacy in (
            self.family_prompt_label,
            self.family_strip,
            self.radio_prompt_label,
            self.radio_strip,
            self.task_prompt_label,
            self.task_strip,
            self.unassigned_label,
        ):
            legacy.setVisible(False)

        QWidget.setTabOrder(self.radio_combo, self.software_combo)
        QWidget.setTabOrder(self.software_combo, self.manage_instances_button)
        QWidget.setTabOrder(self.manage_instances_button, self.status_check_button)
        QWidget.setTabOrder(self.status_check_button, self.task_navigation)

    def set_embedded(self, embedded: bool = True) -> None:
        """Avoid repeating the Settings section title inside the workspace."""

        self._embedded = bool(embedded)
        self.heading_label.setVisible(not self._embedded)
        # New task editors can suppress the identity/runtime line already
        # carried by the workspace banner.  Keep this optional for older host
        # editor implementations and preserve operation/validation messages.
        for editor in tuple(self._registered_editors.values()):
            setter = getattr(editor, "set_embedded_context", None)
            if callable(setter):
                setter(self._embedded)
        setter = getattr(self._legacy_editor, "set_embedded_context", None)
        if callable(setter):
            setter(self._embedded)

    def request_status_check(self) -> bool:
        """Emit the host-owned status refresh intent for the selected context."""

        if not self._family_key:
            return False
        self.status_check_requested.emit()
        return True

    def set_observation_summary(self, text: str) -> None:
        """Publish host-owned status freshness beside the shared context."""

        self._observation_summary = str(text or "").strip()
        self._update_context(self._snapshot.family(self._family_key))

    def status_summary_freshness(self) -> Mapping[str, object]:
        """Return the published status summary without inventing an age.

        The compact workspace read model currently carries the normalized
        status label but not an observation timestamp.  Returning an explicit
        unavailable freshness value lets a host with richer evidence extend
        the summary without making this cache-only layer claim a fresh result.
        """

        family = self._snapshot.family(self._family_key)
        assignment = next(
            (item for item in family.assignments if item.radio_id == self._radio_id),
            None,
        ) if family is not None and self._radio_id is not None else None
        return {
            "family_key": self._family_key,
            "radio_id": self._radio_id,
            "status": assignment.status_text if assignment is not None else "Unknown",
            "freshness": "Unavailable",
            "last_observed": None,
        }

    def set_instance_context(
        self,
        *,
        radios: Iterable[Mapping[str, Any]],
        inventory_by_family: Mapping[str, Iterable[Mapping[str, Any]]],
        varac_clusters: Iterable[Mapping[str, Any]] = (),
        managed_root: str = "",
    ) -> None:
        """Cache Settings-owned values used when the explicit assistant opens."""

        self._radio_inventory_known = True
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
                + f"\n\nUnassigned retained instances for recovery ({len(family.unassigned_instances)}): {names}{suffix}."
            )
        if self.keep_instance_assistant_visible():
            return
        self.editor_stack.setCurrentWidget(self.editor_placeholder)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._apply_context_row_layout(self._context_row_needs_reflow(event.size().width()))
        self._apply_compact_height(event.size().height() < 680)
        self._apply_task_navigation_row_height()

    def _context_row_needs_reflow(self, width: int) -> bool:
        """Use font metrics and action widths to choose the compact row form."""

        if width <= 0:
            return False
        natural = sum(
            widget.sizeHint().width()
            for widget in self._context_widgets
            if widget is not None
        )
        # Include spacing and a small expansion allowance for the two closed
        # selectors.  Font scaling therefore drives reflow before clipping.
        return width < natural + self.fontMetrics().horizontalAdvance("  ") * 4 + 16

    def _apply_context_row_layout(self, compact: bool) -> None:
        layout = self._context_row_layout
        if layout is None or self._context_row_compact is bool(compact):
            return
        self._context_row_compact = bool(compact)
        while layout.count():
            layout.takeAt(0)
        software_label, software_combo, radio_label, radio_combo, manage, check = self._context_widgets
        for column in range(5):
            layout.setColumnStretch(column, 0)
        software_combo.setMinimumWidth(0)
        radio_combo.setMinimumWidth(0)
        if compact:
            layout.addWidget(radio_label, 0, 0)
            layout.addWidget(radio_combo, 0, 1)
            layout.addWidget(manage, 0, 2, 2, 1)
            layout.addWidget(check, 0, 3, 2, 1)
            layout.addWidget(software_label, 1, 0)
            layout.addWidget(software_combo, 1, 1)
            layout.setColumnStretch(1, 1)
        else:
            layout.addWidget(radio_label, 0, 0)
            layout.addWidget(radio_combo, 0, 1)
            layout.addWidget(software_label, 0, 2)
            layout.addWidget(software_combo, 0, 3)
            layout.addWidget(manage, 0, 4)
            layout.addWidget(check, 0, 5)
            layout.setColumnStretch(1, 1)
            layout.setColumnStretch(3, 1)
        layout.invalidate()

    def _apply_compact_height(self, compact: bool) -> None:
        """Give the task editor priority when vertical space is constrained."""

        # The context row and task list remain present at every supported
        # height.  Compact mode shortens explanatory copy and lets the editor
        # own the available vertical space.
        self.explanation_label.setVisible(not compact)
        self.family_prompt_label.setVisible(False)
        self.radio_prompt_label.setVisible(False)
        self.task_prompt_label.setVisible(False)
        self.family_strip.setVisible(False)
        self.radio_strip.setVisible(False)
        self.task_strip.setVisible(False)
        strip_height = button_height_for_font(
            self.assign_button,
            vertical_padding=14 if compact else 18,
            floor=30,
        )
        for strip in (self.family_strip, self.radio_strip, self.task_strip):
            strip.setProperty("fioChipRowHeight", int(strip_height))
            self._sync_horizontal_strip_height(strip)
        self.unassigned_label.setVisible(False)
        for legacy_action in (
            self.assign_button,
            self.add_instance_button,
            self.disassociate_button,
            self.save_all_button,
        ):
            legacy_action.setVisible(False)
        # The active editor's inner scroll area owns overflow.  Do not impose
        # a viewport-height floor here: that floor makes the Settings page
        # taller than its host at compact widths and clips the editor footer.
        self.editor_host.setMinimumHeight(0)

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
    def _sync_horizontal_strip_content_width(scroll: QScrollArea, layout: QHBoxLayout) -> None:
        """Keep a dense chip row wider than its viewport when it overflows.

        ``QScrollArea.widgetResizable`` normally shrinks the content widget to
        the viewport, even when its layout's size hint is wider.  That makes a
        row of ``Preferred`` tool buttons look complete while silently
        dropping its horizontal scroll range.  Give the content widget an
        explicit minimum based on the chip layout after each rebuild; the
        scroll area can then expose the remaining chips through its own bar.
        """

        content = scroll.widget()
        if content is None:
            return
        layout.activate()
        content.setMinimumWidth(max(0, layout.sizeHint().width()))

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

    def update_runtime_status_labels(self, snapshot: SoftwareAdministrationSnapshot) -> None:
        """Update registered software editor status text without rebuilding them.

        The Settings host may publish a newer cache-only runtime projection
        while an editor contains an unsaved draft.  Only the dedicated
        ``set_runtime_status`` presentation seam is called here; validation or
        operator-application widgets do not expose that seam and are left
        untouched.
        """
        if snapshot is None:
            return
        for (family_key, radio_id, _task_key), editor in tuple(self._registered_editors.items()):
            if radio_id is None:
                continue
            setter = getattr(editor, "set_runtime_status", None)
            if not callable(setter):
                continue
            family = snapshot.family(family_key)
            assignment = next(
                (item for item in family.assignments if item.radio_id == radio_id),
                None,
            ) if family is not None else None
            if assignment is not None:
                setter(assignment.status_text)

    def select_context(
        self, family_key: str, radio_id: Optional[int] = None, task_key: Optional[str] = None
    ) -> None:
        family = self._snapshot.family(family_key)
        if self._instance_assistant is not None:
            requested_family = family.key if family is not None else ""
            requested_task = task_key or self._task_key
            if (requested_family, radio_id, requested_task) != (self._family_key, self._radio_id, self._task_key):
                if self._keep_open_instance_assistant_visible():
                    return
        self._family_key = family.key if family is not None else ""
        valid_radio_ids = {ident for ident, _name, _assignment in self._radio_choices(family)}
        if radio_id is not None and radio_id not in valid_radio_ids:
            # Losing an inventory item is not an operator choosing All radios.
            self._radio_context_explicit = False
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
        self._sync_visible_context_controls()
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

    def set_task_configuration_states(
        self,
        states: Mapping[str, Mapping[str, object]] | None,
    ) -> None:
        """Publish explicit configuration attention states for task rows.

        ``states`` is a host-owned, cache-only projection keyed by task key.
        Each value may contain ``state`` (one of ``complete``, ``review``,
        ``unknown``, ``optional``, ``unsaved``, or ``error``), plus an
        optional operator-facing ``label`` and ``reason``.  No state is
        inferred from runtime status, dirty contexts, selected editors, or
        any other workspace value.  Invalid or incomplete entries are simply
        omitted so an unverified task remains an unbadged navigation choice.

        Existing ``QListWidgetItem`` rows are updated in place.  This keeps
        keyboard focus, current selection, and host code holding an item
        reference stable while a read-model refresh changes the badges.
        """

        normalized: dict[str, dict[str, str]] = {}
        if isinstance(states, Mapping):
            for task_key, value in states.items():
                key = str(task_key or "").strip()
                if not key or not isinstance(value, Mapping):
                    continue
                state = str(value.get("state", "") or "").strip().lower()
                if state not in _TASK_CONFIGURATION_STATES:
                    continue
                label = str(value.get("label", "") or "").strip()
                reason = str(value.get("reason", "") or "").strip()
                normalized[key] = {
                    "state": state,
                    "label": label,
                    "reason": reason,
                }
        self._task_configuration_states = normalized
        self._refresh_task_configuration_rows()
        assistant = getattr(self, "_instance_assistant", None)
        if assistant is not None:
            assistant.set_configuration_attention(normalized.get(self._task_key))

    def task_configuration_states(self) -> Mapping[str, Mapping[str, str]]:
        """Return the currently published task-state projection."""

        return {
            key: dict(value)
            for key, value in self._task_configuration_states.items()
        }

    def _task_configuration_parts(self, task_key: str) -> tuple[str, str, str]:
        """Return ``(suffix, accessible_status, reason)`` for one task key."""

        entry = self._task_configuration_states.get(str(task_key), {})
        state = entry.get("state", "")
        if not state:
            return "", "", ""
        icon, default_label = _TASK_CONFIGURATION_PRESENTATION[state]
        label = entry.get("label") or default_label
        suffix = icon
        accessible = f"Configuration status: {icon} {label}."
        reason = entry.get("reason", "")
        if reason:
            accessible += f" {reason}"
        return suffix, accessible, reason

    def _task_display_text(self, task_key: str, base_label: str) -> str:
        suffix, _accessible, _reason = self._task_configuration_parts(task_key)
        return f"{suffix}  {base_label}" if suffix else base_label

    def _task_tooltip(self, task_key: str, base_label: str, enabled: bool) -> str:
        tooltip = (
            f"Select {base_label} task"
            if enabled
            else f"Choose a radio before configuring {base_label}"
        )
        _suffix, accessible, _reason = self._task_configuration_parts(task_key)
        if accessible:
            tooltip += f" {accessible}"
        return tooltip

    def _apply_task_item_presentation(
        self,
        item: QListWidgetItem,
        task_key: str,
        base_label: str,
        enabled: bool,
    ) -> None:
        """Update one row without replacing its QListWidgetItem instance."""

        suffix, accessible, reason = self._task_configuration_parts(task_key)
        # The shared legend owns status words; keep only the icon beside the
        # original section name. Full semantics remain accessible and in hints.
        item_text = f"{suffix}  {base_label}" if suffix else base_label
        item.setText(item_text)
        item.setData(Qt.UserRole, task_key)
        item.setData(_TASK_CONFIGURATION_STATE_ROLE, self._task_configuration_states.get(task_key, {}).get("state", ""))
        item.setData(_TASK_CONFIGURATION_REASON_ROLE, reason)
        item.setToolTip(self._task_tooltip(task_key, base_label, enabled))
        accessible_role = getattr(Qt, "AccessibleTextRole", Qt.ToolTipRole)
        item.setData(
            accessible_role,
            f"{base_label}. {accessible}" if accessible else base_label,
        )
        flags = item.flags()
        if enabled:
            item.setFlags(flags | Qt.ItemIsEnabled)
        else:
            item.setFlags(flags & ~Qt.ItemIsEnabled)
        item.setSizeHint(QSize(0, self._task_item_height(item_text)))

    def _task_item_height(self, text: str) -> int:
        """Return a font-derived height for a possibly wrapped task row."""

        row_floor = button_height_for_font(
            self.task_navigation,
            vertical_padding=8 if self._text_scale <= 1.0 else 10,
            floor=30,
        )
        viewport_width = self.task_navigation.viewport().width()
        if viewport_width <= 0:
            viewport_width = self.task_navigation.width()
        # Account for the list frame, item margins, and the two item-side
        # paddings from the shared navigation treatment.  A conservative
        # fallback is only used before the widget has received geometry.
        content_width = max(140, viewport_width - 22) if viewport_width > 0 else 220
        metrics = self.task_navigation.fontMetrics()
        bounds = metrics.boundingRect(
            QRect(0, 0, content_width, 1_000_000),
            Qt.TextWordWrap,
            text,
        )
        text_height = max(metrics.lineSpacing(), bounds.height())
        one_line_height = max(1, metrics.boundingRect("Ag").height())
        return max(row_floor, row_floor + max(0, text_height - one_line_height))

    def _refresh_task_configuration_rows(self) -> None:
        """Refresh task buttons and list rows from the explicit state cache."""

        tasks = _TASKS.get(self._family_key, ())
        for key, base_label in tasks:
            button = self._task_buttons.get(key)
            if button is not None:
                enabled = self._radio_id is not None or key == "overview"
                display = self._task_display_text(key, base_label)
                button.setText(display.replace("&", "&&"))
                # Keep the legacy button API's canonical task name stable;
                # assistive clients can discover the explicit attention
                # projection through the description and tooltip.
                button.setAccessibleName(base_label)
                _suffix, accessible, _reason = self._task_configuration_parts(key)
                button.setAccessibleDescription(accessible)
                button.setToolTip(self._task_tooltip(key, base_label, enabled))
            item = self._task_list_items.get(key)
            if item is not None:
                self._apply_task_item_presentation(
                    item,
                    key,
                    base_label,
                    self._radio_id is not None or key == "overview",
                )
        self._sync_checked_buttons()

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
        if widget is not None:
            setter = getattr(widget, "set_embedded_context", None)
            if callable(setter):
                setter(self._embedded)
            self._apply_editor_theme(widget)
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
        setter = getattr(widget, "set_embedded_context", None)
        if callable(setter):
            setter(self._embedded)
        self._apply_editor_theme(widget)
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

    def _apply_editor_theme(self, widget: Optional[QWidget]) -> None:
        """Forward the current shared palette to an editor when supported."""

        apply_theme = getattr(widget, "apply_theme", None)
        if callable(apply_theme):
            apply_theme(dict(self._theme))

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
        self.task_navigation.setProperty("fioTaskSurface", theme["surface_alt"])
        self.task_navigation.setProperty("fioTaskBorder", theme["border"])
        self.task_navigation.setProperty("fioTaskHoverBorder", theme["accent_hover"])
        self.task_navigation.setProperty("fioTaskSelectedSurface", theme["accent"])
        self.task_navigation.setProperty("fioTaskSelectedBorder", theme["accent_active"])
        self.task_navigation.setProperty("fioTaskSelectedText", accent_text)
        self.task_navigation.setProperty("fioTaskText", theme["text"])
        self.task_navigation.setProperty("fioTaskMuted", theme["text_muted"])
        self.setStyleSheet(
            "QWidget { background: %(bg)s; color: %(text)s; }"
            "QLabel { color: %(text)s; }"
            "QLabel#softwareAdministrationContextBanner { background: %(surface_alt)s;"
            " border: 1px solid %(accent)s; border-radius: 6px; padding: 7px; font-weight: 600; }"
            "QWidget#softwareAdministrationEditorHost { background: %(surface)s;"
            " border: 1px solid %(border)s; border-radius: 6px; }"
            "QComboBox { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s;"
            " border-radius: 6px; padding: 5px 8px; }"
            "QListWidget#softwareAdministrationTaskNavigation { background: %(surface)s; color: %(text)s;"
            " border: 1px solid %(border)s; border-radius: 6px; padding: 3px; outline: none; }"
            "QListWidget#softwareAdministrationTaskNavigation:focus { border: 2px solid %(focus)s; }"
            "QListWidget#softwareAdministrationTaskNavigation::item { background: %(surface_alt)s;"
            " border: 1px solid %(border)s; border-radius: 6px; padding: 6px 8px; margin: 2px 1px; }"
            "QListWidget#softwareAdministrationTaskNavigation::item:hover { border-color: %(accent_hover)s; }"
            "QListWidget#softwareAdministrationTaskNavigation::item:focus { border-color: %(focus)s; }"
            "QListWidget#softwareAdministrationTaskNavigation::item:selected { background: %(accent)s;"
            " color: %(accent_text)s; border-color: %(accent_active)s; }"
            "QListWidget#softwareAdministrationTaskNavigation::item:disabled { color: %(text_muted)s;"
            " background: %(surface)s; border-color: %(border)s; }"
            "QToolButton { background: %(surface_alt)s; border: 1px solid %(border)s;"
            " border-radius: 6px; padding: 6px 9px; }"
            "QToolButton:checked { background: %(accent)s; color: %(accent_text)s; border-color: %(accent_active)s; }"
            "QToolButton:hover { border-color: %(accent)s; }"
            "QPushButton { background: %(accent)s; color: %(accent_text)s; border: 1px solid %(accent_active)s;"
            " border-radius: 6px; padding: 6px 10px; font-weight: 600; }"
            "QPushButton#softwareAdministrationSecondaryAction { background: %(surface_alt)s; color: %(text)s;"
            " border-color: %(border)s; font-weight: 500; }"
            "QPushButton#softwareAdministrationSaveAllButton { background: %(surface_alt)s; color: %(text)s;"
            " border-color: %(accent)s; }"
            "QPushButton:disabled { background: %(surface_alt)s; color: %(text_muted)s; border-color: %(border)s; }"
            % {**theme, "accent_text": accent_text}
        )
        self.heading_label.setStyleSheet(label_style("text", theme, weight=700))
        if self._instance_assistant is not None:
            self._instance_assistant.apply_theme(theme)
        seen: set[int] = set()
        for editor in (*self._registered_editors.values(), self._legacy_editor):
            if editor is None or id(editor) in seen or editor is self._instance_assistant:
                continue
            seen.add(id(editor))
            self._apply_editor_theme(editor)
        self._apply_minimum_hit_heights()
        self._context_row_compact = None
        self._apply_context_row_layout(self._context_row_needs_reflow(self.width()))

    def _apply_minimum_hit_heights(self) -> None:
        height = button_height_for_font(self.assign_button, vertical_padding=12, floor=30)
        for button in (*self._family_buttons.values(), *self._radio_buttons.values(), *self._task_buttons.values()):
            button.setMinimumHeight(height)
        self.assign_button.setMinimumHeight(height)
        self.add_instance_button.setMinimumHeight(height)
        self.disassociate_button.setMinimumHeight(height)
        self.save_all_button.setMinimumHeight(height)
        self.software_combo.setMinimumHeight(height)
        self.radio_combo.setMinimumHeight(height)
        self.manage_instances_button.setMinimumHeight(height)
        self.status_check_button.setMinimumHeight(height)
        self._apply_task_navigation_row_height()

    def _apply_task_navigation_row_height(self) -> None:
        for index in range(self.task_navigation.count()):
            item = self.task_navigation.item(index)
            if item is not None:
                item.setSizeHint(QSize(0, self._task_item_height(item.text())))

    def _rebuild_software_combo(self) -> None:
        combo = self.software_combo
        self._context_controls_loading = True
        try:
            with QSignalBlocker(combo):
                combo.clear()
                for family in self._snapshot.families:
                    dirty_count = self._dirty_count_for_family(family.key)
                    # Closed selectors carry only the identity.  The popup
                    # and tooltip retain the complete operator context so
                    # counts/status/dirty state do not truncate the choice.
                    combo.addItem(family.title, family.key)
                    index = combo.count() - 1
                    combo.setItemData(
                        index,
                        self._family_choice_details(family, dirty_count),
                        Qt.ToolTipRole,
                    )
                    combo.setItemData(
                        index,
                        self._family_choice_details(family, dirty_count),
                        getattr(Qt, "AccessibleTextRole", Qt.ToolTipRole),
                    )
                index = combo.findData(self._family_key)
                combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))
                fit_combo_box_to_contents(combo)
                combo.setMaximumWidth(16777215)
        finally:
            self._context_controls_loading = False

    def _radio_choices(self, family: Optional[SoftwareFamilySummary]) -> tuple:
        """All configured radios, with optional per-family assignment evidence."""
        names = {int(row["id"]): str(row.get("name") or f"Radio {row['id']}")
                 for row in self._available_radios if row.get("id")}
        if not self._radio_inventory_known:
            for candidate in self._snapshot.families:
                for assignment in candidate.assignments:
                    names.setdefault(assignment.radio_id, assignment.radio_name)
        assignments = {item.radio_id: item for item in family.assignments} if family else {}
        return tuple((ident, name, assignments.get(ident)) for ident, name in names.items())

    def _radio_context_details(self, ident: int, name: str, assignment, family) -> str:
        if assignment is not None:
            return self._radio_choice_details(assignment, family.key)
        return f"{name}: {family.title if family else 'Software'} is not configured. Choose Create or use instance from Manage instances."

    def _rebuild_radio_combo(self, family: Optional[SoftwareFamilySummary]) -> None:
        combo = self.radio_combo
        self._context_controls_loading = True
        try:
            with QSignalBlocker(combo):
                combo.clear()
                dirty_count = self._dirty_count_for_family(family.key) if family else 0
                all_label = "All radios"
                combo.addItem(all_label, None)
                combo.setItemData(
                    combo.count() - 1,
                    self._all_radios_choice_details(family, dirty_count),
                    Qt.ToolTipRole,
                )
                combo.setItemData(
                    combo.count() - 1,
                    self._all_radios_choice_details(family, dirty_count),
                    getattr(Qt, "AccessibleTextRole", Qt.ToolTipRole),
                )
                for ident, name, assignment in self._radio_choices(family):
                    combo.addItem(name, ident)
                    index = combo.count() - 1
                    combo.setItemData(
                        index,
                        self._radio_context_details(ident, name, assignment, family),
                        Qt.ToolTipRole,
                    )
                    combo.setItemData(
                        index,
                        self._radio_context_details(ident, name, assignment, family),
                        getattr(Qt, "AccessibleTextRole", Qt.ToolTipRole),
                    )
                index = combo.findData(self._radio_id)
                combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))
                fit_combo_box_to_contents(combo)
                combo.setMaximumWidth(16777215)
        finally:
            self._context_controls_loading = False

    def _rebuild_task_navigation(self, tasks: tuple[tuple[str, str], ...]) -> None:
        self._context_controls_loading = True
        try:
            with QSignalBlocker(self.task_navigation):
                # Reuse rows by task key when a context refresh has the same
                # task set.  Besides avoiding needless repaint churn, this
                # preserves item references and the focused row for hosts
                # that publish a new cache projection.
                existing = dict(self._task_list_items)
                desired_keys = {key for key, _label in tasks}
                # Detach all existing rows first so the canonical task order
                # is restored when a family shares keys such as Launch,
                # Health, or Advanced with the previous family.
                for row in tuple(existing.values()):
                    index = self.task_navigation.row(row)
                    if index >= 0:
                        self.task_navigation.takeItem(index)
                self._task_list_items = {}
                for key, label in tasks:
                    enabled = self._radio_id is not None or key == "overview"
                    item = existing.pop(key, None)
                    if item is None:
                        item = QListWidgetItem()
                    self.task_navigation.addItem(item)
                    self._apply_task_item_presentation(item, key, label, enabled)
                    self._task_list_items[key] = item
                selected = self._task_list_items.get(self._task_key)
                if selected is not None:
                    self.task_navigation.setCurrentItem(selected)
        finally:
            self._context_controls_loading = False

    def _on_software_combo_changed(self, index: int) -> None:
        if self._context_controls_loading or index < 0:
            return
        key = str(self.software_combo.itemData(index) or "")
        if not key or key == self._family_key:
            return
        before = self._family_key
        self._choose_family(key)
        if self._family_key != key:
            self._restore_combo_selection(self.software_combo, before)

    def _on_radio_combo_changed(self, index: int) -> None:
        if self._context_controls_loading or index < 0:
            return
        value = self.radio_combo.itemData(index)
        radio_id: Optional[int]
        try:
            radio_id = int(value) if value is not None else None
        except (TypeError, ValueError):
            radio_id = None
        before = self._radio_id
        self._choose_radio(radio_id)
        if self._radio_id != radio_id:
            self._restore_combo_selection(self.radio_combo, before)

    def _on_task_list_changed(
        self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]
    ) -> None:
        if self._context_controls_loading or current is None:
            return
        key = str(current.data(Qt.UserRole) or "")
        if not key or key == self._task_key:
            return
        before = self._task_key
        self._choose_task(key)
        if self._task_key != key:
            self._restore_task_selection(before)

    @staticmethod
    def _restore_combo_selection(combo: QComboBox, value: object) -> None:
        with QSignalBlocker(combo):
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)

    def _restore_task_selection(self, key: str) -> None:
        item = self._task_list_items.get(key)
        if item is None:
            return
        with QSignalBlocker(self.task_navigation):
            self.task_navigation.setCurrentItem(item)

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
            button = self._make_chip(
                f"{family.title} · {summary}",
                self._family_choice_details(family, dirty_count),
            )
            button.clicked.connect(lambda _checked=False, key=family.key: self._choose_family(key))
            self._family_group.addButton(button)
            self.family_layout.addWidget(button)
            self._family_buttons[family.key] = button
        self.family_layout.addStretch(1)
        self._sync_horizontal_strip_content_width(self.family_strip, self.family_layout)
        self._rebuild_software_combo()

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
            self._all_radios_choice_details(family, dirty_count),
        )
        all_button.clicked.connect(lambda: self._choose_radio(None))
        self._radio_group.addButton(all_button)
        self.radio_layout.addWidget(all_button)
        self._radio_buttons[None] = all_button
        for ident, name, assignment in self._radio_choices(family):
            ownership = (f"Assigned: {assignment.instance_name or 'Missing instance'}"
                         if assignment is not None and assignment.instance_id is not None else "Available")
            text = f"{name} — {ownership}"
            if family and self._is_dirty(ident, family.key):
                text += " · Unsaved changes"
            button = self._make_chip(text, self._radio_context_details(ident, name, assignment, family))
            button.clicked.connect(lambda _checked=False, radio_id=ident: self._choose_radio(radio_id))
            self._radio_group.addButton(button)
            self.radio_layout.addWidget(button)
            self._radio_buttons[ident] = button
        self.radio_layout.addStretch(1)
        self._sync_horizontal_strip_content_width(self.radio_strip, self.radio_layout)
        self._rebuild_radio_combo(family)

    def _rebuild_task_buttons(self, tasks: tuple[tuple[str, str], ...]) -> None:
        self._clear_layout(self.task_layout)
        for button in self._task_group.buttons():
            self._task_group.removeButton(button)
        self._task_buttons = {}
        for key, label in tasks:
            all_context = self._radio_id is None
            enabled = not all_context or key == "overview"
            display = self._task_display_text(key, label)
            button = self._make_chip(
                display,
                self._task_tooltip(key, label, enabled),
            )
            button.setAccessibleName(label)
            _suffix, accessible, _reason = self._task_configuration_parts(key)
            button.setAccessibleDescription(accessible)
            button.setEnabled(enabled)
            button.clicked.connect(lambda _checked=False, task_key=key: self._choose_task(task_key))
            self._task_group.addButton(button)
            self.task_layout.addWidget(button)
            self._task_buttons[key] = button
        self.task_layout.addStretch(1)
        self._sync_horizontal_strip_content_width(self.task_strip, self.task_layout)
        self._rebuild_task_navigation(tasks)

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
        if key == self._family_key:
            self._sync_checked_buttons()
            return
        if self._keep_open_instance_assistant_visible():
            return
        self.select_context(key, self._radio_id)
        self.family_selected.emit(self._family_key)

    def _choose_radio(self, radio_id: Optional[int]) -> None:
        if radio_id is not None and radio_id not in {ident for ident, _name, _ in self._radio_choices(self._snapshot.family(self._family_key))}:
            return
        if radio_id == self._radio_id:
            self._radio_context_explicit = True
            self._sync_checked_buttons()
            return
        if self._keep_open_instance_assistant_visible():
            self._sync_visible_context_controls()
            return
        self._radio_context_explicit = True
        self._radio_id = radio_id
        tasks = _TASKS.get(self._family_key, ())
        if radio_id is None and tasks:
            self._task_key = tasks[0][0]
        self._rebuild_task_buttons(tasks)
        self._update_context(self._snapshot.family(self._family_key))
        self._sync_checked_buttons()
        self._sync_visible_context_controls()
        self._show_registered_editor_for_context()
        self.radio_selected.emit(radio_id)

    def _choose_task(self, key: str) -> None:
        if self._radio_id is None and key != "overview":
            return
        if key == self._task_key:
            self._sync_checked_buttons()
            return
        if self._keep_open_instance_assistant_visible():
            return
        self._task_key = key
        self._sync_checked_buttons()
        self._sync_visible_context_controls()
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
        family = self._snapshot.family(self._family_key)
        assignment = next((item for item in family.assignments if item.radio_id == self._radio_id), None) if family else None
        self._open_instance_assistant(replacement_requested=bool(assignment and assignment.instance_id is not None))

    def _open_instance_assistant(
        self,
        *,
        initial_source: str = "managed",
        initial_discovery_results: Iterable[Mapping[str, Any]] = (),
        initial_edit_instance: Mapping[str, Any] | None = None,
        replacement_requested: bool = False,
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
            edit_seed["expected_current_instance_id"] = initial_edit_instance.get("id")
            edit_seed.update(
                {
                    "family_key": family.key,
                    "radio_id": self._radio_id,
                    "source_locked": False,
                    "writer_operation": "update",
                }
            )
        assignment = next((item for item in family.assignments if item.radio_id == self._radio_id), None)
        if assignment and assignment.instance_id is not None:
            edit_seed = dict(edit_seed or {})
            edit_seed['expected_current_instance_id'] = assignment.instance_id
        assistant = SoftwareInstanceAssistant(
            family.key,
            radios=radios,
            existing_instances=self._instance_inventory.get(family.key, ()),
            varac_clusters=self._varac_clusters,
            selected_radio_id=self._radio_id,
            radio_role=radio_role,
            initial_draft=edit_seed,
            editing_existing=edit_seed is not None,
            replacement_requested=replacement_requested,
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
        assistant.discard_unfinished_requested.connect(
            lambda value: self.operator_unfinished_discard_requested.emit({
                **dict(value), 'family_key':family.key,'radio_id':self._radio_id}))
        self.operator_edit_opened.emit({'family_key':family.key,'radio_id':self._radio_id})
        self._ensure_editor_in_stack(assistant)
        self.editor_stack.setCurrentWidget(assistant)
        if str(initial_source or "").strip().lower() == "discover":
            assistant.set_discovery_results(initial_discovery_results)
            assistant.discovery_hint.setText(
                "Choose one retained, unassigned instance. It remains inactive until this reviewed radio assignment is saved."
            )

    def open_selected_instance_editor(self, *, task_key: str | None = None) -> bool:
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
        assistant = self._instance_assistant
        if assistant is not None:
            self._scope_instance_editor(assistant, task_key or self._task_key)
        return assistant is not None

    def _scope_instance_editor(self, assistant, task_key: str) -> None:
        """Filter the saved editor's view; its atomic payload retains all fields."""
        family = self._family_key
        fields = None
        components = None
        if family == 'js8call':
            fields = {
                'application_profile': ('application_path', 'settings_identity_mode', 'rig_name', 'launch_control', 'configuration_path', 'instance_name', 'custom_argv', 'launch_command'),
                'api_radio': ('host', 'port', 'offset_hz'),
                'message_storage': ('storage_path', 'configuration_path', 'directed_path', 'all_path', 'inbox_path'),
                'ingest_forms': ('storage_path', 'directed_path', 'all_path', 'inbox_path'),
                'launch': ('application_path', 'settings_identity_mode', 'rig_name', 'launch_control', 'launch_at_startup', 'launch_command', 'custom_argv', 'working_directory'),
            }.get(task_key)
        elif family == 'fast_light':
            # The application editor owns the canonical task-to-component
            # scope map.  Reuse it here so Radio Configure and Software
            # Administration present the same component surface and retain
            # every other component in the atomic draft.
            if task_key in {'flrig_control', 'fldigi_modem_logs', 'flmsg', 'flamp_signing'}:
                assistant.set_task_scope(task_key)
            elif task_key == 'message_folders':
                components = ()
                fields = ('flmsg_message_path', 'flamp_receive_path', 'storage_path', 'secondary_storage_path')
        if not (family == 'fast_light' and task_key in {'flrig_control', 'fldigi_modem_logs', 'flmsg', 'flamp_signing'}):
            assistant.set_view_scope(visible_components=components, visible_fields=fields)
        if fields:
            assistant.editor.more_button.setChecked(True)
        if family == 'js8call' and task_key not in {'application_profile', 'launch', 'overview', 'advanced'}:
            assistant.editor.launch_preview_label.hide()
        assistant.set_configuration_attention(getattr(self, '_task_configuration_states', {}).get(task_key))
        heading = assistant.findChild(QLabel, 'operatorInstanceAssistantHeading')
        if heading is not None:
            heading.setText(dict(_TASKS.get(family, ())).get(task_key, family.replace('_', ' ').title()))

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
        assistant = self._instance_assistant
        if assistant is not None and not assistant._busy and not assistant.editor.is_dirty():
            # Viewing saved settings must not lock ordinary navigation.
            self._close_instance_assistant()
            return False
        held = self.keep_instance_assistant_visible(announce=True)
        if held:
            # A checkable button toggles before clicked is delivered. Rejected
            # navigation must restore the displayed context immediately.
            self._sync_checked_buttons()
            self.context_banner.setText(
                f"Editing {self._snapshot.family(self._family_key).title if self._snapshot.family(self._family_key) else self._family_key}. "
                "Save or cancel the open editor before changing software, radio, or task."
            )
        return held

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
        self._update_context(self._snapshot.family(self._family_key))
        self._sync_checked_buttons()
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
            self.status_check_button.setEnabled(False)
            self.disassociate_button.setVisible(False)
            self.add_instance_button.setText("Add software instance…")
            self._update_manage_menu()
            return
        self.status_check_button.setEnabled(True)
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
        if assignment is None and self._radio_id is not None:
            name = next((name for ident, name, _ in self._radio_choices(family) if ident == self._radio_id), "Selected radio")
            self.context_banner.setText(f"{name} · {family.title}: Not configured. Use Manage instances to create or assign an instance.")
            self.context_banner.setToolTip(self.context_banner.text())
        elif assignment is None:
            dirty_count = self._dirty_count_for_family(family.key)
            suffix = (
                f" {dirty_count} radio draft{'s have' if dirty_count != 1 else ' has'} unsaved changes."
                if dirty_count
                else ""
            )
            unassigned = len(family.unassigned_instances)
            unassigned_suffix = (
                f" {unassigned} unassigned retained instance{'s' if unassigned != 1 else ''} available for recovery."
                if unassigned else ""
            )
            observation = f" {self._observation_summary}" if self._observation_summary else ""
            self.context_banner.setText(
                f"Viewing all radios using {family.title}.{suffix}{unassigned_suffix}{observation}"
            )
            self.context_banner.setToolTip("")
        else:
            instance = assignment.instance_name or ("Not configured" if not assignment.software_enabled and assignment.instance_id is None else "No linked instance")
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
            if assignment.launch_summary:
                detail_parts.append(f"Launches as: {assignment.launch_summary}")
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
            observation = f" {self._observation_summary}" if self._observation_summary else ""
            self.context_banner.setText(
                f"Instance: {instance}. "
                f"Status: {assignment.status_text}."
                f"{' ' + ownership_label + '.' if ownership_label else ''}{shared}{dirty_suffix}{observation}"
            )
            self.context_banner.setToolTip(
                "\n".join(
                    part
                    for part in (
                        detail.strip(),
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
                f"Unassigned retained instances for recovery ({len(unassigned)}): {names}{suffix}. "
                "They remain available for an explicit assignment review."
            )
        else:
            self.unassigned_label.setText("Unassigned retained instances for recovery: none.")
        self._apply_compact_height(self.height() < 680)
        self._update_manage_menu()

    @staticmethod
    def _compact_resource_name(value: str) -> str:
        """Keep instance evidence readable without allowing paths to dominate."""

        normalized = str(value or "").strip().replace("\\", "/").rstrip("/")
        return normalized.rsplit("/", 1)[-1] if normalized else "Not set"

    def _family_choice_details(self, family: SoftwareFamilySummary, dirty_count: int) -> str:
        parts = [family.title, family.description]
        parts.append(
            f"Assigned radios: {family.assigned_radio_count}."
        )
        if family.attention_count:
            parts.append(f"Needs attention: {family.attention_count}.")
        if family.unassigned_instances:
            parts.append(
                f"Unassigned retained instances: {len(family.unassigned_instances)}."
            )
        if dirty_count:
            parts.append(f"Unsaved drafts: {dirty_count}.")
        return " ".join(parts)

    def _all_radios_choice_details(
        self, family: Optional[SoftwareFamilySummary], dirty_count: int
    ) -> str:
        if family is None:
            return "Show all radios."
        parts = [f"Show all radios using {family.title}."]
        if dirty_count:
            parts.append(f"Unsaved drafts: {dirty_count}.")
        if family.unassigned_instances:
            parts.append(
                f"Unassigned retained instances: {len(family.unassigned_instances)}."
            )
        return " ".join(parts)

    def _radio_choice_details(self, assignment: Any, family_key: str) -> str:
        parts = [str(assignment.radio_name or "Radio")]
        if assignment.instance_name:
            parts.append(f"Instance: {assignment.instance_name}.")
        parts.append(f"Status: {assignment.status_text or 'Unknown'}.")
        if assignment.is_shared:
            others = tuple(name for name in assignment.shared_radio_names if name != assignment.radio_name)
            if others:
                parts.append("Shared with: " + ", ".join(others) + ".")
        if self._is_dirty(assignment.radio_id, family_key):
            parts.append("Unsaved changes for this software and radio.")
        return " ".join(parts)

    def _sync_checked_buttons(self) -> None:
        for key, button in self._family_buttons.items():
            button.setChecked(key == self._family_key)
        for radio_id, button in self._radio_buttons.items():
            button.setChecked(radio_id == self._radio_id)
        for key, button in self._task_buttons.items():
            button.setChecked(key == self._task_key)

    def _sync_visible_context_controls(self) -> None:
        """Publish the same selected route through the compact controls."""
        self._context_controls_loading = True
        try:
            with QSignalBlocker(self.software_combo):
                for family in self._snapshot.families:
                    choice = self.software_combo.findData(family.key)
                    if choice >= 0:
                        assignment = next((item for item in family.assignments if item.radio_id == self._radio_id), None)
                        detail = self._family_choice_details(family, self._dirty_count_for_family(family.key))
                        if self._radio_id is not None and (assignment is None or not assignment.software_enabled):
                            detail = "Not configured for the selected radio. " + detail
                        self.software_combo.setItemData(choice, detail, Qt.ToolTipRole)
                        self.software_combo.setItemData(choice, detail, getattr(Qt, "AccessibleTextRole", Qt.ToolTipRole))
                index = self.software_combo.findData(self._family_key)
                if index >= 0:
                    self.software_combo.setCurrentIndex(index)
            with QSignalBlocker(self.radio_combo):
                index = self.radio_combo.findData(self._radio_id)
                if index >= 0:
                    self.radio_combo.setCurrentIndex(index)
            with QSignalBlocker(self.task_navigation):
                item = self._task_list_items.get(self._task_key)
                if item is not None:
                    self.task_navigation.setCurrentItem(item)
        finally:
            self._context_controls_loading = False

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
        self._update_manage_menu()

    def _update_manage_menu(self) -> None:
        """Keep secondary actions and unassigned/dirty counts truthful."""
        family = self._snapshot.family(self._family_key)
        unassigned_count = len(family.unassigned_instances) if family is not None else 0
        dirty = len(self._dirty_contexts)
        suffixes = []
        if unassigned_count:
            suffixes.append(f"{unassigned_count} unassigned")
        if dirty:
            suffixes.append(f"{dirty} unsaved")
        self.manage_instances_button.setText(
            "Manage instances" + (f" ({', '.join(suffixes)})" if suffixes else "")
        )
        self.manage_instances_button.setAccessibleName(
            "Manage software instances"
            + (f"; {unassigned_count} unassigned retained instance{'s' if unassigned_count != 1 else ''}" if unassigned_count else "")
            + (f"; {dirty} unsaved draft{'s' if dirty != 1 else ''}" if dirty else "")
        )
        if self._manage_summary_action is not None:
            if unassigned_count:
                self._manage_summary_action.setText(
                    f"{unassigned_count} unassigned retained instance{'s' if unassigned_count != 1 else ''} in {family.title if family else 'selected family'}"
                )
            else:
                self._manage_summary_action.setText("No unassigned retained instances in selected family")
        if self._assign_action is not None:
            can_assign = bool(family and family.unassigned_instances and self._available_radios)
            self._assign_action.setEnabled(can_assign)
            self._assign_action.setToolTip(
                "Assign a retained unassigned instance to an existing radio"
                if can_assign
                else "No retained unassigned instance is available for assignment"
            )
        if self._add_instance_action is not None:
            self._add_instance_action.setText(
                self.add_instance_button.text().replace("…", "…")
                if self.add_instance_button.text()
                else "Create or use instance…"
            )
        if self._disassociate_action is not None:
            family_assignment = next(
                (
                    item for item in (family.assignments if family else ())
                    if item.radio_id == self._radio_id and item.instance_id is not None
                ),
                None,
            )
            can_disassociate = family_assignment is not None
            self._disassociate_action.setEnabled(can_disassociate)
            self._disassociate_action.setToolTip(
                "Remove this radio assignment while retaining the external instance and files"
                if can_disassociate
                else "Choose a radio with an assigned instance first"
            )
        if self._save_all_action is not None:
            self._save_all_action.setEnabled(dirty > 0)
            self._save_all_action.setText(
                f"Save all drafts ({dirty})" if dirty else "Save all drafts"
            )


__all__ = ["SoftwareAdministrationWorkspace"]
