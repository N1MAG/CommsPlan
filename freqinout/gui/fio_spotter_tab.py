"""Primary, lazy CommsPlan Spotter service workspace.

This surface intentionally consumes the established projection, Expect, form and
import stores.  It does not maintain a second Spotter catalog in the UI.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Mapping

from PySide6.QtCore import QEvent, Qt, QTimer, Signal, QStringListModel
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox,
    QCompleter, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QFileDialog, QMessageBox, QMenu,
    QListWidget, QListWidgetItem, QScrollArea, QSizePolicy, QSplitter, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.fio_spotter_store import (
    MATCH_MODES, PRIORITIES, WATCH_KINDS, delete_spotter_watch,
    list_spotter_watches, save_spotter_watch, watch_matches,
)
from freqinout.core.js8_expect_dispatcher import list_expect_dispatch_audit
from freqinout.core.js8_expect_runtime import (
    load_expect_automation_runtime_state, set_expect_automation_runtime_state,
)
from freqinout.core.js8_send_service import js8_profile_allows_transmit
from freqinout.core.js8_expect_store import (
    bulk_set_expect_entry_auto_reply_state,
    bulk_refresh_expect_datecodes,
    default_expect_db_path,
    delete_expect_allow_policy, delete_expect_entry,
    list_expect_allow_policies, list_expect_entries,
    list_expect_operator_access_catalog, list_expect_runtime_audit,
    save_expect_allow_policy, save_expect_entry, update_mcform_response_datecode,
)
from freqinout.core.js8_spotter_forms import (
    MAPPER_SETTINGS_KEY,
    PURPOSE_OPTIONS,
    cp_spotter_forms_library_dir,
    choose_effective_spotter_forms_catalog,
    discover_effective_spotter_forms,
    effective_mapping_rows,
    factory_mapping_for_form,
    import_spotter_forms_to_library,
    normalize_mapping_row,
    preview_spotter_forms_import,
    resolve_effective_spotter_forms_dir,
)
from freqinout.core.js8spotter_importer import import_js8spotter_database, preview_js8spotter_import
from freqinout.core.perf_metrics import emit_span
from freqinout.core.logger import log
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.varac_bbs_vault import list_flamp_transfer_index_statuses
from freqinout.gui.theme import (
    button_style,
    choice_chip_selector_style,
    control_height_for_font,
    fit_child_combo_boxes,
    fit_wrapping_choice_chip_selector,
    label_style,
    resolve_theme,
    style_splitter_handles,
)


_MAX_ROWS = 200
_TAB_NAMES = ("Watches", "Expect", "Access Policies", "Forms", "Imports")
_TAB_INDEX = {name: index for index, name in enumerate(_TAB_NAMES)}


def _csv(value: object) -> list[str]:
    return [part.strip().upper() for part in str(value or "").split(",") if part.strip()]


def _text(value: object) -> str:
    return str(value or "").strip()


def _when(value: object) -> str:
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if stamp <= 0:
        return "—"
    return dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


class _TokenListEditor(QWidget):
    """Visible, de-duplicated tokens plus a lookup/custom-value editor."""

    valuesChanged = Signal()

    def __init__(self, parent: QWidget | None = None, *, token_prefix: str = "") -> None:
        super().__init__(parent)
        self._token_prefix = str(token_prefix or "").strip().upper()
        self._values: list[str] = []
        self._completion_values: list[str] = []
        self._chip_buttons: list[QPushButton] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.chip_scroll = QScrollArea(self)
        self.chip_scroll.setObjectName("fioSpotterTokenChips")
        self.chip_scroll.setWidgetResizable(False)
        self.chip_scroll.setFrameShape(QFrame.NoFrame)
        self.chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chip_body = QWidget(self.chip_scroll)
        self.chip_layout = QHBoxLayout(self.chip_body)
        self.chip_layout.setContentsMargins(0, 0, 0, 0)
        self.chip_layout.setSpacing(4)
        self.chip_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.chip_scroll.setWidget(self.chip_body)
        # Start from a font-derived hit target; populated chips grow from
        # their actual contents in _rebuild_chips.
        self.chip_scroll.setMinimumHeight(control_height_for_font(self, vertical_padding=10, floor=28))
        self.chip_scroll.setVisible(False)
        layout.addWidget(self.chip_scroll)

        entry_row = QHBoxLayout()
        entry_row.setContentsMargins(0, 0, 0, 0)
        entry_row.setSpacing(4)
        self.input = QLineEdit(self)
        self.input.setAccessibleName("Value to add")
        self.add_button = QPushButton("Add", self)
        self.add_button.setAccessibleName("Add entered value")
        self.add_button.setToolTip("Add the lookup selection or custom value. Enter does the same thing.")
        entry_row.addWidget(self.input, 1)
        entry_row.addWidget(self.add_button)
        layout.addLayout(entry_row)

        self._completion_model = QStringListModel(self)
        self._token_completer = QCompleter(self._completion_model, self)
        self._token_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._token_completer.setFilterMode(Qt.MatchContains)
        self._token_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._token_completer.setMaxVisibleItems(14)
        self.input.setCompleter(self._token_completer)
        self._token_completer.activated[str].connect(self._insert_completion)
        self.input.textEdited.connect(self._complete_token)
        self.input.returnPressed.connect(self._add_current_input)
        self.add_button.clicked.connect(self._add_current_input)

    def set_completion_values(self, values: list[str]) -> None:
        self._completion_values = sorted({self._normalize(value) for value in values if self._normalize(value)})
        self._completion_model.setStringList(self._completion_values)

    def _normalize(self, value: object) -> str:
        token = str(value or "").strip().upper()
        if not token:
            return ""
        if self._token_prefix:
            token = f"{self._token_prefix}{token.lstrip(self._token_prefix)}"
        return token

    def setPlaceholderText(self, text: str) -> None:
        self.input.setPlaceholderText(text)

    def placeholderText(self) -> str:
        return self.input.placeholderText()

    def completer(self) -> QCompleter:
        return self._token_completer

    def setFocus(self, reason=Qt.OtherFocusReason) -> None:  # type: ignore[override]
        self.input.setFocus(reason)

    def text(self) -> str:
        return ", ".join(self._values)

    def setText(self, text: object) -> None:
        values: list[str] = []
        seen: set[str] = set()
        for part in str(text or "").split(","):
            value = self._normalize(part)
            if value and value not in seen:
                values.append(value)
                seen.add(value)
        self._values = values
        self.input.clear()
        self._rebuild_chips()

    def clear(self) -> None:
        self._values = []
        self.input.clear()
        self._rebuild_chips()

    def _complete_token(self, text: str) -> None:
        token = self._normalize(text)
        if not token:
            self._token_completer.popup().hide()
            return
        self._token_completer.setCompletionPrefix(token)
        if self._token_completer.completionCount():
            self._token_completer.complete()

    def _insert_completion(self, value: str) -> None:
        self._add_values(value)
        # QLineEdit applies a clicked QCompleter value after activated handlers
        # return on some Qt/platform combinations. Clear again on the next event
        # turn so the accepted lookup cannot remain in front of the next search.
        QTimer.singleShot(0, self, self._reset_entry_input)

    def _add_current_input(self) -> None:
        self._add_values(self.input.text())

    def _add_values(self, text: object) -> None:
        changed = False
        existing = set(self._values)
        for part in str(text or "").split(","):
            value = self._normalize(part)
            if value and value not in existing:
                self._values.append(value)
                existing.add(value)
                changed = True
        self._reset_entry_input()
        if changed:
            self._rebuild_chips()
        self.input.setFocus()

    def _reset_entry_input(self) -> None:
        self.input.clear()
        self._token_completer.setCompletionPrefix("")
        self._token_completer.popup().hide()

    def _remove_value(self, value: str) -> None:
        self._values = [item for item in self._values if item != value]
        self._rebuild_chips()

    def _rebuild_chips(self) -> None:
        self.setMinimumHeight(0)
        existing = {
            str(chip.property("tokenValue") or ""): chip
            for chip in self._chip_buttons
        }
        wanted = set(self._values)
        for value, chip in existing.items():
            if value not in wanted:
                self.chip_layout.removeWidget(chip)
                chip.hide()
                chip.deleteLater()

        next_buttons: list[QPushButton] = []
        for value in self._values:
            chip = existing.get(value)
            if chip is None:
                chip = QPushButton(f"{value}  ×", self.chip_body)
                chip.setObjectName("fioSpotterTokenChip")
                chip.setProperty("tokenValue", value)
                chip.setAccessibleName(f"Remove {value}")
                chip.setToolTip(f"Remove {value}")
                chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                chip.clicked.connect(lambda _checked=False, selected=value: self._remove_value(selected))
            else:
                self.chip_layout.removeWidget(chip)
            self.chip_layout.addWidget(chip)
            chip.show()
            next_buttons.append(chip)
        self._chip_buttons = next_buttons
        self.chip_layout.activate()
        chip_height = max((chip.sizeHint().height() for chip in self._chip_buttons), default=0)
        scrollbar_height = self.chip_scroll.horizontalScrollBar().sizeHint().height()
        self.chip_scroll.setFixedHeight(
            max(self.input.sizeHint().height() + 6, chip_height + scrollbar_height + 4)
        )
        body_hint = self.chip_layout.sizeHint()
        self.chip_body.setFixedSize(max(1, body_hint.width()), max(1, body_hint.height()))
        self.chip_scroll.setVisible(bool(self._values))
        self.layout().activate()
        self.setMinimumHeight(self.sizeHint().height())
        self.updateGeometry()
        self.valuesChanged.emit()


class FioSpotterTab(QWidget):
    """A bounded browser for CommsPlan's existing Spotter facilities.

    Tables deliberately contain at most 200 rows.  A tab is populated only on
    first visit or explicit refresh, so changing selection or painting never
    opens a database or walks a forms directory.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: SettingsManager | None = None,
        open_compose: Callable[..., None] | None = None,
        open_inbox: Callable[[dict[str, Any]], None] | None = None,
        open_map: Callable[[dict[str, Any]], None] | None = None,
        open_operator: Callable[[dict[str, Any]], None] | None = None,
        radio_store: object | None = None,
        legacy_form_sources: object | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings or SettingsManager()
        self._open_compose = open_compose
        self._open_inbox = open_inbox
        self._open_map = open_map
        self._open_operator = open_operator
        self._radio_store_override = radio_store
        self._legacy_form_sources: list[tuple[str, str]] = []
        self._set_legacy_form_sources_model(legacy_form_sources)
        self._built: set[int] = set()
        self._loaded: set[int] = set()
        self._policy_rows: list[dict[str, Any]] = []
        self._entry_rows: list[dict[str, Any]] = []
        self._policy_usage_entries: dict[int, list[dict[str, Any]]] = {}
        self._policy_radio_name_to_id: dict[str, str] = {}
        self._policy_radio_id_to_name: dict[str, str] = {}
        self._watch_rows: list[dict[str, Any]] = []
        self._expect_access_catalog_loaded_at = 0.0
        self._pending_expect_entry_id = 0
        self._pending_expect_key = ""
        self._compact = False
        self.setObjectName("fioSpotterTab")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 12)
        outer.setSpacing(8)
        title = QLabel("CommsPlan Spotter")
        title.setObjectName("fioSpotterTitle")
        title.setAccessibleName("CommsPlan Spotter service")
        outer.addWidget(title)
        why = QLabel("Configure shared watches and safely administer JS8 Expect automation. Operational traffic is unified in Message Inbox.")
        why.setWordWrap(True)
        why.setObjectName("fioSpotterWhy")
        outer.addWidget(why)
        self.tab_selector = QListWidget()
        self.tab_selector.setObjectName("fioSpotterModeSelector")
        self.tab_selector.setAccessibleName("CommsPlan Spotter configuration sections")
        self.tab_selector.setFlow(QListWidget.LeftToRight)
        self.tab_selector.setWrapping(True)
        self.tab_selector.setResizeMode(QListWidget.Adjust)
        self.tab_selector.setMovement(QListWidget.Static)
        self.tab_selector.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tab_selector.setUniformItemSizes(False)
        self.tab_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for name in _TAB_NAMES:
            item = QListWidgetItem(name)
            item.setToolTip(f"Open CommsPlan Spotter {name}")
            self.tab_selector.addItem(item)
        self.tab_selector.setCurrentRow(0)
        outer.addWidget(self.tab_selector)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("fioSpotterBrowserTabs")
        self.tabs.setAccessibleName("CommsPlan Spotter browser tabs")
        for name in _TAB_NAMES:
            page = QWidget()
            page.setObjectName(f"fioSpotter{name}Page")
            self.tabs.addTab(page, name)
        self.tabs.tabBar().hide()
        self.tab_selector.currentRowChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self._sync_tab_selector)
        self.tabs.currentChanged.connect(self._activate_tab)
        outer.addWidget(self.tabs, 1)
        # Construct the default page in its final parent before first show, but
        # defer its store read until the page is activated.  This avoids a
        # first-frame placeholder/page replacement and keeps window geometry
        # stable while retaining the existing bounded refresh behavior.
        self._activate_tab(_TAB_INDEX["Watches"], refresh=False)
        self._refresh_tab_selector_geometry()

    def set_tab_active(self, active: bool) -> None:
        """Lifecycle hook used by MainWindow's lazy screen controller."""
        if active:
            self._activate_tab(self.tabs.currentIndex())

    def open_expect_entry(self, *, entry_id: int = 0, expect_key: str = "") -> None:
        """Open authoritative Expect administration and select fresh store data."""
        self._pending_expect_entry_id = max(0, int(entry_id or 0))
        self._pending_expect_key = _text(expect_key).upper()
        expect_index = _TAB_INDEX["Expect"]
        already_built = expect_index in self._built
        self.tabs.setCurrentIndex(expect_index)
        if already_built:
            self.refresh_expect()

    def open_forms(self) -> None:
        """Select the Forms workspace and refresh its bounded catalog."""
        index = _TAB_INDEX["Forms"]
        self.tabs.setCurrentIndex(index)
        if index in self._built:
            self.refresh_forms()

    def _set_legacy_form_sources_model(self, rows: object) -> None:
        choices: list[tuple[str, str]] = []
        for row in rows or ():
            if isinstance(row, Mapping):
                path = _text(row.get("path") or row.get("forms_path") or row.get("js8_forms_path"))
                label = _text(row.get("label") or row.get("radio_name") or row.get("name") or path)
            else:
                path = _text(row)
                label = path
            if path and (label, path) not in choices:
                choices.append((label, path))
        self._legacy_form_sources = choices

    def set_legacy_form_sources(self, rows: object) -> None:
        """Publish cached per-radio form paths for an explicit fallback choice."""
        self._set_legacy_form_sources_model(rows)
        combo = getattr(self, "forms_legacy_combo", None)
        if isinstance(combo, QComboBox):
            selected_path = str(combo.currentData() or "")
            combo.blockSignals(True)
            combo.clear()
            for label, path in self._legacy_form_sources:
                combo.addItem(f"{label} — {path}", path)
            selected_index = combo.findData(selected_path)
            if selected_index >= 0:
                combo.setCurrentIndex(selected_index)
            combo.setVisible(bool(self._legacy_form_sources))
            combo.blockSignals(False)
        if _TAB_INDEX["Forms"] in self._built:
            self._refresh_forms_state()
            self.refresh_forms()

    def open_imports(self) -> None:
        """Select the bounded External Spotter import snapshot workspace."""
        index = _TAB_INDEX["Imports"]
        self.tabs.setCurrentIndex(index)
        if index in self._built:
            self.refresh_imports()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        compact = self.width() <= 1000
        if compact != self._compact:
            self._compact = compact
            for splitter in self.findChildren(QSplitter):
                splitter.setOrientation(Qt.Vertical if compact else Qt.Horizontal)
            # The generic shell transition may have already assigned the
            # Watches splitter its new orientation. Preserve the fact that it
            # still needs its table-dominant default allocation once Qt has
            # published the resized viewport.
            self._watch_split_needs_default_sizing = True
        # Watches need a little more room than the other editor/list pairs:
        # below this breakpoint a side-by-side editor would be narrower than
        # its condition controls and make the table look clipped.  This is
        # geometry-only; no refresh or other store work is triggered here.
        self._apply_watch_responsive_layout()
        super().resizeEvent(event)
        self._refresh_tab_selector_geometry()
        # Expect's metadata breakpoints are based on its actual laid-out
        # content width, not merely the outer tab width.  Rechecking after Qt
        # applies geometry makes the first visible frame correct and is
        # layout-only work.
        self._apply_expect_responsive_layout()
        if hasattr(self, "expect_editor_split"):
            # The page layout must be reactivated after a previously wide
            # splitter becomes compact. This releases a cached wide child size
            # in QScrollArea without scheduling work or resetting scroll.
            page = self.expect_editor_split.parentWidget()
            if page is not None and page.layout() is not None:
                page.layout().invalidate()
                page.layout().activate()
                page.updateGeometry()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            # Font changes alter control minimums and preview line heights even
            # when the outer viewport is unchanged. Reflow only from cached
            # widget metrics; never activate a tab or refresh a store here.
            self._apply_watch_responsive_layout()
            self._apply_expect_responsive_layout()
            self._fit_forms_preview_geometry()
            self._refresh_tab_selector_geometry()

    def _sync_tab_selector(self, index: int) -> None:
        if not hasattr(self, "tab_selector") or self.tab_selector.currentRow() == index:
            return
        self.tab_selector.blockSignals(True)
        self.tab_selector.setCurrentRow(index)
        self.tab_selector.blockSignals(False)

    def _refresh_tab_selector_geometry(self) -> None:
        selector = getattr(self, "tab_selector", None)
        if not isinstance(selector, QListWidget):
            return
        fit_wrapping_choice_chip_selector(selector)

    def apply_theme(self) -> None:
        """Apply shared semantic roles to Spotter actions and splitters.

        The application stylesheet owns the base widget vocabulary.  This
        hook only assigns roles to this workspace's actions so theme changes
        do not create a screen-local visual language.
        """
        theme = resolve_theme(self.settings)
        role_map = {
            "watches_refresh": "muted",
            "watch_save": "primary",
            "watch_new": "secondary",
            "watch_delete": "danger",
            "watch_test": "info",
            "expect_refresh": "muted",
            "expect_update_dates": "secondary",
            "expect_select_shown": "secondary",
            "expect_bulk_auto_reply": "primary",
            "expect_bulk_saved_only": "secondary",
            "expect_runtime_chip": "muted",
            "dynamic_flamp_chip": "muted",
            "expect_policy_summary": "secondary",
            "expect_legacy_convert": "secondary",
            "expect_save": "primary",
            "expect_view": "secondary",
            "expect_send_now": "secondary",
            "expect_new": "secondary",
            "expect_manage_policies": "secondary",
            "expect_delete": "danger",
            "expect_history": "muted",
            "policy_refresh": "muted",
            "policy_new": "secondary",
            "policy_save": "primary",
            "policy_delete": "danger",
            "forms_refresh_btn": "muted",
            "forms_browse_btn": "secondary",
            "forms_use_folder_btn": "secondary",
            "forms_add_btn": "secondary",
            "forms_classify_btn": "secondary",
            "forms_save_btn": "primary",
            "forms_preview_btn": "secondary",
            "forms_compose_btn": "primary",
            "forms_expect_btn": "secondary",
            "import_choose_btn": "secondary",
            "import_preview_btn": "secondary",
            "import_apply_btn": "primary",
        }
        for name, role in role_map.items():
            button = getattr(self, name, None)
            if button is not None:
                button.setStyleSheet(button_style(role, theme))
        self._style_expect_status_chips(theme)
        selector = getattr(self, "tab_selector", None)
        if isinstance(selector, QListWidget):
            selector.setStyleSheet(choice_chip_selector_style(selector.objectName(), theme))
            self._refresh_tab_selector_geometry()
        for splitter in self.findChildren(QSplitter):
            style_splitter_handles(splitter, theme)
        for name in ("forms_state", "imports_state"):
            label = getattr(self, name, None)
            if isinstance(label, QLabel):
                label.setStyleSheet(label_style("muted", theme, weight=600))
        self._refresh_forms_action_state()
        self._refresh_import_action_state()
        fit_child_combo_boxes(self)

    def _style_expect_status_chips(self, theme=None) -> None:
        """Apply shared semantic button roles to compact Expect status chips."""
        colors = theme or resolve_theme(self.settings)
        service = getattr(self, "expect_runtime_chip", None)
        if isinstance(service, QPushButton):
            service.setStyleSheet(button_style("success" if service.isChecked() else "warning", colors))
        flamp = getattr(self, "dynamic_flamp_chip", None)
        if isinstance(flamp, QPushButton):
            state = str(flamp.property("expectStatus") or "waiting")
            flamp.setStyleSheet(button_style(
                {"ready": "success", "attention": "warning", "waiting": "muted"}.get(state, "muted"),
                colors,
            ))

    def _apply_watch_responsive_layout(self) -> None:
        if not hasattr(self, "watches_split"):
            return
        split = self.watches_split
        editor = split.widget(1)
        editor_min_width = editor.minimumWidth() if editor is not None else 0
        # Keep enough room for the editor's condition row and at least an
        # equally useful table pane.  The font-derived term matters in Large
        # Text mode; a fixed breakpoint alone would clip the editor there.
        compact = self.width() <= max(1200, (editor_min_width * 2) + 40)
        wanted = Qt.Vertical if compact else Qt.Horizontal
        changed = split.orientation() != wanted
        if changed:
            split.setOrientation(wanted)
            self._watch_split_needs_default_sizing = True
        available = split.height() if compact else split.width()
        needs_default = bool(getattr(self, "_watch_split_needs_default_sizing", False))
        if available > 0 and (needs_default or sum(split.sizes()) <= 0):
            if compact:
                first = max(1, available // 2)
            else:
                first = max(1, int(available * 0.75))
            split.setSizes([first, max(1, available - first)])
            self._watch_split_needs_default_sizing = False

    def _apply_expect_responsive_layout(self) -> None:
        compact = self.width() <= 1000
        # This follows the same responsive breakpoint as the two-column form
        # so an ordinary wide/compact transition always reapplies it.
        expect_wide = not compact
        if hasattr(self, "policy_editor_columns"):
            self.policy_editor_columns.setDirection(
                QHBoxLayout.TopToBottom if compact else QHBoxLayout.LeftToRight
            )
            self.policy_editor_columns.invalidate()
            self.policy_editor_columns.activate()
            self.policy_editor_columns.parentWidget().updateGeometry()
            if hasattr(self, "policy_summary_layout"):
                self.policy_summary_layout.setDirection(
                    QHBoxLayout.TopToBottom if compact else QHBoxLayout.LeftToRight
                )
                self.policy_summary_layout.invalidate()
                self.policy_summary_layout.activate()
            self._update_policy_editor_height()
        if not hasattr(self, "expect_editor_split"):
            return
        # The response editor is intentionally above the saved-response table
        # at every width.  The page scroll area owns short-height overflow;
        # changing this splitter to horizontal makes the editor unusably narrow.
        self.expect_editor_split.setOrientation(Qt.Vertical)
        metadata_reflowed = False
        if hasattr(self, "expect_editor_meta"):
            # The response identity is most useful as one short scan row on a
            # normal desktop. Reflow only existing widgets when the available
            # width changes: resize never opens the store or contacts JS8Call.
            # Each field group keeps its label and input together so the scan
            # order remains clear when two groups wrap or all groups stack.
            minimums = [group.minimumSizeHint().width() for group in self.expect_editor_meta_groups]
            spacing = self.expect_editor_meta.horizontalSpacing()
            # Use the outer tab width as the upper bound.  A grid that was
            # previously two-column can otherwise keep its own minimum width
            # and prevent the compact state from ever being selected.
            available = max(
                0,
                min(
                    self.expect_editor_meta.parentWidget().contentsRect().width(),
                    self.width() - control_height_for_font(self, vertical_padding=18, floor=48),
                ),
            )
            one_row_width = sum(minimums) + (spacing * (len(minimums) - 1))
            two_column_width = max(
                minimums[0] + minimums[1],
                minimums[2] + minimums[3],
            ) + spacing
            wanted_mode = "wide" if available >= one_row_width else (
                "medium" if available >= two_column_width else "compact"
            )
            if wanted_mode != getattr(self, "_expect_meta_layout_mode", ""):
                for group in self.expect_editor_meta_groups:
                    self.expect_editor_meta.removeWidget(group)
                if wanted_mode == "wide":
                    for column, group in enumerate(self.expect_editor_meta_groups):
                        self.expect_editor_meta.addWidget(group, 0, column)
                        self.expect_editor_meta.setColumnStretch(column, 1)
                elif wanted_mode == "medium":
                    for index, group in enumerate(self.expect_editor_meta_groups):
                        row = index // 2
                        column = index % 2
                        self.expect_editor_meta.addWidget(group, row, column)
                        self.expect_editor_meta.setColumnStretch(column, 1)
                else:
                    for row, group in enumerate(self.expect_editor_meta_groups):
                        self.expect_editor_meta.addWidget(group, row, 0)
                    self.expect_editor_meta.setColumnStretch(0, 1)
                self._expect_meta_layout_mode = wanted_mode
                metadata_reflowed = True
                self.expect_editor_meta.invalidate()
                self.expect_editor_meta.activate()
                self.expect_editor_meta.parentWidget().updateGeometry()
        if hasattr(self, "expect_editor_actions"):
            # A wide desktop has room for one action row. Keeping these action
            # controls in a two-column grid wastes rows and pushes
            # the saved-response table below the fold at high-DPI scales.
            for index, button in enumerate(self.expect_editor_action_buttons):
                self.expect_editor_actions.addWidget(
                    button, 0 if expect_wide else index // 2,
                    index if expect_wide else index % 2,
                )
            for column in range(8):
                self.expect_editor_actions.setColumnStretch(column, 0)
            self.expect_editor_actions.setColumnStretch(7 if expect_wide else 2, 1)
            self.expect_editor_actions.invalidate()
            self.expect_editor_actions.activate()
            self.expect_editor_actions.parentWidget().updateGeometry()
        if metadata_reflowed:
            # A QScrollArea caches its child geometry through a prior wide
            # splitter layout. Invalidate the page layout once after a real
            # reflow so a compact window releases that stale width instead of
            # acquiring page-level horizontal overflow.
            page = self.expect_editor_split.parentWidget()
            if page is not None and page.layout() is not None:
                page.layout().invalidate()
                page.layout().activate()
                page.updateGeometry()
        if hasattr(self, "expect_history_split"):
            self.expect_history_split.setOrientation(
                Qt.Vertical if compact else Qt.Horizontal
            )
            self.expect_history_split.setMaximumHeight(self._expect_history_height())
        self._update_expect_editor_minimum_height()

    def _expect_history_height(self) -> int:
        """Return a font/content-aware cap for the optional history preview."""

        split = getattr(self, "expect_history_split", None)
        if split is None:
            return 1
        tables = [
            table for table in (
                getattr(self, "expect_requests_table", None),
                getattr(self, "expect_replies_table", None),
            ) if table is not None
        ]
        line_height = max(
            (table.fontMetrics().lineSpacing() for table in tables),
            default=1,
        )
        row_height = max(
            [
                line_height + 8,
                *(
                    table.sizeHintForRow(0)
                    for table in tables
                    if table.rowCount() > 0 and table.sizeHintForRow(0) > 0
                ),
            ]
        )
        minimum = max(
            control_height_for_font(split, vertical_padding=12, floor=32) * 3,
            row_height * 3 + 12,
        )
        viewport = max(minimum * 2, int(self.height() or 0))
        return max(minimum, int(viewport * 0.4))

    def _load_expect_access_completions(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._expect_access_catalog_loaded_at and (now - self._expect_access_catalog_loaded_at) < 60.0:
            return
        try:
            catalog = list_expect_operator_access_catalog(limit=2000)
        except Exception:
            catalog = []
        callsigns = [str(row.get("callsign") or "") for row in catalog]
        groups = sorted({
            str(group or "").strip().upper()
            for row in catalog for group in (row.get("groups") or [])
            if str(group or "").strip()
        })
        addressed_groups = [f"@{group.lstrip('@')}" for group in groups]
        caller_editors = [
            widget for name in ("expect_calls", "expect_blocked", "policy_calls", "policy_blocked")
            if (widget := getattr(self, name, None)) is not None
        ]
        query_group_editors = [
            widget for name in ("expect_groups", "policy_groups")
            if (widget := getattr(self, name, None)) is not None
        ]
        trusted_group_editors = [
            widget for name in ("expect_trusted_groups", "policy_trusted_groups")
            if (widget := getattr(self, name, None)) is not None
        ]
        for widget in caller_editors:
            widget.set_completion_values(callsigns)
        for widget in query_group_editors:
            widget.set_completion_values(addressed_groups)
        for widget in trusted_group_editors:
            widget.set_completion_values(groups)
        historical = sum(1 for row in catalog if row.get("historical"))
        trusted = len({str(row.get("current_callsign") or "") for row in catalog if row.get("trusted")})
        state_label = getattr(self, "expect_access_catalog_state", None)
        if state_label is not None:
            state_label.setText(
                f"Lookup: {len(callsigns)} callsigns ({historical} historical); "
                f"{trusted} trusted operators; {len(groups)} operator groups."
            )
        policy_state_label = getattr(self, "policy_access_catalog_state", None)
        if policy_state_label is not None:
            policy_state_label.setText(
                f"Lookup: {len(callsigns)} callsigns; {trusted} trusted operators; "
                f"{len(groups)} operator groups."
            )
            policy_state_label.updateGeometry()
            if hasattr(self, "policy_editor_panel"):
                self._update_policy_editor_height()
        self._expect_access_catalog_loaded_at = now
        self._expect_access_catalog_loaded_at = now

    @staticmethod
    def _fit_spotter_combo(combo: QComboBox, *, minimum_characters: int = 10) -> None:
        """Keep compact editors flexible while making every popup legible."""
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(max(6, min(24, int(minimum_characters))))
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        metrics = combo.fontMetrics()
        widest = max(
            (metrics.horizontalAdvance(combo.itemText(index)) for index in range(combo.count())),
            default=metrics.horizontalAdvance(combo.currentText()),
        )
        try:
            combo.view().setMinimumWidth(min(max(widest + 56, 160), 640))
        except Exception:
            pass
        for index in range(combo.count()):
            combo.setItemData(index, combo.itemText(index), Qt.ToolTipRole)

    def _radio_store(self) -> object:
        if callable(getattr(self._radio_store_override, "list_device_profiles", None)):
            return self._radio_store_override
        host_store = getattr(self.window(), "multi_radio_store", None)
        if callable(getattr(host_store, "list_device_profiles", None)):
            return host_store
        db_path = getattr(self.settings, "db_path", None)
        return MultiRadioStore(Path(db_path)) if db_path else MultiRadioStore()

    def _refresh_expect_radio_choices(self) -> None:
        if not hasattr(self, "expect_radio") and not hasattr(self, "policy_radios"):
            return
        selected = _text(self.expect_radio.currentData()) if hasattr(self, "expect_radio") else ""
        try:
            profiles = list(self._radio_store().list_device_profiles())
        except Exception as exc:
            profiles = []
            log.warning("CommsPlan Spotter: configured radio list unavailable: %s", exc)
        js8_profiles = [
            dict(profile) for profile in profiles
            if (profile.get("use_js8call") or profile.get("js8_instance_id"))
            and js8_profile_allows_transmit(profile)
        ]
        self._policy_radio_name_to_id: dict[str, str] = {}
        self._policy_radio_id_to_name: dict[str, str] = {}
        choices: list[tuple[str, str]] = []
        for profile in js8_profiles:
            profile_id = _text(profile.get("id"))
            if not profile_id:
                continue
            name = _text(profile.get("name")) or f"Radio {profile_id}"
            if not bool(profile.get("enabled", True)):
                name = f"{name} (inactive)"
            choices.append((name, profile_id))
            self._policy_radio_name_to_id[name.upper()] = profile_id
            self._policy_radio_id_to_name[profile_id] = name
        if hasattr(self, "expect_radio"):
            self.expect_radio.blockSignals(True)
            try:
                self.expect_radio.clear()
                self.expect_radio.addItem("All JS8 radios — reply on receiving radio", "")
                for name, profile_id in choices:
                    self.expect_radio.addItem(name, profile_id)
                match = self.expect_radio.findData(selected)
                self.expect_radio.setCurrentIndex(match if match >= 0 else 0)
            finally:
                self.expect_radio.blockSignals(False)
            self._fit_spotter_combo(self.expect_radio, minimum_characters=20)
        if hasattr(self, "policy_radios"):
            self.policy_radios.set_completion_values([name for name, _profile_id in choices])

    def _policy_radio_ids(self) -> list[str]:
        """Map known radio labels back to durable IDs; preserve legacy IDs."""
        return [
            self._policy_radio_name_to_id.get(value.upper(), value)
            for value in _csv(self.policy_radios.text())
        ]

    def _policy_radio_labels(self, radio_ids: object) -> str:
        values = (
            [_text(value) for value in radio_ids if _text(value)]
            if isinstance(radio_ids, (list, tuple, set)) else _csv(radio_ids)
        )
        return ", ".join(
            self._policy_radio_id_to_name.get(value, f"Unavailable radio ({value})")
            for value in values
        )

    def _cache_policy_usage_entries(self) -> None:
        """Index the already-loaded response page for selection-time detail."""
        usage: dict[int, list[dict[str, Any]]] = {}
        for entry in self._entry_rows:
            policy_id = int(entry.get("allow_policy_id") or 0)
            if policy_id > 0:
                usage.setdefault(policy_id, []).append(entry)
        self._policy_usage_entries = usage

    def _show_policy_usage(self, policy_id: int) -> None:
        """Show cached policy impact without querying while the editor changes."""
        if not hasattr(self, "policy_usage_state"):
            return
        policy = next(
            (row for row in self._policy_rows if int(row.get("id") or 0) == policy_id),
            {},
        )
        entries = self._policy_usage_entries.get(policy_id, [])
        count = int(policy.get("usage_count") or len(entries))
        keys = ", ".join(_text(entry.get("expect_key")) for entry in entries[:8])
        if not count:
            text = "Not yet used by an Expect response."
        elif keys:
            more = " …" if count > len(entries) else ""
            text = f"Used by {count} saved response(s): {keys}{more}."
        else:
            text = f"Used by {count} saved response(s). Open Expect to review or reassign them."
        self.policy_usage_state.setText(text)
        self.policy_usage_state.updateGeometry()
        self._update_policy_editor_height()

    def _select_expect_radio(self, source_scope: object, source_radio_id: object) -> None:
        radio_id = _text(source_radio_id)
        if _text(source_scope).lower() != "radio" or not radio_id:
            self.expect_radio.setCurrentIndex(0)
            return
        index = self.expect_radio.findData(radio_id)
        if index < 0:
            self.expect_radio.addItem(f"Unavailable configured radio · ID {radio_id}", radio_id)
            self._fit_spotter_combo(self.expect_radio, minimum_characters=20)
            index = self.expect_radio.count() - 1
        self.expect_radio.setCurrentIndex(index)

    def _expect_radio_label(self, source_scope: object, source_radio_id: object) -> str:
        radio_id = _text(source_radio_id)
        if _text(source_scope).lower() != "radio" or not radio_id:
            return "All JS8 radios"
        index = self.expect_radio.findData(radio_id) if hasattr(self, "expect_radio") else -1
        return self.expect_radio.itemText(index) if index >= 0 else f"Radio ID {radio_id}"

    def _activate_tab(self, index: int, *, refresh: bool = True) -> None:
        newly_built = index not in self._built
        started = time.perf_counter()
        builders: tuple[Callable[[QWidget], None], ...] = (
            self._build_watches, self._build_expect,
            self._build_policies, self._build_forms, self._build_imports,
        )
        if newly_built:
            builders[index](self.tabs.widget(index))
            self._built.add(index)
            self.apply_theme()
        if not refresh or index in self._loaded:
            return
        self._loaded.add(index)
        # Forms/import preview can touch an external directory/database, so
        # those scans are operator-triggered rather than tab-activation work.
        if index == _TAB_INDEX["Watches"]:
            self.refresh_watches()
        elif index == _TAB_INDEX["Expect"]:
            self.refresh_expect()
            # The first build happens before Qt has assigned the tab page its
            # final width.  One generation-neutral, geometry-only follow-up
            # lets the metadata strip choose its true initial wrap without
            # doing I/O or creating a resize loop.
            QTimer.singleShot(0, self._apply_expect_responsive_layout)
        elif index == _TAB_INDEX["Access Policies"]:
            self.refresh_policies()
        elif index == _TAB_INDEX["Forms"]:
            self._refresh_forms_state()
        else:
            self.refresh_imports()
        emit_span(
            "fio_spotter.tab_first_load",
            (time.perf_counter() - started) * 1000.0,
            meta={"index": index, "tab": _TAB_NAMES[index]},
            min_ms=10.0,
        )

    @staticmethod
    def _table(headers: list[str], *, name: str) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setObjectName(name)
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSizeAdjustPolicy(QAbstractItemView.AdjustIgnored)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    @staticmethod
    def _put(table: QTableWidget, row: int, col: int, value: object, *, data: object = None) -> None:
        item = QTableWidgetItem(_text(value) or "—")
        if data is not None:
            item.setData(Qt.UserRole, data)
        item.setToolTip(_text(value))
        table.setItem(row, col, item)

    @staticmethod
    def _page_layout(page: QWidget) -> QVBoxLayout:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(page)
        scroll.setObjectName(f"{page.objectName()}Scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # Tables own their horizontal overflow.  Keeping page-level horizontal
        # scrolling disabled prevents the whole service workspace from sliding
        # sideways at compact widths or with Large Text enabled.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content = QWidget(scroll)
        content.setObjectName(f"{page.objectName()}Content")
        # The scroll viewport owns width. Ignore aggregate child size hints so
        # a few frame pixels or a long label cannot create hidden page-level
        # horizontal overflow; responsive child layouts handle the reflow.
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(8)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        return layout

    def open_watch_draft(self, candidate: Mapping[str, object]) -> None:
        """Stage, but never save, a watch suggested by a cached Inbox row."""

        row = dict(candidate or {})
        watches_index = _TAB_INDEX["Watches"]
        self.tabs.setCurrentIndex(watches_index)
        if watches_index not in self._built:
            self._activate_tab(watches_index)
        self._clear_watch()
        self._watch_preview_candidate = row
        callsign = _text(row.get("from_call")).upper()
        topics = [str(value or "").strip() for value in row.get("topics") or () if str(value or "").strip()]
        status = _text(row.get("status")).upper()
        group = _text(row.get("group_name") or row.get("to_call")).upper().lstrip("@")
        primary_kind = "callsign" if callsign else "group" if group else "topic" if topics else "status"
        primary_value = callsign or group or (topics[0] if topics else status)
        secondary_kind = "topic" if callsign and topics else "status" if callsign and status in {"YELLOW", "RED"} else ""
        secondary_value = topics[0] if secondary_kind == "topic" else status if secondary_kind else ""
        self.watch_name.setText(
            " · ".join(value for value in (callsign or group, secondary_value) if value)
            or "Inbox watch"
        )
        self.watch_kind.setCurrentText(primary_kind)
        self.watch_pattern.setText(primary_value)
        self.watch_mode.setCurrentText("exact" if primary_kind in {"callsign", "group", "status"} else "contains")
        if hasattr(self, "watch_secondary_kind"):
            self.watch_secondary_kind.setCurrentIndex(
                max(0, self.watch_secondary_kind.findData(secondary_kind))
            )
            self.watch_secondary_pattern.setText(secondary_value)
            self.watch_secondary_mode.setCurrentText(
                "exact" if secondary_kind == "status" else "contains"
            )
        source = _text(row.get("source_scope") or row.get("source_family")).lower()
        self.watch_sources.setText(source if source else "")
        radio_id = _text(row.get("radio_id"))
        self.watch_radios.setText(radio_id)
        self.watch_status.setText(
            "Review this suggested watch, adjust its criteria or expiry, then Save. "
            "Nothing has been added yet."
        )

    # Watches --------------------------------------------------------------
    def _build_watches(self, page: QWidget) -> None:
        layout = self._page_layout(page)
        hint = QLabel("Shared watches match callsigns, groups, sources, kinds, topics, status, locations, and keywords across supported Message Inbox traffic. Changes apply to the shared station watch service.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        header = QHBoxLayout()
        header.addWidget(QLabel("Watches"))
        header.addStretch(1)
        refresh = QPushButton("Refresh")
        self.watches_refresh = refresh
        refresh.setAccessibleName("Refresh Spotter watches")
        refresh.setToolTip("Read the latest bounded watch list")
        refresh.clicked.connect(self.refresh_watches)
        header.addWidget(refresh)
        layout.addLayout(header)
        split = QSplitter(Qt.Horizontal)
        split.setObjectName("fioSpotterWatchesSplit")
        split.setChildrenCollapsible(False)
        self.watches_split = split
        # A lazily-built tab may be created after the host has already entered
        # compact mode, so apply the current responsive orientation here too.
        if self.width() <= 1200:
            split.setOrientation(Qt.Vertical)
        self.watches_table = self._table(["State", "Watch", "Match", "Sources", "Priority", "Expires", "Last match", "Health"], name="fioSpotterWatchesTable")
        self.watches_table.itemSelectionChanged.connect(self._load_selected_watch)
        watch_hdr = self.watches_table.horizontalHeader()
        for col in (0, 2, 4, 5, 6, 7):
            watch_hdr.setSectionResizeMode(col, QHeaderView.Interactive)
        watch_hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, width in ((0, 50), (2, 150), (4, 80), (5, 130), (6, 130), (7, 90)):
            self.watches_table.setColumnWidth(col, width)
        split.addWidget(self.watches_table)
        editor = QGroupBox("Watch editor")
        editor.setObjectName("fioSpotterWatchEditor")
        editor.setAccessibleName("Spotter watch editor")
        # Let the splitter honor its table-first sizing even when a large-font
        # form has a generous size hint; controls remain locally scrollable in
        # the page rather than widening the whole service shell.
        editor.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(10, 8, 10, 8)
        editor_layout.setSpacing(6)

        # Keep the enable state with the actions.  This makes it a quick
        # editor-level control instead of another full-width form row.
        editor_actions = QWidget(editor)
        editor_actions.setObjectName("fioSpotterWatchEditorActions")
        editor_actions.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        editor_action_row = QHBoxLayout(editor_actions)
        editor_action_row.setContentsMargins(0, 0, 0, 0)
        editor_action_row.setSpacing(5)
        self.watch_enabled = QCheckBox("Enabled")
        self.watch_enabled.setObjectName("fioSpotterWatchEnabled")
        self.watch_enabled.setAccessibleName("Watch enabled")
        self.watch_enabled.setChecked(True)
        editor_action_row.addWidget(self.watch_enabled)
        editor_action_row.addStretch(1)
        save = QPushButton("Save"); self.watch_save = save; save.setAccessibleName("Save Spotter watch"); save.clicked.connect(self._save_watch)
        new = QPushButton("New"); self.watch_new = new; new.setAccessibleName("Start a new Spotter watch"); new.clicked.connect(self._clear_watch)
        editor_action_row.addWidget(save)
        editor_action_row.addWidget(new)
        editor_layout.addWidget(editor_actions)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.watch_name = QLineEdit(); self.watch_name.setPlaceholderText("Watch name")
        self.watch_kind = QComboBox(); self.watch_kind.addItems(WATCH_KINDS); self.watch_kind.setCurrentText("keyword")
        self.watch_pattern = QLineEdit(); self.watch_pattern.setPlaceholderText("What to match")
        self.watch_mode = QComboBox(); self.watch_mode.addItems(MATCH_MODES)
        self.watch_secondary_kind = QComboBox()
        self.watch_secondary_kind.addItem("No second condition", "")
        for kind in WATCH_KINDS:
            if kind != "structured":
                self.watch_secondary_kind.addItem(kind.title(), kind)
        self.watch_secondary_pattern = QLineEdit()
        self.watch_secondary_pattern.setPlaceholderText("Optional second condition")
        self.watch_secondary_mode = QComboBox(); self.watch_secondary_mode.addItems(MATCH_MODES)
        secondary = QWidget()
        secondary_layout = QHBoxLayout(secondary)
        secondary_layout.setContentsMargins(0, 0, 0, 0)
        secondary_layout.addWidget(self.watch_secondary_kind)
        secondary_layout.addWidget(self.watch_secondary_pattern, 1)
        secondary_layout.addWidget(self.watch_secondary_mode)
        self.watch_priority = QComboBox(); self.watch_priority.addItems(PRIORITIES)
        for combo in (self.watch_kind, self.watch_mode, self.watch_secondary_kind, self.watch_secondary_mode, self.watch_priority):
            self._fit_spotter_combo(combo)
        self.watch_sources = QLineEdit(); self.watch_sources.setPlaceholderText("js8, spotter, commstat, flmsg, flamp, varac, bbs, mesh (blank = all Inbox sources)")
        self.watch_radios = QLineEdit(); self.watch_radios.setPlaceholderText("Radio IDs, comma separated")
        self.watch_expiry_days = QSpinBox(); self.watch_expiry_days.setRange(0, 3650); self.watch_expiry_days.setSuffix(" days (0 = never)")
        self.watch_notes = QLineEdit(); self.watch_notes.setPlaceholderText("Optional operator note")
        primary = QWidget()
        primary.setObjectName("fioSpotterWatchPrimaryCondition")
        primary_layout = QHBoxLayout(primary)
        primary_layout.setContentsMargins(0, 0, 0, 0)
        primary_layout.setSpacing(5)
        primary_layout.addWidget(self.watch_kind)
        primary_layout.addWidget(self.watch_pattern, 1)
        primary_layout.addWidget(self.watch_mode)
        for label, widget in (("Name", self.watch_name), ("Primary condition", primary), ("Priority", self.watch_priority), ("Sources", self.watch_sources), ("Radios", self.watch_radios), ("Expiry", self.watch_expiry_days), ("Notes", self.watch_notes)):
            form.addRow(label, widget)
        secondary.setObjectName("fioSpotterWatchSecondaryCondition")
        form.addRow("AND (optional)", secondary)
        actions = QWidget(); action_grid = QGridLayout(actions); action_grid.setContentsMargins(0, 4, 0, 0)
        delete = QPushButton("Delete"); self.watch_delete = delete; delete.setAccessibleName("Delete selected Spotter watch"); delete.clicked.connect(self._delete_watch)
        test = QPushButton("Test staged message"); self.watch_test = test; test.setAccessibleName("Test the staged Inbox message against this watch"); test.clicked.connect(self._test_watch)
        action_grid.addWidget(delete, 0, 0)
        action_grid.addWidget(test, 0, 1)
        action_grid.setColumnStretch(3, 1)
        form.addRow(actions)
        self.watch_status = QLabel("Select a watch or add a new one.")
        self.watch_status.setWordWrap(True); self.watch_status.setObjectName("fioSpotterWatchStatus")
        form.addRow(self.watch_status)
        editor_layout.addLayout(form)
        # Keep the action bar and form top-packed.  Without a trailing stretch,
        # Qt may assign the editor's spare height to the first widget, leaving
        # a screenshot-sized gap before the Name field.
        editor_layout.addStretch(1)
        # Preserve the natural width of the editor controls when the table and
        # editor are side by side.  The responsive helper stacks them before
        # this minimum can crowd the page at smaller or large-text widths.
        # Placeholder copy is intentionally descriptive, but it must not make
        # the editor's intrinsic size hint consume half of a wide workspace.
        # Use a font-derived control floor and let fields elide/scroll locally.
        editor_width_floor = max(360, editor.fontMetrics().horizontalAdvance("M" * 28))
        editor.setMinimumWidth(editor_width_floor)
        split.addWidget(editor)
        # Give the table the dominant share of the horizontal workspace while
        # retaining enough editor width for its three-part condition rows.
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        if self.width() <= max(1200, (editor_width_floor * 2) + 40):
            split.setOrientation(Qt.Vertical)
        if split.orientation() == Qt.Vertical:
            split.setSizes([400, 400])
        else:
            split.setSizes([780, 400])
        layout.addWidget(split, 1)

    def refresh_watches(self) -> None:
        if not hasattr(self, "watches_table"):
            return
        selected_before = self._selected_watch()
        selected_id = int(selected_before.get("id") or 0) if selected_before else 0
        try:
            self._watch_rows = list_spotter_watches(limit=_MAX_ROWS)
        except Exception as exc:
            self._watch_rows = []
            self.watch_status.setText(f"Watch service unavailable: {exc}")
        self.watches_table.setUpdatesEnabled(False)
        self.watches_table.blockSignals(True)
        try:
            self.watches_table.clearContents()
            self.watches_table.setRowCount(len(self._watch_rows))
            for i, row in enumerate(self._watch_rows):
                source_text = ", ".join(row.get("source_families") or ()) or "All sources"
                criteria = list(row.get("criteria") or ())
                match_text = " AND ".join(
                    f"{item.get('kind')}: {item.get('pattern')}" for item in criteria
                ) if criteria else f"{row.get('watch_kind')}: {row.get('pattern')}"
                values = (
                    "On" if row.get("enabled") else "Off", row.get("name"),
                    match_text, source_text,
                    row.get("priority"), _when(row.get("expires_ts")),
                    _when(row.get("last_match_ts")), row.get("health"),
                )
                for col, value in enumerate(values):
                    self._put(self.watches_table, i, col, value, data=row if col == 0 else None)
        finally:
            self.watches_table.blockSignals(False)
            self.watches_table.setUpdatesEnabled(True)
        if selected_id:
            for row_index, row in enumerate(self._watch_rows):
                if int(row.get("id") or 0) == selected_id:
                    self.watches_table.selectRow(row_index)
                    self._load_selected_watch()
                    break
        if not self._watch_rows:
            self.watch_status.setText("No watches yet. Add a bounded station watch.")

    def _selected_watch(self) -> dict[str, Any] | None:
        if not hasattr(self, "watches_table") or self.watches_table.currentRow() < 0:
            return None
        item = self.watches_table.item(self.watches_table.currentRow(), 0)
        row = item.data(Qt.UserRole) if item is not None else None
        return dict(row) if isinstance(row, dict) else None

    def _load_selected_watch(self) -> None:
        row = self._selected_watch()
        if not row:
            return
        self.watch_name.setText(_text(row.get("name")))
        criteria = list(row.get("criteria") or ())
        first = criteria[0] if criteria else row
        second = criteria[1] if len(criteria) > 1 else {}
        first_kind = _text(first.get("kind") or first.get("watch_kind") or "keyword")
        first_mode = _text(first.get("match_mode") or "contains")
        self.watch_kind.setCurrentIndex(max(0, self.watch_kind.findText(first_kind)))
        self.watch_pattern.setText(_text(first.get("pattern")))
        self.watch_mode.setCurrentIndex(max(0, self.watch_mode.findText(first_mode)))
        self.watch_secondary_kind.setCurrentIndex(
            max(0, self.watch_secondary_kind.findData(_text(second.get("kind"))))
        )
        self.watch_secondary_pattern.setText(_text(second.get("pattern")))
        self.watch_secondary_mode.setCurrentIndex(
            max(0, self.watch_secondary_mode.findText(_text(second.get("match_mode")) or "contains"))
        )
        self.watch_priority.setCurrentIndex(max(0, self.watch_priority.findText(_text(row.get("priority")))))
        self.watch_sources.setText(", ".join(row.get("source_families") or ()))
        self.watch_radios.setText(", ".join(row.get("source_radio_ids") or ()))
        expires = float(row.get("expires_ts") or 0)
        self.watch_expiry_days.setValue(max(0, int(round((expires - time.time()) / 86400))) if expires else 0)
        self.watch_enabled.setChecked(bool(row.get("enabled")))
        self.watch_notes.setText(_text(row.get("notes")))
        self.watch_status.setText(f"Selected {row.get('name')}. Matched {int(row.get('match_count') or 0)} time(s).")

    def _clear_watch(self) -> None:
        self._watch_preview_candidate = None
        self.watches_table.clearSelection(); self.watch_name.clear(); self.watch_kind.setCurrentText("keyword"); self.watch_pattern.clear(); self.watch_mode.setCurrentIndex(0); self.watch_secondary_kind.setCurrentIndex(0); self.watch_secondary_pattern.clear(); self.watch_secondary_mode.setCurrentIndex(0); self.watch_priority.setCurrentIndex(0); self.watch_sources.clear(); self.watch_radios.clear(); self.watch_expiry_days.setValue(0); self.watch_enabled.setChecked(True); self.watch_notes.clear(); self.watch_status.setText("New watch. Save to add it to the station service.")

    def _watch_values(self, existing: dict[str, Any] | None = None) -> dict[str, Any]:
        days = self.watch_expiry_days.value()
        secondary_kind = _text(self.watch_secondary_kind.currentData())
        criteria = []
        if secondary_kind and _text(self.watch_secondary_pattern.text()):
            criteria = [
                {
                    "kind": self.watch_kind.currentText(),
                    "pattern": self.watch_pattern.text(),
                    "match_mode": self.watch_mode.currentText(),
                },
                {
                    "kind": secondary_kind,
                    "pattern": self.watch_secondary_pattern.text(),
                    "match_mode": self.watch_secondary_mode.currentText(),
                },
            ]
        return {
            "id": int((existing or {}).get("id") or 0), "name": self.watch_name.text(),
            "watch_kind": self.watch_kind.currentText(), "pattern": self.watch_pattern.text(),
            "match_mode": self.watch_mode.currentText(), "priority": self.watch_priority.currentText(),
            "criteria": criteria,
            "source_families": [part.strip().lower() for part in self.watch_sources.text().split(",") if part.strip()],
            "source_radio_ids": _csv(self.watch_radios.text()),
            "expires_ts": time.time() + days * 86400 if days else 0,
            "enabled": self.watch_enabled.isChecked(), "notes": self.watch_notes.text(),
            "import_source": "fio-spotter",
        }

    def _save_watch(self) -> None:
        try:
            saved = save_spotter_watch(self._watch_values(self._selected_watch()))
        except Exception as exc:
            self.watch_status.setText(f"Watch not saved: {exc}")
            return
        self.refresh_watches()
        self.watch_status.setText(f"{'Added' if saved.created else 'Saved'} watch; {'enabled' if saved.enabled else 'disabled'}.")

    def _toggle_watch(self) -> None:
        current = self._selected_watch()
        if not current:
            self.watch_status.setText("Select a watch to enable or disable.")
            return
        self.watch_enabled.setChecked(not bool(current.get("enabled")))
        self._save_watch()

    def _delete_watch(self) -> None:
        current = self._selected_watch()
        if not current:
            self.watch_status.setText("Select a watch to delete.")
            return
        try:
            deleted = delete_spotter_watch(int(current.get("id") or 0))
        except Exception as exc:
            self.watch_status.setText(f"Watch not deleted: {exc}")
            return
        self._clear_watch(); self.refresh_watches()
        self.watch_status.setText("Deleted watch." if deleted else "Watch was already unavailable.")

    def _test_watch(self) -> None:
        current = self._selected_watch()
        if not current:
            current = self._watch_values()
        candidate = getattr(self, "_watch_preview_candidate", None)
        if not isinstance(candidate, dict):
            self.watch_status.setText("Use Add to Watch from Message Inbox to stage a message for testing. Testing never changes match counts.")
            return
        self.watch_status.setText("Test match: matched staged message." if watch_matches(current, candidate) else "Test match: no match for staged message.")

    # Expect ---------------------------------------------------------------
    def _build_expect(self, page: QWidget) -> None:
        layout = self._page_layout(page)
        intro = QLabel(
            "Manage saved E? responses and their access here. Compose builds a response; "
            "CommsPlan Spotter owns whether it may answer."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        intro_row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        self.expect_refresh = refresh
        refresh.setAccessibleName("Refresh Expect rules")
        refresh.clicked.connect(self.refresh_expect)
        update_dates = QPushButton("Update Expect form dates…")
        self.expect_update_dates = update_dates
        update_dates.setAccessibleName("Update dates on saved Expect form responses")
        update_dates.setToolTip(
            "Preview and update the compact date/time code on eligible saved form responses. "
            "This never transmits or enables a rule."
        )
        update_dates.clicked.connect(self._bulk_update_expect_dates)
        intro_row.addWidget(refresh)
        intro_row.addWidget(update_dates)
        intro_row.addStretch(1)
        layout.addLayout(intro_row)
        self.expect_maintenance_state = QLabel()
        self.expect_maintenance_state.setObjectName("fioSpotterExpectMaintenanceState")
        self.expect_maintenance_state.setWordWrap(True)
        layout.addWidget(self.expect_maintenance_state)
        runtime = QWidget()
        runtime_row = QHBoxLayout(runtime)
        runtime_row.setContentsMargins(0, 0, 0, 0)
        runtime_row.setSpacing(6)
        self.expect_runtime_state = QLabel()
        self.expect_runtime_state.setObjectName("fioSpotterExpectRuntimeState")
        self.expect_runtime_state.setAccessibleName("Expect runtime enabled or paused state")
        self.expect_runtime_state.setVisible(False)
        self.expect_runtime_enabled = QCheckBox("Expect service on")
        self.expect_runtime_enabled.setVisible(False)
        self.expect_runtime_enabled.setToolTip(
            "Pause or resume all configured automatic Expect replies without changing any saved response."
        )
        # Retained as internal compatibility controls. The operator-facing
        # workflow has one service state and one per-entry Auto reply state.
        self.expect_runtime_paused = QCheckBox()
        self.dynamic_flamp_enabled = QCheckBox()
        self.expect_runtime_enabled.toggled.connect(self._save_runtime_state)
        self.expect_runtime_paused.toggled.connect(self._save_runtime_state)
        self.dynamic_flamp_enabled.toggled.connect(self._save_dynamic_flamp_state)
        self.expect_runtime_chip = QPushButton("Expect service: Paused")
        self.expect_runtime_chip.setObjectName("fioSpotterExpectRuntimeChip")
        self.expect_runtime_chip.setProperty("statusChip", True)
        self.expect_runtime_chip.setCheckable(True)
        self.expect_runtime_chip.setAccessibleName("Toggle Expect service")
        self.expect_runtime_chip.setToolTip(
            "Pause or resume all configured automatic Expect replies. This does not change saved responses or policies."
        )
        self.expect_runtime_chip.toggled.connect(self._set_expect_runtime_from_chip)
        self.dynamic_flamp_state = QLabel()
        self.dynamic_flamp_state.setObjectName("fioSpotterDynamicFlampQState")
        self.dynamic_flamp_state.setWordWrap(True)
        self.dynamic_flamp_state.setAccessibleName("Dynamic FLAMP Q service status")
        self.dynamic_flamp_state.setVisible(False)
        self.dynamic_flamp_chip = QPushButton("FLAMP Q: Waiting for scan")
        self.dynamic_flamp_chip.setObjectName("fioSpotterDynamicFlampChip")
        self.dynamic_flamp_chip.setProperty("statusChip", True)
        self.dynamic_flamp_chip.setAccessibleName("FLAMP Q status")
        self.dynamic_flamp_chip.setToolTip("Waiting for the first background FLAMP source scan.")
        self.dynamic_flamp_chip.clicked.connect(self._show_flamp_q_detail)
        runtime_row.addWidget(self.expect_runtime_chip)
        runtime_row.addWidget(self.dynamic_flamp_chip)
        runtime_row.addStretch(1)
        layout.addWidget(runtime)
        split = QSplitter(Qt.Vertical)
        self.expect_editor_split = split
        table_panel = QWidget(); table_layout = QVBoxLayout(table_panel); table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(QLabel("Saved responses"))
        filters = QHBoxLayout()
        self.expect_filter = QLineEdit()
        self.expect_filter.setPlaceholderText("Filter by E? token or response")
        self.expect_state_filter = QComboBox()
        self.expect_state_filter.addItems(("All", "Auto reply on", "Saved only", "Needs attention"))
        self.expect_filter.textChanged.connect(self._render_expect_entries)
        self.expect_state_filter.currentIndexChanged.connect(self._render_expect_entries)
        filters.addWidget(self.expect_filter, 1)
        filters.addWidget(self.expect_state_filter)
        table_layout.addLayout(filters)
        self.expect_entries_table = self._table(
            ["Select", "E? Token", "Response", "Access", "State", "Updated", "Action"],
            name="fioSpotterExpectEntriesTable",
        )
        self.expect_entries_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.expect_entries_table.itemSelectionChanged.connect(self._load_selected_entry)
        self.expect_entries_table.cellClicked.connect(self._expect_table_cell_clicked)
        table_layout.addWidget(self.expect_entries_table, 1)
        bulk = QHBoxLayout()
        select_shown = QPushButton("Select shown (max 100)")
        self.expect_select_shown = select_shown
        select_shown.clicked.connect(self._select_shown_expect_entries)
        enable_selected = QPushButton("Auto reply on")
        self.expect_bulk_auto_reply = enable_selected
        enable_selected.clicked.connect(lambda: self._bulk_set_expect_auto_reply(True))
        save_selected = QPushButton("Set saved only")
        self.expect_bulk_saved_only = save_selected
        save_selected.clicked.connect(lambda: self._bulk_set_expect_auto_reply(False))
        bulk.addWidget(select_shown)
        bulk.addWidget(enable_selected)
        bulk.addWidget(save_selected)
        bulk.addStretch(1)
        table_layout.addLayout(bulk)

        editor_panel = QGroupBox("Saved response")
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(8, 8, 8, 8)
        editor_layout.setSpacing(6)
        form = QWidget()
        self.expect_editor_columns = QVBoxLayout(form)
        self.expect_editor_columns.setContentsMargins(0, 0, 0, 0)
        self.expect_editor_columns.setSpacing(6)
        metadata = QWidget(form)
        self.expect_editor_meta = QGridLayout(metadata)
        self.expect_editor_meta.setContentsMargins(0, 0, 0, 0)
        self.expect_editor_meta.setHorizontalSpacing(12)
        self.expect_editor_meta.setVerticalSpacing(4)
        self.expect_key = QLineEdit(); self.expect_key.setPlaceholderText("E? token, e.g. INFO or Q")
        # A one-line reply input makes a saved response unnecessarily hard to
        # read and edit.  The compact, wrapping text surface remains bounded
        # to a few lines so the saved-response table still owns the rest of
        # the workspace.
        self.expect_reply = QTextEdit()
        self.expect_reply.setAcceptRichText(False)
        self.expect_reply.setPlaceholderText("Reply text")
        self.expect_reply.setAccessibleName("Expect reply text")
        self.expect_reply.setLineWrapMode(QTextEdit.WidgetWidth)
        reply_height = max(
            control_height_for_font(self.expect_reply, vertical_padding=10, floor=28),
            (self.expect_reply.fontMetrics().lineSpacing() * 3) + 16,
        )
        self.expect_reply.setMinimumHeight(reply_height)
        self.expect_reply.setMaximumHeight(reply_height + self.expect_reply.fontMetrics().lineSpacing())
        self.expect_reply.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.expect_policy = QComboBox(); self.expect_policy.addItem("Choose an access policy…", 0)
        self.expect_policy.setToolTip("Select the named policy that controls who may receive automatic replies.")
        self.expect_policy.currentIndexChanged.connect(self._update_expect_policy_presentation)
        self.expect_policy_summary = QPushButton("Policy: required for Auto reply")
        self.expect_policy_summary.setAccessibleName("Assigned access policy summary")
        self.expect_policy_summary.setToolTip("Automatic replies require a named, enabled access policy. Saved-only responses do not.")
        self.expect_policy_summary.clicked.connect(
            lambda: self.tabs.setCurrentIndex(_TAB_INDEX["Access Policies"])
        )
        self.expect_access_catalog_state = QLabel()
        self.expect_access_catalog_state.setVisible(False)
        self.expect_access_catalog_state.setObjectName("fioSpotterExpectAccessCatalogState")
        self.expect_calls = _TokenListEditor(); self.expect_calls.setPlaceholderText("Callsign or *")
        self.expect_groups = _TokenListEditor(token_prefix="@"); self.expect_groups.setPlaceholderText("Find or enter a query group")
        self.expect_blocked = _TokenListEditor(); self.expect_blocked.setPlaceholderText("Find or enter a blocked callsign")
        self.expect_trusted = QCheckBox("Allow all trusted operators")
        self.expect_trusted_groups = _TokenListEditor(); self.expect_trusted_groups.setPlaceholderText("Find an operator group")
        self.expect_radio = QComboBox()
        self.expect_radio.setAccessibleName("Radios that accept this Expect query")
        self.expect_radio.setToolTip(
            "All JS8 radios is recommended. CommsPlan replies through the radio and JS8Call service that received the query."
        )
        self.expect_radio.addItem("All JS8 radios — reply on receiving radio", "")
        self._fit_spotter_combo(self.expect_radio, minimum_characters=20)
        self.expect_max = QSpinBox(); self.expect_max.setRange(1, 99); self.expect_max.setValue(1)
        self.expect_cooldown = QSpinBox(); self.expect_cooldown.setRange(0, 86400); self.expect_cooldown.setSuffix(" sec")
        self.expect_radio.currentIndexChanged.connect(self._update_expect_options_summary)
        self.expect_max.valueChanged.connect(self._update_expect_options_summary)
        self.expect_cooldown.valueChanged.connect(self._update_expect_options_summary)
        self.expect_allow_any = QCheckBox("Allow all callers (*) — use cautiously")
        self.expect_allow_any.toggled.connect(self._sync_allow_any_callers)
        self.expect_enabled = QCheckBox()
        self.expect_auto = QCheckBox()
        self.expect_auto.setVisible(False)
        self.expect_unattended = QCheckBox()
        self.expect_unattended.setVisible(False)
        self.expect_delivery_mode = QComboBox()
        self.expect_delivery_mode.addItem("Saved only", False)
        self.expect_delivery_mode.addItem("Auto reply on", True)
        self.expect_delivery_mode.setAccessibleName("Expect response delivery mode")
        self.expect_delivery_mode.setToolTip("Auto reply requires a named, enabled access policy. Saved only remains available for review and manual sending.")
        self.expect_delivery_mode.currentIndexChanged.connect(self._sync_expect_delivery_mode)

        def metadata_group(label_text: str, widget: QWidget | None = None) -> QWidget:
            group = QWidget(metadata)
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            if label_text:
                label = QLabel(label_text, group)
                label.setObjectName("fioSpotterExpectFieldLabel")
                if widget is not None:
                    label.setBuddy(widget)
                row.addWidget(label)
            if widget is not None:
                row.addWidget(widget, 1)
            group.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed)
            return group

        # Keep the four response-identity decisions in one true scan row when
        # the workspace permits. The policy summary is already self-labelling,
        # so it remains a concise semantic chip rather than repeating prose.
        self.expect_editor_meta_groups = (
            metadata_group("E? Token", self.expect_key),
            metadata_group("Access policy", self.expect_policy),
            metadata_group("", self.expect_policy_summary),
            metadata_group("Mode", self.expect_delivery_mode),
        )
        # Qt is willing to reduce an editable/combo control to only a few
        # characters. Give the four scan decisions a font-derived working
        # width, which in turn makes the responsive breakpoint reflect what a
        # person can actually read rather than the smallest legal widget.
        for widget, sample in (
            (self.expect_key, "E? token, e.g. INFO"),
            (self.expect_policy, "Choose an access policy…"),
            (self.expect_policy_summary, "Policy: required for Auto reply"),
            (self.expect_delivery_mode, "Auto reply on"),
        ):
            widget.setMinimumWidth(
                widget.fontMetrics().horizontalAdvance(sample)
                + control_height_for_font(widget, vertical_padding=8, floor=24)
            )
        self._expect_meta_layout_mode = ""
        self.expect_editor_columns.addWidget(metadata)
        self.expect_reply_label = QLabel("Reply")
        self.expect_editor_columns.addWidget(self.expect_reply_label)
        self.expect_editor_columns.addWidget(self.expect_reply)
        editor_layout.addWidget(form)
        self.expect_options = QGroupBox("Options — all JS8 radios · 1 reply · no cooldown")
        self.expect_options.setCheckable(True)
        self.expect_options.setChecked(False)
        self.expect_options.setAccessibleName("Expect response options")
        options_group_layout = QVBoxLayout(self.expect_options)
        self.expect_options_body = QWidget(self.expect_options)
        options_group_layout.addWidget(self.expect_options_body)
        options_layout = QFormLayout(self.expect_options_body)
        options_layout.setContentsMargins(8, 8, 8, 8)
        options_layout.setSpacing(4)
        options_layout.addRow("Radios", self.expect_radio)
        options_layout.addRow("Max replies", self.expect_max)
        options_layout.addRow("Cooldown", self.expect_cooldown)
        self.expect_options.toggled.connect(self._toggle_expect_options)
        self._toggle_expect_options(False)
        editor_layout.addWidget(self.expect_options)
        self.expect_legacy_access = QGroupBox("Legacy inline access (compatibility)")
        self.expect_legacy_access.setCheckable(True)
        self.expect_legacy_access.setChecked(False)
        self.expect_legacy_access.setAccessibleName("Legacy inline access compatibility controls")
        legacy_group_layout = QVBoxLayout(self.expect_legacy_access)
        self.expect_legacy_access_body = QWidget(self.expect_legacy_access)
        legacy_group_layout.addWidget(self.expect_legacy_access_body)
        legacy_layout = QFormLayout(self.expect_legacy_access_body)
        legacy_layout.setContentsMargins(8, 8, 8, 8)
        legacy_layout.setSpacing(4)
        legacy_layout.addRow("Allowed callers", self.expect_calls)
        legacy_layout.addRow(self.expect_allow_any)
        legacy_layout.addRow("Query groups", self.expect_groups)
        legacy_layout.addRow(self.expect_trusted)
        legacy_layout.addRow("Trusted operators from groups", self.expect_trusted_groups)
        legacy_layout.addRow("Blocked callers", self.expect_blocked)
        self.expect_legacy_convert = QPushButton("Create policy from this access…")
        self.expect_legacy_convert.setToolTip(
            "Copy these preserved inline conditions into a new Access Policy draft. The response is not reassigned until you save and select that policy."
        )
        self.expect_legacy_convert.clicked.connect(self._draft_policy_from_legacy_access)
        legacy_layout.addRow(self.expect_legacy_convert)
        self.expect_legacy_access.toggled.connect(self._toggle_expect_legacy_access)
        self._toggle_expect_legacy_access(False)
        self.expect_legacy_access.setVisible(False)
        editor_layout.addWidget(self.expect_legacy_access)
        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(10)
        actions.setVerticalSpacing(6)
        self.expect_editor_actions = actions
        save = QPushButton("Save response"); self.expect_save = save; save.clicked.connect(self._save_entry)
        view = QPushButton("View"); self.expect_view = view; view.clicked.connect(self._view_selected_expect)
        send_now = QPushButton("Send now…"); self.expect_send_now = send_now; send_now.clicked.connect(self._send_selected_expect_now)
        remove = QPushButton("Delete"); self.expect_delete = remove; remove.clicked.connect(self._delete_entry)
        new = QPushButton("New response…"); self.expect_new = new
        # Expect is the workflow owner, so make the three valid starts explicit
        # at the point where operators are already creating an entry.  An MCF
        # form is handed to the one Compose implementation; text stays local
        # to this editor and Q remains the dedicated dynamic-rule path.
        new_menu = QMenu(new)
        new_menu.addAction("From an MCF form", self._new_expect_from_mcf_form)
        new_menu.addAction("Text response", self._clear_entry)
        new_menu.addAction("FLAMP Q rule", self._new_dynamic_q_entry)
        new.setMenu(new_menu)
        manage_policies = QPushButton("Access policies…"); self.expect_manage_policies = manage_policies
        manage_policies.clicked.connect(
            lambda: self.tabs.setCurrentIndex(_TAB_INDEX["Access Policies"])
        )
        self.expect_editor_action_buttons = (
            save, view, send_now, new, manage_policies, remove,
        )
        for index, button in enumerate(self.expect_editor_action_buttons):
            actions.addWidget(button, index // 2, index % 2)
        actions.setColumnStretch(2, 1)
        editor_layout.addLayout(actions)
        for token_editor in (
            self.expect_calls,
            self.expect_groups,
            self.expect_trusted_groups,
            self.expect_blocked,
        ):
            token_editor.valuesChanged.connect(self._update_expect_editor_minimum_height)
        split.addWidget(editor_panel)
        split.addWidget(table_panel)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        histories = QSplitter(Qt.Horizontal)
        self.expect_history_split = histories
        self.expect_requests_table = self._table(["When", "Decision", "Key", "Caller", "Reason"], name="fioSpotterExpectRequestsTable")
        self.expect_replies_table = self._table(["When", "Decision", "Reply radio", "Response"], name="fioSpotterExpectRepliesTable")
        histories.addWidget(self.expect_requests_table); histories.addWidget(self.expect_replies_table)
        histories.setMaximumHeight(190)
        histories.setVisible(False)
        show_history = QPushButton("Show request history")
        self.expect_history = show_history
        show_history.setCheckable(True)
        show_history.setAccessibleName("Show or hide Expect request and reply history")
        show_history.toggled.connect(histories.setVisible)
        show_history.toggled.connect(
            lambda checked: show_history.setText(
                "Hide request history" if checked else "Show request history"
            )
        )
        layout.addWidget(show_history)
        layout.addWidget(histories)
        self._load_expect_access_completions()
        self._apply_expect_responsive_layout()

    def _update_expect_editor_minimum_height(self) -> None:
        if not hasattr(self, "expect_editor_split") or self.expect_editor_split.count() < 2:
            return
        editor = self.expect_editor_split.widget(0)
        editor.setMinimumHeight(0)
        if editor.layout() is not None:
            editor.layout().activate()
        editor.setMinimumHeight(editor.minimumSizeHint().height())
        editor.updateGeometry()
        table_panel = self.expect_editor_split.widget(1)
        table_minimum = table_panel.minimumSizeHint().height()
        table_panel.setMinimumHeight(table_minimum)
        self.expect_editor_split.setMinimumHeight(
            editor.minimumHeight() + table_minimum + self.expect_editor_split.handleWidth()
        )
        self.expect_editor_split.setSizes([editor.minimumHeight(), editor.minimumHeight() * 2])
        self.expect_editor_split.updateGeometry()

    def _set_expect_runtime_from_chip(self, enabled: bool) -> None:
        """Keep the compact action chip and retained runtime control in sync."""
        self.expect_runtime_enabled.setChecked(bool(enabled))
        self.expect_maintenance_state.setText(
            "Expect service resumed. Eligible saved responses may now reply automatically."
            if enabled else
            "Expect service paused. Saved responses and policies are unchanged."
        )

    def _show_flamp_q_detail(self) -> None:
        """Reveal the cached FLAMP-Q explanation without performing new I/O."""
        self.expect_maintenance_state.setText(self.dynamic_flamp_state.text())

    def _draft_policy_from_legacy_access(self) -> None:
        """Copy legacy inline access into a reviewable, unsaved policy draft."""
        selected = self.expect_entries_table.selectedItems()
        row = selected[0].data(Qt.UserRole) if selected else None
        if not isinstance(row, dict) or not self._has_legacy_inline_access(row):
            self.expect_maintenance_state.setText(
                "Select a response with preserved legacy inline access first."
            )
            return
        # Access Policies is lazy-built. Activate it before addressing its
        # editor widgets; this transition is explicit and performs the normal
        # bounded tab refresh once.
        self.tabs.setCurrentIndex(_TAB_INDEX["Access Policies"])
        self._clear_policy_editor()
        base_name = f"{_text(row.get('expect_key')).upper() or 'Expect'} access"
        existing_names = {
            _text(policy.get("name")).casefold() for policy in getattr(self, "_policy_rows", ())
        }
        name = base_name
        suffix = 2
        while name.casefold() in existing_names:
            name = f"{base_name} {suffix}"
            suffix += 1
        self.policy_name.setText(name)
        self.policy_calls.setText(", ".join(row.get("allowed_callsigns") or ()))
        self.policy_groups.setText(", ".join(row.get("allowed_groups") or ()))
        self.policy_trusted.setChecked(bool(row.get("allow_trusted_operators")))
        self.policy_trusted_groups.setText(", ".join(row.get("trusted_operator_groups") or ()))
        self.policy_blocked.setText(", ".join(row.get("blocked_callsigns") or ()))
        radio_id = _text(row.get("source_radio_id"))
        if _text(row.get("source_scope")).lower() == "radio" and radio_id:
            self.policy_scope.setCurrentIndex(max(0, self.policy_scope.findData("radio")))
            self.policy_radios.setText(self._policy_radio_labels([radio_id]))
        else:
            self.policy_scope.setCurrentIndex(max(0, self.policy_scope.findData("all")))
        self.policy_enabled.setChecked(True)
        self.policy_status.setText(
            "Review and save this new policy. Then return to Expect and assign it to the response; no access has changed yet."
        )

    def _toggle_expect_options(self, expanded: bool) -> None:
        self.expect_options_body.setVisible(bool(expanded))
        self._update_expect_options_summary()
        self._update_expect_editor_minimum_height()

    def _toggle_expect_legacy_access(self, expanded: bool) -> None:
        self.expect_legacy_access_body.setVisible(bool(expanded))
        self._update_expect_editor_minimum_height()

    def _update_expect_options_summary(self, *_args) -> None:
        if not hasattr(self, "expect_options"):
            return
        radio = _text(self.expect_radio.currentText()) or "All JS8 radios"
        if radio.startswith("All JS8 radios"):
            radio = "all JS8 radios"
        replies = self.expect_max.value()
        cooldown = self.expect_cooldown.value()
        cooldown_text = "no cooldown" if cooldown == 0 else f"{cooldown} sec cooldown"
        self.expect_options.setTitle(
            f"Options — {radio} · {replies} repl{'y' if replies == 1 else 'ies'} · {cooldown_text}"
        )

    @staticmethod
    def _has_legacy_inline_access(row: dict[str, Any]) -> bool:
        return bool(
            row.get("allow_any") or row.get("allow_trusted_operators")
            or row.get("allowed_callsigns") or row.get("allowed_groups")
            or row.get("trusted_operator_groups") or row.get("blocked_callsigns")
        )

    def _set_expect_legacy_access_for_row(self, row: dict[str, Any] | None) -> None:
        has_legacy = bool(row and self._has_legacy_inline_access(row))
        self.expect_legacy_access.setVisible(has_legacy)
        if not has_legacy:
            self.expect_legacy_access.blockSignals(True)
            self.expect_legacy_access.setChecked(False)
            self.expect_legacy_access.blockSignals(False)
        if has_legacy:
            count = sum((
                len(row.get("allowed_callsigns") or []), len(row.get("allowed_groups") or []),
                len(row.get("trusted_operator_groups") or []), len(row.get("blocked_callsigns") or []),
                int(bool(row.get("allow_any"))), int(bool(row.get("allow_trusted_operators"))),
            ))
            self.expect_legacy_access.setTitle(
                f"Legacy inline access ({count} condition{'s' if count != 1 else ''}; preserved for compatibility)"
            )
        self._toggle_expect_legacy_access(self.expect_legacy_access.isChecked() and has_legacy)

    def _selected_expect_policy_row(self) -> dict[str, Any] | None:
        policy_id = int(self.expect_policy.currentData() or 0)
        return next(
            (row for row in getattr(self, "_policy_rows", ()) if int(row.get("id") or 0) == policy_id),
            None,
        )

    def _update_expect_policy_presentation(self, *_args) -> None:
        if not hasattr(self, "expect_policy_summary"):
            return
        policy = self._selected_expect_policy_row()
        if policy is None:
            text = "Policy: required for Auto reply"
            tip = "Choose or create a named, enabled access policy before enabling Auto reply."
        elif policy.get("enabled"):
            text = f"Policy: {policy.get('name') or 'Unnamed'}"
            tip = "This named policy controls automatic-reply access. Click Access policies to review or change it."
        else:
            text = f"Policy: {policy.get('name') or 'Unnamed'} (disabled)"
            tip = "This policy is disabled. The response can be saved, but Auto reply cannot be enabled."
        self.expect_policy_summary.setText(text)
        self.expect_policy_summary.setToolTip(tip)
        self._sync_expect_delivery_mode()

    def _sync_expect_delivery_mode(self, *_args) -> None:
        """Present one delivery choice while retaining legacy flags for the store."""
        if not hasattr(self, "expect_delivery_mode"):
            return
        auto_reply = bool(self.expect_delivery_mode.currentData())
        self.expect_auto.blockSignals(True)
        self.expect_unattended.blockSignals(True)
        self.expect_auto.setChecked(auto_reply)
        self.expect_unattended.setChecked(auto_reply)
        self.expect_auto.blockSignals(False)
        self.expect_unattended.blockSignals(False)
        policy = self._selected_expect_policy_row()
        invalid = auto_reply and (policy is None or not bool(policy.get("enabled")))
        self.expect_delivery_mode.setToolTip(
            "Auto reply requires a named, enabled access policy. Choose Saved only to retain this response for review and manual sending."
            if invalid else "Choose whether this response is saved only or may reply automatically."
        )

    def _save_runtime_state(self) -> None:
        if not hasattr(self, "expect_runtime_enabled"):
            return
        try:
            set_expect_automation_runtime_state(
                self.settings,
                enabled=self.expect_runtime_enabled.isChecked(),
                paused=False,
            )
        except Exception:
            pass
        self._refresh_runtime_state()

    def _sync_allow_any_callers(self, checked: bool) -> None:
        if not hasattr(self, "expect_calls"):
            return
        values = _csv(self.expect_calls.text())
        values = [value for value in values if value != "*"]
        if checked:
            values.insert(0, "*")
        self.expect_calls.setText(", ".join(values))

    def _refresh_runtime_state(self) -> None:
        state = load_expect_automation_runtime_state(self.settings)
        self.expect_runtime_enabled.blockSignals(True); self.expect_runtime_paused.blockSignals(True); self.dynamic_flamp_enabled.blockSignals(True); self.expect_runtime_chip.blockSignals(True)
        self.expect_runtime_enabled.setChecked(state.active); self.expect_runtime_paused.setChecked(False)
        self.dynamic_flamp_enabled.setChecked(bool(self.settings.get("js8_expect_dynamic_flamp_enabled", False)))
        self.expect_runtime_chip.setChecked(state.active)
        self.expect_runtime_enabled.blockSignals(False); self.expect_runtime_paused.blockSignals(False); self.dynamic_flamp_enabled.blockSignals(False); self.expect_runtime_chip.blockSignals(False)
        self.expect_runtime_state.setText(
            ("● Expect service on" if state.active else "○ Expect service paused")
            + " — " + state.reason
        )
        self.expect_runtime_chip.setText("Expect service: On" if state.active else "Expect service: Paused")
        self.expect_runtime_chip.setAccessibleDescription(self.expect_runtime_state.text())
        try:
            statuses = list_flamp_transfer_index_statuses(
                db_path=Path(self.settings.config_dir) / "freqinout_nets.db",
                limit=25,
            )
        except Exception:
            statuses = []
        ready = [row for row in statuses if row.get("scan_success")]
        files = sum(int(row.get("file_count") or 0) for row in ready)
        if ready:
            flamp_status = "ready"
            newest = max(float(row.get("scanned_ts") or 0.0) for row in ready)
            scan_text = f"{len(ready)} source(s), {files} indexed transfer(s), last scan {_when(newest)}"
        elif statuses:
            flamp_status = "attention"
            scan_text = "index needs attention — " + (_text(statuses[0].get("error_text")) or "no successful source scan")
        else:
            flamp_status = "waiting"
            scan_text = "waiting for the first background FLAMP source scan"
        q_rules = [row for row in self._entry_rows if _text(row.get("expect_key")).upper() == "Q"]
        enabled_q = [row for row in q_rules if row.get("enabled") and row.get("auto_reply_enabled") and row.get("unattended_auto_reply_enabled")]
        self.dynamic_flamp_state.setText(
            f"{'●' if self.dynamic_flamp_enabled.isChecked() and enabled_q else '○'} FLAMP Q — "
            f"{len(enabled_q)} approved Q rule(s); {scan_text}. Replies use the receiving JS8 source."
        )
        chip_text = {
            "ready": "FLAMP Q: Ready",
            "attention": "FLAMP Q: Needs attention",
            "waiting": "FLAMP Q: Waiting for scan",
        }[flamp_status]
        self.dynamic_flamp_chip.setText(chip_text)
        self.dynamic_flamp_chip.setProperty("expectStatus", flamp_status)
        self.dynamic_flamp_chip.setToolTip(self.dynamic_flamp_state.text())
        self.dynamic_flamp_chip.setAccessibleDescription(self.dynamic_flamp_state.text())
        self._style_expect_status_chips()

    def _save_dynamic_flamp_state(self) -> None:
        enabled = self.dynamic_flamp_enabled.isChecked()
        try:
            self.settings.set("js8_expect_dynamic_flamp_enabled", enabled)
            if hasattr(self.settings, "save"):
                self.settings.save()
        except Exception:
            pass
        if enabled:
            try:
                controller = getattr(self.window(), "background_ingest", None)
                if controller is not None and hasattr(controller, "request_refresh"):
                    controller.request_refresh("dynamic_flamp")
            except Exception:
                pass
        self._refresh_runtime_state()

    def refresh_expect(self) -> None:
        if not hasattr(self, "expect_entries_table"):
            return
        selected_entry_id = self._selected_expect_entry_id()
        self._refresh_expect_radio_choices()
        self._load_expect_access_completions()
        try:
            self._policy_rows = list_expect_allow_policies()
            self._entry_rows = list_expect_entries(limit=_MAX_ROWS)
            self._cache_policy_usage_entries()
        except Exception as exc:
            log.warning("CommsPlan Spotter: Expect administration read failed: %s", exc, exc_info=True)
            self._policy_rows, self._entry_rows = [], []
            self._policy_usage_entries = {}
            self.expect_maintenance_state.setText(
                "Expect storage could not be read. Restart CommsPlan to run startup database repair; details are in the log."
            )
        self._refresh_runtime_state()
        selected = self.expect_policy.currentData()
        self.expect_policy.blockSignals(True); self.expect_policy.clear(); self.expect_policy.addItem("Choose an access policy…", 0)
        for row in self._policy_rows:
            label = f"{row.get('name') or 'Unnamed policy'}"
            if not row.get("enabled"):
                label += " (disabled)"
            self.expect_policy.addItem(label, int(row.get('id') or 0))
        self.expect_policy.setCurrentIndex(max(0, self.expect_policy.findData(selected)))
        self.expect_policy.blockSignals(False)
        self._fit_spotter_combo(self.expect_policy, minimum_characters=20)
        self._update_expect_policy_presentation()
        self._render_expect_entries()
        self._refresh_expect_history()
        if self._pending_expect_entry_id or self._pending_expect_key:
            self._select_pending_expect_entry()
        elif selected_entry_id:
            self._select_expect_entry(entry_id=selected_entry_id)

    def _filtered_expect_rows(self) -> list[dict[str, Any]]:
        query = _text(self.expect_filter.text()).casefold() if hasattr(self, "expect_filter") else ""
        state = self.expect_state_filter.currentText() if hasattr(self, "expect_state_filter") else "All"
        rows: list[dict[str, Any]] = []
        for row in self._entry_rows:
            if query and query not in (
                f"{row.get('expect_key', '')} {row.get('response_text', '')} "
                f"{row.get('allow_policy_name', '')}"
            ).casefold():
                continue
            visible_state = self._expect_display_state(row)
            if state != "All" and visible_state != state:
                continue
            rows.append(row)
        return rows

    def _render_expect_entries(self, *_args) -> None:
        if not hasattr(self, "expect_entries_table"):
            return
        table = self.expect_entries_table
        rows = self._filtered_expect_rows()
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.clearContents()
            table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                select_item = QTableWidgetItem("")
                select_item.setFlags(select_item.flags() | Qt.ItemIsUserCheckable)
                select_item.setCheckState(Qt.Unchecked)
                select_item.setData(Qt.UserRole, row)
                table.setItem(i, 0, select_item)
                values = (
                    row.get("expect_key"), row.get("response_text"),
                    self._expect_access_summary(row), self._expect_display_state(row),
                    _when(row.get("updated_ts")),
                    "View" if _text(row.get("expect_key")).upper() != "Q" else "Rule",
                )
                for col, value in enumerate(values, start=1):
                    self._put(table, i, col, value)
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)

    @staticmethod
    def _expect_display_state(row: dict[str, Any]) -> str:
        if row.get("auto_reply_state") == "auto-reply-on":
            return "Auto reply on"
        if row.get("auto_reply_enabled") or row.get("unattended_auto_reply_enabled"):
            return "Needs attention"
        return "Saved only"

    def _expect_table_cell_clicked(self, row_index: int, column: int) -> None:
        if column != 6:
            return
        item = self.expect_entries_table.item(row_index, 0)
        row = item.data(Qt.UserRole) if item else None
        if isinstance(row, dict) and _text(row.get("expect_key")).upper() != "Q":
            self.expect_entries_table.selectRow(row_index)
            self._view_selected_expect()

    def _checked_expect_entry_ids(self) -> list[int]:
        ids: list[int] = []
        for row_index in range(self.expect_entries_table.rowCount()):
            item = self.expect_entries_table.item(row_index, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            row = item.data(Qt.UserRole)
            if isinstance(row, dict) and int(row.get("id") or 0) > 0:
                ids.append(int(row.get("id") or 0))
        if ids:
            return ids
        return sorted({
            int((self.expect_entries_table.item(index.row(), 0).data(Qt.UserRole) or {}).get("id") or 0)
            for index in self.expect_entries_table.selectionModel().selectedRows()
            if self.expect_entries_table.item(index.row(), 0) is not None
        } - {0})

    def _select_shown_expect_entries(self) -> None:
        for row_index in range(self.expect_entries_table.rowCount()):
            item = self.expect_entries_table.item(row_index, 0)
            if item is not None:
                item.setCheckState(Qt.Checked if row_index < 100 else Qt.Unchecked)
        if self.expect_entries_table.rowCount() > 100:
            self.expect_maintenance_state.setText(
                "Selected the first 100 shown responses. Refine the filter to safely manage another batch."
            )

    def _bulk_set_expect_auto_reply(self, enabled: bool) -> None:
        ids = self._checked_expect_entry_ids()
        if not ids:
            self.expect_maintenance_state.setText("Select one or more saved responses first.")
            return
        try:
            result = bulk_set_expect_entry_auto_reply_state(
                ids,
                enabled,
                require_named_policy=bool(enabled),
            )
        except Exception as exc:
            self.expect_maintenance_state.setText(f"No changes were applied: {exc}")
            return
        self.refresh_expect()
        label = "Auto reply on" if enabled else "Saved only"
        self.expect_maintenance_state.setText(
            f"{label}: updated {result.updated_count}; skipped {result.skipped_count}."
        )

    def _selected_expect_entry_id(self) -> int:
        if not hasattr(self, "expect_entries_table"):
            return 0
        selected = self.expect_entries_table.selectedItems()
        if not selected:
            return 0
        row = selected[0].data(Qt.UserRole)
        try:
            return int((row or {}).get("id") or 0) if isinstance(row, dict) else 0
        except (TypeError, ValueError):
            return 0

    def _select_expect_entry(self, *, entry_id: int = 0, expect_key: str = "") -> bool:
        wanted_key = _text(expect_key).upper()
        for row_index in range(self.expect_entries_table.rowCount()):
            item = self.expect_entries_table.item(row_index, 0)
            row = item.data(Qt.UserRole) if item is not None else None
            if not isinstance(row, dict):
                continue
            if (entry_id and int(row.get("id") or 0) == entry_id) or (
                wanted_key and _text(row.get("expect_key")).upper() == wanted_key
            ):
                self.expect_entries_table.selectRow(row_index)
                self.expect_entries_table.scrollToItem(item)
                return True
        return False

    def _select_pending_expect_entry(self) -> None:
        if not hasattr(self, "expect_entries_table"):
            return
        found = self._select_expect_entry(
            entry_id=self._pending_expect_entry_id,
            expect_key=self._pending_expect_key,
        )
        if not found and (self._pending_expect_entry_id or self._pending_expect_key):
            self.expect_maintenance_state.setText(
                "The requested Expect rule is not in the current bounded list. Use Refresh to read the store again."
            )
        self._pending_expect_entry_id = 0
        self._pending_expect_key = ""

    def _bulk_update_expect_dates(self) -> None:
        """Preview, confirm, then atomically refresh eligible saved form dates."""
        try:
            stored_rows = list_expect_entries(limit=5000)
        except Exception as exc:
            log.warning("CommsPlan Spotter: Expect date preview failed: %s", exc, exc_info=True)
            self.expect_maintenance_state.setText(f"○ Saved form dates could not be reviewed: {exc}")
            return
        eligible = [
            row for row in stored_rows
            if update_mcform_response_datecode(
                row.get("response_text"), row.get("expect_key"), "#0000"
            ) is not None
        ]
        if not eligible:
            self.expect_maintenance_state.setText(
                "No eligible saved form responses were found. Dynamic Q and non-form rules are unchanged."
            )
            return
        names = ", ".join(_text(row.get("expect_key")) for row in eligible[:8])
        if len(eligible) > 8:
            names += f", and {len(eligible) - 8} more"
        answer = QMessageBox.question(
            self,
            "Update saved form dates",
            f"Update the date/time code on {len(eligible)} saved form response(s) to now?\n\n"
            f"{names}\n\nThis does not transmit, enable rules, or change access policy.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self.expect_maintenance_state.setText("No form dates were changed.")
            return
        selected_entry_id = self._selected_expect_entry_id()
        try:
            result = bulk_refresh_expect_datecodes(
                entry_ids=[int(row.get("id") or 0) for row in eligible]
            )
        except Exception as exc:
            log.warning("CommsPlan Spotter: bulk Expect date refresh failed: %s", exc, exc_info=True)
            self.expect_maintenance_state.setText(f"○ Form dates were not updated: {exc}")
            return
        self.refresh_expect()
        if selected_entry_id:
            self._select_expect_entry(entry_id=selected_entry_id)
        self.expect_maintenance_state.setText(
            f"● Updated {result.updated_count} saved form date(s). "
            f"Skipped {result.skipped_count}; no message was transmitted."
        )

    @staticmethod
    def _expect_access_summary(row: dict[str, Any]) -> str:
        allowed = list(row.get("allowed_callsigns") or [])
        parts: list[str] = []
        if row.get("allow_any") or "*" in allowed:
            parts.append("Any caller")
        else:
            explicit = len([value for value in allowed if value != "*"])
            if explicit:
                parts.append(f"{explicit} caller{'s' if explicit != 1 else ''}")
            if row.get("allow_trusted_operators"):
                parts.append("Trusted")
            trusted_groups = len(row.get("trusted_operator_groups") or [])
            if trusted_groups:
                parts.append(f"Trusted via {trusted_groups} group{'s' if trusted_groups != 1 else ''}")
        addressed = len(row.get("allowed_groups") or [])
        if addressed:
            parts.append(f"{addressed} query group{'s' if addressed != 1 else ''}")
        policy = _text(row.get("allow_policy_name"))
        if policy:
            parts.append(policy)
        return " · ".join(parts) or "No callers"

    def _refresh_expect_history(self) -> None:
        try:
            runtime_rows = list_expect_runtime_audit(limit=_MAX_ROWS)
            dispatch_rows = list_expect_dispatch_audit(limit=_MAX_ROWS)
        except Exception:
            runtime_rows, dispatch_rows = [], []
        for table in (self.expect_requests_table, self.expect_replies_table):
            table.setUpdatesEnabled(False)
            table.blockSignals(True)
            table.clearContents()
        try:
            self.expect_requests_table.setRowCount(min(_MAX_ROWS, len(runtime_rows)))
            for i, row in enumerate(runtime_rows[:_MAX_ROWS]):
                for col, value in enumerate((_when(row.get("created_ts")), row.get("decision"), row.get("expect_key"), row.get("requesting_callsign"), row.get("reason"))): self._put(self.expect_requests_table, i, col, value)
            self.expect_replies_table.setRowCount(min(_MAX_ROWS, len(dispatch_rows)))
            for i, row in enumerate(dispatch_rows[:_MAX_ROWS]):
                for col, value in enumerate((_when(row.get("created_ts")), row.get("decision"), row.get("reply_radio_id"), row.get("transmitted_text"))): self._put(self.expect_replies_table, i, col, value)
        finally:
            for table in (self.expect_requests_table, self.expect_replies_table):
                table.blockSignals(False)
                table.setUpdatesEnabled(True)

    def _load_selected_entry(self) -> None:
        selected = self.expect_entries_table.selectedItems()
        if not selected: return
        row = selected[0].data(Qt.UserRole) or {}
        self.expect_key.setText(_text(row.get("expect_key"))); self.expect_reply.setPlainText(_text(row.get("response_text")))
        self.expect_policy.setCurrentIndex(max(0, self.expect_policy.findData(row.get("allow_policy_id") or 0)))
        self.expect_calls.setText(", ".join(row.get("allowed_callsigns") or ())); self.expect_groups.setText(", ".join(row.get("allowed_groups") or ())); self.expect_blocked.setText(", ".join(row.get("blocked_callsigns") or ()))
        self.expect_trusted_groups.setText(", ".join(row.get("trusted_operator_groups") or ()))
        self._select_expect_radio(row.get("source_scope"), row.get("source_radio_id"))
        self.expect_max.setValue(int(row.get("max_replies") or 1)); self.expect_cooldown.setValue(int(row.get("cooldown_seconds") or 0))
        self.expect_allow_any.setChecked(bool(row.get("allow_any") or "*" in (row.get("allowed_callsigns") or ())))
        self.expect_trusted.setChecked(bool(row.get("allow_trusted_operators")))
        self.expect_enabled.setChecked(True)
        auto_reply_on = row.get("auto_reply_state") == "auto-reply-on"
        self.expect_delivery_mode.blockSignals(True)
        self.expect_delivery_mode.setCurrentIndex(1 if auto_reply_on else 0)
        self.expect_delivery_mode.blockSignals(False)
        self._set_expect_legacy_access_for_row(row)
        self._update_expect_policy_presentation()
        self._sync_expect_delivery_mode()

    def _clear_entry(self) -> None:
        self.expect_entries_table.clearSelection(); self.expect_key.clear(); self.expect_reply.clear(); self.expect_policy.setCurrentIndex(0); self.expect_calls.clear(); self.expect_groups.clear(); self.expect_trusted_groups.clear(); self.expect_blocked.clear(); self.expect_radio.setCurrentIndex(0); self.expect_max.setValue(1); self.expect_cooldown.setValue(0); self.expect_allow_any.setChecked(False); self.expect_trusted.setChecked(False); self.expect_enabled.setChecked(True)
        self.expect_delivery_mode.setCurrentIndex(0)
        self._set_expect_legacy_access_for_row(None)
        self._update_expect_policy_presentation()

    def _open_expect_compose(self, intent: dict[str, object]) -> bool:
        """Navigate to the shared Compose implementation for an Expect task."""
        if self._open_compose is None:
            self.expect_maintenance_state.setText(
                "Message Compose is unavailable in this window."
            )
            return False
        try:
            self._open_compose(intent)
        except TypeError:
            # Embedders from before typed navigation still get a safe fallback.
            self._open_compose()
        return True

    def _view_selected_expect(self) -> None:
        """Open a static Expect response in Compose as a working copy.

        Dynamic FLAMP Q is deliberately excluded: its response is generated
        from transfer state and has no standalone message to view.
        """
        selected = self.expect_entries_table.selectedItems()
        row = selected[0].data(Qt.UserRole) if selected else None
        if not isinstance(row, dict):
            self.expect_maintenance_state.setText("Select a saved response to view.")
            return
        key = _text(row.get("expect_key")).upper()
        if key == "Q":
            self.expect_maintenance_state.setText(
                "Dynamic FLAMP Q replies remain available in the Expect rule editor."
            )
            return
        if self._open_expect_compose({
            "mode": "spotter",
            "transport": "spotter",
            "source": "fio_spotter_expect_view",
            "source_label": "Expect",
            "expect_entry_id": int(row.get("id") or 0),
            "expect_key": key,
            "spotter_form_code": key,
            "expect_view": True,
        }):
            self.expect_maintenance_state.setText(
                f"Opened {key} in Message Compose as an editable working copy. Expect storage is unchanged."
            )

    def _new_expect_from_mcf_form(self) -> None:
        """Start the MCF-form Expect workflow in shared Compose."""
        if self._open_expect_compose({
            "mode": "spotter",
            "transport": "spotter",
            "source": "fio_spotter_expect_create",
            "source_label": "Expect",
            "expect_create": True,
        }):
            self.expect_maintenance_state.setText(
                "Choose an MCF form in Message Compose. The new Expect response starts Saved only until you explicitly enable Auto reply."
            )

    def _new_dynamic_q_entry(self) -> None:
        self._clear_entry()
        self.expect_key.setText("Q")
        self.expect_reply.setPlaceholderText("Generated dynamically: Q <ID> YES / NO / missing blocks")
        self.expect_enabled.setChecked(True)
        self.expect_delivery_mode.setCurrentIndex(1)
        self.expect_radio.setCurrentIndex(0)
        self.expect_groups.setFocus()
        self.expect_maintenance_state.setText(
            "Choose a named, enabled access policy before saving this automatic FLAMP Q rule. The Q ID and response are generated from the request and indexed FLAMP state."
        )
        log.info("CommsPlan Spotter: opened new dynamic FLAMP Q rule editor")

    def _save_entry(self) -> None:
        selected = self.expect_entries_table.selectedItems(); existing = selected[0].data(Qt.UserRole) if selected else {}
        try:
            is_dynamic_q = self.expect_key.text().strip().upper() == "Q"
            allowed_calls = _csv(self.expect_calls.text())
            allow_any = self.expect_allow_any.isChecked() or "*" in allowed_calls
            if allow_any and "*" not in allowed_calls:
                allowed_calls.insert(0, "*")
            existing = dict(existing) if isinstance(existing, dict) else {}
            source_radio_id = _text(self.expect_radio.currentData())
            source_scope = "radio" if source_radio_id else "all"
            same_radio_choice = (
                source_radio_id == _text(existing.get("source_radio_id"))
                and (
                    source_scope == _text(existing.get("source_scope")).lower()
                    or (not source_radio_id and not _text(existing.get("source_radio_id")))
                )
            )
            # Instance and schedule are routing metadata, not operator-facing
            # rule fields. Preserve legacy restrictions while the radio choice
            # is unchanged; a deliberate new radio choice adopts that radio's
            # current JS8Call configuration and normal CommsPlan schedule.
            js8_instance_id = _text(existing.get("js8_instance_id")) if same_radio_choice else ""
            auto_tx_schedule = _text(existing.get("auto_tx_schedule")) if same_radio_choice else ""
            auto_reply = bool(self.expect_delivery_mode.currentData())
            selected_policy = self._selected_expect_policy_row()
            existing = dict(existing) if isinstance(existing, dict) else {}
            is_existing_legacy_rule = bool(existing.get("id")) and self._has_legacy_inline_access(existing)
            legacy_was_auto_reply = bool(
                is_existing_legacy_rule
                and (
                    existing.get("auto_reply_state") == "auto-reply-on"
                    or (
                        existing.get("enabled")
                        and existing.get("auto_reply_enabled")
                        and existing.get("unattended_auto_reply_enabled")
                    )
                )
            )
            if auto_reply and (selected_policy is None or not bool(selected_policy.get("enabled"))) and not legacy_was_auto_reply:
                raise ValueError(
                    "Auto reply requires a named, enabled access policy. Choose Saved only or create/select a policy."
                )
            payload = {"expect_key": self.expect_key.text(), "response_text": self.expect_reply.toPlainText(), "allow_policy_id": self.expect_policy.currentData() or None, "allowed_callsigns": allowed_calls, "allowed_groups": _csv(self.expect_groups.text()), "allow_any": allow_any, "allow_trusted_operators": self.expect_trusted.isChecked(), "trusted_operator_groups": _csv(self.expect_trusted_groups.text()), "blocked_callsigns": _csv(self.expect_blocked.text()), "max_replies": self.expect_max.value(), "cooldown_seconds": self.expect_cooldown.value(), "auto_tx_schedule": auto_tx_schedule, "enabled": True, "auto_reply_enabled": auto_reply, "unattended_auto_reply_enabled": auto_reply, "source_radio_id": source_radio_id, "source_scope": source_scope or ("all" if is_dynamic_q else "radio"), "js8_instance_id": js8_instance_id, "import_source": "fio-spotter"}
            saved = save_expect_entry(payload)
            if is_dynamic_q and bool(self.settings.get("js8_expect_dynamic_flamp_enabled", False)) != auto_reply:
                self.settings.set("js8_expect_dynamic_flamp_enabled", auto_reply)
                if hasattr(self.settings, "save"):
                    self.settings.save()
        except Exception as exc:
            log.warning("CommsPlan Spotter: Expect rule save failed: %s", exc, exc_info=True)
            self.expect_maintenance_state.setText(f"Rule not saved: {exc}")
            return
        log.info(
            "CommsPlan Spotter: %s Expect rule id=%s key=%s policy_id=%s scope=%s",
            "created" if saved.created else "saved",
            saved.id,
            saved.expect_key,
            payload.get("allow_policy_id") or 0,
            payload.get("source_scope"),
        )
        self.refresh_expect()

    def _send_selected_expect_now(self) -> None:
        selected = self.expect_entries_table.selectedItems()
        row = selected[0].data(Qt.UserRole) if selected else None
        if not isinstance(row, dict):
            self.expect_maintenance_state.setText("Select a saved response to review and send.")
            return
        if _text(row.get("expect_key")).upper() == "Q":
            self.expect_maintenance_state.setText(
                "Dynamic FLAMP Q replies require an incoming E? Q request and cannot be sent as a generic saved response."
            )
            return
        if not row.get("manual_send_available"):
            self.expect_maintenance_state.setText("This entry has no saved response to send.")
            return
        if self._open_compose is None:
            self.expect_maintenance_state.setText("Message Compose is unavailable in this window.")
            return
        intent = {
            "transport": "spotter",
            "expect_entry_id": int(row.get("id") or 0),
            "expect_key": _text(row.get("expect_key")),
            "source": "fio_spotter_expect",
        }
        try:
            self._open_compose(intent)
        except TypeError:
            # Compatibility for embedders that still provide the original
            # zero-argument navigation callback.
            self._open_compose()
        self.expect_maintenance_state.setText(
            "Opened a working copy in Message Compose. Review the destination and radio before sending."
        )

    def _delete_entry(self) -> None:
        selected = self.expect_entries_table.selectedItems()
        if selected:
            try: delete_expect_entry(int((selected[0].data(Qt.UserRole) or {}).get("id") or 0))
            except Exception: pass
        self._clear_entry(); self.refresh_expect()

    def _save_policy(self) -> None:
        try:
            saved = save_expect_allow_policy({
                "id": self.policy_manage.currentData() or 0,
                "name": self.policy_name.text(),
                "allowed_callsigns": _csv(self.policy_calls.text()),
                "allowed_groups": _csv(self.policy_groups.text()),
                "allow_trusted_operators": self.policy_trusted.isChecked(),
                "trusted_operator_groups": _csv(self.policy_trusted_groups.text()),
                "blocked_callsigns": _csv(self.policy_blocked.text()),
                "enabled": self.policy_enabled.isChecked(),
                "source_scope": self.policy_scope.currentData() or "all",
                "source_radio_ids": self._policy_radio_ids(),
                "import_source": "fio-spotter",
            })
        except Exception as exc:
            log.warning("CommsPlan Spotter: allow policy save failed: %s", exc, exc_info=True)
            getattr(self, "policy_status", getattr(self, "expect_runtime_state", None)).setText(
                f"○ Policy not saved: {exc}"
            )
            return
        log.info(
            "CommsPlan Spotter: %s Expect allow policy id=%s name=%s",
            "created" if saved.created else "saved",
            saved.id,
            saved.name,
        )
        if hasattr(self, "policy_table"):
            self.refresh_policies()
            self._select_policy_row(saved.id)
        if hasattr(self, "expect_policy"):
            self.refresh_expect()
        if hasattr(self, "policy_status"):
            self.policy_status.setText(
                f"● {'Created' if saved.created else 'Saved'} access policy “{saved.name}”."
            )

    def _load_policy_editor(self) -> None:
        policy_id = int(self.policy_manage.currentData() or 0)
        row = next((item for item in self._policy_rows if int(item.get("id") or 0) == policy_id), None)
        if row is None:
            self._clear_policy_editor(reset_selection=False)
            return
        self.policy_name.setText(_text(row.get("name")))
        self.policy_calls.setText(", ".join(row.get("allowed_callsigns") or ()))
        self.policy_groups.setText(", ".join(row.get("allowed_groups") or ()))
        self.policy_trusted.setChecked(bool(row.get("allow_trusted_operators")))
        self.policy_trusted_groups.setText(", ".join(row.get("trusted_operator_groups") or ()))
        self.policy_blocked.setText(", ".join(row.get("blocked_callsigns") or ()))
        self.policy_scope.setCurrentIndex(max(0, self.policy_scope.findData(_text(row.get("source_scope")) or "all")))
        self.policy_radios.setText(self._policy_radio_labels(row.get("source_radio_ids") or ()))
        self.policy_enabled.setChecked(bool(row.get("enabled")))
        self._show_policy_usage(policy_id)

    def _clear_policy_editor(self, *, reset_selection: bool = True) -> None:
        if reset_selection:
            self.policy_manage.setCurrentIndex(0)
        self.policy_name.clear(); self.policy_calls.clear(); self.policy_groups.clear(); self.policy_trusted.setChecked(False); self.policy_trusted_groups.clear(); self.policy_blocked.clear(); self.policy_scope.setCurrentIndex(0); self.policy_radios.clear(); self.policy_enabled.setChecked(True)
        if hasattr(self, "policy_usage_state"):
            self.policy_usage_state.setText("Not yet used by an Expect response.")

    def _delete_policy(self) -> None:
        policy_id = int(self.policy_manage.currentData() or 0)
        if policy_id <= 0:
            getattr(self, "policy_status", getattr(self, "expect_runtime_state", None)).setText(
                "Select an access policy to delete."
            )
            return
        try:
            delete_expect_allow_policy(policy_id)
        except Exception as exc:
            getattr(self, "policy_status", getattr(self, "expect_runtime_state", None)).setText(
                f"○ Policy not deleted: {exc}"
            )
            return
        self._clear_policy_editor()
        self.refresh_policies()
        if hasattr(self, "expect_policy"):
            self.refresh_expect()

    def _sync_policy_radio_scope(self) -> None:
        """Reveal selected-radio editing only when that scope is active."""
        if not hasattr(self, "policy_scope") or not hasattr(self, "policy_radios"):
            return
        selected = self.policy_scope.currentData() == "radio"
        self.policy_radios.setEnabled(selected)
        self.policy_radios.setToolTip(
            "Select known CommsPlan radios that may use this policy."
            if selected else
            "All JS8 radios may use this policy. Choose selected radios to limit its scope."
        )

    # Access policies -----------------------------------------------------
    def _build_policies(self, page: QWidget) -> None:
        layout = self._page_layout(page)
        intro = QLabel(
            "Access policies answer one question: who may request an automatic Expect reply? "
            "Blocked callers always win. A policy may be shared by several saved responses."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        header = QHBoxLayout()
        refresh = QPushButton("Refresh")
        self.policy_refresh = refresh
        refresh.clicked.connect(self.refresh_policies)
        new_policy = QPushButton("New policy")
        self.policy_new = new_policy
        new_policy.clicked.connect(self._clear_policy_editor)
        header.addWidget(refresh)
        header.addWidget(new_policy)
        header.addStretch(1)
        layout.addLayout(header)

        section = QWidget()
        self.policy_split = section
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(8)

        editor = QGroupBox("Access policy editor")
        editor.setObjectName("fioSpotterAccessPolicyEditor")
        self.policy_editor_panel = editor
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(8, 8, 8, 8)
        editor_layout.setSpacing(6)
        policy_fields = QWidget(editor)
        self.policy_editor_columns = QHBoxLayout(policy_fields)
        self.policy_editor_columns.setContentsMargins(0, 0, 0, 0)
        self.policy_editor_columns.setSpacing(12)
        who_form = QFormLayout()
        who_form.setContentsMargins(0, 0, 0, 0)
        who_form.setSpacing(4)
        scope_form = QFormLayout()
        scope_form.setContentsMargins(0, 0, 0, 0)
        scope_form.setSpacing(4)
        self.policy_manage = QComboBox()
        self.policy_manage.addItem("New policy", 0)
        self.policy_manage.currentIndexChanged.connect(self._load_policy_editor)
        self.policy_manage.setVisible(False)
        self.policy_name = QLineEdit()
        self.policy_name.setPlaceholderText("A clear name operators will recognize")
        self.policy_calls = _TokenListEditor()
        self.policy_calls.setPlaceholderText("Callsign or *")
        self.policy_groups = _TokenListEditor(token_prefix="@")
        self.policy_groups.setPlaceholderText("Find or enter a query group")
        self.policy_trusted = QCheckBox("Allow trusted operators")
        self.policy_trusted_groups = _TokenListEditor()
        self.policy_trusted_groups.setPlaceholderText("Find an operator group")
        self.policy_blocked = _TokenListEditor()
        self.policy_blocked.setPlaceholderText("Find or enter a blocked callsign")
        self.policy_scope = QComboBox()
        self.policy_scope.addItem("All JS8 radios", "all")
        self.policy_scope.addItem("Only selected CommsPlan radios", "radio")
        self.policy_scope.currentIndexChanged.connect(self._sync_policy_radio_scope)
        self.policy_radios = _TokenListEditor()
        self.policy_radios.setPlaceholderText("Selected CommsPlan radios")
        self.policy_enabled = QCheckBox("Policy available for automatic replies")
        self.policy_enabled.setChecked(True)
        self.policy_access_catalog_state = QLabel()
        self.policy_access_catalog_state.setWordWrap(True)
        self.policy_access_catalog_state.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Minimum
        )
        self.policy_access_catalog_state.setObjectName("fioSpotterPolicyLookupState")
        self.policy_usage_state = QLabel("Not yet used by an Expect response.")
        self.policy_usage_state.setWordWrap(True)
        self.policy_usage_state.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.policy_usage_state.setObjectName("fioSpotterPolicyUsageState")

        who_form.addRow("Name", self.policy_name)
        who_form.addRow("Allowed callers", self.policy_calls)
        who_form.addRow("Query groups", self.policy_groups)
        who_form.addRow(self.policy_trusted)
        who_form.addRow("Trusted operators from groups", self.policy_trusted_groups)
        scope_form.addRow("Always blocked", self.policy_blocked)
        scope_form.addRow("Where it applies", self.policy_scope)
        scope_form.addRow("Selected radios", self.policy_radios)
        scope_form.addRow(self.policy_enabled)
        self.policy_editor_columns.addLayout(who_form, 1)
        self.policy_editor_columns.addLayout(scope_form, 1)
        editor_layout.addWidget(policy_fields)
        policy_summary = QWidget(editor)
        self.policy_summary_layout = QHBoxLayout(policy_summary)
        self.policy_summary_layout.setContentsMargins(0, 0, 0, 0)
        self.policy_summary_layout.setSpacing(12)
        self.policy_summary_layout.addWidget(self.policy_access_catalog_state, 1)
        self.policy_summary_layout.addWidget(self.policy_usage_state, 1)
        editor_layout.addWidget(policy_summary)
        actions = QWidget()
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        save = QPushButton("Save policy")
        self.policy_save = save
        save.clicked.connect(self._save_policy)
        delete = QPushButton("Delete policy")
        self.policy_delete = delete
        delete.clicked.connect(self._delete_policy)
        action_layout.addWidget(save)
        action_layout.addWidget(delete)
        action_layout.addStretch(1)
        editor_layout.addWidget(actions)
        section_layout.addWidget(editor)

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(QLabel("Saved access policies"))
        self.policy_table = self._table(
            ["Policy", "Who may ask", "Where", "Used by", "Status"],
            name="fioSpotterAccessPoliciesTable",
        )
        self.policy_table.itemSelectionChanged.connect(self._load_policy_from_table)
        self.policy_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.policy_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table_layout.addWidget(self.policy_table, 1)
        table_panel.setMinimumHeight(table_panel.minimumSizeHint().height())
        section_layout.addWidget(table_panel, 1)
        layout.addWidget(section, 1)
        self.policy_status = QLabel("Select a policy or create a new one.")
        self.policy_status.setWordWrap(True)
        layout.addWidget(self.policy_status)
        for token_editor in (
            self.policy_calls, self.policy_groups, self.policy_trusted_groups,
            self.policy_blocked, self.policy_radios,
        ):
            token_editor.valuesChanged.connect(self._update_policy_editor_height)
        self._refresh_expect_radio_choices()
        self._sync_policy_radio_scope()
        self._load_expect_access_completions(force=True)
        self._apply_expect_responsive_layout()
        self._update_policy_editor_height()

    def _update_policy_editor_height(self) -> None:
        if not hasattr(self, "policy_split"):
            return
        editor = getattr(self, "policy_editor_panel", None)
        if editor is None:
            return
        editor.setMinimumHeight(0)
        if editor.layout() is not None:
            editor.layout().activate()
        editor.setMinimumHeight(editor.minimumSizeHint().height())
        editor.updateGeometry()
        self.policy_split.updateGeometry()

    def refresh_policies(self) -> None:
        if not hasattr(self, "policy_table"):
            return
        selected_id = int(self.policy_manage.currentData() or 0)
        try:
            self._policy_rows = list_expect_allow_policies(include_usage=True)
            # Policy selection only reads this bounded, already-loaded page;
            # it must never initiate a storage read merely to update usage text.
            self._entry_rows = list_expect_entries(limit=_MAX_ROWS)
            self._cache_policy_usage_entries()
        except Exception as exc:
            self._policy_rows = []
            self._policy_usage_entries = {}
            self.policy_status.setText(f"Access policies could not be read: {exc}")
        self.policy_manage.blockSignals(True)
        self.policy_manage.clear()
        self.policy_manage.addItem("New policy", 0)
        for row in self._policy_rows:
            self.policy_manage.addItem(_text(row.get("name")), int(row.get("id") or 0))
        self.policy_manage.setCurrentIndex(max(0, self.policy_manage.findData(selected_id)))
        self.policy_manage.blockSignals(False)
        if selected_id and self.policy_manage.currentData() == 0:
            self._clear_policy_editor(reset_selection=False)

        table = self.policy_table
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.clearContents()
            table.setRowCount(len(self._policy_rows))
            for row_index, row in enumerate(self._policy_rows):
                values = (
                    row.get("name"), self._expect_access_summary(row),
                    "All JS8 radios" if row.get("source_scope") != "radio" else
                    f"{len(row.get('source_radio_ids') or ())} selected radio(s)",
                    int(row.get("usage_count") or 0),
                    "Available" if row.get("enabled") else "Paused",
                )
                for column, value in enumerate(values):
                    self._put(table, row_index, column, value, data=row if column == 0 else None)
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
        if selected_id:
            self._select_policy_row(selected_id)
        elif not self._policy_rows:
            self._clear_policy_editor()
            self.policy_status.setText("No reusable access policies yet. Expect entries may still use their own access list.")

    def _select_policy_row(self, policy_id: int) -> None:
        for row_index in range(self.policy_table.rowCount()):
            item = self.policy_table.item(row_index, 0)
            row = item.data(Qt.UserRole) if item else None
            if isinstance(row, dict) and int(row.get("id") or 0) == policy_id:
                self.policy_table.selectRow(row_index)
                return

    def _load_policy_from_table(self) -> None:
        selected = self.policy_table.selectedItems()
        if not selected:
            return
        row = selected[0].data(Qt.UserRole)
        if not isinstance(row, dict):
            return
        policy_id = int(row.get("id") or 0)
        index = self.policy_manage.findData(policy_id)
        if index >= 0:
            self.policy_manage.setCurrentIndex(index)
        self._show_policy_usage(policy_id)

    # Forms / imports ------------------------------------------------------
    def _build_forms(self, page: QWidget) -> None:
        layout = self._page_layout(page)
        header = QHBoxLayout()
        header.addWidget(QLabel("MCF form catalog"))
        header.addStretch(1)
        self.forms_refresh_btn = QPushButton("Refresh catalog")
        self.forms_refresh_btn.setAccessibleName("Refresh Spotter form catalog")
        self.forms_refresh_btn.setToolTip("Rescan the selected forms folder. This is the only catalog scan on this page.")
        self.forms_refresh_btn.clicked.connect(self.refresh_forms)
        header.addWidget(self.forms_refresh_btn)
        layout.addLayout(header)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("1. Forms folder"))
        self.forms_path = QLineEdit(); self.forms_path.setPlaceholderText("MCF forms folder")
        self._forms_draft_dirty = False
        self.forms_path.textEdited.connect(self._mark_forms_path_draft_dirty)
        self.forms_browse_btn = QPushButton("Browse…")
        self.forms_browse_btn.setAccessibleName("Choose MCF forms folder")
        self.forms_browse_btn.clicked.connect(self._choose_forms_folder)
        self.forms_use_folder_btn = QPushButton("Use folder")
        self.forms_use_folder_btn.setAccessibleName("Use selected MCF forms folder")
        self.forms_use_folder_btn.clicked.connect(self._use_forms_folder)
        self.forms_add_btn = QPushButton("Add forms…")
        self.forms_add_btn.setAccessibleName("Add forms to the CP Forms Library")
        self.forms_add_btn.setToolTip("Review existing MCF files or a folder, then explicitly copy them into the station-owned CP Forms Library.")
        add_menu = QMenu(self.forms_add_btn)
        add_menu.addAction("Choose MCF files…", self._choose_forms_files)
        add_menu.addAction("Choose a forms folder…", self._choose_forms_import_folder)
        self.forms_add_btn.setMenu(add_menu)
        path_row.addWidget(self.forms_path, 1)
        path_row.addWidget(self.forms_browse_btn)
        path_row.addWidget(self.forms_use_folder_btn)
        path_row.addWidget(self.forms_add_btn)
        layout.addLayout(path_row)
        self.forms_legacy_combo = QComboBox()
        self.forms_legacy_combo.setAccessibleName("Legacy per-radio MCF forms source")
        self.forms_legacy_combo.setToolTip("Shown only when no station-global CP Forms Library is selected. This legacy source is read-only until you choose Use folder.")
        for label, path in self._legacy_form_sources:
            self.forms_legacy_combo.addItem(f"{label} — {path}", path)
        self.forms_legacy_combo.setVisible(bool(self._legacy_form_sources))
        self.forms_legacy_combo.currentIndexChanged.connect(self._on_legacy_forms_source_changed)
        layout.addWidget(self.forms_legacy_combo)

        self.forms_state = QLabel()
        self.forms_state.setWordWrap(True)
        self.forms_state.setStyleSheet(label_style("muted", resolve_theme(self.settings), weight=600))
        layout.addWidget(self.forms_state)

        mapping_row = QHBoxLayout()
        mapping_row.addWidget(QLabel("2. Route selected catalog forms"))
        mapping_row.addStretch(1)
        self.forms_classify_btn = QPushButton("Auto-classify")
        self.forms_classify_btn.setAccessibleName("Auto-classify Spotter forms")
        self.forms_classify_btn.setToolTip("Stage the standard route mappings for the loaded catalog. Save mappings to keep them.")
        self.forms_classify_btn.clicked.connect(self._auto_classify_forms)
        self.forms_save_btn = QPushButton("Save mappings")
        self.forms_save_btn.setAccessibleName("Save Spotter form mappings")
        self.forms_save_btn.setToolTip("Save the currently visible form-route choices.")
        self.forms_save_btn.clicked.connect(self._save_form_mappings)
        mapping_row.addWidget(self.forms_classify_btn)
        mapping_row.addWidget(self.forms_save_btn)
        layout.addLayout(mapping_row)
        self.forms_table = self._table(
            ["Form", "Name", "Purpose", "Inbox", "Map", "Alert", "Net", "Status route", "Expect status"],
            name="fioSpotterFormsTable",
        )
        forms_header = self.forms_table.horizontalHeader()
        forms_header.setSectionResizeMode(0, QHeaderView.Interactive)
        forms_header.setSectionResizeMode(1, QHeaderView.Stretch)
        forms_header.setSectionResizeMode(2, QHeaderView.Interactive)
        for column in range(3, 9):
            forms_header.setSectionResizeMode(column, QHeaderView.Interactive)
        self.forms_table.setColumnWidth(0, 90)
        purpose_width = max(
            self.forms_table.fontMetrics().horizontalAdvance(option)
            for option in PURPOSE_OPTIONS
        ) + 56
        self.forms_table.setColumnWidth(2, min(max(purpose_width, 180), 340))
        for column in range(3, 8):
            self.forms_table.setColumnWidth(column, 65)
        self.forms_table.setColumnWidth(8, 115)
        self.forms_table.itemSelectionChanged.connect(self._on_forms_selection_changed)
        layout.addWidget(self.forms_table, 1)
        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("3. Review or use selected form"))
        action_row.addStretch(1)
        self.forms_preview_btn = QPushButton("Preview selected")
        self.forms_preview_btn.setAccessibleName("Preview selected Spotter form")
        self.forms_preview_btn.setToolTip("Read the selected form source without changing its mapping.")
        self.forms_preview_btn.clicked.connect(self._preview_selected_form)
        self.forms_compose_btn = QPushButton("Compose selected form")
        self.forms_compose_btn.setAccessibleName("Compose and send selected Spotter form")
        self.forms_compose_btn.setToolTip("Open this form in the guarded CommsPlan Spotter Compose workflow.")
        self.forms_compose_btn.clicked.connect(self._open_spotter_compose)
        self.forms_expect_btn = QPushButton("Make available by E?…")
        self.forms_expect_btn.setAccessibleName("Configure selected Spotter form as an Expect response")
        self.forms_expect_btn.setToolTip("Open the disabled response setup. You will review access before automatic reply can be enabled.")
        self.forms_expect_btn.clicked.connect(self._configure_selected_form_expect)
        action_row.addWidget(self.forms_preview_btn)
        action_row.addWidget(self.forms_compose_btn)
        action_row.addWidget(self.forms_expect_btn)
        layout.addLayout(action_row)
        self.forms_preview = QTextEdit()
        self.forms_preview.setReadOnly(True)
        self.forms_preview.setAccessibleName("Selected Spotter form preview")
        self.forms_preview.setPlaceholderText("Select a form to review its fields and mapping.")
        self.forms_preview.textChanged.connect(self._fit_forms_preview_geometry)
        layout.addWidget(self.forms_preview)
        self._fit_forms_preview_geometry()
        self._refresh_forms_action_state()

    def _fit_forms_preview_geometry(self) -> None:
        """Keep the bounded form preview readable at the active text scale."""

        preview = getattr(self, "forms_preview", None)
        if preview is None:
            return
        line_height = max(1, preview.fontMetrics().lineSpacing())
        minimum = max(
            control_height_for_font(preview, vertical_padding=14, floor=44) * 2,
            line_height * 3 + 20,
        )
        document_height = int(preview.document().documentLayout().documentSize().height())
        content_height = max(minimum, document_height + 20)
        viewport = max(minimum * 2, int(self.height() or 0))
        preview.setMaximumHeight(max(minimum, min(content_height, int(viewport * 0.45))))

    def _refresh_forms_state(self) -> None:
        if not hasattr(self, "forms_state"):
            return
        path = _text(self.settings.get("js8_forms_path", ""))
        if hasattr(self, "forms_path") and not self._forms_draft_dirty and not self.forms_path.hasFocus():
            self.forms_path.setText(path)
        legacy = _text(getattr(self, "forms_legacy_combo", None).currentData()) if self._legacy_form_sources and hasattr(self, "forms_legacy_combo") else ""
        choice = choose_effective_spotter_forms_catalog(path, legacy)
        owner = choice.ownership
        if hasattr(self, "forms_legacy_combo"):
            self.forms_legacy_combo.setEnabled(not bool(path))
        self.forms_state.setText(
            f"Active CP Forms Library: {choice.path} ({owner}). "
            "Browse only drafts a path; Use folder explicitly saves it. Refresh scans the selected catalog when requested."
        )

    def _on_legacy_forms_source_changed(self, *_args) -> None:
        self._refresh_forms_state()

    def _mark_forms_path_draft_dirty(self, *_args) -> None:
        self._forms_draft_dirty = True

    def _choose_forms_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select MCF forms folder", self.forms_path.text().strip())
        if selected:
            self.forms_path.setText(selected)
            self._forms_draft_dirty = True
            self.forms_state.setText("Forms folder drafted. Choose Use folder to save it, or Add forms… to copy reviewed files into the CP Forms Library.")

    def _choose_forms_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select MCF forms", self.forms_path.text().strip(), "MCF forms (MCF*.txt);;Text files (*.txt);;All files (*)"
        )
        if files:
            self._review_forms_import(files)

    def _choose_forms_import_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select MCF forms folder to import", self.forms_path.text().strip())
        if selected:
            self._review_forms_import([selected])

    def _review_forms_import(self, sources: list[str]) -> None:
        """Preview an import, then require an explicit operator commit."""

        current = _text(self.settings.get("js8_forms_path", ""))
        legacy = _text(self.forms_legacy_combo.currentData()) if self._legacy_form_sources and hasattr(self, "forms_legacy_combo") else ""
        preserve_choice = choose_effective_spotter_forms_catalog(current, legacy)
        preserve = Path(preserve_choice.path).expanduser()
        if not preserve.is_dir():
            preserve = None
        target = cp_spotter_forms_library_dir(getattr(self.settings, "config_dir", None))
        preview = preview_spotter_forms_import(sources, existing_dir=target, existing_dirs=(preserve,))
        if not preview.items:
            self.forms_state.setText("No MCF*.txt files were found in the selected source.")
            return
        lines = []
        for item in preview.items:
            state = "ready" if item.valid and not item.duplicate else "duplicate" if item.duplicate else "rejected"
            detail = f" — {item.reason}" if item.reason else ""
            lines.append(f"{item.name}: {state}{detail}")
        summary = (
            f"Review {preview.source_count} MCF form(s): {preview.valid_count} new, "
            f"{preview.duplicate_count} duplicate(s), {preview.rejected_count} rejected.\n\n"
            + "\n".join(lines[:_MAX_ROWS])
        )
        if preview.bounded:
            summary += "\n\nThe source exceeded the bounded import limit; only the first 200 files were reviewed."
        if preview.existing_bounded:
            summary += "\n\nAn existing catalog exceeds the bounded import limit; no import will be started."
        if preview.rejected_count:
            self.forms_state.setText(
                "Forms import was not started. Resolve the rejected form(s), then preview the complete batch again."
            )
            self.forms_preview.setPlainText(summary)
            return
        if preview.bounded or preview.existing_bounded:
            self.forms_state.setText("Forms import was not started because a catalog exceeds the bounded limit.")
            self.forms_preview.setPlainText(summary)
            return
        answer = QMessageBox.question(
            self, "Review forms import", summary + f"\n\nCopy valid new forms into the CP-owned library at:\n{target}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self.forms_state.setText("Forms import cancelled. No files or mappings were changed.")
            return
        result = import_spotter_forms_to_library(preview, target_dir=target, preserve_dir=preserve)
        if not result.committed:
            self.forms_state.setText(f"Forms import failed; no catalog change was saved: {'; '.join(result.errors)}")
            return
        try:
            self.settings.set("js8_forms_path", str(target))
            if hasattr(self.settings, "save"):
                self.settings.save()
        except Exception as exc:
            try:
                self.settings.set("js8_forms_path", current)
            except Exception:
                pass
            self.forms_path.setText(current)
            self._forms_draft_dirty = False
            self.forms_state.setText(f"Forms were copied but the CP Forms Library is not active; previous catalog was restored: {exc}")
            return
        self.forms_path.setText(str(target))
        self._forms_draft_dirty = False
        self.forms_state.setText(
            f"Saved CP Forms Library at {target}: copied {len(result.copied)} form(s), skipped {len(result.skipped)} duplicate(s)."
        )
        self.refresh_forms()

    def _use_forms_folder(self) -> None:
        path = _text(self.forms_path.text())
        if not path:
            self.forms_state.setText("Choose a forms folder first.")
            return
        if not Path(path).expanduser().is_dir():
            self.forms_state.setText("Forms folder was not found.")
            return
        old_path = _text(self.settings.get("js8_forms_path", ""))
        try:
            self.settings.set("js8_forms_path", path)
            if hasattr(self.settings, "save"):
                self.settings.save()
        except Exception as exc:
            try:
                self.settings.set("js8_forms_path", old_path)
            except Exception:
                pass
            self.forms_state.setText(f"Forms folder was not saved: {exc}")
            return
        self._forms_draft_dirty = False
        self.forms_state.setText("Forms folder saved. Refresh catalog to scan it; existing form mappings are preserved.")

    def refresh_forms(self) -> None:
        if not hasattr(self, "forms_table"): return
        selected = self._selected_form_row()
        selected_code = _text((selected or {}).get("form_code")).upper()
        configured_path = _text(self.settings.get("js8_forms_path", ""))
        legacy_path = _text(self.forms_legacy_combo.currentData()) if self._legacy_form_sources and hasattr(self, "forms_legacy_combo") else ""
        choice = choose_effective_spotter_forms_catalog(configured_path, legacy_path)
        path = str(resolve_effective_spotter_forms_dir(configured_path, legacy_path))
        definitions = discover_effective_spotter_forms(configured_path, legacy_path)[:_MAX_ROWS]
        paths = {form.form_code: form.path for form in definitions}
        mappings = effective_mapping_rows(self.settings, choice.path)[:_MAX_ROWS]
        try:
            expect_by_key = {
                _text(entry.get("expect_key")).upper(): entry
                for entry in list_expect_entries(limit=_MAX_ROWS)
            }
        except Exception:
            expect_by_key = {}
        self.forms_state.setText(
            f"Forms catalog: {path} — {len(mappings)} catalog entries "
            f"(bounded to {_MAX_ROWS}). Select which CommsPlan services receive each form, then Save mappings."
        )
        self.forms_table.setUpdatesEnabled(False)
        self.forms_table.blockSignals(True)
        try:
            self.forms_table.clearContents()
            self.forms_table.setRowCount(len(mappings))
            for row_index, mapping in enumerate(mappings):
                row = dict(mapping)
                row["path"] = paths.get(_text(row.get("form_code")), "")
                self._put(self.forms_table, row_index, 0, row.get("form_code"), data=row)
                self._put(self.forms_table, row_index, 1, row.get("title"))
                purpose = QComboBox()
                purpose.setAccessibleName(f"Purpose for {_text(row.get('form_code'))}")
                purpose.addItems(list(PURPOSE_OPTIONS))
                purpose.setCurrentText(_text(row.get("purpose")))
                self._fit_spotter_combo(purpose, minimum_characters=12)
                self.forms_table.setCellWidget(row_index, 2, purpose)
                for column, key in enumerate(("messages", "map", "alert", "net", "status"), start=3):
                    item = QTableWidgetItem("")
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked if bool(row.get(key)) else Qt.Unchecked)
                    item.setToolTip(f"Use {_text(row.get('form_code'))} for {key}")
                    self.forms_table.setItem(row_index, column, item)
                expect_entry = expect_by_key.get(_text(row.get("form_code")).upper())
                if not expect_entry:
                    availability = "Not in Expect"
                else:
                    availability = self._expect_display_state(expect_entry)
                self._put(self.forms_table, row_index, 8, availability)
        finally:
            self.forms_table.blockSignals(False)
            self.forms_table.setUpdatesEnabled(True)
        if selected_code:
            for row_index in range(self.forms_table.rowCount()):
                item = self.forms_table.item(row_index, 0)
                row = item.data(Qt.UserRole) if item is not None else None
                if _text((row or {}).get("form_code")).upper() == selected_code:
                    self.forms_table.selectRow(row_index)
                    break
        if self.forms_table.currentRow() >= 0:
            self._show_selected_form_summary()
        elif not mappings:
            self.forms_preview.setPlainText("No MCF forms are configured. Choose the folder used by CommsPlan Spotter, then refresh the catalog.")
        self._refresh_forms_action_state()

    def _form_mapping_rows_from_table(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for row_index in range(self.forms_table.rowCount()):
            code_item = self.forms_table.item(row_index, 0)
            source = code_item.data(Qt.UserRole) if code_item is not None else {}
            purpose = self.forms_table.cellWidget(row_index, 2)
            values: dict[str, object] = {
                "form_code": _text((source or {}).get("form_code")),
                "title": _text((source or {}).get("title")),
                "purpose": purpose.currentText() if isinstance(purpose, QComboBox) else "",
            }
            for column, key in enumerate(("messages", "map", "alert", "net", "status"), start=3):
                item = self.forms_table.item(row_index, column)
                values[key] = bool(item is not None and item.checkState() == Qt.Checked)
            rows.append(normalize_mapping_row(values))
        return rows

    def _save_form_mappings(self) -> None:
        if not hasattr(self, "forms_table") or self.forms_table.rowCount() <= 0:
            self.forms_state.setText("No form mappings are available to save.")
            return
        rows = self._form_mapping_rows_from_table()
        try:
            self.settings.set(MAPPER_SETTINGS_KEY, rows)
            if hasattr(self.settings, "save"):
                self.settings.save()
        except Exception as exc:
            self.forms_state.setText(f"Form mappings were not saved: {exc}")
            return
        self.forms_state.setText(f"Saved {len(rows)} CommsPlan Spotter form mapping(s).")

    def _auto_classify_forms(self) -> None:
        if not hasattr(self, "forms_table"):
            return
        for row_index in range(self.forms_table.rowCount()):
            code_item = self.forms_table.item(row_index, 0)
            source = code_item.data(Qt.UserRole) if code_item is not None else {}
            mapping = factory_mapping_for_form(
                (source or {}).get("form_code"),
                (source or {}).get("title"),
            )
            purpose = self.forms_table.cellWidget(row_index, 2)
            if isinstance(purpose, QComboBox):
                purpose.setCurrentText(_text(mapping.get("purpose")))
            for column, key in enumerate(("messages", "map", "alert", "net", "status"), start=3):
                item = self.forms_table.item(row_index, column)
                if item is not None:
                    item.setCheckState(Qt.Checked if bool(mapping.get(key)) else Qt.Unchecked)
        self.forms_state.setText("Factory classifications are staged. Choose Save mappings to apply them.")
        self._show_selected_form_summary()
        self._refresh_forms_action_state()

    def _selected_form_row(self) -> dict[str, object] | None:
        if not hasattr(self, "forms_table") or self.forms_table.currentRow() < 0:
            return None
        item = self.forms_table.item(self.forms_table.currentRow(), 0)
        row = item.data(Qt.UserRole) if item is not None else None
        return dict(row) if isinstance(row, dict) else None

    def _on_forms_selection_changed(self) -> None:
        """Selection is cache-only; source-file reads require Preview selected."""
        self._show_selected_form_summary()
        self._refresh_forms_action_state()

    def _show_selected_form_summary(self) -> None:
        row = self._selected_form_row()
        if not row:
            return
        mapping = self._form_mapping_rows_from_table()[self.forms_table.currentRow()]
        routes = [key.title() for key in ("messages", "map", "alert", "net", "status") if mapping.get(key)]
        self.forms_preview.setPlainText(
            f"{_text(row.get('form_code'))} — {_text(row.get('title'))}\n"
            f"Purpose: {_text(mapping.get('purpose'))} · Routes: {', '.join(routes) or 'None'}\n\n"
            "Choose Preview selected to read the source, Compose selected form to prepare a guarded message, "
            "or Make available by E?… to review a disabled saved response."
        )
        self._fit_forms_preview_geometry()

    def _preview_selected_form(self) -> None:
        row = self._selected_form_row()
        if not row:
            self.forms_state.setText("Select a form to preview.")
            return
        path = Path(_text(row.get("path")))
        try:
            body = path.read_text(encoding="utf-8", errors="replace")[:65536]
        except Exception as exc:
            body = f"Form source is unavailable: {exc}"
        mapping = self._form_mapping_rows_from_table()[self.forms_table.currentRow()]
        routes = [key.title() for key in ("messages", "map", "alert", "net", "status") if mapping.get(key)]
        self.forms_preview.setPlainText(
            f"{_text(row.get('form_code'))} — {_text(row.get('title'))}\n"
            f"Purpose: {_text(mapping.get('purpose'))} · Routes: {', '.join(routes) or 'None'}\n\n{body}"
        )
        self._fit_forms_preview_geometry()

    def _refresh_forms_action_state(self) -> None:
        theme = resolve_theme(self.settings)
        has_rows = bool(hasattr(self, "forms_table") and self.forms_table.rowCount())
        has_selection = self._selected_form_row() is not None
        for button, role in (
            (getattr(self, "forms_refresh_btn", None), "muted"),
            (getattr(self, "forms_browse_btn", None), "secondary"),
            (getattr(self, "forms_use_folder_btn", None), "secondary"),
            (getattr(self, "forms_add_btn", None), "secondary"),
            (getattr(self, "forms_classify_btn", None), "secondary"),
        ):
            if isinstance(button, QPushButton):
                button.setStyleSheet(button_style(role, theme))
        save = getattr(self, "forms_save_btn", None)
        if isinstance(save, QPushButton):
            save.setEnabled(has_rows)
            save.setStyleSheet(button_style("primary" if has_rows else "muted", theme))
        for button, role in (
            (getattr(self, "forms_preview_btn", None), "secondary"),
            (getattr(self, "forms_compose_btn", None), "primary"),
            (getattr(self, "forms_expect_btn", None), "secondary"),
        ):
            if isinstance(button, QPushButton):
                button.setEnabled(has_selection)
                button.setStyleSheet(button_style(role if has_selection else "muted", theme))

    def _open_spotter_compose(self) -> None:
        row = self._selected_form_row()
        if self._open_compose is None:
            self.forms_state.setText("Open Messages > Compose and choose Spotter to use this form.")
            return
        intent = {
            "mode": "spotter",
            "spotter_form_code": _text((row or {}).get("form_code")).upper(),
        }
        try:
            self._open_compose(intent)
        except TypeError:
            # Older embedding callers accepted no navigation payload.  The
            # guarded Compose workflow still opens, just without a preselect.
            self._open_compose()

    def _configure_selected_form_expect(self) -> None:
        row = self._selected_form_row()
        if not row:
            self.forms_state.setText("Select a form first.")
            return
        form_code = _text(row.get("form_code")).upper()
        # Forms and Expect share one guided Compose handoff.  If this form is
        # already available by E?, pass its identity so Compose opens it as a
        # working copy; otherwise Compose starts a new response for this form.
        existing_id = 0
        try:
            existing = next(
                (entry for entry in list_expect_entries(expect_key=form_code, limit=1)),
                None,
            )
            existing_id = int((existing or {}).get("id") or 0)
        except Exception:
            existing_id = 0
        opened = self._open_expect_compose({
            "mode": "spotter",
            "transport": "spotter",
            "source": "fio_spotter_form_expect",
            "source_label": "Forms",
            "expect_entry_id": existing_id,
            "expect_key": form_code,
            "spotter_form_code": form_code,
            "expect_view": bool(existing_id),
            "expect_create": not bool(existing_id),
        })
        if not opened:
            return
        self.forms_state.setText(
            f"Opened {form_code} in Message Compose. Save to Expect creates a Saved-only response; review its policy before enabling Auto reply."
            if not existing_id else
            f"Opened saved {form_code} in Message Compose as a working copy. Expect storage is unchanged until Save to Expect."
        )

    def _build_imports(self, page: QWidget) -> None:
        layout = self._page_layout(page)
        intro = QLabel("1. Select a JS8Spotter database. 2. Preview its bounded changes. 3. Import only after the review is clear.")
        intro.setWordWrap(True)
        intro.setStyleSheet(label_style("muted", resolve_theme(self.settings), weight=600))
        layout.addWidget(intro)
        source_row = QHBoxLayout(); source_row.addWidget(QLabel("JS8Spotter database"))
        self.import_source = QLineEdit(); self.import_source.setPlaceholderText("Select a JS8Spotter SQLite database")
        self.import_source.textChanged.connect(self._on_import_source_changed)
        self.import_choose_btn = QPushButton("Choose…"); self.import_choose_btn.setAccessibleName("Choose JS8Spotter import database"); self.import_choose_btn.clicked.connect(self._choose_import_source)
        source_row.addWidget(self.import_source, 1); source_row.addWidget(self.import_choose_btn); layout.addLayout(source_row)
        actions = QHBoxLayout()
        self.import_preview_btn = QPushButton("Preview changes"); self.import_preview_btn.setAccessibleName("Preview JS8Spotter import counts"); self.import_preview_btn.clicked.connect(self._preview_import)
        self.import_apply_btn = QPushButton("Import reviewed data…"); self.import_apply_btn.setAccessibleName("Import JS8Spotter database"); self.import_apply_btn.clicked.connect(self._apply_import)
        actions.addWidget(self.import_preview_btn); actions.addWidget(self.import_apply_btn); actions.addStretch(1); layout.addLayout(actions)
        self.imports_state = QLabel(); self.imports_state.setWordWrap(True); self.imports_state.setStyleSheet(label_style("muted", resolve_theme(self.settings), weight=600)); layout.addWidget(self.imports_state)
        layout.addStretch(1)
        self._refresh_import_action_state()

    def refresh_imports(self) -> None:
        if hasattr(self, "imports_state"):
            source = _text(self.settings.get("js8spotter_import_db_path", ""))
            if hasattr(self, "import_source") and not self.import_source.hasFocus():
                self.import_source.setText(source)
            last_import = self._published_import_freshness(source)
            self.imports_state.setText(
                f"External Spotter source: {source or 'Not configured'}. "
                "This page shows an explicit import snapshot; it does not watch the external database or enable CP automation. "
                f"{last_import} Choose Preview import to inspect bounded counts before changing station data."
            )
            self._refresh_import_action_state()

    @staticmethod
    def _published_import_freshness(source: str) -> str:
        """Read existing import-log freshness without creating schema or polling."""
        if not source:
            return "No published import source is configured."
        db_path = default_expect_db_path()
        if not db_path.is_file():
            return "No published import is recorded."
        try:
            source_db = str(Path(source).expanduser().resolve())
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT MAX(imported_ts) FROM js8spotter_import_log WHERE source_db=?",
                    (source_db,),
                ).fetchone()
            value = float(row[0]) if row and row[0] else 0.0
            if value <= 0:
                return "No published import is recorded."
            stamp = dt.datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M")
            return f"Last published import: {stamp}."
        except Exception:
            return "Published import freshness is unavailable."

    def _on_import_source_changed(self, *_args) -> None:
        self._import_preview = None
        self._refresh_import_action_state()

    def _refresh_import_action_state(self) -> None:
        theme = resolve_theme(self.settings)
        source = _text(getattr(getattr(self, "import_source", None), "text", lambda: "")())
        preview = getattr(self, "_import_preview", None)
        preview_matches = bool(
            preview is not None
            and _text(getattr(preview, "source_db", "")) == str(Path(source).expanduser())
            and not getattr(preview, "warnings", ())
        )
        for button, enabled, role in (
            (getattr(self, "import_choose_btn", None), True, "secondary"),
            (getattr(self, "import_preview_btn", None), bool(source), "secondary"),
            (getattr(self, "import_apply_btn", None), preview_matches, "primary"),
        ):
            if isinstance(button, QPushButton):
                button.setEnabled(enabled)
                button.setStyleSheet(button_style(role if enabled else "muted", theme))

    def _choose_import_source(self) -> None:
        current = _text(self.import_source.text())
        start = str(Path(current).expanduser().parent) if current else ""
        filename, _ = QFileDialog.getOpenFileName(self, "Select JS8Spotter database", start, "SQLite Databases (*.db *.sqlite *.sqlite3);;All Files (*)")
        if filename:
            self.import_source.setText(filename)
            self._save_import_source(filename)

    def _save_import_source(self, source: str) -> None:
        try:
            self.settings.set("js8spotter_import_db_path", source)
            if hasattr(self.settings, "save"):
                self.settings.save()
        except Exception:
            pass

    def _preview_import(self) -> None:
        source = _text(self.import_source.text())
        if not source:
            self.imports_state.setText("Choose a JS8Spotter database first.")
            return
        self._save_import_source(source)
        try:
            preview = preview_js8spotter_import(source, limit_per_table=5000)
        except Exception as exc:
            self.imports_state.setText(f"Preview failed: {exc}")
            return
        self._import_preview = preview
        warnings = f" Warnings: {'; '.join(preview.warnings[:2])}" if preview.warnings else ""
        self.imports_state.setText(
            f"Preview: {preview.candidates} candidates — forms {preview.forms}, Expect {preview.expect}, "
            f"watches {getattr(preview, 'watches', 0)}, archive {preview.archive}; "
            f"duplicates {preview.duplicates}, skipped {preview.skipped}, conflicts {preview.conflicts}." + warnings
        )
        self._refresh_import_action_state()

    def _apply_import(self) -> None:
        source = _text(self.import_source.text())
        if not source:
            self.imports_state.setText("Choose and preview a JS8Spotter database first.")
            return
        preview = getattr(self, "_import_preview", None)
        if preview is None or _text(getattr(preview, "source_db", "")) != str(Path(source).expanduser()):
            self._preview_import()
            self.imports_state.setText("Preview is ready. Review the bounded counts and warnings, then choose Import reviewed data…")
            return
        if preview is None or getattr(preview, "warnings", ()):
            self.imports_state.setText("Import was not started; resolve preview warnings first.")
            return
        answer = QMessageBox.question(self, "Import JS8Spotter database", f"Import {preview.candidates} previewed records from\n{source}?\n\nThis is an explicit station-data change.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            self.imports_state.setText("Import cancelled.")
            return
        try:
            stats = import_js8spotter_database(source)
        except Exception as exc:
            self.imports_state.setText(f"Import failed: {exc}")
            return
        self._save_import_source(source)
        errors = f" Errors: {'; '.join(stats.errors[:2])}" if stats.errors else ""
        self.imports_state.setText(
            f"Imported: forms {stats.forms_imported}/{stats.forms_scanned}, "
            f"Expect {stats.expect_imported}/{stats.expect_scanned}, "
            f"watches {stats.watches_imported}/{stats.watches_scanned}, "
            f"archive {stats.archive_imported}/{stats.archive_scanned}." + errors
        )
