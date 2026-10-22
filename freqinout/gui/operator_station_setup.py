"""Cache-only station setup overview.

This widget is a navigation and review surface over the existing Settings
owners.  It never loads configuration, checks a process, contacts a radio, or
changes scheduling state itself; hosts provide an immutable setup snapshot and
handle the intent signals.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from freqinout.gui.theme import (
    active_app_theme,
    button_height_for_font,
    button_style,
    control_height_for_font,
    fit_child_combo_boxes,
    label_style,
)


_STEPS = (
    ("operator", "Operator"),
    ("groups", "Groups"),
    ("radios", "Radios & applications"),
    ("behavior", "Operating behavior"),
    ("schedule", "Schedule"),
    ("review", "Review & enable"),
)


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _display_text(value: object) -> str:
    """Render labels containing ampersands without mnemonic underlines."""
    return _text(value).replace("&", "&&")


def _radio_id(row: Mapping[str, Any] | None) -> int:
    try:
        return int((row or {}).get("id", 0) or 0)
    except (TypeError, ValueError):
        return 0


class OperatorStationSetup(QWidget):
    """Six-step station setup checklist backed entirely by a supplied snapshot."""

    route_requested = Signal(str, int)
    review_requested = Signal(int)
    enable_requested = Signal(int, str)
    disable_requested = Signal(int)

    def __init__(
        self,
        snapshot: Mapping[str, Any] | None = None,
        *,
        initial_step: str = "operator",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._snapshot: dict[str, Any] = {}
        self._step_key = "operator"
        self._busy = False
        self._build_ui()
        self.set_snapshot(snapshot or {})
        self.set_step(initial_step)
        self.apply_theme()

    # ----- public cache/status contract ---------------------------------

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._snapshot)

    def set_snapshot(self, snapshot: Mapping[str, Any] | None) -> None:
        self._snapshot = deepcopy(dict(snapshot or {}))
        self._load_radio_choices()
        self._refresh_all_step_states()
        self._refresh_review()

    def current_step(self) -> str:
        return self._step_key

    def set_step(self, step: str) -> bool:
        key = _text(step).strip().lower()
        if key not in {item[0] for item in _STEPS}:
            return False
        self._step_key = key
        index = next(index for index, item in enumerate(_STEPS) if item[0] == key)
        self.step_stack.setCurrentIndex(index)
        for step_key, button in self.step_buttons.items():
            button.setProperty("selected", step_key == key)
            button.style().unpolish(button)
            button.style().polish(button)
            button.setStyleSheet(button_style("primary" if step_key == key else "muted", active_app_theme()))
        self._refresh_navigation_buttons()
        if key == "review":
            self._refresh_review()
            self.review_requested.emit(self._selected_radio_id())
        return True

    def set_operation_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(_text(text))
        self.status_label.setToolTip(_text(text))
        self.status_label.setProperty("status_kind", "error" if error else "info")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        # Review workers may take a moment; navigation and the explicit Main
        # escape remain available while cached facts are being refreshed.
        for button in (*self.step_buttons.values(), self.back_button, self.next_button, self.main_button):
            button.setEnabled(True)
        self.radio_combo.setEnabled(True)
        self.enable_button.setEnabled(not self._busy and self.enable_button.isVisible())
        # Disabling an already-approved schedule remains an immediate escape
        # while a review check is in flight.  The Settings host fences and
        # serializes the resulting worker actions.
        self.disable_button.setEnabled(self.disable_button.isVisible())

    # ----- construction --------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        heading = QLabel("Station setup")
        heading.setObjectName("operatorStationSetupHeading")
        heading.setAccessibleName("Station setup")
        heading.setStyleSheet(label_style("text", active_app_theme(), weight=700))
        outer.addWidget(heading)

        self.status_label = QLabel("Ready to review saved setup.")
        self.status_label.setObjectName("operatorStationSetupStatus")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.step_buttons: dict[str, QPushButton] = {}
        steps_row = QGridLayout()
        steps_row.setHorizontalSpacing(4)
        steps_row.setVerticalSpacing(4)
        for index, (key, title) in enumerate(_STEPS):
            button = QPushButton(_display_text(title))
            button.setObjectName(f"operatorStationSetupStep_{key}")
            button.setAccessibleName(f"Open setup step: {title}")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(lambda _checked=False, step_key=key: self.set_step(step_key))
            self.step_buttons[key] = button
            steps_row.addWidget(button, index // 3, index % 3)
        outer.addLayout(steps_row)

        self.step_stack = QStackedWidget()
        self.step_stack.setObjectName("operatorStationSetupSteps")
        scroll = QScrollArea()
        scroll.setObjectName("operatorStationSetupScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.step_stack)
        outer.addWidget(scroll, 1)

        self._step_state_labels: dict[str, QLabel] = {}
        self._step_detail_labels: dict[str, QLabel] = {}
        for key, title in _STEPS:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(8)
            title_label = QLabel(title)
            title_label.setObjectName(f"operatorStationSetupTitle_{key}")
            title_font = title_label.font()
            title_font.setBold(True)
            title_label.setFont(title_font)
            layout.addWidget(title_label)
            state = QLabel()
            state.setObjectName(f"operatorStationSetupState_{key}")
            state.setAccessibleName(f"{title} setup state")
            layout.addWidget(state)
            detail = QLabel()
            detail.setObjectName(f"operatorStationSetupDetail_{key}")
            detail.setWordWrap(True)
            layout.addWidget(detail)
            self._step_state_labels[key] = state
            self._step_detail_labels[key] = detail
            self._build_step_actions(key, layout)
            layout.addStretch(1)
            self.step_stack.addWidget(page)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.back_button = QPushButton("Back")
        self.back_button.setAccessibleName("Previous station setup step")
        self.back_button.clicked.connect(self._go_back)
        footer.addWidget(self.back_button)
        self.next_button = QPushButton()
        self.next_button.setAccessibleName("Next station setup step")
        self.next_button.clicked.connect(self._go_next)
        footer.addWidget(self.next_button)
        footer.addStretch(1)
        self.main_button = QPushButton("Go to Main")
        self.main_button.setAccessibleName("Go to Main without enabling scheduling")
        self.main_button.setToolTip("Return to Main. Scheduling remains unchanged.")
        self.main_button.clicked.connect(self._go_to_main)
        footer.addWidget(self.main_button)
        outer.addLayout(footer)

    def _build_step_actions(self, key: str, layout: QVBoxLayout) -> None:
        if key == "review":
            form = QFormLayout()
            self.radio_combo = QComboBox()
            self.radio_combo.setObjectName("operatorStationSetupReviewRadio")
            self.radio_combo.setAccessibleName("Radio to review")
            self.radio_combo.currentIndexChanged.connect(self._refresh_review)
            form.addRow("Radio", self.radio_combo)
            layout.addLayout(form)
            self.review_summary_label = QLabel()
            self.review_summary_label.setWordWrap(True)
            layout.addWidget(self.review_summary_label)
            self.enable_button = QPushButton("Enable scheduling")
            self.enable_button.setAccessibleName("Enable scheduling for reviewed radio")
            self.enable_button.clicked.connect(self._enable_selected)
            self.disable_button = QPushButton("Disable scheduling")
            self.disable_button.setAccessibleName("Disable scheduling for reviewed radio")
            self.disable_button.clicked.connect(self._disable_selected)
            layout.addWidget(self.enable_button, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(self.disable_button, 0, Qt.AlignmentFlag.AlignLeft)
            self.review_button = QPushButton("Review saved setup")
            self.review_button.setAccessibleName("Review saved setup")
            self.review_button.clicked.connect(lambda: self.review_requested.emit(self._selected_radio_id()))
            layout.addWidget(self.review_button, 0, Qt.AlignmentFlag.AlignLeft)
        else:
            route_labels = {
                "operator": "Open Operator settings",
                "groups": "Open Groups settings",
                "radios": "Open Radios & applications",
                "behavior": "Open Operating behavior",
                "schedule": "Open Schedule settings",
            }
            button = QPushButton(_display_text(route_labels[key]))
            button.setAccessibleName(route_labels[key])
            button.clicked.connect(lambda _checked=False, route=key: self._route_to_owner(route))
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)

    # ----- snapshot projection ------------------------------------------

    def _radio_rows(self) -> list[dict[str, Any]]:
        rows = self._snapshot.get("radios", ())
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def _load_radio_choices(self) -> None:
        if not hasattr(self, "radio_combo"):
            return
        previous = self._selected_radio_id()
        self.radio_combo.blockSignals(True)
        try:
            self.radio_combo.clear()
            for row in self._radio_rows():
                radio_id = _radio_id(row)
                self.radio_combo.addItem(_text(row.get("name") or f"Radio {radio_id or ''}").strip(), radio_id)
            if previous:
                index = self.radio_combo.findData(previous)
                if index >= 0:
                    self.radio_combo.setCurrentIndex(index)
        finally:
            self.radio_combo.blockSignals(False)

    def _selected_radio_id(self) -> int:
        if not hasattr(self, "radio_combo"):
            return 0
        try:
            return int(self.radio_combo.currentData() or 0)
        except (TypeError, ValueError):
            return 0

    def _selected_radio(self) -> dict[str, Any]:
        selected = self._selected_radio_id()
        return next((row for row in self._radio_rows() if _radio_id(row) == selected), {})

    def _refresh_all_step_states(self) -> None:
        station = self._snapshot.get("station")
        station = station if isinstance(station, Mapping) else {}
        station_complete = bool(
            _text(station.get("callsign") or station.get("name") or "").strip()
            and _text(station.get("timezone") or "").strip()
        )
        groups = self._snapshot.get("groups")
        group_complete = not groups or isinstance(groups, (list, tuple))
        radios = self._radio_rows()
        radio_complete = bool(radios) and all(
            bool(row.get("configuration_complete", True)) for row in radios
        )
        review_complete = bool(radios) and all(
            not bool(row.get("schedule_required", True))
            or (
                isinstance(row.get("review"), Mapping)
                and bool(row["review"].get("valid", False))
            )
            for row in radios
        )
        self._set_step("operator", "Complete" if station_complete else "Needs attention", "Review station identity and timezone.")
        self._set_step("groups", "Complete" if group_complete else "Needs attention", "No groups is valid when no group-dependent function is selected.")
        self._set_step("radios", "Complete" if radio_complete else ("Needs attention" if radios else "Not required"), "Review saved applications, endpoints, startup choices, and settings identity.")
        self._set_step("behavior", self._generic_step_state("behavior"), "Review who controls tuning and PTT for each radio.")
        self._set_step("schedule", self._generic_step_state("schedule"), "Review saved plans; scheduling is enabled separately at the final review.")
        self._set_step("review", "Complete" if review_complete else ("Not required" if radios and all(not bool(row.get("schedule_required", True)) for row in radios) else "Needs attention"), "Inspect the selected radio before enabling scheduling.")

    def _generic_step_state(self, key: str) -> str:
        value = self._snapshot.get(f"{key}_state", self._snapshot.get(key))
        if isinstance(value, Mapping):
            value = value.get("state") or value.get("label")
        text = _text(value).strip().casefold()
        if text in {"complete", "ready", "not required"}:
            return "Not required" if text == "not required" else "Complete"
        if text:
            return "Needs attention"
        return "Needs attention"

    def _set_step(self, key: str, state: str, detail: str) -> None:
        self._step_state_labels[key].setText(state)
        self._step_detail_labels[key].setText(detail)

    def _refresh_review(self, _index: int = -1) -> None:
        if not hasattr(self, "review_summary_label"):
            return
        row = self._selected_radio()
        review = row.get("review") if isinstance(row.get("review"), Mapping) else {}
        lines = [
            _text(review.get("label") or review.get("state") or "Review saved configuration."),
        ]
        fingerprint = _text(review.get("fingerprint") or "").strip()
        if review.get("issues"):
            issues = review.get("issues")
            if isinstance(issues, (list, tuple)):
                lines.append("; ".join(_text(item) for item in issues if _text(item).strip()))
            else:
                lines.append(_text(issues))
        warnings = review.get("warnings")
        if warnings:
            if isinstance(warnings, (list, tuple)):
                warning_text = "; ".join(_text(item) for item in warnings if _text(item).strip())
            else:
                warning_text = _text(warnings)
            if warning_text:
                lines.append("Warnings: " + warning_text)
        launch = review.get("launch_summaries") or row.get("launch_summaries") or ()
        if launch:
            launch_lines = []
            for item in launch:
                summary = _text(item).strip()
                if summary and "launches as" not in summary.casefold():
                    summary = "Launches as: " + summary
                if summary:
                    launch_lines.append(summary)
            if launch_lines:
                lines.append("; ".join(launch_lines))
        if review.get("next_event"):
            lines.append(self._format_next_event(review.get("next_event")))
        if review.get("plan_name"):
            lines.append("Plan: " + _text(review.get("plan_name")))
        if review.get("time_basis"):
            lines.append("Time basis: " + _text(review.get("time_basis")))
        self.review_summary_label.setText("\n".join(line for line in lines if line.strip()))
        enabled = bool(review.get("enabled", row.get("enabled", False)))
        valid = bool(review.get("valid", False)) and bool(fingerprint)
        self.enable_button.setVisible(bool(not enabled and valid))
        self.disable_button.setVisible(bool(enabled))
        self.enable_button.setEnabled(bool(not self._busy and not enabled and valid))
        self.disable_button.setEnabled(bool(enabled))

    @staticmethod
    def _format_next_event(value: object) -> str:
        raw = _text(value).strip()
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            utc_value = parsed.astimezone(timezone.utc)
            local_value = utc_value.astimezone()
            local_zone = local_value.tzname() or "local time"
            return (
                "Next event: "
                f"{local_value:%Y-%m-%d %H:%M} {local_zone} "
                f"({utc_value:%Y-%m-%d %H:%M} UTC)"
            )
        except (TypeError, ValueError, OverflowError):
            return "Next event: " + raw

    # ----- navigation/intents -------------------------------------------

    def _refresh_navigation_buttons(self) -> None:
        index = next(index for index, item in enumerate(_STEPS) if item[0] == self._step_key)
        self.back_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < len(_STEPS) - 1)
        if index < len(_STEPS) - 1:
            self.next_button.setText(f"Next: {_display_text(_STEPS[index + 1][1])}")
        else:
            self.next_button.setText("Review")

    def _go_back(self) -> None:
        index = next(index for index, item in enumerate(_STEPS) if item[0] == self._step_key)
        if index > 0:
            self.set_step(_STEPS[index - 1][0])

    def _go_next(self) -> None:
        index = next(index for index, item in enumerate(_STEPS) if item[0] == self._step_key)
        if index < len(_STEPS) - 1:
            self.set_step(_STEPS[index + 1][0])

    def _route_to_owner(self, route: str) -> None:
        radio_id = self._selected_radio_id() if route in {"radios", "behavior", "schedule"} else 0
        self.route_requested.emit(route, radio_id)

    def _go_to_main(self) -> None:
        self.route_requested.emit("main", self._selected_radio_id())

    def _enable_selected(self) -> None:
        row = self._selected_radio()
        review = row.get("review") if isinstance(row.get("review"), Mapping) else {}
        fingerprint = _text(review.get("fingerprint") or "").strip()
        if bool(review.get("valid", False)) and fingerprint and not bool(review.get("enabled", row.get("enabled", False))):
            self.enable_requested.emit(_radio_id(row), fingerprint)

    def _disable_selected(self) -> None:
        row = self._selected_radio()
        review = row.get("review") if isinstance(row.get("review"), Mapping) else {}
        if bool(review.get("enabled", row.get("enabled", False))):
            self.disable_requested.emit(_radio_id(row))

    # ----- shared theme --------------------------------------------------

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        palette = dict(theme or active_app_theme())
        self.status_label.setStyleSheet(label_style("muted", palette))
        for label in (*self._step_state_labels.values(), *self._step_detail_labels.values()):
            label.setStyleSheet(label_style("muted", palette))
        self.review_summary_label.setStyleSheet(label_style("muted", palette))
        for key, button in self.step_buttons.items():
            button.setStyleSheet(button_style("primary" if key == self._step_key else "muted", palette))
            button.setMinimumHeight(button_height_for_font(button, vertical_padding=8, floor=30))
        for button in (self.back_button, self.next_button, self.main_button, self.enable_button, self.disable_button, self.review_button):
            button.setMinimumHeight(button_height_for_font(button, vertical_padding=8, floor=30))
        self.next_button.setStyleSheet(button_style("primary", palette))
        self.main_button.setStyleSheet(button_style("secondary", palette))
        self.enable_button.setStyleSheet(button_style("primary", palette))
        self.disable_button.setStyleSheet(button_style("secondary", palette))
        self.review_button.setStyleSheet(button_style("muted", palette))
        self.radio_combo.setMinimumHeight(control_height_for_font(self.radio_combo, vertical_padding=8, floor=28))
        fit_child_combo_boxes(self)
        self._refresh_navigation_buttons()
        self._refresh_review()


__all__ = ["OperatorStationSetup"]
