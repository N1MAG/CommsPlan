"""Cache-only operator radio editor.

The dialog collects a complete radio draft and hands it to its owner.  It does
not read or write a store, inspect applications, prepare native files, or
change scheduler state.  Application details are edited through the existing
cache-only :class:`OperatorInstanceAssistant` in a nested dialog.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Iterable, Mapping
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.multi_radio_store import COMMON_AMATEUR_BANDS
from freqinout.core.operator_radio_configuration import normalize_supported_band
from freqinout.gui.operator_instance_assistant import OperatorInstanceAssistant
from freqinout.gui.responsive_geometry import WrappingControlLayout
from freqinout.gui.theme import (
    ThemedCheckBox,
    active_app_theme,
    button_height_for_font,
    button_style,
    control_height_for_font,
    fit_child_combo_boxes,
    label_style,
)


_APPLICATION_FLAGS = (
    ("use_flrig", "FLRig"),
    ("use_fldigi", "FLDigi"),
    ("use_flmsg", "FLMsg"),
    ("use_flamp", "FLAmp"),
    ("use_js8call", "JS8Call"),
    ("use_varac", "VarAC"),
    ("use_js8spotter", "CommsPlan Spotter"),
    ("use_external_js8spotter", "External JS8Spotter"),
    ("use_commstat", "CommStat"),
)

# These are the only application families for which this editor owns a
# nested identity/configuration editor.  The remaining application flags stay
# visible and point back to their owning Settings route instead of receiving a
# fabricated editor here.
_APPLICATION_EDITORS = {
    "use_js8call": ("js8call", "Edit JS8Call…"),
    "use_flrig": ("fast_light", "Edit Fast Light…"),
    "use_fldigi": ("fast_light", "Edit Fast Light…"),
    "use_flmsg": ("fast_light", "Edit Fast Light…"),
    "use_flamp": ("fast_light", "Edit Fast Light…"),
    "use_varac": ("varac", "Edit VarAC…"),
}

_SECTION_TITLES = ("Radio", "Applications", "Operating behavior", "Schedule", "Review")


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _present(row: Mapping[str, Any], *keys: str) -> Any:
    """Return the first explicitly stored value, including an empty string."""

    for key in keys:
        if key in row:
            return row[key]
    return None


def _row_id(row: Mapping[str, Any]) -> Any:
    return _first(row, "id", "operating_profile_id", "schedule_plan_id", "profile_id")


def _row_label(row: Mapping[str, Any], fallback: str) -> str:
    return _text(_first(row, "name", "label", "title", "description")) or fallback


def _bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


class _ApplicationPage(QWidget):
    """In-frame editor completion without QDialog's native presentation lifecycle."""
    finished = Signal(int)

    def accept(self):
        self.hide()
        self.finished.emit(int(QDialog.DialogCode.Accepted))

    def reject(self):
        self.hide()
        self.finished.emit(int(QDialog.DialogCode.Rejected))


class OperatorRadioEditor(QDialog):
    """Settings-style radio editor with an explicit owner handoff payload.

    ``tabs`` remains the canonical page stack for callers that used the old
    tabbed editor.  Its tab bar is hidden and ``section_nav`` provides the
    persistent Settings > Main style rail; both controls are synchronized.
    """

    save_requested = Signal(object)
    draft_save_requested = Signal(object)
    application_find_requested = Signal(object)
    cancelled = Signal()
    recover_requested = Signal(object)
    discard_unfinished_requested = Signal(object)
    application_owner_requested = Signal(object)

    def __init__(
        self,
        profile: Mapping[str, Any] | None = None,
        application_seeds: Mapping[str, Mapping[str, Any]] | None = None,
        operating_profiles: Iterable[Mapping[str, Any]] = (),
        schedule_plans: Iterable[Mapping[str, Any]] = (),
        initial_step: str = "radio",
        parent: QWidget | None = None,
        radio_catalog: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("operatorRadioEditor")
        self.setAccessibleName("Radio editor")
        self.setWindowTitle("Radio setup")
        self.resize(900, 560)
        self._base = deepcopy(dict(profile or {}))
        self._values = deepcopy(self._base)
        self._radio_catalog = deepcopy(dict(radio_catalog or {}))
        self._applying_catalog = False
        self._radio_name_edited = bool(_first(self._base, "name", "radio_name"))
        self.operator_session_id = uuid4().hex
        self._operator_recovered_from: str | None = None
        self._operator_draft_revision: Any = None
        self._application_seeds = {
            str(key): deepcopy(dict(value))
            for key, value in dict(application_seeds or {}).items()
            if isinstance(value, Mapping)
        }
        self._application_drafts: dict[str, dict[str, Any]] = deepcopy(
            self._values.get("application_drafts") or {}
        )
        self._operating_profiles = tuple(
            deepcopy(dict(row)) for row in operating_profiles if isinstance(row, Mapping)
        )
        self._schedule_plans = tuple(
            deepcopy(dict(row)) for row in schedule_plans if isinstance(row, Mapping)
        )
        self._field_widgets: dict[str, QWidget] = {}
        self._field_labels: dict[str, QLabel] = {}
        self._application_checks: dict[str, QCheckBox] = {}
        self._application_raw_values: dict[str, Any] = {}
        self._application_touched: set[str] = set()
        self._endpoint_fields_edited: set[str] = set()
        self._application_rows: dict[str, int] = {}
        self._application_identity_labels: dict[str, QLabel] = {}
        self._application_configure_buttons: dict[str, QPushButton] = {}
        self._syncing_navigation = False
        self._dirty = False
        self._busy = False
        self._status_before_busy = "Ready to review"
        self._selected_operating_profile_id: Any = None
        self._selected_schedule_plan_id: Any = None
        self._nested_dialogs: list[_ApplicationPage] = []
        self._unfinished_envelope: dict[str, Any] | None = None
        self._allow_close = False

        self._build_ui()
        self._load_profile()
        self._select_initial_step(initial_step)
        self.apply_theme()

    # ----- public interface ---------------------------------------------

    def values(self) -> dict[str, Any]:
        self._sync_values()
        return deepcopy(self._values)

    def base(self) -> dict[str, Any]:
        return deepcopy(self._base)

    def is_dirty(self) -> bool:
        return self._dirty

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        if self._busy:
            self._status_before_busy = self.status_label.text()
            self.set_status("Saving…")
        else:
            self.set_status(self._status_before_busy)
        for widget in self._field_widgets.values():
            widget.setEnabled(not self._busy)
        self.radio_model_combo.setEnabled(not self._busy)
        for checkbox in self.band_checks.values():
            checkbox.setEnabled(not self._busy)
        for button in (self.back_button, self.next_button, self.save_button, self.cancel_button, self.keep_draft_button):
            button.setEnabled(not self._busy)
        for button in self._edit_application_buttons.values():
            button.setEnabled(not self._busy)
        for button in self._application_configure_buttons.values():
            button.setEnabled(not self._busy)
        self._refresh_recovery_guidance()

    def set_status(self, text: str) -> None:
        self.status_label.setText(_text(text))
        self.status_label.setToolTip(_text(text))

    def set_result(self, result: Mapping[str, Any] | object | None) -> None:
        """Apply an owner result; only an acknowledged saved state closes."""

        state = result.get("state") if isinstance(result, Mapping) else getattr(result, "state", None)
        message = result.get("message") if isinstance(result, Mapping) else getattr(result, "message", None)
        draft_revision = result.get("draft_revision") if isinstance(result, Mapping) else getattr(result, "draft_revision", None)
        issues = result.get("issues") if isinstance(result, Mapping) else getattr(result, "issues", ())
        if draft_revision is not None:
            self._operator_draft_revision = draft_revision
        previous_warning = getattr(self, "_save_failure_box", None)
        if previous_warning is not None:
            previous_warning.close()
            previous_warning.deleteLater()
            self._save_failure_box = None
        if _text(state).lower() == "draft_saved":
            self._dirty = False
            self._allow_close = True
            self.reject()
            return
        if _text(state).lower() in {"saved", "complete", "published"}:
            self._dirty = False
            self.set_status(_text(message) or "Saved")
            self.accept()
            return
        status = _text(message) or "Saved incomplete; review the remaining fields."
        if issues:
            status = status + " " + " ".join(str(issue) for issue in issues)
        self.set_status(status)
        # The owner has durably retained the raw draft.  Keep it open and
        # correctable, but do not make the acknowledgement look unsaved.
        self._dirty = False
        self._refresh_dirty_state()
        corrections = result.get('corrections', ()) if isinstance(result, Mapping) else ()
        if corrections:
            first = corrections[0]
            family, field = first.get('family'), first.get('field')
            if family == 'radio':
                self.tabs.setCurrentIndex(0)
                widget = self.band_selector if field == 'antenna_supported_bands' else self._field_widgets.get(field)
                if widget is not None:
                    self.tabs.widget(0).ensureWidgetVisible(widget)
                    widget.setFocus()
                return
            component = None
            if family == 'fast_light':
                if field in ('secondary_port', 'fldigi_host'):
                    component = 'fldigi'
                elif field in ('host', 'port'):
                    component = 'flrig'
                elif str(field).split('_')[0] in ('flrig', 'fldigi', 'flmsg', 'flamp'):
                    component = str(field).split('_')[0]
                else:
                    component = next((key for key in ('flrig', 'fldigi', 'flmsg', 'flamp')
                                      if self._values.get('use_' + key)), 'flrig')
            self._open_application_editor(family, component)
            assistant = self._nested_dialogs[-1].findChild(OperatorInstanceAssistant)
            assistant.set_operation_status(('Radio changes not saved. ' if self._profile_radio_id() else 'Radio not added yet. ')
                                           + 'Complete the required fields below, Apply to radio setup, then Save radio.')
            assistant.editor.focus_required_field(field)
        elif "antenna band" in status.lower():
            self.tabs.setCurrentIndex(0)
            self.tabs.widget(0).ensureWidgetVisible(self.band_selector)
            self.band_selector.setFocus()
        elif issues:
            # Publication/ownership failures have no field correction metadata.
            # A muted status line alone is too easy to miss. Keep this warning
            # parent-owned and nonblocking; the raw draft stays in the editor.
            self.show_save_failure(issues, draft_retained=True)

    def show_save_failure(self, issues, *, draft_retained=False, draft_only=False) -> None:
        """Explain a worker failure without changing entries or claiming persistence."""
        previous = getattr(self, "_save_failure_box", None)
        if previous is not None:
            previous.close()
            previous.deleteLater()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Draft not saved" if draft_only else "Radio changes not saved")
        box.setTextFormat(Qt.PlainText)
        box.setText("The setup draft was not saved." if draft_only else
                    "Radio changes were not saved." if self._profile_radio_id() else
                    "This radio was not added to Configured Radios.")
        recovery = ("Your entries are kept as a draft." if draft_retained else
                    "Your current entries are still open here. Saving this draft was not confirmed.")
        box.setInformativeText("\n".join(str(issue) for issue in issues)
            + "\n\n" + recovery + " Correct the reported settings, then choose "
            + ("Save draft and close again." if draft_only else "Save radio again. "
               "Save draft and close keeps unfinished work; it does not add or update a radio."))
        box.addButton("Keep editing", QMessageBox.AcceptRole)
        self._save_failure_box = box
        box.open()

    def set_context(self, response: Mapping[str, Any] | object | None) -> bool:
        """Load a worker-published context before editing begins.

        A context publication cannot replace an active local draft.  The host
        may retry after the operator cancels or saves that draft.
        """

        if isinstance(response, Mapping) and "radio_catalog" in response:
            self.set_radio_catalog(response["radio_catalog"])
        if self._dirty:
            self.set_status("Context is newer; finish or cancel the current edits first.")
            return False
        if not isinstance(response, Mapping):
            return False
        profile = response.get("profile", response.get("values", {}))
        if not isinstance(profile, Mapping):
            return False
        self._base = deepcopy(dict(profile))
        self._values = deepcopy(dict(profile))
        self.operator_session_id = uuid4().hex
        self._operator_recovered_from = None
        self._operator_draft_revision = None
        seeds = response.get("application_seeds", {})
        self._application_seeds = {
            str(key): deepcopy(dict(value))
            for key, value in dict(seeds or {}).items()
            if isinstance(value, Mapping)
        }
        drafts = response.get("application_drafts", self._values.get("application_drafts"))
        self._application_drafts = deepcopy(dict(drafts)) if isinstance(drafts, Mapping) else {}
        self._operating_profiles = self._rows(response.get("operating_profiles", ()))
        self._schedule_plans = self._rows(response.get("schedule_plans", ()))
        self._repopulate_selection_combos()
        self._load_profile()
        self.set_status("Ready to review")
        return True

    @staticmethod
    def _rows(rows: object) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(dict(row)) for row in (rows or ()) if isinstance(row, Mapping))

    def _repopulate_selection_combos(self) -> None:
        operating_id = _first(self._values, "_operating_profile_id", "operating_profile_id", "operating_model_id")
        schedule_id = _first(self._values, "_schedule_plan_id", "schedule_plan_id", "sop_schedule_plan_id")
        self.operating_profile_combo.blockSignals(True)
        self.operating_profile_combo.clear()
        self.operating_profile_combo.addItem("No operating model selected", None)
        for row in self._operating_profiles:
            self.operating_profile_combo.addItem(_row_label(row, "Unnamed operating model"), _row_id(row))
        self.operating_profile_combo.blockSignals(False)
        self.schedule_plan_combo.blockSignals(True)
        self.schedule_plan_combo.clear()
        self.schedule_plan_combo.addItem("No schedule plan selected", None)
        for row in self._schedule_plans:
            self.schedule_plan_combo.addItem(_row_label(row, "Unnamed schedule plan"), _row_id(row))
        self.schedule_plan_combo.blockSignals(False)
        self._select_combo(self.operating_profile_combo, operating_id)
        self._select_combo(self.schedule_plan_combo, schedule_id)

    def set_unfinished(self, envelope: Mapping[str, Any] | None) -> None:
        self._unfinished_envelope = deepcopy(dict(envelope or {})) if envelope else None
        self._refresh_recovery_guidance()

    def _has_other_saved_draft(self) -> bool:
        payload = (self._unfinished_envelope or {}).get("payload")
        if not isinstance(payload, Mapping):
            return False
        known_revision = self._operator_draft_revision
        return (payload.get("session_id") not in (self.operator_session_id, self._operator_recovered_from)
                or (known_revision is not None and self._unfinished_envelope.get('revision') != known_revision))

    def _refresh_recovery_guidance(self) -> None:
        visible = isinstance((self._unfinished_envelope or {}).get("payload"), Mapping)
        self.recover_button.setVisible(visible)
        self.discard_unfinished_button.setVisible(visible)
        self.recovery_hint.setVisible(visible)
        self.recovery_hint.setText(
            "A previous draft is available. Keep working with the current entries, or load the previous draft. "
            "Save radio and Save draft and close let you replace it after confirmation."
        )
        for button in (self.recover_button, self.discard_unfinished_button):
            button.setEnabled(not self._busy)
        blocked = self._has_other_saved_draft()
        self.save_button.setEnabled(not self._busy)
        self.save_button.setToolTip(
            "Save the current entries; review replacing the previous draft first." if blocked
            else "Save this radio and its application choices, then close setup."
        )
        self._refresh_dirty_state()

    def _recover_unfinished(self) -> None:
        envelope = deepcopy(self._unfinished_envelope or {})
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            self.set_status("No recoverable unfinished radio draft is available.")
            return
        base = payload.get("base")
        values = payload.get("values")
        session_id = payload.get("session_id")
        if not isinstance(base, Mapping) or not isinstance(values, Mapping) or session_id in (None, ""):
            self.set_status("The unfinished radio draft has an invalid format.")
            return
        if self._busy:
            return
        if self._dirty:
            box = QMessageBox(self)
            box.setWindowTitle("Recover saved draft?")
            box.setText("Load the previous draft instead of the entries on screen?")
            current_name = _text(self._values.get('name')) or 'Unnamed radio'
            previous_name = _text(values.get('name')) or 'Unnamed radio'
            box.setInformativeText(
                f"Current setup: {current_name}\nApps: {', '.join(label for key, label in _APPLICATION_FLAGS if _bool(self._values.get(key))) or 'None'}\nBands: {_text(self._values.get('antenna_supported_bands')) or 'No limit'}\n\n"
                f"Previous draft: {previous_name}\nApps: {', '.join(label for key, label in _APPLICATION_FLAGS if _bool(values.get(key))) or 'None'}\nBands: {_text(values.get('antenna_supported_bands')) or 'No limit'}\n\n"
                "Loading replaces the current unsaved entries, including application choices and bands. "
                "Choose Keep editing to retain what is on screen. Saved radios are unchanged.")
            recover = box.addButton("Load previous draft", QMessageBox.ButtonRole.AcceptRole)
            keep = box.addButton("Keep editing", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() is not recover:
                return
        self.recover_requested.emit(deepcopy(envelope))
        self._base = deepcopy(dict(base))
        self._values = deepcopy(dict(values))
        self.operator_session_id = uuid4().hex
        self._operator_recovered_from = _text(session_id)
        self._operator_draft_revision = envelope.get("revision")
        drafts = self._values.get("application_drafts", self._values.get("_application_drafts")) or {}
        self._application_drafts = deepcopy(dict(drafts)) if isinstance(drafts, Mapping) else {}
        self._repopulate_selection_combos()
        self._load_profile()
        self._dirty = True
        self._refresh_dirty_state()
        self.set_unfinished(None)
        self.set_status("Saved draft recovered. Review the entries, then choose Save radio.")

    def _discard_unfinished(self) -> None:
        envelope = deepcopy(self._unfinished_envelope or {})
        if not envelope or self._busy:
            return
        payload = envelope.get("payload")
        self.discard_unfinished_requested.emit({
            "draft_revision": envelope.get("revision"),
            "revision": envelope.get("revision"),
            "operator_session_id": payload.get("session_id") if isinstance(payload, Mapping) else None,
        })

    # ----- UI construction ----------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._editor_stack = QStackedWidget()
        outer.addWidget(self._editor_stack)
        self._main_panel = QWidget()
        self._editor_stack.addWidget(self._main_panel)
        root = QVBoxLayout(self._main_panel)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        heading = QLabel("Radio setup")
        heading.setObjectName("operatorRadioEditorHeading")
        heading.setStyleSheet(label_style("text", active_app_theme(), weight=700))
        root.addWidget(heading)
        # Full guidance follows the active section into its existing scroll.
        # It must never become an unbounded fixed-height strip above the form.
        self._guidance_panel = QWidget(self._main_panel)
        self._guidance_layout = QVBoxLayout(self._guidance_panel)
        self._guidance_layout.setContentsMargins(0, 0, 0, 0)
        self._guidance_layout.setSpacing(8)
        self.status_label = QLabel("Ready to review")
        self.status_label.setObjectName("operatorRadioEditorStatus")
        self.status_label.setWordWrap(True)
        self._guidance_layout.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("operatorRadioEditorTabs")
        # Compatibility page stack: Settings navigation owns the visible
        # selection, while existing integrations may continue using tabs.
        self.tabs.tabBar().setVisible(False)
        # The visible section rail owns navigation; a hidden tab bar must
        # not intercept keyboard traversal into the form and footer.
        self.tabs.setFocusPolicy(Qt.NoFocus)
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._build_radio_tab()
        self._build_applications_tab()
        self._build_behavior_tab()
        self._build_schedule_tab()
        self._build_review_tab()

        editor_area = QSplitter(Qt.Orientation.Horizontal)
        editor_area.setObjectName("operatorRadioEditorArea")
        editor_area.setChildrenCollapsible(False)
        self.section_nav = QListWidget()
        self.section_nav.setObjectName("operatorRadioEditorSectionNav")
        self.section_nav.setAccessibleName("Radio setup sections")
        self.section_nav.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.section_nav.setMinimumWidth(150)
        self.section_nav.setMaximumWidth(230)
        self.section_nav.setSpacing(2)
        self.section_nav.setUniformItemSizes(True)
        for title in _SECTION_TITLES:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, self.section_nav.count())
            self.section_nav.addItem(item)
        self._refresh_section_nav_item_geometry()
        self.left_nav = self.section_nav
        self.navigation_list = self.section_nav
        self.section_nav.currentRowChanged.connect(self._nav_changed)
        editor_area.addWidget(self.section_nav)
        editor_area.addWidget(self.tabs)
        editor_area.setStretchFactor(1, 1)
        editor_area.setSizes([170, 680])
        root.addWidget(editor_area, 1)

        self.recovery_hint = QLabel()
        self.recovery_hint.setWordWrap(True)
        self.recovery_hint.setVisible(False)
        self.recovery_hint.setAccessibleName("Saved radio draft choices")
        self._guidance_layout.addWidget(self.recovery_hint)
        recovery = WrappingControlLayout()
        self.recover_button = QPushButton("Load previous draft…")
        self.recover_button.setObjectName("operatorRadioRecoverUnfinished")
        self.recover_button.setVisible(False)
        self.recover_button.clicked.connect(self._recover_unfinished)
        self.discard_unfinished_button = QPushButton("Delete previous draft")
        self.discard_unfinished_button.setObjectName("operatorRadioDiscardUnfinished")
        self.discard_unfinished_button.setVisible(False)
        self.discard_unfinished_button.clicked.connect(self._discard_unfinished)
        recovery.addWidget(self.recover_button)
        recovery.addWidget(self.discard_unfinished_button)
        self._guidance_layout.addLayout(recovery)

        footer = WrappingControlLayout()
        self.dirty_label = QLabel("Saved")
        self.dirty_label.setObjectName("operatorRadioEditorDirty")
        self.dirty_label.setWordWrap(True)
        self.dirty_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        root.addWidget(self.dirty_label)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self._back)
        footer.addWidget(self.back_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next)
        footer.addWidget(self.next_button)
        self.keep_draft_button = QPushButton("Save draft and close")
        self.keep_draft_button.clicked.connect(lambda: self._emit_save(keep_draft=True))
        footer.addWidget(self.keep_draft_button)
        self.cancel_button = QPushButton("Close setup")
        self.cancel_button.clicked.connect(self._cancel)
        footer.addWidget(self.cancel_button)
        self.save_button = QPushButton("Save radio")
        self.save_button.clicked.connect(self._emit_save)
        footer.addWidget(self.save_button)
        root.addLayout(footer)
        self._place_section_guidance(self.tabs.currentIndex())

    def _place_section_guidance(self, index):
        page = self.tabs.widget(index)
        if page is None:
            return
        content = page.widget()
        if self._guidance_panel.parentWidget() is not content:
            self._guidance_panel.setParent(content)
            content.layout().insertWidget(0, self._guidance_panel)
            self._guidance_panel.show()

    def _scroll_page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        scroll = QScrollArea()
        scroll.setObjectName("operatorRadioEditorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, title)
        return content, layout

    def _build_radio_tab(self) -> None:
        _content, layout = self._scroll_page("Radio")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.radio_model_combo = QComboBox()
        self.radio_model_combo.setObjectName("operatorRadioModelCatalog")
        self.radio_model_combo.setAccessibleName("Search radio models")
        self.radio_model_combo.setEditable(True)
        self.radio_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.radio_model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.radio_model_combo.setMinimumContentsLength(24)
        self.radio_model_combo.lineEdit().setPlaceholderText("Search manufacturer or model")
        self.radio_model_combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.radio_model_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.radio_model_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.radio_model_combo.activated.connect(self._radio_model_selected)
        form.addRow("Radio model", self.radio_model_combo)
        self._add_line(form, "radio_manufacturer", "Manufacturer", "Manufacturer (optional)")
        self._add_line(form, "radio_model", "Model", "Model (optional)")
        self._add_line(form, "name", "Radio name (required)", "Your station label")
        self._add_combo(form, "device_class", "Role", (("tx_rx", "Transmit and receive"), ("observer", "Observer / receive only"), ("gateway", "Gateway")))
        layout.addLayout(form)
        self.radio_model_hint = QLabel()
        self.radio_model_hint.setWordWrap(True)
        layout.addWidget(self.radio_model_hint)
        self.set_radio_catalog(self._radio_catalog)
        more = QGroupBox("More radio settings")
        more_form = QFormLayout(more)
        more_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        backend_combo = self._add_combo(
            more_form,
            "control_backend",
            "Control backend",
            (("manual", "Manual"), ("flrig", "FLRig"), ("rigctld", "rigctld"), ("js8call", "JS8Call")),
        )
        backend_combo.currentIndexChanged.connect(self._backend_changed)
        self._add_line(more_form, "flrig_host", "FLRig host", "Host")
        self._add_line(more_form, "flrig_port", "FLRig port", "Port")
        self._add_line(more_form, "rig_host", "Hamlib host", "Host")
        self._add_line(more_form, "rig_port", "Hamlib port", "Port")
        self._add_line(more_form, "antenna_group", "Antenna group", "Group")
        self.band_selector = QWidget()
        self.band_grid = WrappingControlLayout(self.band_selector, spacing=10,
                                               alignment=Qt.AlignLeft, max_columns=4)
        self.band_checks = {}
        for band in COMMON_AMATEUR_BANDS:
            self._add_band_checkbox(band)
        more_form.addRow("Supported antenna bands", self.band_selector)
        band_hint = QLabel("Select the bands this antenna supports. Leave all unchecked if no band limit is specified.")
        band_hint.setWordWrap(True)
        more_form.addRow(band_hint)
        layout.addWidget(more)
        layout.addStretch(1)
        self._backend_changed()

    def _add_band_checkbox(self, band: str) -> None:
        label = band if band in COMMON_AMATEUR_BANDS else f"{band[:24]} (saved)"
        checkbox = ThemedCheckBox(label)
        checkbox.setAccessibleName(f"Supported antenna band {band}")
        checkbox.setToolTip(band)
        checkbox.toggled.connect(self._supported_bands_changed)
        self.band_checks[band] = checkbox
        self.band_grid.addWidget(checkbox)
        self._size_band_checkbox(checkbox)

    @staticmethod
    def _size_band_checkbox(checkbox):
        checkbox.ensurePolished()
        metrics = checkbox.fontMetrics()
        gap = metrics.horizontalAdvance("M")
        checkbox.setMinimumWidth(metrics.horizontalAdvance(checkbox.text()) + checkbox.style().pixelMetric(QStyle.PM_IndicatorWidth) + gap * 3)
        checkbox.setMinimumHeight(max(metrics.height(), checkbox.style().pixelMetric(QStyle.PM_IndicatorHeight)) + gap)
        checkbox.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

    def _load_supported_bands(self) -> None:
        raw = _present(self._values, *self._field_aliases()["antenna_supported_bands"])
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                raw = parsed if isinstance(parsed, list) else raw
            except (TypeError, ValueError):
                pass
        text = ", ".join(_text(item) for item in raw) if isinstance(raw, (list, tuple)) else _text(raw)
        # Preserve the saved raw spelling/order until an explicit checkbox edit.
        self._values["antenna_supported_bands"] = text
        selected = {normalize_supported_band(part) for part in text.split(",") if part.strip()}
        for band in tuple(self.band_checks):
            if band not in COMMON_AMATEUR_BANDS:
                checkbox = self.band_checks.pop(band)
                self.band_grid.removeWidget(checkbox)
                checkbox.hide()
                checkbox.deleteLater()
        for band in sorted(selected - set(COMMON_AMATEUR_BANDS)):
            self._add_band_checkbox(band)
        for band, checkbox in self.band_checks.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(band in selected)
            checkbox.blockSignals(False)

    def _supported_bands_changed(self, _checked: bool = False) -> None:
        if not self._busy:
            self._set_value("antenna_supported_bands", ", ".join(
                band for band, checkbox in self.band_checks.items() if checkbox.isChecked()))

    def set_radio_catalog(self, payload: Mapping[str, Any] | None) -> None:
        """Publish worker data only; catalog arrival never changes the draft."""
        self._radio_catalog = deepcopy(dict(payload or {}))
        combo = self.radio_model_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("Manual entry", None)
            for entry in self._radio_catalog.get("entries", ()) or ():
                if isinstance(entry, Mapping) and entry.get("display_name"):
                    combo.addItem(_text(entry["display_name"]), deepcopy(dict(entry)))
        finally:
            combo.blockSignals(False)
        self._sync_radio_model_selection()
        source = self._radio_catalog.get("source")
        origin = "Hamlib models" if source == "hamlib-rigctl" else "Built-in models" if source == "static-fallback" else "Radio models"
        self.radio_model_hint.setText(
            f"{origin}: search and choose a model, or enter manufacturer and model manually. "
            "Radio name is your station label. Control backend stays your choice.")

    def _sync_radio_model_selection(self) -> None:
        combo = self.radio_model_combo
        combo.blockSignals(True)
        try:
            catalog_id = _text(self._values.get("radio_catalog_id"))
            index = next((i for i in range(1, combo.count())
                          if _text((combo.itemData(i) or {}).get("catalog_id")) == catalog_id), -1) if catalog_id else -1
            combo.setCurrentIndex(index if index >= 0 else 0)
            if index < 0 and (self._values.get("radio_manufacturer") or self._values.get("radio_model")):
                combo.setEditText(" ".join(_text(self._values.get(key)).strip()
                                          for key in ("radio_manufacturer", "radio_model")).strip())
        finally:
            combo.blockSignals(False)

    def _radio_model_selected(self, index: int) -> None:
        if self._busy:
            return
        if self.radio_model_combo.currentText().strip() != self.radio_model_combo.itemText(index).strip():
            return
        entry = self.radio_model_combo.itemData(index)
        # Editable combo text is a search until an actual catalog row is chosen.
        if not isinstance(entry, Mapping):
            if index == 0:
                self._set_value("radio_catalog_id", "")
            return
        self._applying_catalog = True
        try:
            self._set_value("radio_catalog_id", _text(entry.get("catalog_id")))
            self._field_widgets["radio_manufacturer"].setText(_text(entry.get("manufacturer")))
            self._field_widgets["radio_model"].setText(_text(entry.get("model_name")))
            if not self._radio_name_edited:
                self._field_widgets["name"].setText(_text(entry.get("display_name")))
        finally:
            self._applying_catalog = False
        self._sync_radio_model_selection()

    def _build_applications_tab(self) -> None:
        _content, layout = self._scroll_page("Applications")
        intro = QLabel("Choose the software this radio uses. Edit connection details beside supported applications.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        group = QGroupBox("Applications assigned to this radio")
        group.setObjectName("operatorRadioApplicationsGroup")
        group_layout = QVBoxLayout(group)
        self.application_table = QTableWidget(len(_APPLICATION_FLAGS), 4)
        self.application_table.setObjectName("operatorRadioApplicationTable")
        self.application_table.setAccessibleName("Radio application assignments")
        self.application_table.setHorizontalHeaderLabels(("Application", "Use", "Launch identity", "Configure"))
        self.application_table.verticalHeader().setVisible(False)
        self.application_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.application_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.application_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._application_table = self.application_table
        self.applications_table = self.application_table
        self.application_list = self.application_table
        self.application_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        for row, (key, label) in enumerate(_APPLICATION_FLAGS):
            self._application_rows[key] = row
            name_item = QTableWidgetItem(label)
            name_item.setData(Qt.ItemDataRole.UserRole, key)
            self.application_table.setItem(row, 0, name_item)
            # The table header already names this column.  Keep the control
            # itself icon-only so each row does not repeat "Use" while its
            # accessible name remains explicit for keyboard/screen-reader use.
            check = ThemedCheckBox()
            check.setObjectName("operatorRadioApplicationFlag_" + key)
            check.setAccessibleName(f"Use {label}")
            check.toggled.connect(lambda checked, field_key=key: self._set_value(field_key, bool(checked)))
            self._application_checks[key] = check
            self._field_widgets[key] = check
            self.application_table.setCellWidget(row, 1, check)
            identity = QLabel("Launch settings not recorded")
            identity.setObjectName("operatorRadioApplicationIdentity_" + key)
            identity.setWordWrap(True)
            identity.setToolTip("No saved launch identity is recorded for this application yet.")
            self._application_identity_labels[key] = identity
            self.application_table.setCellWidget(row, 2, identity)
            editor_spec = _APPLICATION_EDITORS.get(key)
            if editor_spec is not None:
                button = QPushButton("Configure…")
                button.setObjectName("operatorRadioConfigure_" + key)
                button.setAccessibleName(f"Configure {label}")
                button.setToolTip(editor_spec[1])
                button.clicked.connect(
                    lambda _checked=False, family_key=editor_spec[0], application_key=key:
                    self._open_application_editor(
                        family_key,
                        component=(application_key.removeprefix("use_") if family_key == "fast_light" else None),
                    )
                )
            else:
                button = QPushButton("Software settings…")
                button.setObjectName("operatorRadioOwnerRoute_" + key)
                button.setAccessibleName(f"Open owner settings for {label}")
                button.setToolTip(f"{label} is owned by the Software settings route.")
                button.clicked.connect(
                    lambda _checked=False, field_key=key, app_label=label: self._route_application_owner(field_key, app_label)
                )
            self._application_configure_buttons[key] = button
            self.application_table.setCellWidget(row, 3, button)
        self.application_table.resizeRowsToContents()
        group_layout.addWidget(self.application_table)
        group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(group, 1)
        self.launch_summary = QLabel()
        self.launch_summary.setObjectName("operatorRadioLaunchSummary")
        self.launch_summary.setWordWrap(True)
        self.launch_summary.setVisible(False)
        layout.addWidget(self.launch_summary)
        # Compatibility aliases for callers that used the old family-button
        # map.  The table is the primary route and keeps each action beside its
        # corresponding flag.
        buttons = QHBoxLayout()
        self._edit_application_buttons: dict[str, QPushButton] = {}
        for family, label in (("js8call", "Edit JS8Call…"), ("fast_light", "Edit Fast Light…"), ("varac", "Edit VarAC…")):
            button = QPushButton(label)
            button.setObjectName("operatorRadioEdit_" + family)
            button.clicked.connect(lambda _checked=False, family_key=family: self._open_application_editor(family_key))
            self._edit_application_buttons[family] = button
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons_widget = QWidget()
        buttons_widget.setLayout(buttons)
        buttons_widget.setVisible(False)
        layout.addWidget(buttons_widget)

    def _build_behavior_tab(self) -> None:
        _content, layout = self._scroll_page("Operating behavior")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.operating_profile_combo = QComboBox()
        self.operating_profile_combo.setObjectName("operatorRadioOperatingProfile")
        self.operating_profile_combo.addItem("No operating model selected", None)
        for row in self._operating_profiles:
            self.operating_profile_combo.addItem(_row_label(row, "Unnamed operating model"), _row_id(row))
        self.operating_profile_combo.currentIndexChanged.connect(self._operating_profile_changed)
        form.addRow("Operating model", self.operating_profile_combo)
        layout.addLayout(form)
        hint = QLabel("Scheduler enablement is reviewed separately after this radio profile is saved.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def _build_schedule_tab(self) -> None:
        _content, layout = self._scroll_page("Schedule")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.schedule_plan_combo = QComboBox()
        self.schedule_plan_combo.setObjectName("operatorRadioSchedulePlan")
        self.schedule_plan_combo.addItem("No schedule plan selected", None)
        for row in self._schedule_plans:
            self.schedule_plan_combo.addItem(_row_label(row, "Unnamed schedule plan"), _row_id(row))
        self.schedule_plan_combo.currentIndexChanged.connect(self._schedule_plan_changed)
        form.addRow("Schedule plan", self.schedule_plan_combo)
        layout.addLayout(form)
        hint = QLabel("A plan may be selected here; enablement and scheduling changes are reviewed separately.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def _build_review_tab(self) -> None:
        _content, layout = self._scroll_page("Review")
        self.review_label = QLabel()
        self.review_label.setObjectName("operatorRadioReview")
        self.review_label.setWordWrap(True)
        layout.addWidget(self.review_label)
        self.review_note = QLabel("Save publishes this radio's choices. Scheduling remains paused until it is enabled separately.")
        self.review_note.setWordWrap(True)
        layout.addWidget(self.review_note)
        layout.addStretch(1)

    def _add_line(self, form: QFormLayout, key: str, label: str, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setObjectName("operatorRadioField_" + key)
        edit.setAccessibleName(label)
        edit.setPlaceholderText(placeholder)
        edit.textChanged.connect(lambda value, field_key=key: self._set_value(field_key, value))
        self._field_widgets[key] = edit
        self._field_labels[key] = QLabel(label)
        form.addRow(self._field_labels[key], edit)
        return edit

    def _add_combo(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        items: tuple[tuple[str, str], ...],
    ) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName("operatorRadioField_" + key)
        combo.setAccessibleName(label)
        for value, text in items:
            combo.addItem(text, value)
        combo.currentIndexChanged.connect(lambda _index, field_key=key, widget=combo: self._set_value(field_key, widget.currentData()))
        self._field_widgets[key] = combo
        self._field_labels[key] = QLabel(label)
        form.addRow(self._field_labels[key], combo)
        return combo

    # ----- draft state ---------------------------------------------------

    def _field_aliases(self) -> dict[str, tuple[str, ...]]:
        return {
            "name": ("name", "radio_name"),
            "device_class": ("device_class", "radio_role", "role"),
            "control_backend": ("control_backend", "cat_backend", "backend"),
            "flrig_host": ("flrig_host", "cat_host", "host", "endpoint_host"),
            "flrig_port": ("flrig_port", "cat_port", "port", "endpoint_port"),
            "rig_host": ("rig_host", "rigctld_host"),
            "rig_port": ("rig_port", "rigctld_port"),
            "antenna_group": ("antenna_group", "antenna_groups", "groups"),
            "antenna_supported_bands": ("antenna_supported_bands", "antenna_supported_bands_json", "bands", "band_plan", "supported_bands"),
        }

    def _backend_changed(self, _index: int = -1) -> None:
        backend = _text(self._field_widgets.get("control_backend").currentData() if isinstance(self._field_widgets.get("control_backend"), QComboBox) else "")
        show_flrig = backend == "flrig"
        show_rigctld = backend == "rigctld"
        for key, visible in (("flrig_host", show_flrig), ("flrig_port", show_flrig), ("rig_host", show_rigctld), ("rig_port", show_rigctld)):
            widget = self._field_widgets.get(key)
            label = self._field_labels.get(key)
            if widget is not None:
                widget.setVisible(visible)
            if label is not None:
                label.setVisible(visible)

    def _load_profile(self) -> None:
        aliases = self._field_aliases()
        self._application_raw_values = {
            key: deepcopy(self._values[key]) for key, _label in _APPLICATION_FLAGS if key in self._values
        }
        self._application_touched.clear()
        blocked = True
        try:
            for key, widget in self._field_widgets.items():
                if key in self._application_checks:
                    # Assignment is an explicit nullable/raw configuration
                    # choice.  Preserve the established legacy migration
                    # rule for an absent nullable External Spotter flag: a
                    # saved launch path is the configuration owner.  Explicit
                    # false/null values always win; runtime status/discovery
                    # never turns an assignment on.
                    if key == "use_external_js8spotter" and key not in self._values:
                        value = bool(self._values.get("spotter_launch_path"))
                    else:
                        value = self._values.get(key, False)
                else:
                    source = aliases.get(key, (key,))
                    value = _present(self._values, *source)
                    if value is None:
                        value = "" if not isinstance(widget, QComboBox) else None
                    if key == "antenna_supported_bands" and isinstance(value, str):
                        try:
                            parsed = json.loads(value)
                            if isinstance(parsed, list):
                                value = ", ".join(_text(item) for item in parsed)
                        except (TypeError, ValueError):
                            pass
                    elif key == "antenna_supported_bands" and isinstance(value, (list, tuple)):
                        value = ", ".join(_text(item) for item in value)
                widget.blockSignals(True)
                if isinstance(widget, QLineEdit):
                    widget.setText(_text(value))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(_bool(value))
                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index < 0 and value not in (None, ""):
                        widget.addItem(f"Current: {value}", value)
                        index = widget.count() - 1
                    widget.setCurrentIndex(index if index >= 0 else 0)
                widget.blockSignals(False)
            self._load_supported_bands()
            operating_id = _first(self._values, "_operating_profile_id", "operating_profile_id", "operating_model_id")
            schedule_id = _first(self._values, "_schedule_plan_id", "schedule_plan_id", "sop_schedule_plan_id")
            self._select_combo(self.operating_profile_combo, operating_id)
            self._select_combo(self.schedule_plan_combo, schedule_id)
            self._selected_operating_profile_id = (
                self.operating_profile_combo.currentData()
                if self.operating_profile_combo.findData(operating_id) >= 0
                else operating_id
            )
            self._selected_schedule_plan_id = (
                self.schedule_plan_combo.currentData()
                if self.schedule_plan_combo.findData(schedule_id) >= 0
                else schedule_id
            )
        finally:
            if blocked:
                self._dirty = False
                self._refresh_dirty_state()
        self._radio_name_edited = bool(self._profile_radio_id() or self._operator_recovered_from
                                       or _first(self._values, "name", "radio_name"))
        self._sync_radio_model_selection()
        self._backend_changed()
        self._refresh_summaries()
        self._refresh_application_identity_labels()

    @staticmethod
    def _select_combo(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        combo.blockSignals(True)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _sync_values(self) -> None:
        aliases = self._field_aliases()
        for key, widget in self._field_widgets.items():
            if isinstance(widget, QLineEdit):
                value: Any = widget.text()
                if key in {"radio_manufacturer", "radio_model"} and value == _text(self._values.get(key)):
                    if key not in self._values:
                        continue
                    value = deepcopy(self._values[key])
            elif isinstance(widget, QCheckBox):
                if key in self._application_raw_values and key not in self._application_touched:
                    # Keep explicit null/unknown/raw flag values intact until
                    # the operator edits that checkbox.
                    value = deepcopy(self._application_raw_values[key])
                else:
                    value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = widget.currentData()
            else:
                continue
            self._values[key] = value
            if aliases.get(key):
                self._values[aliases[key][0]] = value
            for alias in aliases.get(key, ()):
                if alias in self._base:
                    self._values[alias] = value
        self._values["_operating_profile_id"] = self._selected_operating_profile_id
        self._values["_schedule_plan_id"] = self._selected_schedule_plan_id
        if "operating_profile_id" in self._base or "operating_profile_id" in self._values:
            self._values["operating_profile_id"] = self._selected_operating_profile_id
        if "schedule_plan_id" in self._base or "schedule_plan_id" in self._values:
            self._values["schedule_plan_id"] = self._selected_schedule_plan_id
        self._values["application_drafts"] = deepcopy(self._application_drafts)

    def _set_value(self, key: str, value: Any) -> None:
        if self._busy:
            return
        self._values[key] = deepcopy(value)
        if key in {'flrig_host', 'flrig_port', 'rig_host', 'rig_port'}:
            self._endpoint_fields_edited.add(key)
        if not self._applying_catalog:
            if key == "name":
                self._radio_name_edited = True
            elif key in {"radio_manufacturer", "radio_model"}:
                self._values["radio_catalog_id"] = ""
                self._sync_radio_model_selection()
        if key in {'flrig_host', 'flrig_port'}:
            draft = self._application_drafts.get('fast_light')
            if isinstance(draft, Mapping) and draft.get('operator_values', {}).get('flrig_enabled'):
                draft['operator_values']['host' if key == 'flrig_host' else 'port'] = deepcopy(value)
        if key in self._application_checks:
            self._application_raw_values[key] = deepcopy(value)
            self._application_touched.add(key)
        if self._field_aliases().get(key):
            self._values[self._field_aliases()[key][0]] = deepcopy(value)
        for alias in self._field_aliases().get(key, ()):
            if alias in self._base:
                self._values[alias] = deepcopy(value)
        self._dirty = True
        self._refresh_dirty_state()
        if key == "control_backend":
            # Selecting a control application is an explicit operator choice.
            # Carry it into Applications instead of opening a component editor
            # whose selected set is empty. Loading an existing profile never
            # goes through this setter and therefore does not enable software.
            control_flag = {"flrig": "use_flrig", "js8call": "use_js8call"}.get(value)
            if control_flag:
                self._application_checks[control_flag].setChecked(True)
            if value == "flrig":
                for field, default in (("flrig_host", "127.0.0.1"), ("flrig_port", "12345")):
                    if not self._field_widgets[field].text().strip():
                        self._field_widgets[field].setText(default)
            self._backend_changed()
        self._refresh_summaries()

    def _operating_profile_changed(self, _index: int) -> None:
        self._selected_operating_profile_id = self.operating_profile_combo.currentData()
        self._set_value("_operating_profile_id", self._selected_operating_profile_id)
        self._values["operating_profile_id"] = self._selected_operating_profile_id

    def _schedule_plan_changed(self, _index: int) -> None:
        self._selected_schedule_plan_id = self.schedule_plan_combo.currentData()
        self._set_value("_schedule_plan_id", self._selected_schedule_plan_id)
        self._values["schedule_plan_id"] = self._selected_schedule_plan_id

    def _refresh_dirty_state(self) -> None:
        if self._dirty:
            text = "Unsaved radio changes"
        elif (self._unfinished_envelope or {}).get("payload") and not self._has_other_saved_draft():
            text = "Draft kept; radio changes not saved"
        elif self._profile_radio_id() in (None, ""):
            text = "Radio not saved"
        else:
            text = "Saved radio"
        self.dirty_label.setText(text)

    # ----- applications --------------------------------------------------

    def _profile_radio_id(self) -> Any:
        return _first(self._base, "id", "radio_id", "device_profile_id", "profile_id")

    def _open_application_editor(self, family_key: str, component: str | None = None) -> None:
        if self._nested_dialogs:
            self._editor_stack.setCurrentWidget(self._nested_dialogs[-1])
            return
        self._sync_values()
        seed = deepcopy(self._application_seeds.get(family_key, {}))
        existing_draft = self._application_drafts.get(family_key)
        if isinstance(existing_draft, Mapping):
            draft_values = existing_draft.get("operator_values")
            if isinstance(draft_values, Mapping):
                seed.update(deepcopy(dict(draft_values)))
            else:
                seed.update(deepcopy(dict(existing_draft)))
        if family_key == 'fast_light':
            for name in ('flrig', 'fldigi', 'flmsg', 'flamp'):
                seed[f'{name}_enabled'] = bool(seed.get(f'{name}_enabled')
                    or self._values.get(f'use_{name}') or component == name)
            if seed.get('flrig_enabled'):
                for target, source, default in (('host', 'flrig_host', '127.0.0.1'), ('port', 'flrig_port', '12345')):
                    new_endpoint = (not self._profile_radio_id() and not self._operator_recovered_from
                                    and source not in self._base and source not in self._endpoint_fields_edited)
                    seed.setdefault(target, default if new_endpoint else _text(self._values.get(source)))
        # Common project defaults are editable proposals only for a genuinely
        # new application. Never repair a saved/recovered blank implicitly.
        if not existing_draft and family_key not in self._application_seeds:
            if family_key == 'js8call':
                seed.setdefault('port', '2442')
            elif family_key == 'fast_light' and seed.get('fldigi_enabled'):
                seed.setdefault('secondary_port', '7362')
        radio_id = self._profile_radio_id()
        if radio_id not in (None, ""):
            seed["radio_id"] = radio_id
        device_profile_id = _first(self._base, "device_profile_id", "id")
        if device_profile_id not in (None, ""):
            seed["device_profile_id"] = device_profile_id
        task_scope = {
            "flrig": "flrig_control",
            "fldigi": "fldigi_modem_logs",
            "flmsg": "flmsg",
            "flamp": "flamp_signing",
        }.get(str(component or "").strip().lower())
        dialog = _ApplicationPage(self._editor_stack)
        assistant = OperatorInstanceAssistant(
            family_key,
            radios=(self._base,),
            selected_radio_id=radio_id,
            initial_draft=seed,
            existing_instances=(),
            task_scope=task_scope,
            visible_components=((component,) if component else None),
            parent=dialog,
        )
        assistant.save_button.setText("Apply to radio setup")
        assistant.save_button.setToolTip("Update this radio draft. Choose Save radio in Radio setup to keep it.")
        if isinstance(existing_draft, Mapping):
            original_base = existing_draft.get("operator_base_values")
            if isinstance(original_base, Mapping):
                assistant.operator_base_values = deepcopy(dict(original_base))
            if existing_draft.get("operator_session_id"):
                assistant.operator_session_id = _text(existing_draft["operator_session_id"])
            recovered_from = existing_draft.get("operator_recovered_from")
            if recovered_from not in (None, ""):
                assistant._recovered_from = _text(recovered_from)
            if existing_draft.get("operator_draft_revision") is not None:
                assistant.operator_draft_revision = existing_draft["operator_draft_revision"]
            for key in (
                "expected_identity_generation",
                "expected_current_instance_id",
                "replacement_instance_id",
                "operator_launch_snapshot",
            ):
                if key in existing_draft:
                    assistant._raw_fields[key] = deepcopy(existing_draft[key])
            if existing_draft.get("expected_identity_generation") is not None:
                assistant._expected_identity_generation = existing_draft["expected_identity_generation"]
            if existing_draft.get("expected_current_instance_id") not in (None, ""):
                assistant._replacement_instance_id = existing_draft["expected_current_instance_id"]
        assistant._radio_setup_context = True
        assistant.cancel_button.setText("Back to radio setup")
        assistant.create_radio_button.hide()
        title = {"flrig": "FLRig", "fldigi": "FLDigi", "flmsg": "FLMsg", "flamp": "FLAmp",
                 "js8call": "JS8Call", "varac": "VarAC", "fast_light": "Fast Light"}.get(
                     str(component or family_key), str(component or family_key).replace("_", " ").title())
        dialog.setAccessibleName(f"{title} setup")
        heading = assistant.findChild(QLabel, 'operatorInstanceAssistantHeading')
        if heading is not None:
            heading.setText(f"{title} setup")
        dialog.resize(900, 560)
        layout = QVBoxLayout(dialog)
        layout.addWidget(assistant)
        self._nested_dialogs.append(dialog)

        def accept_payload(payload: object) -> None:
            if isinstance(payload, Mapping):
                self._application_drafts[family_key] = deepcopy(dict(payload))
                if family_key == 'fast_light':
                    application_values = payload.get('operator_values', {})
                    if application_values.get('flrig_enabled'):
                        for field, source in (('flrig_host', 'host'), ('flrig_port', 'port')):
                            if source in application_values:
                                self._field_widgets[field].setText(_text(application_values[source]))
                self._values["application_drafts"] = deepcopy(self._application_drafts)
                self._dirty = True
                self._refresh_dirty_state()
                self._refresh_summaries()
                self.set_status(
                    "Application choices kept in the current setup. Save radio can replace the previous draft."
                    if self._has_other_saved_draft() else
                    "Application changes applied to this draft. Choose Save radio to keep them.")
            dialog.accept()

        assistant.discover_requested.connect(lambda _family: self.application_find_requested.emit({"assistant":assistant,"family_key":family_key}))
        assistant.guard_dialog(dialog)
        assistant.completed.connect(accept_payload)
        assistant.cancelled.connect(dialog.reject)
        def return_to_radio(_result):
            self._editor_stack.setCurrentWidget(self._main_panel)
            self._editor_stack.removeWidget(dialog)
            if dialog in self._nested_dialogs:
                self._nested_dialogs.remove(dialog)
        dialog.finished.connect(return_to_radio)
        self._present_application_editor(dialog)

    def _present_application_editor(self, dialog):
        self._editor_stack.addWidget(dialog)
        self._editor_stack.setCurrentWidget(dialog)
        dialog.show()

    def _route_application_owner(self, field_key: str, label: str) -> None:
        """Hand unsupported application configuration to its real owner.

        This editor owns assignment flags and the three supported nested
        identity editors only.  Built-in/external Spotter and CommStat remain
        available here, but their detail configuration stays in Software
        settings; the owner receives a route request with no guessed editor.
        """

        self._sync_values()
        payload = {
            "field_key": field_key,
            "label": label,
            "owner_route": "software_settings",
            "radio_id": self._profile_radio_id(),
            "saved_radio": deepcopy(self._base),
            "pending_values": deepcopy(self._values),
            "has_unsaved_changes": self._dirty,
            "navigation_policy": "preserve_draft_and_guard_unsaved_changes",
        }
        self.application_owner_requested.emit(payload)
        self.set_status(
            f"{label} details are maintained in Settings > Software. "
            "Save or cancel this radio draft before changing pages."
        )

    def _application_draft(self, family_key: str) -> Mapping[str, Any]:
        draft = self._application_drafts.get(family_key)
        if not isinstance(draft, Mapping):
            draft = self._application_seeds.get(family_key)
        if not isinstance(draft, Mapping):
            return {}
        values = draft.get("operator_values")
        return values if isinstance(values, Mapping) else draft

    def _identity_summary(self, family_key: str) -> tuple[str, str]:
        draft = self._application_draft(family_key)
        if not draft:
            return "Launch settings not recorded", "No saved launch identity is recorded for this application yet."
        if family_key == "js8call":
            mode_raw = draft.get("settings_identity_mode")
            mode = _text(mode_raw).strip().lower()
            if mode in {"default", "default_profile"}:
                text = "Default profile"
            elif mode in {"isolated", "separate", "separatefolder", "separate_folder"}:
                name = _text(draft.get("rig_name") or draft.get("profile_name"))
                text = f"Named profile: {name}" if name else "Named profile (name unknown)"
            elif mode in {"custom_launcher", "launcher"}:
                text = "Existing launcher"
            else:
                text = f"Launch settings: {_text(mode_raw)}" if mode_raw not in (None, "") else "Launch settings not recorded"
            path = _text(draft.get("application_path") or draft.get("launch_command"))
            return text, f"{text} · {path}" if path else text
        if family_key == "fast_light":
            mode_raw = draft.get("settings_identity_mode")
            mode = _text(mode_raw).strip().lower()
            if mode in {"separatefolder", "separate_folder", "isolated"}:
                text = "Separate settings"
            elif mode in {"default", "default_profile"}:
                text = "Default settings"
            elif mode_raw not in (None, ""):
                text = f"Settings: {_text(mode_raw)}"
            else:
                text = "Launch settings not recorded"
            return text, text
        command = _text(draft.get("launch_command") or draft.get("application_path"))
        return (command or "Launch settings not recorded"), (command or "No launch identity is recorded for this application.")

    def _refresh_application_identity_labels(self) -> None:
        family_by_flag = {
            "use_js8call": "js8call",
            "use_flrig": "fast_light",
            "use_fldigi": "fast_light",
            "use_flmsg": "fast_light",
            "use_flamp": "fast_light",
            "use_varac": "varac",
        }
        for key, label in self._application_identity_labels.items():
            family = family_by_flag.get(key)
            if family is None:
                raw = _present(self._values, key)
                summary = "Use flag unknown" if raw is None else ("Enabled" if _bool(raw) else "Disabled")
                detail = f"{summary}. Detail is maintained in Software settings."
            else:
                summary, detail = self._identity_summary(family)
            label.setText(summary)
            label.setToolTip(detail)

    def _refresh_summaries(self) -> None:
        selected = [label for key, label in _APPLICATION_FLAGS if _bool(self._values.get(key, False))]
        lines = ["Available: " + (", ".join(selected) if selected else "None")]
        for family, label in (("js8call", "JS8Call"), ("fast_light", "Fast Light"), ("varac", "VarAC")):
            flags = ('use_flrig', 'use_fldigi', 'use_flmsg', 'use_flamp') if family == 'fast_light' else ('use_js8call',) if family == 'js8call' else ('use_varac',)
            if not any(_bool(self._values.get(flag)) for flag in flags):
                continue
            draft = self._application_drafts.get(family) or self._application_seeds.get(family) or {}
            if not isinstance(draft, Mapping):
                continue
            if isinstance(draft.get("operator_values"), Mapping):
                draft = draft["operator_values"]
            if family == "js8call":
                mode = _text(draft.get("settings_identity_mode") or "default").lower()
                name = _text(draft.get("rig_name") or "actual profile")
                mode_label = {
                    "default": "Default profile",
                    "isolated": f"Named profile: {name}",
                    "custom_launcher": "Existing launcher",
                }.get(mode, "Default profile")
                lines.append(f"{label} launches as {mode_label}")
            elif family == "fast_light":
                identity = _text(draft.get("settings_identity_mode") or "default").lower()
                identity_label = "Separate settings" if identity in {"separatefolder", "separate_folder"} else "Default settings"
                components = []
                components_map = draft.get("components")
                if not isinstance(components_map, Mapping):
                    components_map = {}
                for component in ("flrig", "fldigi", "flmsg", "flamp"):
                    enabled = _bool(draft.get(f"{component}_enabled", components_map.get(component, False)))
                    starts = _bool(draft.get(f"{component}_launch_at_startup", False))
                    if enabled:
                        components.append(f"{component.upper()} ({'starts' if starts else 'manual'})")
                lines.append(f"{label} · {identity_label}: {', '.join(components) if components else 'no components selected'}")
            else:
                command = _text(draft.get("launch_command") or draft.get("application_path") or "launcher not set")
                lines.append(f"{label} launches: {command}")
        self.launch_summary.setText("\n".join(lines))
        self._refresh_application_identity_labels()
        self.review_label.setText(self._review_text())

    def _review_text(self) -> str:
        name = _text(self._values.get("name") or self._values.get("radio_name") or "Unnamed radio")
        role = _text(self._values.get("device_class") or self._values.get("radio_role") or self._values.get("role") or "tx_rx")
        op = _text(self._selected_operating_profile_id) or "none"
        plan = _text(self._selected_schedule_plan_id) or "none"
        return f"Radio: {name}\nRole: {role}\nOperating model: {op}\nSchedule plan: {plan}\n\n{self.launch_summary.text()}"

    # ----- navigation and actions ---------------------------------------

    def _select_initial_step(self, step: str) -> None:
        normalized = _text(step).strip().lower()
        aliases = {"software": "apps", "model": "behavior", "plans": "schedule"}
        normalized = aliases.get(normalized, normalized)
        index = {"radio": 0, "apps": 1, "applications": 1, "behavior": 2, "schedule": 3, "review": 4}.get(normalized, 0)
        self.tabs.setCurrentIndex(index)
        self._tab_changed(index)

    def _nav_changed(self, index: int) -> None:
        if self._syncing_navigation or index < 0 or index >= self.tabs.count():
            return
        self._syncing_navigation = True
        try:
            self.tabs.setCurrentIndex(index)
        finally:
            self._syncing_navigation = False

    def _tab_changed(self, index: int) -> None:
        if not hasattr(self, "back_button"):
            return
        if hasattr(self, "section_nav") and self.section_nav.currentRow() != index:
            self._syncing_navigation = True
            try:
                self.section_nav.setCurrentRow(index)
            finally:
                self._syncing_navigation = False
        self._place_section_guidance(index)
        self.back_button.setEnabled(not self._busy and index > 0)
        self.next_button.setEnabled(not self._busy and index < self.tabs.count() - 1)
        self.next_button.setText("Review" if index == self.tabs.count() - 2 else "Next")
        self._refresh_summaries()

    def _back(self) -> None:
        if self.tabs.currentIndex() > 0:
            self.tabs.setCurrentIndex(self.tabs.currentIndex() - 1)

    def _next(self) -> None:
        if self.tabs.currentIndex() < self.tabs.count() - 1:
            self.tabs.setCurrentIndex(self.tabs.currentIndex() + 1)

    def _emit_save(self, _checked=False, *, keep_draft=False) -> None:
        if self._busy:
            return
        replacement = None
        if self._has_other_saved_draft():
            box = QMessageBox(self)
            box.setWindowTitle("Use the current setup?")
            box.setText("Replace the previous draft with the entries currently on screen?")
            box.setInformativeText(
                "The previous draft will no longer be available. "
                + ("The current entries will be kept as a draft; saved radios are unchanged."
                   if keep_draft else "Save radio will save the current entries. If corrections are needed, they remain as your current draft."))
            confirm = box.addButton("Save current draft" if keep_draft else "Save current setup", QMessageBox.AcceptRole)
            cancel = box.addButton("Keep editing", QMessageBox.RejectRole)
            box.setDefaultButton(cancel)
            box.exec()
            if box.clickedButton() is not confirm:
                return
            envelope = self._unfinished_envelope or {}
            replacement = {'revision': envelope.get('revision'),
                           'session_id': (envelope.get('payload') or {}).get('session_id')}
        self._sync_values()
        payload = {
            "operator_radio_v1": True,
            "operator_session_id": self.operator_session_id,
            "operator_recovered_from": self._operator_recovered_from,
            "operator_draft_revision": self._operator_draft_revision,
            "base": deepcopy(self._base),
            "values": deepcopy(self._values),
            "application_drafts": deepcopy(self._application_drafts),
            "operating_profile_id": self._selected_operating_profile_id,
            "schedule_plan_id": self._selected_schedule_plan_id,
        }
        if replacement:
            payload['replace_saved_draft'] = replacement
        (self.draft_save_requested if keep_draft else self.save_requested).emit(payload)

    def _cancel(self) -> None:
        if self._nested_dialogs:
            self._nested_dialogs[-1].findChild(OperatorInstanceAssistant)._cancel()
            return
        if not self._dirty:
            self.cancelled.emit()
            self._allow_close = True
            self.reject()
            return
        action = self._dirty_close_action()
        if action == "save":
            self._emit_save()
        elif action == "draft":
            self._emit_save(keep_draft=True)
        elif action == "discard":
            self._dirty = False
            self.cancelled.emit()
            self._allow_close = True
            self.reject()

    def _dirty_close_action(self) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("Close radio setup?")
        box.setText("The current radio changes have not been saved.")
        box.setInformativeText(
            "Save draft and close keeps the current entries so you can continue later, without changing saved radios. "
            "Discard current changes and close leaves existing radios and the previous draft unchanged.")
        save = box.addButton("Save radio and close", QMessageBox.ButtonRole.AcceptRole)
        draft = box.addButton("Save draft and close", QMessageBox.ButtonRole.ActionRole)
        discard = box.addButton("Discard current changes and close", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("Keep editing", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.exec()
        if box.clickedButton() is draft:
            return "draft"
        if box.clickedButton() is save:
            return "save"
        if box.clickedButton() is discard:
            return "discard"
        return "stay"

    def reject(self) -> None:
        # Escape uses QDialog.reject directly and bypasses closeEvent.
        if self._allow_close:
            super().reject()
        elif self._busy:
            return
        else:
            self._cancel()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._nested_dialogs:
            self._cancel()
            event.ignore()
            return
        if self._allow_close:
            event.accept()
            return
        if self._busy:
            event.ignore()
            return
        if not self.isVisible():
            event.accept()
            return
        if not self._dirty:
            self.cancelled.emit()
            event.accept()
            return
        action = self._dirty_close_action()
        if action == "save":
            self._emit_save()
            event.ignore()
        elif action == "draft":
            self._emit_save(keep_draft=True)
            event.ignore()
        elif action == "discard":
            self._dirty = False
            self.cancelled.emit()
            event.accept()
        else:
            event.ignore()

    # ----- theme ---------------------------------------------------------

    def _refresh_section_nav_item_geometry(self) -> None:
        """Give each left-rail item a font-derived, keyboard-friendly hit target."""

        if not hasattr(self, "section_nav"):
            return
        item_height = control_height_for_font(self.section_nav, vertical_padding=8, floor=30)
        for index in range(self.section_nav.count()):
            item = self.section_nav.item(index)
            if item is not None:
                item.setSizeHint(QSize(0, item_height))

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        palette = dict(theme or active_app_theme())
        self.status_label.setStyleSheet(label_style("muted", palette))
        self.dirty_label.setStyleSheet(label_style("muted", palette))
        self.save_button.setStyleSheet(button_style("primary", palette))
        self.cancel_button.setStyleSheet(button_style("secondary", palette))
        self.keep_draft_button.setStyleSheet(button_style("secondary", palette))
        for button in (self.back_button, self.next_button):
            button.setStyleSheet(button_style("muted", palette))
        height = control_height_for_font(self, vertical_padding=8, floor=28)
        button_height = button_height_for_font(self, vertical_padding=10, floor=30)
        for widget in self._field_widgets.values():
            if isinstance(widget, (QLineEdit, QComboBox, QCheckBox)):
                widget.setMinimumHeight(height)
        for button in (self.back_button, self.next_button, self.cancel_button, self.save_button, self.keep_draft_button, *self._edit_application_buttons.values()):
            button.setMinimumHeight(button_height)
        for checkbox in self.band_checks.values():
            self._size_band_checkbox(checkbox)
        for button in self._application_configure_buttons.values():
            button.setMinimumHeight(button_height)
        for label in self._application_identity_labels.values():
            label.setMinimumHeight(max(label.sizeHint().height(), height))
        self.application_table.resizeRowsToContents()
        self._refresh_section_nav_item_geometry()
        fit_child_combo_boxes(self)


__all__ = ["OperatorRadioEditor"]
