"""Cached, signal-only guided plan assignment controls for Plan Builder.

The host owns all I/O, validation, persistence and navigation. A review token
belongs to the exact displayed draft; changing the draft invalidates it.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QStyle, QStyleOptionTabWidgetFrame, QTabWidget, QTableView, QToolButton, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel

from .responsive_geometry import WrappingControlLayout
from .theme import active_app_theme, button_style, fit_text_action_button


def _label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _name(value, fallback="None") -> str:
    if isinstance(value, Mapping):
        return str(value.get("name") or fallback)
    return str(value or fallback)


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _issue_severity(issue):
    value = str(issue.get("severity") or "blocked").strip().lower()
    if value in {"warning", "caution"}:
        return "warning"
    if value in {"info", "wait"}:
        return "info"
    return "blocked"


def _repair_destination(remedy):
    target = remedy.get("target")
    return target.get("target") if isinstance(target, Mapping) else target


def _issue_detail(issue, context=None):
    """Summarize cached operator-facing evidence without exposing storage IDs."""
    context = context or {}
    lines = []
    scope = str(issue.get("scope_label") or "").strip()
    if not scope:
        radios = context.get("radios") or ()
        plans = context.get("plans") or ()
        radio_id = issue.get("radio_id") or issue.get("device_profile_id")
        plan_id = issue.get("frequency_plan_id")
        radio = next((row for row in radios if isinstance(row, Mapping) and str(row.get("id")) == str(radio_id)), {})
        plan = next((row for row in plans if isinstance(row, Mapping) and str(row.get("id")) == str(plan_id)), {})
        if not plan and str((context.get("plan") or {}).get("id")) == str(plan_id):
            plan = context.get("plan") or {}
        scope = " · ".join(str(item.get("name")) for item in (radio, plan) if item.get("name"))
    if scope:
        lines.append("Affected: " + scope)
    source = str(issue.get("source_label") or "").strip()
    if source:
        lines.append("Source: " + source)
    sources = [str(value).strip() for value in (issue.get("affected_sources") or ()) if str(value).strip()]
    if sources:
        lines.append("Affected sources: " + ", ".join(sources))
    related = [str(row.get("name") or "").strip() for row in (issue.get("related_radios") or ())
               if isinstance(row, Mapping) and row.get("name")]
    if related:
        lines.append("Radios: " + ", ".join(related))
    resource = issue.get("resource") or {}
    if isinstance(resource, Mapping) and resource.get("name"):
        lines.append("Resource: " + str(resource["name"]))
    window = issue.get("window") or {}
    if isinstance(window, Mapping):
        if window.get("label"):
            lines.append("Window: " + str(window["label"]))
        else:
            timing = " · ".join(str(window.get(key)) for key in ("day_utc", "start_utc", "end_utc") if window.get(key))
            if timing:
                lines.append("Window: " + timing + " UTC")
        for key, label in (("windows", "Plan windows"), ("peer_windows", "Other radio windows")):
            descriptions = []
            for row in window.get(key) or ():
                if not isinstance(row, Mapping):
                    continue
                def clock(value):
                    try:
                        minute = int(value)
                        return f"{minute // 60:02d}:{minute % 60:02d}"
                    except (TypeError, ValueError):
                        return ""
                day = str(row.get("day_utc") or "All days")
                start = clock(row.get("start_minute"))
                end = clock(row.get("end_minute"))
                timing = f"{day} {start}–{end} UTC" if start and end else day
                if start and end and int(row["end_minute"]) <= int(row["start_minute"]):
                    timing += " (overnight)"
                band = str(row.get("band") or "").strip()
                descriptions.append(timing + (f" · {band}" if band else ""))
            if descriptions:
                lines.append(label + ": " + "; ".join(descriptions))
    differences = [str(value).strip() for value in (issue.get("window_differences") or ()) if str(value).strip()]
    if differences:
        lines.extend(["Window differences", *differences])
    evidence = issue.get("evidence") or {}
    if isinstance(evidence, Mapping) and evidence.get("scope"):
        lines.append("Review scope: " + str(evidence["scope"]))
    return "\n".join(lines)


class _AssignmentReviewTabs(QTabWidget):
    """Use the active empty comparison's height instead of the schedule table's."""

    def __init__(self):
        super().__init__()
        self._expanded_maximum = self.maximumHeight()

    def fit_current_page(self):
        page = self.currentWidget()
        compact = page is not None and page.property("emptyComparison")
        self.setMaximumHeight(self.sizeHint().height() if compact else self._expanded_maximum)
        self.updateGeometry()

    def _fit_empty_page(self, hint):
        page = self.currentWidget()
        if page is not None and page.property("emptyComparison"):
            options = QStyleOptionTabWidgetFrame()
            self.initStyleOption(options)
            height = page.sizeHint().height() + self.tabBar().sizeHint().height()
            size = self.style().sizeFromContents(
                QStyle.ContentsType.CT_TabWidget, options, QSize(hint.width(), height), self,
            )
            hint.setHeight(size.height())
        return hint

    def sizeHint(self):
        return self._fit_empty_page(super().sizeHint())

    def minimumSizeHint(self):
        return self._fit_empty_page(super().minimumSizeHint())


class PlanAssignmentPanel(QWidget):
    """Draft and review a plan assignment without accessing live state."""

    review_requested = Signal(dict)
    commit_requested = Signal(dict)
    repair_requested = Signal(dict)
    schedule_page_requested = Signal(dict)
    cancel_requested = Signal()
    operation_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context = {}
        self._review = None
        self._selected = set()
        self._remedies = {}
        self._copy_names = {}
        self._pending_review = None
        self._pending_schedule_page = None
        self._schedule_page_error = ""
        self._busy = False
        self._stage = "choose"
        self._review_tab_index = 0
        self._theme = active_app_theme()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.title = _label("Radio assignments")
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        root.addWidget(self.title)
        self.summary = _label()
        root.addWidget(self.summary)
        self.action_row = QWidget()
        self.action_row_layout = QHBoxLayout(self.action_row)
        self.action_row_layout.setContentsMargins(0, 0, 0, 0)
        self.operation_buttons = {}
        for label, operation in (("Swap plans", "swap"), ("Remove assignment", "remove"),
                                 ("Review pending changes…", "apply")):
            button = QPushButton(label)
            button.setObjectName(f"assignment_action_{operation}")
            button.setAccessibleName(label)
            button.clicked.connect(lambda _checked=False, op=operation: self._choose_operation(op))
            self.operation_buttons[operation] = button
            self.action_row_layout.addWidget(button)
        self.action_row_layout.addStretch(1)
        self.message = _label()
        root.addWidget(self.message)
        self.content = QVBoxLayout()
        root.addLayout(self.content)
        actions = QHBoxLayout()
        self.primary_button = QPushButton("Review assignment…")
        self.primary_button.clicked.connect(self._primary)
        actions.addWidget(self.primary_button)
        self.recheck_button = QPushButton("Recheck issues")
        self.recheck_button.clicked.connect(self._recheck)
        actions.addWidget(self.recheck_button)
        self.back_button = QPushButton("Edit assignment")
        self.back_button.clicked.connect(self._back)
        actions.addWidget(self.back_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        root.addLayout(actions)
        root.addWidget(self.action_row)
        self.apply_theme()
        self._render()

    def apply_theme(self, theme=None):
        self._theme = theme or active_app_theme()
        self.primary_button.setStyleSheet(button_style("primary", self._theme))
        self.back_button.setStyleSheet(button_style("secondary", self._theme))
        self.cancel_button.setStyleSheet(button_style("secondary", self._theme))
        self._update_operation_buttons()
        for badge in self.findChildren(QPushButton, "assignment_rf_status"):
            badge.setStyleSheet(button_style(badge.property("rfRole") or "secondary", self._theme))
        for toggle in self.findChildren(QToolButton, "assignment_issue_disclosure"):
            fit_text_action_button(toggle)
        tabs = self.findChild(_AssignmentReviewTabs, "assignment_review_tabs")
        if tabs is not None:
            tabs.fit_current_page()

    def set_context(self, context: dict):
        """Refresh cached data while preserving the current draft for this plan."""
        reused_context = context is self._context
        context = deepcopy(context or {})
        old_plan = (self._context.get("plan") or {}).get("id")
        new_plan = (context.get("plan") or {}).get("id")
        new_operation = context.get("operation", "assign")
        old_operation = self._context.get("operation", "assign")
        changed = old_plan != new_plan or old_operation != new_operation or not self._context or bool(context.get("reset_selection"))
        keep_review = (context.pop('keep_visible_review', False) and not changed
                       and self._review is not None and self._review.get('operation') == new_operation
                       and (new_operation != 'apply' or context.get('plan_draft')))
        def visible_context(value):
            return {**{k: v for k, v in value.items() if k not in {'revision', 'draft', 'radios'}},
                    'radios': [{k: v for k, v in radio.items() if k != 'current_assignment'}
                               for radio in value.get('radios', ())]}

        same_context = not reused_context and visible_context(self._context) == visible_context(context)
        self._context = context
        available = {int(r["id"]) for r in context.get("radios", ()) if r.get("id") is not None}
        if changed:
            self._selected = {int(i) for i in context.get("preselected_radio_ids", ())} & available
            self._remedies = {}
            self._copy_names = {}
            self._stage = "choose"
            self.message.clear()
        else:
            self._selected &= available
            self._remedies = {key: value for key, value in self._remedies.items() if int(key) in self._selected}
        if new_operation == "remove":
            assigned = {int(row["id"]) for row in context.get("radios", ())
                        if self._assigned_plan_id(row) > 0}
            self._selected &= assigned
        if new_operation == "apply":
            self._selected = {int(i) for i in context.get("preselected_radio_ids", ())} & available
        # A new snapshot can change resources or consumers even when labels match.
        self._pending_review = None
        self._busy = False
        valid_selection = len(self._selected) == 2 if new_operation == 'swap' else bool(self._selected)
        if keep_review and valid_selection:
            self._recheck()
            return
        had_review = self._review is not None
        self._review = None
        if same_context and not changed and not had_review:
            self._render_actions()
        else:
            self._render()

    def set_review(self, review: dict):
        """Accept only a result for the draft most recently submitted for review."""
        if self._pending_review is None or self._pending_review != self._intent():
            return
        def visible_result(value):
            result = {k: v for k, v in value.items() if k not in {'token', 'issues', 'comparison'}}
            result['issues'] = [{k: v for k, v in item.items() if k != 'revisions'}
                                for item in value.get('issues', ())]
            result['comparison'] = {k: v for k, v in (value.get('comparison') or {}).items()
                                    if k != 'page_token'}
            return result

        signature = visible_result(review or {})
        unchanged = self._review is not None and getattr(self, '_review_visible_signature', None) == signature
        if unchanged:
            self._review['token'] = (review or {}).get('token')
            if self._review.get('comparison') is not None:
                self._review['comparison']['page_token'] = (review.get('comparison') or {}).get('page_token')
        else:
            self._review = deepcopy(review or {})
        self._review_visible_signature = signature
        self._pending_review = None
        self._pending_schedule_page = None
        self._schedule_page_error = ""
        self._busy = False
        self._stage = "review"
        self.message.clear()
        if unchanged:
            self._render_actions()
            for name in ("assignment_load_more_windows", "assignment_load_more_differences"):
                button = self.findChild(QPushButton, name)
                if button is not None:
                    button.setEnabled(True)
        else:
            self._render()

    def set_busy(self, busy: bool):
        self._busy = bool(busy)
        self._render_actions()

    def append_schedule_page(self, result: dict):
        """Append a detached Full Schedule batch only to its requested review."""
        pending = self._pending_schedule_page
        review = self._review or {}
        result = deepcopy(result or {})
        if not pending or result.get("review_token") != pending.get("review_token"):
            return
        self._pending_schedule_page = None
        comparison = review.setdefault("comparison", {})
        differences = pending.get("view") == "differences"
        rows_key = "rows" if differences else "full_schedule"
        prefix = "" if differences else "full_schedule_"
        if result.get("error"):
            self._schedule_page_error = str(result.get("error"))
        else:
            rows = list(comparison.get(rows_key) or [])
            rows.extend(result.get("rows") or [])
            comparison[rows_key] = rows
            comparison[prefix + "total"] = result.get("total", len(rows))
            comparison[prefix + "complete"] = bool(result.get("complete", False))
            comparison[prefix + "next_offset"] = result.get("next_offset")
            self._schedule_page_error = ""
        active_tab = self._review_tab_index
        self._render()
        tabs = self.findChild(QTabWidget, "assignment_review_tabs")
        if tabs is not None:
            tabs.setCurrentIndex(active_tab)

    def show_result(self, result: dict):
        self._busy = False
        self._review = None
        self._pending_review = None
        self.message.setText(str((result or {}).get("message") or "Settings changed; review updated results."))
        self._stage = "choose"
        self._review_tab_index = 0
        self._render()

    def _select_operation(self, operation):
        if operation == self._context.get("operation", "assign"):
            return
        if not self._operation_available(operation):
            return
        self._context["operation"] = operation
        if operation == "remove":
            self._selected.clear()
            self._remedies.clear()
        elif operation == "apply":
            self._selected = {int(i) for i in self._context.get("preselected_radio_ids", ())}
        self._invalidate()
        self.operation_changed.emit(operation)

    def _choose_operation(self, operation):
        current = self._context.get("operation", "assign")
        self._select_operation("assign" if operation == current else operation)

    @staticmethod
    def _assigned_plan_id(radio):
        current_plan = radio.get("current_plan") or {}
        assignment = radio.get("current_assignment") or {}
        try:
            return int(current_plan.get("id") or assignment.get("frequency_plan_id") or 0)
        except (TypeError, ValueError):
            return 0

    def _plan_is_assignable(self):
        plan = self._context.get("plan") or {}
        status = str(plan.get("status") or "saved").strip().lower()
        return bool(plan.get("id") and not self._context.get("plan_unsaved")
                    and status == "saved" and bool(plan.get("enabled", True)))

    def _operation_can_review(self, operation):
        radios = {int(row["id"]): row for row in self._context.get("radios", ())
                  if row.get("id") is not None}
        selected = [radios[ident] for ident in self._selected if ident in radios]
        if not selected:
            return False
        if operation == "assign":
            return self._plan_is_assignable()
        if operation == "remove":
            return all(self._assigned_plan_id(radio) > 0 for radio in selected)
        if operation == "swap":
            if len(selected) != 2:
                return False
            first, second = (self._assigned_plan_id(radio) for radio in selected)
            return first > 0 and second > 0 and first != second
        if operation == "apply":
            plan = self._context.get("plan") or {}
            return bool(self._context.get("plan_draft") and plan.get("id"))
        return False

    def _operation_available(self, operation):
        radios = list(self._context.get("radios", ()))
        if operation == "assign":
            return self._plan_is_assignable()
        if operation == "remove":
            return any(self._assigned_plan_id(radio) > 0 for radio in radios)
        if operation == "swap":
            return len({plan_id for radio in radios if (plan_id := self._assigned_plan_id(radio)) > 0}) >= 2
        if operation == "apply":
            plan = self._context.get("plan") or {}
            return bool(self._context.get("plan_draft") and plan.get("id"))
        return False

    def _update_operation_buttons(self):
        operation = self._context.get("operation", "assign")
        reviewing = self._review is not None
        labels = {"swap": "Swap plans…", "remove": "Remove assignment…",
                  "apply": "Review pending changes…"}
        for key, button in self.operation_buttons.items():
            returning = key == operation
            available = self._plan_is_assignable() if returning else self._operation_available(key)
            label = "Back to assigning radios" if returning else labels[key]
            button.setVisible(available and not reviewing)
            button.setEnabled(available and not self._busy and not reviewing)
            button.setText(label)
            button.setAccessibleName(label)
            style = button_style("secondary", self._theme)
            if button.styleSheet() != style:
                button.setStyleSheet(style)
        self.action_row.setVisible(not reviewing and any(
            not button.isHidden() for button in self.operation_buttons.values()
        ))

    def _intent(self):
        intent = {"plan_id": (self._context.get("plan") or {}).get("id"),
                "selected_radio_ids": sorted(self._selected),
                "operation": self._context.get("operation", "assign"),
                "remedies": deepcopy(self._remedies)}
        return intent

    def _invalidate(self):
        self._review = None
        self._pending_review = None
        self._busy = False
        self._stage = "choose"
        self._render()

    def _selection_changed(self, radio_id, checked):
        object_name = f"assignment_radio_{radio_id}"
        previous = self.findChild(QCheckBox, object_name)
        restore_focus = previous is not None and previous.hasFocus()
        if checked:
            self._selected.add(radio_id)
        else:
            self._selected.discard(radio_id)
            self._remedies.pop(str(radio_id), None)
        self._invalidate()
        if restore_focus:
            replacement = self.findChild(QCheckBox, object_name)
            if replacement is not None:
                replacement.show()
                replacement.setFocus(Qt.FocusReason.OtherFocusReason)

    def _remove_remedy(self, radio_id):
        self._remedies.pop(str(radio_id), None)
        self.message.setText("Staged model change removed. Review the remaining choices.")
        self._invalidate()

    def _set_remedy(self, radio_id, remedy):
        self._remedies[str(radio_id)] = remedy
        self.message.setText("Scheduling will be allowed when you assign. Review the complete changes before committing.")
        self._invalidate()

    def _primary(self):
        if self._busy:
            return
        if self._review is None:
            self._pending_review = self._intent()
            self._busy = True
            self._render_actions()
            self.review_requested.emit(deepcopy(self._pending_review))
        elif self._can_commit():
            payload = self._intent()
            if self._review.get("operation") == "refresh_sources":
                payload["refresh_token"] = self._review["token"]
                payload["refresh_plan_ids"] = list(self._review.get("refresh_plan_ids") or [])
            else:
                payload["review_token"] = self._review["token"]
            self._busy = True
            self._render_actions()
            self.commit_requested.emit(payload)

    def _recheck(self):
        if self._busy or self._review is None:
            return
        self._pending_review = self._intent()
        self._busy = True
        self._render_actions()
        self.review_requested.emit(deepcopy(self._pending_review))

    def _back(self):
        self._busy = False
        self._review = None
        self._pending_review = None
        self._stage = "choose"
        self._review_tab_index = 0
        self._selected_comparison_target = None
        self._render()

    def _can_commit(self):
        review = self._review or {}
        return bool(review.get("token") and review.get("can_commit") and
                    not any(_issue_severity(i) == "blocked" for i in review.get("issues", ())))

    def _render_actions(self):
        operation = self._context.get("operation", "assign")
        self.recheck_button.setVisible(self._review is not None and not self._can_commit())
        self.recheck_button.setEnabled(not self._busy)
        self.back_button.setVisible(self._review is not None)
        self.back_button.setEnabled(not self._busy)
        self.cancel_button.setEnabled(not self._busy)
        self._update_operation_buttons()
        if self._review is None:
            review_labels = {"apply": "Review changes…", "assign": "Review assignment…",
                             "swap": "Review swap…", "remove": "Review removal…"}
            label = "Review remaining issues…" if self._remedies else review_labels[operation]
            self.primary_button.setText(label)
            self.primary_button.setEnabled(bool(self._operation_can_review(operation) and not self._busy))
        else:
            labels = {"assign": "Assign plan", "swap": "Swap plans", "remove": "Remove assignment", "apply": "Apply changes"}
            label = labels.get(operation, "Assign plan")
            if self._review.get("operation") == "refresh_sources":
                label = "Refresh source windows"
            elif operation in {"assign", "swap", "apply"} and any(r.get("kind") in {"allow", "copy"} for r in self._remedies.values()):
                label = {"assign": "Allow scheduling and assign plan", "swap": "Allow scheduling and swap plans", "apply": "Allow scheduling and apply changes"}[operation]
            self.primary_button.setText(label if self._can_commit() else "Resolve issues to continue")
            self.primary_button.setEnabled(not self._busy and self._can_commit())

    def _render(self):
        _clear(self.content)
        context = self._context
        operation = context.get("operation", "assign")
        plan = _name(context.get("plan"), "No plan selected")
        reviewing = self._review is not None
        if reviewing:
            titles = {"apply": "Review plan changes", "swap": "Review swap", "remove": "Review removal"}
            self.title.setText(titles.get(operation, "Review assignment"))
        else:
            self.title.setText("Choose radios")
        if operation == 'apply':
            summary = f'{plan} · Pending changes. Review the effects before applying them to the affected radios.'
        elif operation == 'swap':
            summary = 'Review the proposed swap.' if reviewing else 'Choose two radios that have different saved plans.'
        elif operation == 'remove':
            summary = 'Review the proposed removal.' if reviewing else 'Choose radios that already have a plan assigned.'
        elif context.get('plan_unsaved'):
            summary = 'Save the plan in Plan Builder before assigning it.'
        else:
            summary = f'{plan} · Saved plan. ' + ('Review the effects before assigning.' if reviewing else 'Choose the radios that should use it.')
            if context.get('plan_draft'):
                summary += ' Earlier pending changes have not been applied.'
        self.summary.setText(summary)
        self.summary.setVisible(not reviewing or operation != "assign" or not self._review.get("impacts"))
        if operation == "remove":
            self.content.addWidget(_label("Removing an assignment stops future automatic schedule changes. It does not retune the radio or remove a hold."))
        if self._review is None:
            self._render_choices()
        else:
            self._render_review()
        self._render_actions()
        self._set_task_tab_order()

    def _set_task_tab_order(self):
        def controls(layout):
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget()
                if item.layout() is not None:
                    yield from controls(item.layout())
                elif widget is not None:
                    if widget.focusPolicy() & Qt.FocusPolicy.TabFocus:
                        yield widget
                    elif widget is self.action_row:
                        yield from controls(widget.layout())

        order = list(controls(self.layout()))
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)

    def _render_choices(self):
        radios = list(self._context.get("radios", ()))
        if self._context.get("operation") == "remove":
            radios = [radio for radio in radios if self._assigned_plan_id(radio) > 0]
        if not radios:
            message = ("No radios have an assigned plan to remove."
                       if self._context.get("operation") == "remove"
                       else "No radios are available. Configure a radio to continue.")
            self.content.addWidget(_label(message))
        if self._context.get("operation") == "swap":
            self.content.addWidget(_label("Select exactly two radios. Their current saved plans will be swapped."))
        staged_models = {remedy.get("model_id"): int(owner) for owner, remedy in self._remedies.items() if remedy.get("kind") == "allow"}
        for radio in radios:
            radio_id = int(radio["id"])
            check = QCheckBox(str(radio.get("name") or f"Radio {radio_id}"))
            check.setObjectName(f"assignment_radio_{radio_id}")
            check.setChecked(radio_id in self._selected)
            check.setEnabled(self._context.get("operation") != "apply")
            check.toggled.connect(lambda checked, rid=radio_id: self._selection_changed(rid, checked))
            self.content.addWidget(check)
            current_plan = _name(radio.get("current_plan"), "Unassigned")
            if self._context.get("operation") == "remove":
                self.content.addWidget(_label(f"Current plan: {current_plan}"))
                continue
            model = radio.get("model") or {}
            permission = "Scheduling allowed" if model.get("scheduler_enabled") and model.get("enabled", True) and model.get("status", "saved") == "saved" else "Scheduling not allowed"
            self.content.addWidget(_label(f"Current plan: {current_plan} · Operating Model: {_name(model)} · {permission} · {radio.get('status') or 'Status unavailable'}"))
            if radio_id in self._selected and permission != "Scheduling allowed":
                owner = staged_models.get(model.get("id"))
                if owner is not None and owner != radio_id and str(radio_id) not in self._remedies:
                    self.content.addWidget(_label(f"{_name(model)}: Scheduling will be allowed when changes are saved. Existing pauses and holds stay in place."))
                else:
                    self._model_remedies(radio, model)

    def _model_remedies(self, radio, model):
        rid = int(radio["id"])
        name, model_name = _name(radio, f"Radio {rid}"), _name(model, "Unassigned")
        staged = self._remedies.get(str(rid)) or {}
        if staged.get("kind") == "allow":
            self.content.addWidget(_label(f"{model_name}: Scheduling will be allowed when changes are saved. Review the affected radios below."))
        else:
            self.content.addWidget(_label(f"{name} cannot follow this plan yet. Its Operating Model, {model_name}, does not allow scheduling. Allow scheduling below, or choose a different Operating Model."))
        consumers = radio.get("model_consumers") or (self._context.get("model_consumers") or {}).get(str(model.get("id"))) or []
        others = [item for item in consumers if int(item.get("id") or 0) != rid]
        if others:
            self.content.addWidget(_label(f"{model_name} is also used by {', '.join(_name(item) for item in others)}. Allowing scheduling changes this model for all radios using it. Radios with valid assigned plans may begin following them unless paused or held."))
            for item in consumers:
                self.content.addWidget(_label(f"{_name(item)} · Plan: {_name(item.get('current_plan'))} · {item.get('status') or 'Status unavailable'}"))
        remedy = self._remedies.get(str(rid)) or {}
        if remedy.get("kind") == "allow" and not others:
            self.content.addWidget(_label(f"This updates the {model_name} Operating Model and {'swaps the plans' if self._context.get('operation') == 'swap' else f'assigns this plan to {name}'}. Existing pauses and holds stay in place."))
        if remedy:
            chosen = next((option for option in self._context.get("models", ()) if option.get("id") == remedy.get("model_id")), {})
            detail = {"allow": "Scheduling will be allowed when you assign", "model": f"Operating Model {_name(chosen, 'Unavailable')} selected for review", "copy": f"Reusable copy {remedy.get('copy_name') or 'unnamed'} staged for review"}.get(remedy.get("kind"), "Remedy staged")
            self.content.addWidget(_label(detail + ". Existing pauses and holds stay in place."))
        if remedy:
            remove = QPushButton("Remove staged model change")
            remove.clicked.connect(lambda _checked=False, r=rid: self._remove_remedy(r))
            self.content.addWidget(remove)
        if not remedy and model.get("id") and model.get("enabled", True) and model.get("status", "saved") == "saved":
            allow = QPushButton(f"Allow scheduling for all radios using {model_name}…" if others else "Review allowing scheduling…")
            allow.setObjectName(f"assignment_allow_{rid}")
            allow.clicked.connect(lambda _checked=False, r=rid, mid=int(model['id']): self._set_remedy(r, {"kind": "allow", "model_id": mid, "copy_name": ""}))
            self.content.addWidget(allow)
        picker = QComboBox()
        picker.setAccessibleName(f"Different Operating Model for {name}")
        picker.addItem("Choose an existing compatible Operating Model…", None)
        for option in self._context.get("models", ()):
            if option.get("id") and option.get("scheduler_enabled") and option.get("enabled", True) and option.get("status", "saved") == "saved" and (not radio.get("receive_only", False) or option.get("receive_only", False)):
                picker.addItem(_name(option), int(option["id"]))
        if remedy.get("kind") == "model":
            picker.setCurrentIndex(max(0, picker.findData(remedy.get("model_id"))))
        picker.currentIndexChanged.connect(lambda _index, p=picker, r=rid: self._set_remedy(r, {"kind": "model", "model_id": p.currentData(), "copy_name": ""}) if p.currentData() is not None else None)
        self.content.addWidget(picker)
        if model.get("id"):
            row = QHBoxLayout()
            copy_name = QLineEdit()
            copy_name.setPlaceholderText("New reusable Operating Model name")
            copy_name.setAccessibleName(f"New reusable Operating Model name for {name}")
            copy_name.setText(self._copy_names.get(rid, str(remedy.get("copy_name") or "")))
            copy_name.textChanged.connect(lambda text, r=rid: self._copy_names.__setitem__(r, text))
            create = QPushButton("Create reusable copy…")
            create.setEnabled(bool(copy_name.text().strip()))
            copy_name.textChanged.connect(lambda text, button=create: button.setEnabled(bool(text.strip())))
            create.clicked.connect(lambda _checked=False, r=rid, mid=int(model['id']), field=copy_name: self._set_remedy(r, {"kind": "copy", "model_id": mid, "copy_name": field.text().strip()}))
            row.addWidget(copy_name, 1)
            row.addWidget(create)
            self.content.addLayout(row)

    def _render_review(self):
        review = self._review or {}
        impacts = list(review.get("impacts") or ())
        issues = list(review.get("issues") or ())
        if not impacts:
            self._render_radio_context(review)
        else:
            impacted = {str(row.get("radio_id")) for row in impacts}
            context = review.get("radio_context") or {}
            others = [str(row.get("name") or "Radio") for row in context.get("proposed_radios") or ()
                      if isinstance(row, Mapping) and str(row.get("id")) not in impacted]
            if others:
                self.content.addWidget(_label("Also assigned to " + ", ".join(others)))

        first_blocker = True
        finding_widgets = {}
        finding_links = []

        for index, impact in enumerate(impacts):
            if index:
                divider = QFrame()
                divider.setFrameShape(QFrame.Shape.HLine)
                self.content.addWidget(divider)
            radio_id = impact.get("radio_id")
            radio_issues = [issue for issue in issues if str(issue.get("radio_id") or "") == str(radio_id)]
            rf_findings = [
                issue for issue in issues
                if (str(issue.get("category") or "").lower() == "rf_guard"
                    or str(issue.get("code") or "").lower().startswith("rf."))
                and (not issue.get("radio_id")
                     or str(issue.get("radio_id")) == str(radio_id)
                     or any(str(r.get("id")) == str(radio_id)
                            for r in issue.get("related_radios") or () if isinstance(r, Mapping)))
            ]
            severities = {_issue_severity(issue) for issue in rf_findings}
            if "blocked" in severities:
                rf_text, rf_role = "RF Guard · Blocked", "danger"
            elif "warning" in severities:
                rf_text, rf_role = "RF Guard · Warning", "warning"
            elif impact.get("rf_guard_checked"):
                rf_text, rf_role = "RF Guard · No conflicts found", "success"
            elif not impact.get("proposed_plan"):
                rf_text, rf_role = "RF Guard · No proposed plan", "secondary"
            else:
                rf_text, rf_role = "RF Guard · Not verified", "secondary"

            row = QHBoxLayout()
            name = _label(str(impact.get("radio_name") or "Radio"))
            name.setObjectName("assignment_review_radio")
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            row.addWidget(name)
            status = QPushButton(rf_text)
            status.setObjectName("assignment_rf_status")
            status.setProperty("rfRole", rf_role)
            status.setCheckable(not rf_findings)
            status.setAccessibleName(f"{impact.get('radio_name') or 'Radio'} {rf_text}")
            status.setStyleSheet(button_style(rf_role, self._theme))
            row.addWidget(status)
            row.addStretch(1)
            self.content.addLayout(row)
            change = _label(f"{_name(impact.get('current_plan'), 'Unassigned')} → "
                            f"{_name(impact.get('proposed_plan'), 'Unassigned')}")
            change.setObjectName("assignment_radio_change")
            self.content.addWidget(change)
            if str(radio_id) in self._remedies and impact.get("model_name"):
                self.content.addWidget(_label(f"Operating Model after saving: {impact['model_name']}"))
            next_event = impact.get("next_event_display") or impact.get("next_event")
            if next_event:
                self.content.addWidget(_label(f"Next event: {next_event}"))
            if rf_findings:
                finding = next((issue for issue in rf_findings if _issue_severity(issue) == "blocked"),
                               next((issue for issue in rf_findings if _issue_severity(issue) == "warning"),
                                    rf_findings[0]))
                status.setToolTip("Go to the RF Guard finding")
                finding_links.append((status, id(finding)))
            else:
                if impact.get("rf_guard_checked"):
                    explanation = "Configured RF Guard rules found no conflicts. Physical RF isolation and live checks still need verification."
                elif not impact.get("proposed_plan"):
                    explanation = "There is no proposed schedule to check for this radio."
                else:
                    explanation = "The configured RF Guard review could not be verified for this change."
                detail = _label(explanation)
                detail.setObjectName("assignment_rf_explanation")
                detail.hide()
                self.content.addWidget(detail)
                status.toggled.connect(detail.setVisible)
            readiness = [issue for issue in radio_issues if issue.get("code") in
                         {"operation.readiness_unverified", "operation.radio_stopped"}]
            if readiness:
                stopped = any(issue.get("code") == "operation.radio_stopped" for issue in readiness)
                readiness_row = QHBoxLayout()
                readiness_label = _label("Radio stopped · Live readiness not verified" if stopped else
                                         "Live radio readiness not verified")
                readiness_label.setWordWrap(False)
                readiness_row.addWidget(readiness_label)
                route = next(((issue, remedy) for issue in readiness for remedy in issue.get("remedies") or ()
                              if isinstance(remedy, Mapping) and _repair_destination(remedy) == "readiness"), None)
                if route:
                    issue, remedy = route
                    button = self._issue_repair_button(issue, {**remedy, "label": "Check radio readiness…"})
                    button.setObjectName("assignment_radio_readiness")
                    readiness_row.addWidget(button)
                readiness_row.addStretch(1)
                self.content.addLayout(readiness_row)
            for severity, heading in (("blocked", "Must resolve"), ("warning", "Cautions to review"),
                                      ("info", "Operational waits")):
                group = [issue for issue in radio_issues if _issue_severity(issue) == severity
                         and issue not in readiness and issue.get("code") != "execution"]
                if group:
                    title = _label(f"{heading} ({len(group)})")
                    title.setObjectName("assignment_issue_group")
                    font = title.font()
                    font.setBold(True)
                    title.setFont(font)
                    self.content.addWidget(title)
                    for issue in group:
                        finding_widgets[id(issue)] = self._render_issue(
                            issue, primary=severity == "blocked" and first_blocker,
                        )
                        if severity == "blocked":
                            first_blocker = False

        if self._can_commit() and any(issue.get("code") == "execution" for issue in issues):
            self.content.addWidget(_label("An active event may apply after saving when connection, identity, PTT and RF checks permit. Pauses and holds remain."))

        refresh_changes = review.get("source_refresh_changes") or []
        if refresh_changes:
            self.content.addWidget(_label("Review source window changes for every affected radio. Current operation may change after refresh; pending edits, pauses and holds are retained."))
            for change in refresh_changes:
                self.content.addWidget(_label(f"{change.get('plan_name') or 'Plan'} · Windows: {change.get('old_windows', 0)} → {change.get('new_windows', 0)}" + (" · Pending edits retained" if change.get('draft_retained') else "")))
                for key, title in (("old_frequencies", "Current frequencies"), ("new_frequencies", "Refreshed frequencies")):
                    frequencies = list(change.get(key) or ())
                    display = ", ".join(str(value) for value in frequencies[:12]) or "None"
                    if len(frequencies) > 12:
                        display += f" · {len(frequencies) - 12} more"
                    self.content.addWidget(_label(f"{title}: {display}"))
        changes = review.get("plan_changes") or {}
        if changes:
            self.content.addWidget(_label(f"Current plan → proposed changes: {changes.get('old_name') or 'Plan'} → {changes.get('new_name') or 'Plan'} · Windows: {changes.get('old_windows', 0)} → {changes.get('new_windows', 0)}"))
            for key, title in (("old_frequencies", "Current frequencies"), ("new_frequencies", "Proposed frequencies")):
                frequencies = list(changes.get(key) or ())
                display = ", ".join(str(value) for value in frequencies[:12]) or "None"
                if len(frequencies) > 12:
                    display += f" · {len(frequencies) - 12} more"
                self.content.addWidget(_label(f"{title}: {display}"))

        assigned_ids = {str(impact.get("radio_id")) for impact in impacts}
        unscoped = [issue for issue in issues if str(issue.get("radio_id") or "") not in assigned_ids]
        for severity, heading in (("blocked", "Must resolve"), ("warning", "Cautions to review"),
                                  ("info", "Operational waits")):
            group = [issue for issue in unscoped if _issue_severity(issue) == severity]
            if group:
                title = _label(f"{heading} ({len(group)})")
                title.setObjectName("assignment_issue_group")
                font = title.font()
                font.setBold(True)
                title.setFont(font)
                self.content.addWidget(title)
                for issue in group:
                    finding_widgets[id(issue)] = self._render_issue(
                        issue, primary=severity == "blocked" and first_blocker,
                    )
                    if severity == "blocked":
                        first_blocker = False
        for badge, key in finding_links:
            target = finding_widgets.get(key)
            if target is not None:
                badge.clicked.connect(lambda _checked=False, widget=target: self._reveal_review_item(widget))
        if not self._can_commit() and not issues:
            self.content.addWidget(_label("Validation is incomplete. Refresh the review before assigning."))
        self._render_comparison_tabs(review)

    @staticmethod
    def _reveal_review_item(widget):
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(widget)
                break
            parent = parent.parentWidget()
        widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def _render_radio_context(self, review):
        context = review.get("radio_context")
        if not isinstance(context, Mapping):
            return
        plan_name = str(context.get("plan_name") or _name(self._context.get("plan"), "Plan"))
        version = str(context.get("version_label") or "Version unavailable")
        header = _label(f"{plan_name} · Reviewing {version[0].lower() + version[1:]}")
        header.setObjectName("assignment_live_context")
        self.content.addWidget(header)
        for key, title in (("live_radios", "Currently assigned radios"), ("proposed_radios", "Proposed radios")):
            radios = context.get(key)
            if radios is not None:
                names = [str(row.get("name") or "Radio") for row in radios if isinstance(row, Mapping)]
                label = _label(f"{title}: {', '.join(names) if names else 'None'}")
                label.setObjectName("assignment_proposed_context" if key == "proposed_radios" else "assignment_current_context")
                self.content.addWidget(label)

    @staticmethod
    def _window_description(window):
        if not isinstance(window, Mapping):
            return str(window or "—")
        if not window:
            return "—"
        if window.get("display"):
            return str(window["display"])
        fields = []
        for key, label in (("day", "Day"), ("day_utc", "Day"), ("time_zone", "Time zone"),
                           ("timezone", "Time zone"), ("start", "Start"), ("start_utc", "Start"),
                           ("end", "End"), ("end_utc", "End"), ("band", "Band"),
                           ("frequency", "Frequency"), ("mode", "Mode"),
                           ("recurrence", "Recurrence")):
            value = window.get(key)
            if value not in (None, "") and not any(part.startswith(label + ":") for part in fields):
                fields.append(f"{label}: {value}")
        if window.get("overnight") and not any("overnight" in part.lower() for part in fields):
            fields.append("Overnight")
        return " · ".join(fields) or "Window details unavailable"

    def _render_comparison_tabs(self, review):
        comparison = review.get("comparison") or {}
        tabs = _AssignmentReviewTabs()
        tabs.setObjectName("assignment_review_tabs")
        tabs.addTab(self._comparison_page(comparison), "Differences")
        tabs.addTab(self._full_schedule_page(review, comparison), "Full Schedule")
        def tab_changed(index):
            self._review_tab_index = index
            tabs.fit_current_page()

        tabs.setCurrentIndex(self._review_tab_index)
        tab_changed(tabs.currentIndex())
        tabs.currentChanged.connect(tab_changed)
        self.content.addWidget(tabs)

    def _comparison_page(self, comparison):
        page = QWidget()
        layout = QVBoxLayout(page)
        rows = list(comparison.get("rows") or [])
        total = comparison.get("total", len(rows))
        complete = comparison.get("complete", True)
        if complete and not total and not rows:
            page.setProperty("emptyComparison", True)
            empty = _label("No differences in schedule windows.")
            empty.setObjectName("assignment_comparison_status")
            layout.addWidget(empty)
            return page
        kind = comparison.get("kind")
        left_label = str(comparison.get("left_label") or
                         ("Pending changes" if kind == "source_mismatch" else "In use now"))
        right_label = str(comparison.get("right_label") or
                          ("Current sources" if kind == "source_mismatch" else "After Apply"))
        arrow = "↔" if kind == "source_mismatch" else "→"
        heading = _label(f"{left_label} {arrow} {right_label}")
        heading.setObjectName("assignment_comparison_heading")
        layout.addWidget(heading)
        model = QStandardItemModel(0, 5, page)
        model.setHorizontalHeaderLabels(["Source", "Change", left_label, right_label, "Changed fields"])
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            values = [str(row.get("source_label") or "Source unavailable"),
                      str(row.get("change") or "Change unavailable"),
                      self._window_description(row.get("left")),
                      self._window_description(row.get("right")),
                      ", ".join(str(value) for value in row.get("changed_fields") or ()) or "—"]
            items = [QStandardItem(value) for value in values]
            items[0].setData(deepcopy(dict(row)), Qt.ItemDataRole.UserRole)
            for item in items:
                item.setEditable(False)
                item.setToolTip(item.text())
            model.appendRow(items)
        table = QTableView()
        table.setObjectName("assignment_differences_table")
        table.setModel(model)
        table.setWordWrap(True)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(2, header.ResizeMode.Stretch)
        header.setSectionResizeMode(3, header.ResizeMode.Stretch)
        table.setColumnWidth(0, 145)
        table.setColumnWidth(1, 120)
        table.setColumnWidth(4, 120)
        table.verticalHeader().setSectionResizeMode(header.ResizeMode.ResizeToContents)
        table.verticalHeader().hide()
        table.setMinimumHeight(header.sizeHint().height() + 3 * table.fontMetrics().lineSpacing())
        layout.addWidget(table, 1)
        status = (f"Showing {len(rows)} of {total} comparison rows"
                  + (" · Complete" if complete else " · Partial comparison"))
        status_label = _label(status)
        status_label.setObjectName("assignment_comparison_status")
        layout.addWidget(status_label)
        action_row = QHBoxLayout()
        action = QPushButton("Edit selected source…")
        action.setObjectName("assignment_edit_comparison_source")
        action.setEnabled(False)
        action_row.addWidget(action)
        if not complete and comparison.get("next_offset") is not None:
            more = QPushButton("Load more differences…")
            more.setObjectName("assignment_load_more_differences")
            more.clicked.connect(lambda _checked=False:
                self._request_schedule_page(comparison["next_offset"], 100, button=more, view="differences"))
            action_row.addWidget(more)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        if self._schedule_page_error:
            layout.addWidget(_label(self._schedule_page_error))
        def selected_row():
            selected = table.selectionModel().selectedRows()
            if not selected:
                return None
            return model.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        def update_action(*_args):
            row = selected_row()
            target = row.get("target") if isinstance(row, Mapping) else None
            self._selected_comparison_target = self._comparison_target_identity(target)
            action.setEnabled(bool(target))
        table.selectionModel().selectionChanged.connect(update_action)
        selected = getattr(self, "_selected_comparison_target", None)
        if selected is not None:
            for index, row in enumerate(rows):
                if self._comparison_target_identity(row.get("target")) == selected:
                    table.setCurrentIndex(model.index(index, 0))
                    table.selectRow(index)
                    break
        def emit_repair():
            row = selected_row()
            if not isinstance(row, Mapping) or not row.get("target"):
                return
            label = "Edit source…"
            payload = {"plan_id": self._intent().get("plan_id"),
                       "frequency_plan_id": self._intent().get("plan_id"),
                       "target": deepcopy(row["target"]),
                       "remedy_label": label,
                       "remedy_instructions": "Edit the selected comparison row, then review the assignment again."}
            self.repair_requested.emit(payload)
        action.clicked.connect(emit_repair)
        return page

    @staticmethod
    def _comparison_target_identity(target):
        if not isinstance(target, Mapping) or not target:
            return None
        window = target.get("window") or {}
        return (target.get("frequency_plan_id"), target.get("source_ref"),
                *(window.get(field) for field in (
                    "source_key", "source_row_id", "sop_layer_id", "resource_id",
                    "day_utc", "start_utc", "end_utc")))

    def _full_schedule_page(self, review, comparison):
        page = QWidget()
        layout = QVBoxLayout(page)
        context = review.get("radio_context") or {}
        plan_name = str(context.get("plan_name") or _name(self._context.get("plan"), "Plan"))
        version = str(context.get("version_label") or "Version unavailable")
        right_label = str(comparison.get("right_label") or version)
        layout.addWidget(_label(f"{plan_name} · {right_label}"))
        rows = list(comparison.get("full_schedule") or review.get("full_schedule") or [])
        model = QStandardItemModel(0, 2, page)
        model.setHorizontalHeaderLabels(["Source", "Window"])
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            source = str(row.get("source_label") or "Source unavailable")
            description = self._window_description(row)
            items = [QStandardItem(source), QStandardItem(description)]
            for item in items:
                item.setEditable(False)
                item.setToolTip(item.text())
            model.appendRow(items)
        table = QTableView()
        table.setObjectName("assignment_full_schedule_table")
        table.setModel(model)
        table.setWordWrap(True)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)
        total = comparison.get("full_schedule_total", review.get("full_schedule_total", len(rows)))
        complete = comparison.get("full_schedule_complete", review.get("full_schedule_complete", True))
        offset = comparison.get("full_schedule_offset", review.get("full_schedule_offset", 0))
        next_offset = comparison.get("full_schedule_next_offset",
                                     review.get("full_schedule_next_offset"))
        limit = comparison.get("full_schedule_limit", review.get("full_schedule_limit", len(rows)))
        status = f"Showing {len(rows)} of {total} schedule windows" + (" · Complete" if complete else " · Partial")
        layout.addWidget(_label(status))
        if self._schedule_page_error:
            error = _label(f"Could not load more schedule windows: {self._schedule_page_error}")
            error.setObjectName("assignment_schedule_page_error")
            layout.addWidget(error)
        if not complete and limit:
            if next_offset is None:
                next_offset = int(offset or 0) + len(rows)
            more = QPushButton("Retry loading windows…" if self._schedule_page_error else "Load remaining windows…")
            more.setObjectName("assignment_load_more_windows")
            more.clicked.connect(lambda _checked=False, button=more:
                                 self._request_schedule_page(next_offset, int(limit), button=button))
            layout.addWidget(more, alignment=Qt.AlignmentFlag.AlignLeft)
        return page

    def _request_schedule_page(self, offset, limit, review_token=None, button=None, *, view="full_schedule"):
        if self._busy or self._pending_schedule_page:
            return
        review = self._review or {}
        review_token = review_token or (review.get("comparison") or {}).get("page_token") or review.get("token")
        request = {"plan_id": self._intent().get("plan_id"), "offset": int(offset),
                   "limit": int(limit), "review_token": review_token}
        if view != "full_schedule":
            request["view"] = view
        self._pending_schedule_page = deepcopy(request)
        self._schedule_page_error = ""
        if button is not None:
            button.setText("Loading windows…")
            button.setEnabled(False)
        self.schedule_page_requested.emit(request)

    def _render_issue(self, issue, *, primary=False):
        """Display all reasons and actionable remedies from the review snapshot."""
        message = _label(("Primary blocker: " if primary else "") + str(issue.get("message") or "Review this issue."))
        message.setObjectName("assignment_issue_message")
        message.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        font = message.font()
        font.setBold(True)
        message.setFont(font)
        self.content.addWidget(message)
        context = []
        if issue.get("scope_label"):
            context.append(str(issue["scope_label"]))
        if issue.get("affected_sources"):
            context.append("Schedules: " + ", ".join(issue["affected_sources"]))
        elif issue.get("source_label"):
            context.append("Source: " + str(issue["source_label"]))
        radios = [str(row.get("name") or "") for row in (issue.get("related_radios") or ()) if isinstance(row, Mapping) and row.get("name")]
        if radios:
            context.append("Radios: " + ", ".join(radios))
        resource = issue.get("resource") or {}
        if isinstance(resource, Mapping) and resource.get("name"):
            context.append("Resource: " + str(resource["name"]))
        window = issue.get("window") or {}
        if isinstance(window, Mapping):
            timing = window.get("label")
            if not timing:
                timing = " · ".join(str(window.get(key)) for key in ("day_utc", "start_utc", "end_utc") if window.get(key))
                if timing:
                    timing += " UTC"
            if timing:
                context.append("Window: " + str(timing))
        if context:
            self.content.addWidget(_label(" · ".join(context)))
        for field, label in (("impact", "Effect"), ("instructions", "What to do")):
            if issue.get(field):
                self.content.addWidget(_label(f"{label}: {issue[field]}"))
        detail = _issue_detail({
            "window": {key: window.get(key) for key in ("windows", "peer_windows")}
                if isinstance(window, Mapping) else {},
            "window_differences": issue.get("window_differences"),
            "evidence": issue.get("evidence"),
        })
        if detail:
            has_windows = any(detail.startswith(prefix) for prefix in (
                "Plan windows:", "Other radio windows:", "Window differences",
            ))
            subject = "affected windows" if has_windows else "review evidence"
            toggle = QToolButton()
            toggle.setObjectName("assignment_issue_disclosure")
            toggle.setText(f"Show {subject}…")
            toggle.setCheckable(True)
            detail_label = _label(detail)
            detail_label.setObjectName("assignment_issue_detail")
            detail_label.hide()
            def toggle_detail(checked):
                detail_label.setVisible(checked)
                toggle.setText(f"Hide {subject}" if checked else f"Show {subject}…")
                fit_text_action_button(toggle)
            toggle.toggled.connect(toggle_detail)
            fit_text_action_button(toggle)
            self.content.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
            self.content.addWidget(detail_label)
        remedies = issue.get("remedies") or ()
        actions = WrappingControlLayout(alignment=Qt.AlignmentFlag.AlignLeft)
        additional_guidance = []
        for remedy in remedies:
            if not isinstance(remedy, Mapping) or not remedy.get("target"):
                continue
            actions.addWidget(self._issue_repair_button(issue, remedy))
            guidance = str(remedy.get("instructions") or "")
            if guidance and guidance != issue.get("instructions") and guidance not in additional_guidance:
                additional_guidance.append(guidance)
        if not remedies and issue.get("target") and issue.get("action_label"):
            actions.addWidget(self._issue_repair_button(issue))
        if actions.count():
            self.content.addLayout(actions)
        for guidance in additional_guidance:
            self.content.addWidget(_label(guidance))
        return message

    def _issue_repair_button(self, issue, remedy=None):
        label = str(remedy.get("label") or "Review…") if remedy is not None else str(issue["action_label"])
        button = QPushButton(label)
        button.setObjectName("assignment_issue_remedy")
        details = {**deepcopy(issue), "plan_id": self._intent()["plan_id"]}
        if remedy is not None:
            details.update(target=deepcopy(remedy["target"]), remedy_label=label,
                           remedy_instructions=str(remedy.get("instructions") or ""))
        button.clicked.connect(lambda _checked=False: self.repair_requested.emit(deepcopy(details)))
        return button
