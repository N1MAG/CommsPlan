"""Compact, widget-only editor for one SOP action draft.

The dialog owns a private payload copy. Its caller decides whether and how to
persist the accepted result.
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.operating_group_frequency import normalize_frequency_hz, resolve_operating_group_frequency
from freqinout.gui.current_page_stack import CurrentPageStack
from freqinout.gui.theme import active_app_theme, apply_text_size_accessibility_guards, button_style, label_style


_STEPS = ("What", "When", "Details")


class SopActionEditorDialog(QDialog):
    """Edit one action in three short pages, without persistence or discovery."""

    def __init__(self, parent: QWidget | None, payload: dict, options: dict):
        super().__init__(parent)
        self.setWindowTitle("Edit SOP action" if int((payload or {}).get("id") or 0) > 0 else "Add SOP action")
        self.setModal(True)
        self._payload = dict(payload or {})
        self._options = dict(options or {})
        self._category = str(
            self._options.get("category")
            or ("HF" if self._options.get("band") or self._options.get("band_frequency") else "LOCAL")
        ).strip().upper()
        self._step = 0
        self._field_widgets: dict[str, QWidget] = {}

        theme = active_app_theme()
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        heading = QLabel("Build one procedure action")
        heading.setStyleSheet(label_style("text", theme, weight=700))
        root.addWidget(heading)
        self.step_buttons: list[QPushButton] = []
        step_row = QHBoxLayout()
        for index, name in enumerate(_STEPS):
            button = QPushButton(f"{index + 1}. {name}")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, page=index: self._show_step(page))
            self.step_buttons.append(button)
            step_row.addWidget(button)
        root.addLayout(step_row)

        self.pages = CurrentPageStack(self)
        root.addWidget(self.pages, 1)
        self._build_what_page()
        self._build_when_page()
        self._build_details_page()

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("sopActionEditorSummary")
        self.summary_label.setStyleSheet(label_style("muted", theme))
        root.addWidget(self.summary_label)
        self.error_label = QLabel("")
        self.error_label.setObjectName("sopActionEditorError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(label_style("warning", theme, weight=600))
        root.addWidget(self.error_label)

        footer = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(lambda: self._show_step(max(0, self._step - 1)))
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(lambda: self._show_step(min(len(_STEPS) - 1, self._step + 1)))
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.use_button = QPushButton("Use action")
        self.use_button.clicked.connect(self._accept_validated)
        footer.addWidget(self.back_button)
        footer.addWidget(self.next_button)
        footer.addStretch(1)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.use_button)
        root.addLayout(footer)

        self.back_button.setStyleSheet(button_style("muted", theme))
        self.next_button.setStyleSheet(button_style("secondary", theme))
        self.cancel_button.setStyleSheet(button_style("muted", theme))
        self.use_button.setStyleSheet(button_style("primary", theme))
        self._show_step(0)
        self._refresh_summary()
        apply_text_size_accessibility_guards(self, include_widths=False)
        self.resize(720, 480)

    @staticmethod
    def _items(values: Iterable[Any], *, pair_values: bool = False) -> list[tuple[str, Any]]:
        result: list[tuple[str, Any]] = []
        for item in values or ():
            if pair_values and isinstance(item, (tuple, list)) and len(item) >= 2:
                value, label = item[0], item[1]
                result.append((str(label), value))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                value, label = item[0], item[1]
                result.append((str(label), value))
            else:
                text = str(item)
                result.append((text, text))
        return result

    def _combo(self, field: str, values: Iterable[Any], *, editable: bool = True, pairs: bool = False) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(editable)
        if combo.lineEdit() is not None:
            combo.lineEdit().setReadOnly(not editable)
        options = self._items(values, pair_values=pairs)
        selected = self._payload.get(field, "")
        has_value = False
        for label, value in options:
            combo.addItem(label, value)
            if str(value) == str(selected):
                has_value = True
        if selected not in (None, "") and not has_value:
            display = str(selected)
            if field == "action_key":
                display = str(self._payload.get("action_label") or display)
            combo.addItem(display, selected)
        if selected not in (None, ""):
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        self._field_widgets[field] = combo
        combo.currentTextChanged.connect(self._refresh_summary)
        combo.currentIndexChanged.connect(self._refresh_summary)
        if combo.isEditable() and combo.lineEdit() is not None:
            combo.lineEdit().textChanged.connect(self._refresh_summary)
        return combo

    def _line(self, field: str, *, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit(str(self._payload.get(field) or ""))
        edit.setPlaceholderText(placeholder)
        self._field_widgets[field] = edit
        edit.textChanged.connect(self._refresh_summary)
        return edit

    def _spin(self, field: str, *, minimum: int, maximum: int, default: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        try:
            value = int(self._payload.get(field, default) or default)
        except (TypeError, ValueError):
            value = default
        if value > spin.maximum():
            spin.setMaximum(value)
        spin.setValue(value)
        self._field_widgets[field] = spin
        spin.valueChanged.connect(self._refresh_summary)
        return spin

    @staticmethod
    def _page() -> tuple[QWidget, QGridLayout]:
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 8, 0, 4)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        return page, grid

    @staticmethod
    def _add(grid: QGridLayout, row: int, col: int, label: str, widget: QWidget) -> None:
        cell = QWidget()
        form = QFormLayout(cell)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form.addRow(label, widget)
        grid.addWidget(cell, row, col)

    def _build_what_page(self) -> None:
        page, grid = self._page()
        self._add(grid, 0, 0, "Operating group", self._combo("group_name", self._options.get("group_name", ())))
        self._add(grid, 0, 1, "Resource / tool", self._combo("software", self._options.get("software", ())))
        self._add(grid, 1, 0, "Mode / route", self._combo("mode", self._options.get("mode", ())))
        self._add(grid, 1, 1, "Action", self._combo("action_key", self._options.get("action_key", ()), pairs=True))
        self.band_frequency_controls = self._build_band_frequency_controls()
        self._add(grid, 2, 0, "Band · frequency", self.band_frequency_controls)
        self.band_frequency_controls.setVisible(self._category == "HF")
        self.band_frequency_controls.parentWidget().setVisible(self._category == "HF")
        self.description_edit = self._line("description", placeholder="What should the operator do?")
        self._add(grid, 2, 1, "Procedure", self.description_edit)
        assigned_picker = self._category == "LOCAL" and "local_frequency_profiles" in self._options
        self.frequency_reference_combo = QComboBox(page)
        self.frequency_reference_combo.setObjectName("localFrequencyReference")
        self.frequency_reference_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.frequency_reference_combo.setMinimumContentsLength(20)
        self.frequency_reference_combo.currentTextChanged.connect(self.frequency_reference_combo.setToolTip)
        self._add(grid, 3, 0, "Assigned frequency", self.frequency_reference_combo)
        self.frequency_reference_combo.parentWidget().layout().setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.frequency_reference_combo.parentWidget().setVisible(assigned_picker)
        self.frequency_reference_btn = QPushButton("Insert frequency reference" if assigned_picker else "Insert frequency reference…", page)
        self.frequency_reference_btn.setToolTip("Insert a programming-details snapshot into the procedure. Catalog changes do not change this snapshot.")
        self.frequency_reference_btn.setVisible(self._category == "LOCAL" and (assigned_picker or callable(self._options.get("choose_frequency_reference"))))
        self.frequency_reference_btn.clicked.connect(self._insert_frequency_reference)
        grid.addWidget(self.frequency_reference_btn, 3, 1)
        self.frequency_reference_hint = QLabel("No assigned frequencies. Configure them in Settings > Local Comms Groups.", page)
        self.frequency_reference_hint.setWordWrap(True)
        self.frequency_reference_hint.setStyleSheet(label_style("muted", active_app_theme()))
        grid.addWidget(self.frequency_reference_hint, 4, 0)
        self.browse_frequency_btn = QPushButton("Browse catalog…", page)
        self.browse_frequency_btn.setVisible(assigned_picker and callable(self._options.get("choose_frequency_reference")))
        self.browse_frequency_btn.clicked.connect(self._browse_frequency_reference)
        grid.addWidget(self.browse_frequency_btn, 4, 1)
        grid.setRowStretch(5, 1)
        self.pages.addWidget(page)
        self._what_grid = grid
        self._refresh_dependent_choices()
        self._field_widgets["group_name"].currentTextChanged.connect(self._refresh_dependent_choices)
        self._field_widgets["group_name"].currentTextChanged.connect(self._refresh_local_frequencies)
        self._refresh_local_frequencies()
        self._field_widgets["software"].currentTextChanged.connect(self._on_software_changed)
        self._field_widgets["band"].currentIndexChanged.connect(self._on_band_changed)

    def _insert_frequency_reference(self) -> None:
        if self._category == "LOCAL" and "local_frequency_profiles" in self._options:
            resource = self._local_frequency_resources.get(self.frequency_reference_combo.currentData())
            self._append_frequency_reference(resource)
            return
        self._browse_frequency_reference()

    def _browse_frequency_reference(self) -> None:
        chooser = self._options.get("choose_frequency_reference")
        resource = chooser(self) if callable(chooser) else None
        self._append_frequency_reference(resource)

    def _append_frequency_reference(self, resource: Any) -> None:
        from freqinout.gui.resource_picker import frequency_programming_text
        if resource is not None:
            text = frequency_programming_text(resource)
            current = self.description_edit.text().strip()
            if text not in current:
                self.description_edit.setText(f"{current} | {text}" if current else text)
                self.description_edit.setCursorPosition(0)

    def _refresh_local_frequencies(self, *_args: Any) -> None:
        from freqinout.core.local_group_frequency import snapshot_for_profile
        from freqinout.gui.resource_picker import frequency_where_text

        enabled = self._category == "LOCAL" and "local_frequency_profiles" in self._options
        self._local_frequency_resources = {}
        self.frequency_reference_combo.clear()
        group = self._field_widgets["group_name"].currentText().strip().casefold()
        if enabled and group:
            for profile in self._options.get("local_frequency_profiles", ()):
                if not isinstance(profile, dict) or str(profile.get("group") or "").strip().casefold() != group:
                    continue
                resource = snapshot_for_profile(profile)
                if resource is None:
                    continue
                key = resource.frequency_resource_key
                if key in self._local_frequency_resources:
                    continue
                self._local_frequency_resources[key] = resource
                self.frequency_reference_combo.addItem(f"{resource.label} · {frequency_where_text(resource)}", key)
        has_frequencies = bool(self._local_frequency_resources)
        self.frequency_reference_hint.setVisible(enabled and not has_frequencies)
        if enabled:
            self.frequency_reference_combo.setEnabled(has_frequencies)
            self.frequency_reference_btn.setEnabled(has_frequencies)

    def _build_band_frequency_controls(self) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        band = self._combo("band", self._options.get("band", ()))
        frequency = self._combo("frequency", self._options.get("frequency", ()))
        for item in self._options.get("band_frequency", ()) or ():
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            self._append_combo_value(band, str(item[0]))
            self._append_combo_value(frequency, str(item[1]))
        self._select_combo_value(band, str(self._payload.get("band") or ""))
        self._select_combo_value(frequency, str(self._payload.get("frequency") or ""))
        row.addWidget(band, 1)
        row.addWidget(frequency, 2)
        return container

    @staticmethod
    def _append_combo_value(combo: QComboBox, value: str) -> None:
        if value and combo.findText(value) < 0:
            combo.addItem(value, value)

    @classmethod
    def _select_combo_value(cls, combo: QComboBox, value: str) -> None:
        cls._append_combo_value(combo, value)
        if value:
            combo.setCurrentIndex(max(0, combo.findData(value) if combo.findData(value) >= 0 else combo.findText(value)))

    def _build_when_page(self) -> None:
        page, grid = self._page()
        condition_levels = self._combo("condition_levels", self._options.get("condition_levels", ()))
        if self._category == "LOCAL":
            condition_levels.blockSignals(True)
            condition_levels.clear()
            condition_levels.addItem("ALL", "ALL")
            condition_levels.setCurrentIndex(0)
            condition_levels.setEnabled(False)
            condition_levels.blockSignals(False)
        self._add(grid, 0, 0, "Condition levels", condition_levels)
        self._add(grid, 0, 1, "Daily start (UTC)", self._line("daily_start_utc", placeholder="HH:MM"))
        duration = QComboBox()
        duration.setEditable(False)
        duration.addItem("30 minutes", 30)
        duration.addItem("60 minutes", 60)
        try:
            selected_duration = int(self._payload.get("duration_minutes") or 60)
        except (TypeError, ValueError):
            selected_duration = 60
        if selected_duration not in (30, 60):
            duration.addItem(f"{selected_duration} minutes (custom)", selected_duration)
        duration.setCurrentIndex(max(0, duration.findData(selected_duration)))
        self._field_widgets["duration_minutes"] = duration
        duration.currentIndexChanged.connect(self._refresh_summary)
        self._add(grid, 1, 0, "Duration", duration)
        self._add(grid, 1, 1, "Repeat interval (minutes)", self._spin("interval_minutes", minimum=1, maximum=10080, default=180))
        self.end_time_label = QLabel()
        self.end_time_label.setObjectName("sopActionCalculatedEnd")
        self._add(grid, 2, 0, "Calculated end (UTC)", self.end_time_label)
        grid.setRowStretch(3, 1)
        self.pages.addWidget(page)
        self._field_widgets["daily_start_utc"].textChanged.connect(self._refresh_summary)

    def _build_details_page(self) -> None:
        page, grid = self._page()
        self._add(grid, 0, 0, "Contact rule", self._combo("contact_rule", self._options.get("contact_rule", ()), pairs=True))
        self._add(grid, 0, 1, "Contact target", self._combo("contact_target", self._options.get("contact_target", ())))
        self._add(grid, 1, 0, "Interval phase (minutes)", self._spin("interval_phase_minutes", minimum=0, maximum=10079, default=0))
        self._add(grid, 1, 1, "Conflict policy", self._combo("conflict_policy", self._options.get("conflict_policy", ()), pairs=True))
        self.enabled_check = QCheckBox("Action is enabled")
        self.enabled_check.setChecked(bool(self._payload.get("enabled", True)))
        self._field_widgets["enabled"] = self.enabled_check
        self.enabled_check.toggled.connect(self._refresh_summary)
        self._add(grid, 2, 0, "Availability", self.enabled_check)
        self.schedule_applied_check = QCheckBox("Include in schedule")
        self.schedule_applied_check.setChecked(bool(self._payload.get("schedule_applied", True)))
        self._field_widgets["schedule_applied"] = self.schedule_applied_check
        self.schedule_applied_check.toggled.connect(self._refresh_summary)
        self._add(grid, 2, 1, "Schedule", self.schedule_applied_check)
        grid.setRowStretch(3, 1)
        self.pages.addWidget(page)

    def _dependent_values(self) -> dict[str, Any]:
        mapping = self._options.get("dependent_options", {})
        group_combo = self._field_widgets.get("group_name")
        software_combo = self._field_widgets.get("software")
        group = group_combo.currentText() if isinstance(group_combo, QComboBox) else ""
        software = software_combo.currentText() if isinstance(software_combo, QComboBox) else ""
        group_map = mapping.get(group, {}) if isinstance(mapping, dict) else {}
        return group_map.get(software, {}) if isinstance(group_map, dict) and isinstance(group_map.get(software, {}), dict) else {}

    def _refresh_dependent_choices(self, *_args: Any) -> None:
        if self._category == "LOCAL" and "local_group_resources" in self._options:
            group_combo = self._field_widgets.get("group_name")
            software_combo = self._field_widgets.get("software")
            if isinstance(group_combo, QComboBox) and isinstance(software_combo, QComboBox):
                current = self._combo_value(software_combo)
                resources = self._options["local_group_resources"].get(group_combo.currentText().strip().upper(), [])
                software_combo.blockSignals(True)
                software_combo.clear()
                for resource in resources:
                    software_combo.addItem(resource, resource)
                if current and software_combo.findData(current) < 0:
                    software_combo.addItem(str(current), current)
                if current:
                    software_combo.setCurrentIndex(software_combo.findData(current))
                software_combo.blockSignals(False)
        dependent = self._dependent_values()
        for field in ("mode", "action_key", "condition_levels", "contact_target"):
            combo = self._field_widgets.get(field)
            if not isinstance(combo, QComboBox) or field not in dependent:
                continue
            current = self._combo_value(combo)
            typed_action_text = (
                combo.currentText()
                if field == "action_key"
                and combo.isEditable()
                and combo.currentText() != combo.itemText(combo.currentIndex())
                else ""
            )
            values = self._items(dependent.get(field, ()), pair_values=field == "action_key")
            combo.blockSignals(True)
            combo.clear()
            for label, value in values:
                combo.addItem(label, value)
            if current not in (None, "") and combo.findData(current) < 0:
                display = str(current)
                if field == "action_key":
                    display = typed_action_text or str(self._payload.get("action_label") or display)
                combo.addItem(display, current)
            if current not in (None, ""):
                idx = combo.findData(current)
                combo.setCurrentIndex(idx if idx >= 0 else combo.findText(str(current)))
            combo.blockSignals(False)
        if "band_frequency" in dependent:
            pairs = [
                (str(item[0]), str(item[1]))
                for item in dependent.get("band_frequency", ()) or ()
                if isinstance(item, (tuple, list)) and len(item) >= 2
            ]
            for field, position in (("band", 0), ("frequency", 1)):
                combo = self._field_widgets.get(field)
                if not isinstance(combo, QComboBox):
                    continue
                current = combo.currentText()
                available_pairs = pairs
                if field == "frequency":
                    band_combo = self._field_widgets.get("band")
                    selected_band = band_combo.currentText() if isinstance(band_combo, QComboBox) else ""
                    matching = [pair for pair in pairs if pair[0] == selected_band]
                    if matching:
                        available_pairs = matching
                combo.blockSignals(True)
                combo.clear()
                values = list(dict.fromkeys(pair[position] for pair in available_pairs))
                combo.addItems(values)
                self._select_combo_value(combo, current)
                combo.blockSignals(False)
        self._refresh_summary()

    def _on_band_changed(self, *_args: Any) -> None:
        pairs = self._dependent_values().get("band_frequency", ())
        band = self._field_widgets["band"].currentText()
        frequencies = [str(pair[1]) for pair in pairs if len(pair) >= 2 and str(pair[0]) == band]
        if frequencies:
            combo = self._field_widgets["frequency"]
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(list(dict.fromkeys(frequencies)))
            combo.blockSignals(False)
        self._refresh_summary()

    def _on_software_changed(self, *_args: Any) -> None:
        # Actions belong to their resource/tool. Changing that explicit choice
        # clears the old action so it cannot be saved as a stale combination.
        action = self._field_widgets.get("action_key")
        if isinstance(action, QComboBox):
            action.setCurrentIndex(-1)
            if action.isEditable() and action.lineEdit() is not None:
                action.lineEdit().clear()
        self._refresh_dependent_choices()
        if isinstance(action, QComboBox):
            action.setCurrentIndex(-1)
            if action.isEditable() and action.lineEdit() is not None:
                action.lineEdit().clear()

    @staticmethod
    def _combo_value(combo: QComboBox) -> Any:
        """Return typed text instead of a stale currentData value."""
        if combo.isEditable() and combo.currentText() != combo.itemText(combo.currentIndex()):
            return combo.currentText().strip()
        value = combo.currentData()
        return value if value is not None else combo.currentText().strip()

    def _show_step(self, index: int) -> None:
        self._step = max(0, min(len(_STEPS) - 1, int(index)))
        self.pages.setCurrentIndex(self._step)
        for position, button in enumerate(self.step_buttons):
            button.setChecked(position == self._step)
        self.back_button.setEnabled(self._step > 0)
        self.next_button.setEnabled(self._step < len(_STEPS) - 1)

    def _read_values(self) -> dict[str, Any]:
        result = dict(self._payload)
        for field, widget in self._field_widgets.items():
            if field == "duration_minutes" and isinstance(widget, QComboBox):
                try:
                    result[field] = int(widget.currentData())
                except (TypeError, ValueError):
                    result[field] = 0
            elif isinstance(widget, QComboBox):
                value = self._combo_value(widget)
                result[field] = value
                if field == "action_key":
                    typed = widget.isEditable() and widget.currentText() != widget.itemText(widget.currentIndex())
                    result["action_label"] = widget.currentText().strip() if not typed else str(value)
            elif isinstance(widget, QLineEdit):
                result[field] = widget.text().strip()
            elif isinstance(widget, QSpinBox):
                result[field] = int(widget.value())
            elif isinstance(widget, QCheckBox):
                result[field] = bool(widget.isChecked())
        try:
            result["interval_hours"] = max(1, (int(result.get("interval_minutes") or 0) + 59) // 60)
        except (TypeError, ValueError):
            pass
        if self._category == "LOCAL":
            result["condition_levels"] = "ALL"
            result["band"] = ""
            result["frequency"] = ""
            result["operating_group_frequency_key"] = ""
        try:
            start = str(result.get("daily_start_utc") or "00:00")
            duration = int(result.get("duration_minutes") or 0)
            result["daily_end_utc"] = self._end_time(start, duration)
        except (ValueError, TypeError):
            pass
        return result

    @staticmethod
    def _end_time(start: str, duration: int) -> str:
        raw = str(start or "").strip()
        pieces = raw.split(":")
        if len(pieces) != 2 or any(len(piece) != 2 or not piece.isdigit() for piece in pieces):
            raise ValueError("Daily start must use HH:MM in UTC.")
        hour, minute = (int(piece) for piece in pieces)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Daily start must use HH:MM in UTC.")
        if int(duration) not in (30, 60):
            raise ValueError("Duration must be 30 or 60 minutes.")
        total = min((24 * 60) - 1, (hour * 60 + minute + int(duration)))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _refresh_summary(self, *_args: Any) -> None:
        if not hasattr(self, "summary_label") or not hasattr(self, "end_time_label"):
            return
        values = self._read_values()
        start = str(values.get("daily_start_utc") or "")
        duration = int(values.get("duration_minutes") or 0)
        try:
            end = self._end_time(start, duration)
            self.end_time_label.setText(end)
            timing = f"{start}–{end} UTC · every {values.get('interval_minutes', '')} min"
        except (ValueError, TypeError):
            self.end_time_label.setText("Enter a valid HH:MM start")
            timing = "Timing needs review"
        group = str(values.get("group_name") or "Group not set")
        action = str(values.get("action_label") or values.get("action_key") or "Action not set")
        self.summary_label.setText(f"{group} · {action} · {timing}")

    def _accept_validated(self) -> None:
        values = self._read_values()
        try:
            for field, label in (("group_name", "Operating group"), ("software", "Resource or tool"), ("action_key", "Action")):
                if not str(values.get(field) or "").strip():
                    raise ValueError(f"Choose {label.lower()} before using this action.")
            if self._category != "LOCAL" and (
                not str(values.get("band") or "").strip()
                or not str(values.get("frequency") or "").strip()
            ):
                raise ValueError("Choose both a band and frequency for an HF action.")
            if self._category != "LOCAL" and "operating_groups" in self._options:
                original_identity = (
                    str(self._payload.get("group_name") or "").strip().upper(),
                    str(self._payload.get("band") or "").strip().upper(),
                    str(self._payload.get("mode") or "").strip().upper(),
                    normalize_frequency_hz(self._payload.get("frequency")),
                )
                current_identity = (
                    str(values.get("group_name") or "").strip().upper(),
                    str(values.get("band") or "").strip().upper(),
                    str(values.get("mode") or "").strip().upper(),
                    normalize_frequency_hz(values.get("frequency")),
                )
                supplied_key = (
                    str(self._payload.get("operating_group_frequency_key") or "").strip()
                    if original_identity == current_identity else ""
                )
                resolution = resolve_operating_group_frequency(
                    self._options.get("operating_groups", ()),
                    group=values.get("group_name"),
                    frequency_key=supplied_key,
                    band=values.get("band"),
                    mode=values.get("mode"),
                    frequency=values.get("frequency"),
                )
                if not resolution.resolved or resolution.row is None:
                    raise ValueError(resolution.reason or "Choose a configured group frequency.")
                values["operating_group_frequency_key"] = str(
                    resolution.row.get("operating_group_frequency_key") or ""
                )
            start = str(values.get("daily_start_utc") or "").strip()
            duration = int(values.get("duration_minutes") or 0)
            interval = int(values.get("interval_minutes") or 0)
            self._end_time(start, duration)
            if duration not in (30, 60):
                raise ValueError("Duration must be 30 or 60 minutes.")
            if interval <= 0:
                raise ValueError("Repeat interval must be greater than zero.")
        except (ValueError, TypeError) as exc:
            self.error_label.setText(str(exc))
            return
        self._result_payload = values
        self.error_label.clear()
        self.accept()

    def payload(self) -> dict:
        """Return the accepted action copy, preserving fields this editor did not touch."""
        return dict(getattr(self, "_result_payload", self._payload))
