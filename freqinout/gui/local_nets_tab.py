"""Reminder-only Local Nets workspace.

This view deliberately has no scheduler, radio, QSY, or SOP activation
dependency.  It is a bounded presentation/controller over the Local Nets and
canonical resource stores; schema ownership remains with application startup.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy, QAbstractButton,
)

from freqinout.core.known_operating_groups import net_resources_db_path
from freqinout.core.local_net_models import LocalNetSchedule
from freqinout.core.system_timezone import normalize_supported_timezone_name
from freqinout.core.navigation_intent import NavigationIntent
from freqinout.core.local_net_recurrence import project_occurrences
from freqinout.core.local_net_service import (
    new_local_net_schedule_key, resource_statuses, schedule_from_directory_session,
    schedule_with_accepted_frequency,
)
from freqinout.core.local_net_store import LocalNetStore, MAX_LOCAL_NET_SCHEDULES
from freqinout.core.operating_group_identity import operating_group_options
from freqinout.core.resource_catalog_models import FrequencyResource
from freqinout.core.resource_catalog_store import ResourceCatalogStore, STATION_MANUAL_SOURCE_KEY
from freqinout.gui.resource_picker import choose_frequency_resource, frequency_where_text, session_when_text
from freqinout.gui.help_registry import resolve_help_host
from freqinout.gui.theme import (
    label_style, resolve_theme, button_style, font_derived_widget_height,
    fit_combo_box_to_contents,
)


REMINDER_COPY = "Reminder only — CommsPlan will not tune a radio"


def _groups(settings: object) -> tuple[tuple[str, str], ...]:
    try:
        rows = settings.get("operating_groups", []) if settings is not None else []
        return tuple((item.operating_group_key, item.group_name) for item in operating_group_options(rows))
    except Exception:
        return ()


def _when(value: object) -> str:
    if value is None:
        return "—"
    try:
        local = value.local_start
        return f"{local.strftime('%a %b')} {local.day}, {local.strftime('%H:%M %Z')}"
    except Exception:
        return str(value)


def _summary_occurrence(value: object) -> str:
    if value is None:
        return "—"
    return f"{getattr(value, 'name', 'Local Net')} · {_when(value)}"


class LocalNetEditorDialog(QDialog):
    """Single scrollable conceptual editor; Cancel never writes a draft."""

    HANDOFF_RESULT = 2
    handoff_requested = Signal(object)

    def __init__(self, local_store: LocalNetStore, catalog: ResourceCatalogStore, settings: object,
                 schedule: LocalNetSchedule | None = None, parent: QWidget | None = None,
                 *, draft_snapshot: dict[str, object] | None = None) -> None:
        super().__init__(parent)
        self.local_store, self.catalog, self.settings = local_store, catalog, settings
        self.schedule = schedule
        self._selected_frequency: FrequencyResource | None = None
        self.setWindowTitle("Edit Local Net" if schedule else "Add Local Net")
        # Resolve local entry once; timing and geometry consume the loaded draft.
        configured_timezone = settings.get("timezone", "UTC") if settings is not None else "UTC"
        self._local_timezone = normalize_supported_timezone_name(configured_timezone) or "UTC"
        try:
            ZoneInfo(self._local_timezone)
        except (ValueError, KeyError):
            self._local_timezone = "UTC"
        self._timezone_cache = {name: ZoneInfo(name) for name in ("UTC", self._local_timezone)}
        self._confirmed_timezones = set(self._timezone_cache)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.timeout.connect(self._fit_editor_geometry)
        self.resize(760, 680)
        root = QVBoxLayout(self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(self.scroll)
        self._editor_body = body
        self.scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        layout = QVBoxLayout(body)
        layout.setAlignment(Qt.AlignTop)
        gap = max(1, self.fontMetrics().horizontalAdvance(" "))
        layout.setContentsMargins(gap * 2, gap * 2, gap * 2, gap * 2)
        layout.setSpacing(gap * 2)
        self.warning = QLabel(REMINDER_COPY, self)
        self.warning.setObjectName("localNetsReminderOnly")
        self.warning.setWordWrap(True)
        root.addWidget(self.warning)

        def form_for(title):
            box = QGroupBox(title, body)
            form = QFormLayout(box)
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            layout.addWidget(box)
            return box, form

        def action_row(*controls):
            holder = QWidget(body)
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            for control in controls:
                row.addWidget(control)
            row.addStretch()
            return holder

        net_box, net_form = form_for("Net and group")
        self.name_edit = QLineEdit(net_box)
        self.name_edit.setPlaceholderText("Net name")
        self.service_combo = QComboBox(net_box)
        self.service_combo.addItems(["AMATEUR", "GMRS"])
        self.service_combo.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.group_combo = QComboBox(net_box)
        self.group_combo.addItem("Community / Unassigned", (None, None))
        for key, name in _groups(settings):
            self.group_combo.addItem(name, (key, name))
        self.group_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.group_combo.setProperty("fio_no_auto_fit", True)
        self.group_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.group_combo.setMinimumContentsLength(18)
        self.manage_groups_btn = QPushButton("Manage groups…", net_box)
        self.manage_groups_btn.setToolTip("Manage Operating Groups and return to this draft.")
        net_form.addRow("Name", self.name_edit)
        net_form.addRow("Service", self.service_combo)
        net_form.addRow("Operating group", self.group_combo)
        self.frequency_label = QLabel("No frequency resource selected", net_box)
        self.frequency_label.setWordWrap(True)
        net_form.addRow("Frequency", self.frequency_label)
        self.choose_frequency_btn = QPushButton("Choose frequency…", net_box)
        self.manage_frequency_btn = QPushButton("Frequency catalog…", net_box)
        self.manage_frequency_btn.setToolTip("Manage Frequency Catalog and return to this draft.")
        net_form.addRow("", action_row(self.choose_frequency_btn, self.manage_frequency_btn, self.manage_groups_btn))

        self.directory_toggle = QCheckBox("Use a known net meeting", body)
        layout.addWidget(self.directory_toggle)
        self.directory_panel = QWidget(body)
        directory_form = QFormLayout(self.directory_panel)
        directory_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.session_combo = QComboBox(self.directory_panel)
        self.session_combo.setProperty("fio_no_auto_fit", True)
        self.session_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.session_combo.setMinimumContentsLength(18)
        self.session_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.session_combo.addItem("Custom local reminder", None)
        sessions = self.catalog.list_sessions(active=True, limit=200)
        entries = self.catalog.net_entries_by_keys(session.net_entry_key for session in sessions)
        for session in sessions:
            entry = entries.get(session.net_entry_key)
            name = entry.name if entry is not None else session.net_entry_key
            self.session_combo.addItem(f"{name} · {session_when_text(session)}", session.net_session_key)
        self.use_session_btn = QPushButton("Use selected meeting", self.directory_panel)
        directory_form.addRow("Known net meeting", self.session_combo)
        directory_form.addRow("", action_row(self.use_session_btn))
        layout.addWidget(self.directory_panel)
        self.directory_toggle.toggled.connect(self.directory_panel.setVisible)
        self.directory_panel.hide()

        self.frequency_toggle = QCheckBox("Create a frequency resource", body)
        layout.addWidget(self.frequency_toggle)
        self.frequency_panel = QWidget(body)
        frequency_form = QFormLayout(self.frequency_panel)
        frequency_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.custom_kind = QComboBox(self.frequency_panel)
        self.custom_kind.addItems(["simplex", "repeater"])
        self.custom_receive_hz = QLineEdit(self.frequency_panel)
        self.custom_receive_hz.setPlaceholderText("Receive frequency in Hz")
        self.custom_transmit_hz = QLineEdit(self.frequency_panel)
        self.custom_transmit_hz.setPlaceholderText("Repeater transmit Hz; simplex uses receive")
        self.create_frequency_btn = QPushButton("Create station-private resource", self.frequency_panel)
        frequency_form.addRow("New resource type", self.custom_kind)
        frequency_form.addRow("New receive Hz", self.custom_receive_hz)
        frequency_form.addRow("New transmit Hz", self.custom_transmit_hz)
        frequency_form.addRow("", action_row(self.create_frequency_btn))
        layout.addWidget(self.frequency_panel)
        self.frequency_toggle.toggled.connect(self.frequency_panel.setVisible)
        self.frequency_panel.hide()

        when_box, self.when_form = form_for("When and reminder")
        self.recurrence = QComboBox(when_box)
        self.recurrence.addItems(["weekly", "daily", "periodic", "biweekly", "one_time"])
        self.recurrence.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.weekday_checks = [QCheckBox(name, when_box) for name in
                               ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
        self.weekdays_widget = QWidget(when_box)
        self.weekdays_widget.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        weekdays_layout = QGridLayout(self.weekdays_widget)
        weekdays_layout.setContentsMargins(0, 0, 0, 0)
        for index, checkbox in enumerate(self.weekday_checks):
            weekdays_layout.addWidget(checkbox, index // 4, index % 4)
        self.month_week_checks = [QCheckBox(str(number), when_box) for number in range(1, 6)]
        self.month_widget = QWidget(when_box)
        self.month_widget.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        month_layout = QHBoxLayout(self.month_widget)
        month_layout.setContentsMargins(0, 0, 0, 0)
        for checkbox in self.month_week_checks:
            month_layout.addWidget(checkbox)
        month_layout.addStretch()
        self.time_edit = QLineEdit(when_box)
        self.time_edit.setPlaceholderText("HH:MM")
        self.time_edit.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.time_basis_combo = QComboBox(when_box)
        self.time_basis_combo.addItem(f"Local ({self._local_timezone})", self._local_timezone)
        self.time_basis_combo.addItem("UTC", "UTC")
        self.time_basis_combo.addItem("Other time zone…", None)
        self.time_basis_combo.setProperty("fio_no_auto_fit", True)
        self.time_basis_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.timezone_edit = QLineEdit(when_box)
        self.timezone_edit.setText(self._local_timezone)
        self.timezone_edit.setPlaceholderText("IANA time zone")
        self.time_basis_hint = QLabel("Times and dates use the selected zone. Changing it keeps the clock time.", when_box)
        self.time_basis_hint.setWordWrap(True)
        self.date_edit = QLineEdit(when_box)
        self.date_edit.setPlaceholderText("YYYY-MM-DD")
        self.duration = QSpinBox(when_box)
        self.duration.setRange(1, 1440)
        self.duration.setValue(60)
        self.duration.setSuffix(" min")
        self.duration.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.reminder = QSpinBox(when_box)
        self.reminder.setRange(0, 1440)
        self.reminder.setValue(15)
        self.reminder.setSuffix(" min before")
        self.reminder.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.when_form.addRow("Recurrence", self.recurrence)
        self.when_form.addRow("Weekdays", self.weekdays_widget)
        self.when_form.addRow("Month weeks", self.month_widget)
        self.when_form.addRow("Start time", action_row(self.time_edit, self.time_basis_combo))
        self.when_form.addRow("Time zone", self.timezone_edit)
        self.when_form.addRow("", self.time_basis_hint)
        self.when_form.addRow("Date / anchor", self.date_edit)
        self.when_form.addRow("Duration", action_row(self.duration, QLabel("Reminder", when_box), self.reminder))

        self.details_toggle = QCheckBox("Date limits, SOP context and notes", body)
        layout.addWidget(self.details_toggle)
        self.details_panel = QWidget(body)
        details_form = QFormLayout(self.details_panel)
        details_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.effective_start_edit = QLineEdit(self.details_panel)
        self.effective_start_edit.setPlaceholderText("YYYY-MM-DD (optional)")
        self.effective_end_edit = QLineEdit(self.details_panel)
        self.effective_end_edit.setPlaceholderText("YYYY-MM-DD (optional)")
        self.exceptions_edit = QLineEdit(self.details_panel)
        self.exceptions_edit.setPlaceholderText("YYYY-MM-DD, YYYY-MM-DD")
        self.sop_edit = QLineEdit(self.details_panel)
        self.sop_edit.setPlaceholderText("Optional SOP ID; never activated here")
        self.notes_edit = QLineEdit(self.details_panel)
        details_form.addRow("Effective start", self.effective_start_edit)
        details_form.addRow("Effective end", self.effective_end_edit)
        details_form.addRow("Exception dates", self.exceptions_edit)
        details_form.addRow("SOP context", self.sop_edit)
        details_form.addRow("Participation notes", self.notes_edit)
        layout.addWidget(self.details_panel)
        self.details_toggle.toggled.connect(self.details_panel.setVisible)
        self.details_panel.hide()
        layout.addStretch()
        self.scroll.setWidget(body)
        root.addWidget(self.scroll, 1)
        self.preview = QLabel("Complete the timing fields to preview the next occurrence.", self)
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self.use_session_btn.clicked.connect(self._use_directory_session)
        self.choose_frequency_btn.clicked.connect(self._choose_frequency)
        self.create_frequency_btn.clicked.connect(self._create_frequency)
        self.manage_frequency_btn.clicked.connect(lambda: self._request_handoff("resources.frequency_catalog"))
        self.manage_groups_btn.clicked.connect(lambda: self._request_handoff("settings.operating_groups"))
        self.time_basis_combo.currentIndexChanged.connect(self._on_time_basis_changed)
        self.timezone_edit.textChanged.connect(self._sync_time_basis)
        self.timezone_edit.editingFinished.connect(self._confirm_timezone)
        self.recurrence.currentIndexChanged.connect(self._update_recurrence_fields)
        for widget in (self.name_edit, self.time_edit, self.timezone_edit, self.date_edit,
                       self.effective_start_edit, self.effective_end_edit, self.exceptions_edit,
                       self.sop_edit, self.notes_edit):
            widget.textChanged.connect(self._preview)
        for widget in (self.service_combo, self.group_combo, self.recurrence):
            widget.currentIndexChanged.connect(self._preview)
        for widget in (self.duration, self.reminder):
            widget.valueChanged.connect(self._preview)
        for checkbox in (*self.weekday_checks, *self.month_week_checks):
            checkbox.toggled.connect(self._preview)
        if schedule:
            self._load(schedule)
        else:
            self.time_edit.setText("19:00")
        if draft_snapshot:
            self._restore_draft_snapshot(draft_snapshot)
        self._sync_time_basis()
        self._update_recurrence_fields()
        self._preview()
        self.apply_theme()

    def _on_time_basis_changed(self, *_args) -> None:
        zone = self.time_basis_combo.currentData()
        if zone is not None:
            self.timezone_edit.setText(str(zone))
        self.when_form.setRowVisible(self.timezone_edit, zone is None)
        self._preview()

    def _sync_time_basis(self, *_args) -> None:
        zone = self.timezone_edit.text().strip()
        index = 0 if zone == self._local_timezone else 1 if zone == "UTC" else 2
        self.time_basis_combo.blockSignals(True)
        self.time_basis_combo.setCurrentIndex(index)
        self.time_basis_combo.blockSignals(False)
        self.when_form.setRowVisible(self.timezone_edit, index == 2)

    def _confirm_timezone(self) -> bool:
        zone = self.timezone_edit.text().strip()
        try:
            self._timezone_cache[zone] = ZoneInfo(zone)
        except (ValueError, KeyError) as exc:
            self.preview.setText(f"Review time zone: {exc}")
            return False
        self._confirmed_timezones.add(zone)
        self._preview()
        return True

    def _update_recurrence_fields(self, *_args) -> None:
        recurrence = self.recurrence.currentText()
        self.when_form.setRowVisible(self.weekdays_widget, recurrence in {"weekly", "periodic", "biweekly"})
        self.when_form.setRowVisible(self.month_widget, recurrence == "periodic")
        self.when_form.setRowVisible(self.date_edit, recurrence in {"biweekly", "one_time"})
        label = self.when_form.labelForField(self.date_edit)
        if label is not None:
            label.setText("Date" if recurrence == "one_time" else "Anchor date")

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange):
            if hasattr(self, "_geometry_timer"):
                self._geometry_timer.start(0)

    def _fit_editor_geometry(self) -> None:
        self._editor_body.setMaximumWidth(self.fontMetrics().horizontalAdvance("M" * 76))
        self.preview.setMaximumWidth(self.fontMetrics().horizontalAdvance("M" * 76))
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (QLineEdit, QComboBox, QSpinBox, QAbstractButton)):
                widget.setMinimumHeight(font_derived_widget_height(widget))
                if isinstance(widget, QComboBox):
                    fit_combo_box_to_contents(widget)

    def apply_theme(self) -> None:
        theme = resolve_theme(self.settings or {})
        self.warning.setStyleSheet(label_style("warning", theme, weight=700))
        for button in self.findChildren(QPushButton):
            button.setStyleSheet(button_style("muted", theme))
            button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.buttons.button(QDialogButtonBox.Save).setStyleSheet(button_style("eligible_success", theme))
        self._geometry_timer.start(0)

    def _draft_snapshot(self) -> dict[str, object]:
        """Capture incomplete form state without requiring schedule validation."""
        group_key, group_name = self.group_combo.currentData()
        return {
            "name": self.name_edit.text(),
            "service": self.service_combo.currentText(),
            "session_key": self.session_combo.currentData(),
            "group_key": group_key,
            "group_name": group_name,
            "frequency_key": self._selected_frequency.frequency_resource_key if self._selected_frequency else None,
            "recurrence": self.recurrence.currentText(),
            "weekdays": tuple(index for index, box in enumerate(self.weekday_checks) if box.isChecked()),
            "month_weeks": tuple(index for index, box in enumerate(self.month_week_checks, start=1) if box.isChecked()),
            "start_time": self.time_edit.text(),
            "timezone": self.timezone_edit.text(),
            "date_anchor": self.date_edit.text(),
            "effective_start": self.effective_start_edit.text(),
            "effective_end": self.effective_end_edit.text(),
            "exceptions": self.exceptions_edit.text(),
            "duration": self.duration.value(),
            "reminder": self.reminder.value(),
            "sop_id": self.sop_edit.text(),
            "notes": self.notes_edit.text(),
            "custom_kind": self.custom_kind.currentText(),
            "custom_receive_hz": self.custom_receive_hz.text(),
            "custom_transmit_hz": self.custom_transmit_hz.text(),
            "time_basis": self.time_basis_combo.currentIndex(),
            "directory_open": self.directory_toggle.isChecked(),
            "frequency_open": self.frequency_toggle.isChecked(),
            "details_open": self.details_toggle.isChecked(),
        }

    def _restore_draft_snapshot(self, snapshot: dict[str, object]) -> None:
        def text(widget: QLineEdit, name: str) -> None:
            widget.setText(str(snapshot.get(name) or ""))

        text(self.name_edit, "name"); self.service_combo.setCurrentText(str(snapshot.get("service") or "AMATEUR"))
        session_index = self.session_combo.findData(snapshot.get("session_key")); self.session_combo.setCurrentIndex(max(0, session_index))
        group_index = self.group_combo.findData((snapshot.get("group_key"), snapshot.get("group_name")))
        if group_index < 0 and snapshot.get("group_key"):
            for index in range(self.group_combo.count()):
                if self.group_combo.itemData(index)[0] == snapshot.get("group_key"):
                    group_index = index; break
        self.group_combo.setCurrentIndex(max(0, group_index))
        frequency_key = str(snapshot.get("frequency_key") or "")
        self._selected_frequency = self.catalog.get_frequency(frequency_key) if frequency_key else None
        self._set_frequency_label()
        self.recurrence.setCurrentText(str(snapshot.get("recurrence") or "weekly"))
        weekdays = set(snapshot.get("weekdays") or ()); month_weeks = set(snapshot.get("month_weeks") or ())
        for index, box in enumerate(self.weekday_checks): box.setChecked(index in weekdays)
        for index, box in enumerate(self.month_week_checks, start=1): box.setChecked(index in month_weeks)
        text(self.time_edit, "start_time"); text(self.timezone_edit, "timezone"); text(self.date_edit, "date_anchor")
        text(self.effective_start_edit, "effective_start"); text(self.effective_end_edit, "effective_end"); text(self.exceptions_edit, "exceptions")
        self.duration.setValue(int(snapshot.get("duration") or 60)); self.reminder.setValue(int(snapshot.get("reminder") or 15))
        text(self.sop_edit, "sop_id"); text(self.notes_edit, "notes")
        self.custom_kind.setCurrentText(str(snapshot.get("custom_kind") or "simplex"))
        text(self.custom_receive_hz, "custom_receive_hz"); text(self.custom_transmit_hz, "custom_transmit_hz")
        self.directory_toggle.setChecked(bool(snapshot.get("directory_open") or snapshot.get("session_key")))
        self.frequency_toggle.setChecked(bool(snapshot.get("frequency_open") or snapshot.get("custom_receive_hz") or snapshot.get("custom_transmit_hz")))
        self.details_toggle.setChecked(bool(snapshot.get("details_open") or any(snapshot.get(key) for key in ("effective_start", "effective_end", "exceptions", "sop_id", "notes"))))
        self.time_basis_combo.setCurrentIndex(int(snapshot.get("time_basis", self.time_basis_combo.currentIndex())))
        self._confirm_timezone()

    def _request_handoff(self, destination_route: str) -> None:
        snapshot = self._draft_snapshot()
        intent = NavigationIntent(
            origin_surface="local_nets_editor",
            destination_route=destination_route,
            return_route="plans.local_nets",
            draft_id=f"local_net_draft_{uuid.uuid4()}",
            directory_session_ids=(str(snapshot["session_key"]),) if snapshot.get("session_key") else (),
            frequency_resource_id=str(snapshot.get("frequency_key") or "") or None,
            group_id=str(snapshot.get("group_key") or "") or None,
            local_net_schedule_id=self.schedule.local_net_schedule_key if self.schedule else None,
            draft_snapshot=snapshot,
        )
        self.handoff_requested.emit(intent)
        self.done(self.HANDOFF_RESULT)

    def _load(self, row: LocalNetSchedule) -> None:
        try:
            self._timezone_cache[row.timezone_name] = ZoneInfo(row.timezone_name)
            self._confirmed_timezones.add(row.timezone_name)
        except (ValueError, KeyError):
            pass
        self.name_edit.setText(row.name); self.service_combo.setCurrentText(row.service); self.recurrence.setCurrentText(row.recurrence); self.time_edit.setText(row.local_start_time); self.timezone_edit.setText(row.timezone_name); self.duration.setValue(row.duration_minutes); self.reminder.setValue(row.reminder_minutes); self.sop_edit.setText(str(row.sop_id or "")); self.notes_edit.setText(row.participation_notes or "")
        for index, checkbox in enumerate(self.weekday_checks): checkbox.setChecked(index in row.weekdays)
        for index, checkbox in enumerate(self.month_week_checks, start=1): checkbox.setChecked(index in row.month_weeks)
        self.date_edit.setText(row.one_time_local_date or row.biweekly_anchor_date or "")
        self.effective_start_edit.setText(row.effective_start_date or ""); self.effective_end_edit.setText(row.effective_end_date or "")
        self.exceptions_edit.setText(", ".join(row.exception_dates))
        for index in range(self.group_combo.count()):
            if self.group_combo.itemData(index)[0] == row.operating_group_key: self.group_combo.setCurrentIndex(index); break
        if row.frequency_resource_key:
            self._selected_frequency = self.catalog.get_frequency(row.frequency_resource_key); self._set_frequency_label()
        self.details_toggle.setChecked(bool(row.effective_start_date or row.effective_end_date or row.exception_dates or row.sop_id or row.participation_notes))
        self.directory_toggle.setChecked(bool(row.net_session_key))
        if row.net_session_key:
            index = self.session_combo.findData(row.net_session_key)
            if index >= 0: self.session_combo.setCurrentIndex(index)

    def _set_frequency_label(self) -> None:
        self.frequency_label.setText(f"{self._selected_frequency.label} · {frequency_where_text(self._selected_frequency)}" if self._selected_frequency else "No frequency resource selected")

    def _use_directory_session(self) -> None:
        key = self.session_combo.currentData()
        if not key: return
        try:
            group_key, group_name = self.group_combo.currentData()
            draft = schedule_from_directory_session(self.catalog, key, schedule_key=self.schedule.local_net_schedule_key if self.schedule else None, operating_group_key=group_key, operating_group_name=group_name)
            self.schedule = draft; self._load(draft)
        except Exception as exc: QMessageBox.warning(self, "Net meeting", str(exc))

    def _choose_frequency(self) -> None:
        selected = choose_frequency_resource(self.catalog, self, service=self.service_combo.currentText())
        if selected: self._selected_frequency = selected; self._set_frequency_label(); self._preview()

    def _create_frequency(self) -> None:
        try:
            receive_hz = int(self.custom_receive_hz.text().replace(",", "").strip()); kind = self.custom_kind.currentText()
            transmit_text = self.custom_transmit_hz.text().replace(",", "").strip()
            if kind == "repeater" and not transmit_text:
                raise ValueError("Repeater resources require both receive and transmit Hz.")
            transmit_hz = int(transmit_text) if transmit_text else receive_hz
            # This is an explicit user action, not construction-time setup.
            self.catalog.ensure_station_source(STATION_MANUAL_SOURCE_KEY)
            resource = FrequencyResource(frequency_resource_key=f"frequency_local_net_{uuid.uuid4()}", source_key=STATION_MANUAL_SOURCE_KEY, resource_kind=kind, service=self.service_combo.currentText(), label=f"Local Net {receive_hz:,} Hz", receive_hz=receive_hz, transmit_hz=transmit_hz)
            self._selected_frequency = self.catalog.create_frequency(resource); self._set_frequency_label(); self._preview()
        except Exception as exc: QMessageBox.warning(self, "Create resource", str(exc))

    def _draft(self) -> LocalNetSchedule:
        recurrence = self.recurrence.currentText(); key, group = self.group_combo.currentData(); date_value = self.date_edit.text().strip() or None
        old = self.schedule
        weekdays = tuple(index for index, checkbox in enumerate(self.weekday_checks) if checkbox.isChecked())
        month_weeks = tuple(index for index, checkbox in enumerate(self.month_week_checks, start=1) if checkbox.isChecked())
        exceptions = tuple(value.strip() for value in self.exceptions_edit.text().split(",") if value.strip())
        draft = LocalNetSchedule(local_net_schedule_key=old.local_net_schedule_key if old else new_local_net_schedule_key(), name=self.name_edit.text(), service=self.service_combo.currentText(), recurrence=recurrence, local_start_time=self.time_edit.text(), timezone_name=self.timezone_edit.text(), duration_minutes=self.duration.value(), weekdays=() if recurrence in {"daily", "one_time"} else weekdays, month_weeks=month_weeks if recurrence == "periodic" else (), biweekly_anchor_date=date_value if recurrence == "biweekly" else None, one_time_local_date=date_value if recurrence == "one_time" else None, effective_start_date=self.effective_start_edit.text().strip() or None, effective_end_date=self.effective_end_edit.text().strip() or None, exception_dates=exceptions, reminder_minutes=self.reminder.value(), net_entry_key=old.net_entry_key if old else None, net_session_key=old.net_session_key if old else None, frequency_resource_key=self._selected_frequency.frequency_resource_key if self._selected_frequency else (old.frequency_resource_key if old else None), operating_group_key=key, operating_group_name=group, sop_id=self.sop_edit.text().strip() or None, participation_notes=self.notes_edit.text(), accepted_session_version_hash=old.accepted_session_version_hash if old else None, accepted_resource_version_hash=old.accepted_resource_version_hash if old else None, accepted_snapshot_json=old.accepted_snapshot_json if old else None, enabled=old.enabled if old else True)
        selected_changed = self._selected_frequency is not None and (
            old is None or old.frequency_resource_key != self._selected_frequency.frequency_resource_key
        )
        return schedule_with_accepted_frequency(draft, self._selected_frequency) if selected_changed else draft

    def _preview(self, *_args: object) -> None:
        try:
            if self.timezone_edit.text().strip() not in self._confirmed_timezones:
                self.preview.setText("Confirm the IANA time zone to preview this reminder.")
                return
            rows = project_occurrences(self._draft(), datetime.now(timezone.utc), limit=1)
            if rows:
                lines = ["Next occurrence:"]
                for item in rows:
                    local = _when(item)
                    utc = item.start_utc.strftime("%Y-%m-%d %H:%M UTC")
                    lines.append(f"• {local}  |  {utc}")
                self.preview.setText("\n".join(lines))
            else:
                self.preview.setText("No occurrence appears in the next 90 days.")
        except Exception as exc: self.preview.setText(f"Review timing: {exc}")

    def _save(self) -> None:
        if not self._confirm_timezone():
            self.timezone_edit.setFocus()
            return
        try: self.result_schedule = self.local_store.save_schedule(self._draft()); self.accept()
        except Exception as exc: QMessageBox.warning(self, "Save Local Net", str(exc))


class LocalNetsTab(QWidget):
    """Bounded Local Nets list, overview, and explicit reminder actions."""
    open_resources_requested = Signal(object)
    open_settings_requested = Signal(object)
    navigation_requested = Signal(object)
    MAX_ROWS = 200

    def __init__(self, parent: QWidget | None = None, *, settings: object | None = None, db_path: object | None = None) -> None:
        super().__init__(parent); self.settings = settings; path = db_path or net_resources_db_path(); self.store = LocalNetStore(path); self.catalog = ResourceCatalogStore(path); self._rows: tuple[LocalNetSchedule, ...] = (); self._rendered_by_key: dict[str, tuple[object, object]] = {}; self._pending_editor_intent: NavigationIntent | None = None
        self._build_ui(); self.refresh()

    def _open_context_help(self) -> None:
        """Open the Local Nets operator guide without coupling this tab to MainWindow."""
        host = resolve_help_host(self)
        if host is not None and hasattr(host, "open_context_help"):
            try:
                host.open_context_help("tab.local-nets")
            except Exception:
                pass

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self); layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("Local Nets"); title.setStyleSheet(label_style("text", resolve_theme(self.settings or {}), weight=700)); header.addWidget(title)
        header.addStretch(1)
        self.help_btn = QPushButton("Help", self); self.help_btn.setToolTip("Open Local Nets help."); self.help_btn.setAccessibleName("Open Local Nets help")
        self.help_btn.clicked.connect(self._open_context_help); header.addWidget(self.help_btn)
        layout.addLayout(header)
        copy = QLabel(REMINDER_COPY); copy.setWordWrap(True); layout.addWidget(copy)
        self.summary_label = QLabel(""); self.summary_label.setWordWrap(True); self.summary_label.setObjectName("localNetsSummary"); layout.addWidget(self.summary_label)
        filters = QWidget(self); grid = QGridLayout(filters); grid.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(filters); self.search.setPlaceholderText("Search local net, group, or frequency"); self.search.setAccessibleName("Search Local Nets")
        self.group_filter = QComboBox(filters); self.group_filter.addItem("All groups", None); self.group_filter.addItem("Community / Unassigned", "__unassigned__")
        for key, name in _groups(self.settings): self.group_filter.addItem(name, key)
        self.service_filter = QComboBox(filters); self.service_filter.addItems(["All services", "AMATEUR", "GMRS"])
        self.enabled_filter = QComboBox(filters); self.enabled_filter.addItems(["All", "Enabled", "Paused"])
        self.review_filter = QCheckBox("Needs review", filters); self.review_filter.setAccessibleName("Show Local Nets needing review")
        self.refresh_btn = QPushButton("Refresh", filters); self.refresh_btn.setAccessibleName("Refresh Local Nets")
        grid.addWidget(self.search, 0, 0, 1, 2); grid.addWidget(self.group_filter, 0, 2); grid.addWidget(self.service_filter, 1, 0); grid.addWidget(self.enabled_filter, 1, 1); grid.addWidget(self.review_filter, 1, 2); grid.addWidget(self.refresh_btn, 1, 3)
        layout.addWidget(filters)
        self.table = QTableWidget(0, 5, self); self.table.setObjectName("localNetsTable"); self.table.setAccessibleName("Local Net reminders"); self.table.setHorizontalHeaderLabels(["Net", "Group", "Where", "Next", "Status"]); self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.SingleSelection); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); layout.addWidget(self.table, 1)
        self.detail_label = QLabel("Select a Local Net to review its reminder details.", self); self.detail_label.setObjectName("localNetReadOnlyDetail"); self.detail_label.setWordWrap(True); layout.addWidget(self.detail_label)
        actions = QHBoxLayout(); self.add_btn = QPushButton("Add Local Net", self); self.edit_btn = QPushButton("Edit", self); self.enable_btn = QPushButton("Pause", self); self.dismiss_btn = QPushButton("Dismiss This Occurrence", self); self.resources_btn = QPushButton("Resources", self); self.settings_btn = QPushButton("Settings", self)
        for button in (self.add_btn,self.edit_btn,self.enable_btn,self.dismiss_btn,self.resources_btn,self.settings_btn): actions.addWidget(button)
        layout.addLayout(actions); self.status = QLabel(""); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.refresh_btn.clicked.connect(self.refresh); self.search.returnPressed.connect(self.refresh); self.group_filter.currentIndexChanged.connect(self.refresh); self.service_filter.currentIndexChanged.connect(self.refresh); self.enabled_filter.currentIndexChanged.connect(self.refresh); self.review_filter.toggled.connect(self.refresh); self.table.itemSelectionChanged.connect(self._selection_changed); self.add_btn.clicked.connect(self._add); self.edit_btn.clicked.connect(self._edit); self.enable_btn.clicked.connect(self._toggle); self.dismiss_btn.clicked.connect(self._dismiss); self.resources_btn.clicked.connect(lambda: self.open_resources_requested.emit("frequency_catalog")); self.settings_btn.clicked.connect(lambda: self.open_settings_requested.emit("operating_groups")); self._selection_changed()

    def refresh(self, *_args: object) -> None:
        enabled = {"Enabled": True, "Paused": False}.get(self.enabled_filter.currentText()); group = self.group_filter.currentData(); rows = self.store.list_schedules(search=self.search.text(), operating_group_key=None if group == "__unassigned__" else group, service=None if self.service_filter.currentText() == "All services" else self.service_filter.currentText(), enabled=enabled, needs_review=True if self.review_filter.isChecked() else None, limit=self.MAX_ROWS)
        if group == "__unassigned__": rows = tuple(row for row in rows if not row.operating_group_key)
        self._rows = rows; summary = self.store.summary(horizon_days=30)
        frequencies = self.catalog.frequencies_by_keys(
            row.frequency_resource_key for row in rows if row.frequency_resource_key
        )
        all_schedules = self.store.list_schedules(limit=MAX_LOCAL_NET_SCHEDULES)
        statuses = resource_statuses(self.catalog, all_schedules)
        rendered: list[tuple[LocalNetSchedule, object, object, object]] = []
        for row in rows:
            frequency = frequencies.get(row.frequency_resource_key or "")
            state = statuses[row.local_net_schedule_key]
            rendered.append((row, summary.by_schedule.get(row.local_net_schedule_key), frequency, state))
        self._rendered_by_key = {
            row.local_net_schedule_key: (frequency, state)
            for row, _occurrence, frequency, state in rendered
        }
        catalog_attention = sum(
            state.state in {"missing", "retired", "update_available"}
            for state in statuses.values()
        )
        attention = catalog_attention
        self.summary_label.setText(f"Now: {_summary_occurrence(summary.now)}   |   Next: {_summary_occurrence(summary.next)}   |   Today: {summary.today_count}   |   Upcoming: {summary.upcoming_count}   |   Attention: {attention} ({catalog_attention} catalog review)")
        self.table.setRowCount(len(rows))
        for i, (row, occurrence, frequency, state) in enumerate(rendered):
            values = (row.name, row.operating_group_name or "Community / Unassigned", frequency_where_text(frequency) if frequency else "Frequency not selected", _when(occurrence), ("Paused · " if not row.enabled else "") + state.state.replace("_", " "))
            for col, value in enumerate(values):
                item = QTableWidgetItem(value); item.setData(Qt.UserRole, row) if col == 0 else None; self.table.setItem(i, col, item)
        self.status.setText(f"Showing {len(rows)} bounded Local Net reminders (maximum {self.MAX_ROWS}).")
        self._selection_changed()

    def on_tab_activated(self) -> None:
        """Refresh once on navigation; Local Nets intentionally owns no timer."""
        self.refresh()
        if self._pending_editor_intent is not None:
            QTimer.singleShot(0, self._resume_pending_editor)

    def _selected(self) -> LocalNetSchedule | None:
        selected = self.table.selectedItems(); return self.table.item(selected[0].row(), 0).data(Qt.UserRole) if selected else None
    def _selection_changed(self) -> None:
        row = self._selected(); self.edit_btn.setEnabled(row is not None); self.enable_btn.setEnabled(row is not None); self.dismiss_btn.setEnabled(self._dismissible_occurrence(row) is not None); self.enable_btn.setText("Enable" if row and not row.enabled else "Pause"); self._show_readonly_detail(row)

    def _show_readonly_detail(self, row: LocalNetSchedule | None) -> None:
        if row is None:
            self.detail_label.setText("Select a Local Net to review its reminder details.")
            return
        frequency, state = self._rendered_by_key.get(row.local_net_schedule_key, (None, None))
        where = frequency_where_text(frequency) if frequency is not None else "Frequency not selected"
        health = getattr(state, "message", "Resource status unavailable")
        sop = f"SOP {row.sop_id} available for manual review" if row.sop_id else "No SOP linked"
        self.detail_label.setText(
            f"{row.name} · {row.operating_group_name or 'Community / Unassigned'} · "
            f"{row.service} · {where}\n{health} · {sop}. {REMINDER_COPY}."
        )

    def focus_schedule(self, schedule_id: object, occurrence_id: object | None = None) -> bool:
        """Open a stable schedule selection in the read-only detail surface."""
        row = self.store.get_schedule(str(schedule_id or ""))
        if row is None:
            return False
        self.search.setText(row.name)
        self.group_filter.setCurrentIndex(0); self.service_filter.setCurrentIndex(0); self.enabled_filter.setCurrentIndex(0); self.review_filter.setChecked(False)
        self.refresh()
        for index, candidate in enumerate(self._rows):
            if candidate.local_net_schedule_key == row.local_net_schedule_key:
                self.table.selectRow(index); self.table.scrollToItem(self.table.item(index, 0)); return True
        return False
    def _add(self) -> None:
        self._run_editor()
    def _edit(self) -> None:
        row = self._selected()
        if row:
            self._run_editor(row)

    def _run_editor(
        self,
        row: LocalNetSchedule | None = None,
        draft_snapshot: dict[str, object] | None = None,
    ) -> None:
        dialog = LocalNetEditorDialog(
            self.store, self.catalog, self.settings, row, self,
            draft_snapshot=draft_snapshot,
        )
        dialog.handoff_requested.connect(self._remember_editor_handoff)
        if dialog.exec() == QDialog.Accepted:
            self._pending_editor_intent = None
            self.refresh()

    def _remember_editor_handoff(self, intent: object) -> None:
        if not isinstance(intent, NavigationIntent):
            return
        self._pending_editor_intent = intent
        self.navigation_requested.emit(intent)

    def _resume_pending_editor(self) -> None:
        intent = self._pending_editor_intent
        if intent is None:
            return
        self._pending_editor_intent = None
        row = self.store.get_schedule(intent.local_net_schedule_id) if intent.local_net_schedule_id else None
        self._run_editor(row, dict(intent.draft_snapshot))
    def _toggle(self) -> None:
        row = self._selected()
        if row:
            try: self.store.set_enabled(row.local_net_schedule_key, not row.enabled); self.refresh()
            except Exception as exc: QMessageBox.warning(self, "Local Nets", str(exc))
    def _dismiss(self) -> None:
        row = self._selected()
        if not row: return
        occurrence = self._dismissible_occurrence(row)
        if occurrence:
            try: self.store.dismiss_occurrence(occurrence); self.refresh()
            except Exception as exc: QMessageBox.warning(self, "Dismiss reminder", str(exc))

    def _dismissible_occurrence(self, row: LocalNetSchedule | None):
        """Only allow dismissal while this reminder is due or active."""
        if row is None or not row.enabled:
            return None
        now = datetime.now(timezone.utc)
        try:
            candidates = self.store.occurrences_for_schedule(
                row,
                now - timedelta(minutes=row.reminder_minutes), horizon_days=2,
                limit=10, include_dismissed=False,
            )
        except Exception:
            return None
        for item in candidates:
            if item.local_net_schedule_key != row.local_net_schedule_key:
                continue
            if item.start_utc - timedelta(minutes=row.reminder_minutes) <= now < item.end_utc:
                return item
        return None


__all__ = ["LocalNetsTab", "LocalNetEditorDialog", "REMINDER_COPY"]
