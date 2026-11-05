"""Cached, signal-only review of deliberate plan source relinking.

The host owns source catalogs, validation, commit and navigation. This widget
never reads configuration or changes a plan, assignment, hold or endpoint.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from typing import Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget

from .theme import active_app_theme, button_style


def _label(text=""):
    widget = QLabel(str(text))
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _severity(issue):
    severity = str(issue.get('severity') or 'blocked').lower()
    if severity in {'warning', 'caution'}:
        return 'warning'
    if severity in {'info', 'wait'}:
        return 'info'
    return 'blocked'


def _frequencies(values):
    values = list(values or ())
    text = ', '.join(str(value) for value in values[:12]) or 'None'
    return text + (f' · {len(values) - 12} more' if len(values) > 12 else '')


class PlanRelinkPanel(QWidget):
    """Choose Daily and Net sources, review consequences, then apply once."""

    preview_requested = Signal(object)
    commit_requested = Signal(object)
    repair_requested = Signal(object)
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context = {}
        self._review = None
        self._pending = None
        self._busy = False
        self._theme = active_app_theme()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.title = _label('Link plan to schedule sources')
        title_font = self.title.font()
        title_font.setBold(True)
        self.title.setFont(title_font)
        root.addWidget(self.title)
        self.summary = _label()
        root.addWidget(self.summary)
        root.addWidget(_label('This plan keeps its current windows until you apply. Linking replaces its windows with the selected schedules and follows later edits to those sources. Existing SOP sources and saved SOP entries are retained; review their resulting windows below.'))
        root.addWidget(_label('Radio assignments, pauses and holds stay in place. Operation may change after applying when current safety checks permit.'))
        self.daily_label = _label('HF Daily schedule')
        root.addWidget(self.daily_label)
        self.daily_combo = QComboBox()
        self.daily_combo.setAccessibleName('HF Daily schedule source')
        self.daily_label.setBuddy(self.daily_combo)
        root.addWidget(self.daily_combo)
        self.nets_label = _label('HF Net schedule')
        root.addWidget(self.nets_label)
        self.nets_combo = QComboBox()
        self.nets_combo.setAccessibleName('HF Net schedule source')
        self.nets_label.setBuddy(self.nets_combo)
        root.addWidget(self.nets_combo)
        self.daily_combo.currentIndexChanged.connect(self._selection_changed)
        self.nets_combo.currentIndexChanged.connect(self._selection_changed)
        self.status = _label('Choose both sources, then review the changes.')
        root.addWidget(self.status)
        self.details = QVBoxLayout()
        root.addLayout(self.details)
        actions = QHBoxLayout()
        self.review_button = QPushButton('Review changes…')
        self.review_button.clicked.connect(self._request_preview)
        actions.addWidget(self.review_button)
        self.apply_button = QPushButton('Apply links')
        self.apply_button.clicked.connect(self._commit)
        actions.addWidget(self.apply_button)
        self.back_button = QPushButton('Change sources')
        self.back_button.clicked.connect(self._back)
        actions.addWidget(self.back_button)
        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        root.addLayout(actions)
        self.apply_theme()
        self._render_actions()

    def apply_theme(self, theme=None):
        self._theme = theme or active_app_theme()
        self.review_button.setStyleSheet(button_style('primary', self._theme))
        self.apply_button.setStyleSheet(button_style('primary', self._theme))
        self.back_button.setStyleSheet(button_style('secondary', self._theme))
        self.cancel_button.setStyleSheet(button_style('secondary', self._theme))

    def set_context(self, context):
        context = deepcopy(context or {})
        old_id = (self._context.get('plan') or {}).get('id')
        new_id = (context.get('plan') or {}).get('id')
        previous = (self.daily_combo.currentData(), self.nets_combo.currentData()) if old_id == new_id and self._context else (None, None)
        self._context = context
        self.summary.setText('Plan: ' + str((context.get('plan') or {}).get('name') or 'No saved plan selected'))
        for combo, key, selected in ((self.daily_combo, 'daily', previous[0]), (self.nets_combo, 'nets', previous[1])):
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.addItem('Choose a schedule…', None)
            for choice in (context.get('source_choices') or {}).get(key, ()):
                if isinstance(choice, Mapping) and 'ref' in choice and choice['ref'] is not None:
                    combo.addItem(str(choice.get('label') or choice['ref'] or 'None'), str(choice['ref']))
                    combo.setItemData(combo.count() - 1, str(choice.get('tooltip') or choice.get('label') or ''), Qt.ItemDataRole.ToolTipRole)
            index = combo.findData(selected) if selected is not None else -1
            combo.setCurrentIndex(index if index >= 1 else 0)
            combo.blockSignals(blocked)
        self._review = None
        self._pending = None
        self._busy = False
        _clear(self.details)
        self.status.setText('Choose both sources, then review the changes.')
        self._render_actions()

    def _intent(self):
        daily, nets = self.daily_combo.currentData(), self.nets_combo.currentData()
        if daily is None or nets is None:
            return None
        # An explicit empty choice is represented by absence from the refs list;
        # it is different from the unselected placeholder (None).
        return {'plan_id': (self._context.get('plan') or {}).get('id'),
                'source_refs': [str(ref) for ref in (daily, nets) if str(ref)]}

    def _selection_changed(self, *_):
        self._review = None
        self._pending = None
        self._busy = False
        _clear(self.details)
        intent = self._intent()
        self.status.setText('Choose at least one Daily or Net schedule to link.' if intent is not None and not intent['source_refs'] else 'Sources changed. Review the changes before applying.')
        self._render_actions()

    def _request_preview(self):
        intent = self._intent()
        if self._busy or not intent or not intent.get('plan_id') or not intent.get('source_refs'):
            return
        self._pending = deepcopy(intent)
        self._busy = True
        self.status.setText('Checking source windows, affected radios and RF Guard…')
        self._render_actions()
        self.preview_requested.emit(deepcopy(intent))

    def set_review(self, review):
        if self._pending is None or self._pending != self._intent():
            return
        self._review = deepcopy(review or {})
        self._pending = None
        self._busy = False
        self.status.setText('Review the changes and any issues below.')
        self._render_review()
        self._render_actions()

    def set_busy(self, busy):
        self._busy = bool(busy)
        self._render_actions()

    def set_error(self, message):
        self._busy = False
        self._pending = None
        self._review = None
        _clear(self.details)
        self.status.setText(str(message or 'Unable to review the selected sources. Your choices are retained.'))
        self._render_actions()

    def _back(self):
        self._busy = False
        self._pending = None
        self._review = None
        _clear(self.details)
        self.status.setText('Change sources, then review again.')
        self._render_actions()

    def _can_apply(self):
        review = self._review or {}
        return bool(review.get('token') and review.get('can_commit') and
                    not any(_severity(issue) == 'blocked' for issue in review.get('issues', ())))

    def _commit(self):
        if self._busy or not self._can_apply():
            return
        plan_id = (self._context.get('plan') or {}).get('id')
        self._busy = True
        self._render_actions()
        self.commit_requested.emit({'refresh_token': self._review['token'], 'refresh_plan_ids': [plan_id]})

    def _render_actions(self):
        self.daily_combo.setEnabled(not self._busy)
        self.nets_combo.setEnabled(not self._busy)
        self.cancel_button.setEnabled(not self._busy)
        self.review_button.setVisible(self._review is None)
        intent = self._intent()
        self.review_button.setEnabled(not self._busy and bool(intent and intent.get('source_refs')) and bool((self._context.get('plan') or {}).get('id')))
        self.apply_button.setVisible(self._review is not None)
        self.apply_button.setEnabled(not self._busy and self._can_apply())
        self.back_button.setVisible(self._review is not None)
        self.back_button.setEnabled(not self._busy)

    def _source_names(self, refs):
        labels = {str(choice.get('ref')): str(choice.get('label') or choice.get('ref'))
                  for choices in (self._context.get('source_choices') or {}).values()
                  for choice in choices if isinstance(choice, Mapping) and choice.get('ref')}
        labels.update({'hf_daily': 'Saved Daily windows (snapshot)', 'hf_nets': 'Saved Net windows (snapshot)', 'sop': 'Saved SOP entries'})
        return ', '.join(labels.get(str(ref), str(ref)) for ref in (refs or ())) or 'None'

    def _render_review(self):
        _clear(self.details)
        review = self._review or {}
        for change in review.get('source_refresh_changes') or ():
            self.details.addWidget(_label(f"{change.get('plan_name') or 'Plan'} · Windows: {change.get('old_windows', 0)} → {change.get('new_windows', 0)}"))
            if 'old_source_refs' in change or 'new_source_refs' in change:
                self.details.addWidget(_label('Current sources: ' + self._source_names(change.get('old_source_refs'))))
                self.details.addWidget(_label('Linked sources: ' + self._source_names(change.get('new_source_refs'))))
            self.details.addWidget(_label('Current frequencies: ' + _frequencies(change.get('old_frequencies'))))
            self.details.addWidget(_label('Linked frequencies: ' + _frequencies(change.get('new_frequencies'))))
            if change.get('draft_retained'):
                self.details.addWidget(_label('Saved draft content is retained. Applying that draft later can replace these links; review it before applying.'))
            if 'old_schedule_refs' in change or 'new_schedule_refs' in change:
                self._window_comparison(change.get('old_schedule_refs') or [], change.get('new_schedule_refs') or [])
        impacts = review.get('impacts') or ()
        if impacts:
            self.details.addWidget(_label('Affected radios'))
        else:
            self.details.addWidget(_label('No radios currently use this plan.'))
        for impact in impacts:
            self.details.addWidget(_label(f"{impact.get('radio_name') or 'Radio'} · {impact.get('status') or 'Current state unavailable'}"))
        if review.get('rf_guard'):
            self.details.addWidget(_label('RF Guard: ' + str(review['rf_guard'])))
        issues = list(review.get('issues') or ())
        for severity, heading in (('blocked', 'Must resolve'), ('warning', 'Cautions to review'), ('info', 'Operational waits')):
            group = [issue for issue in issues if _severity(issue) == severity]
            if group:
                self.details.addWidget(_label(f'{heading} ({len(group)})'))
            for issue in group:
                self.details.addWidget(_label(str(issue.get('message') or 'Review this issue.')))
                for field, prefix in (('impact', 'Effect'), ('instructions', 'What to do')):
                    if issue.get(field):
                        self.details.addWidget(_label(f'{prefix}: {issue[field]}'))
                remedies = issue.get('remedies') or ()
                available = [item for item in remedies if isinstance(item, Mapping) and item.get('label') and item.get('target')]
                if not available and issue.get('action_label') and issue.get('target'):
                    available = [{'label': issue['action_label'], 'target': issue['target']}]
                for remedy in available:
                    button = QPushButton(str(remedy['label']))
                    payload = {**deepcopy(issue), 'target': deepcopy(remedy['target']),
                               'plan_id': (self._context.get('plan') or {}).get('id'),
                               'remedy_label': str(remedy['label']),
                               'remedy_instructions': str(remedy.get('instructions') or '')}
                    button.clicked.connect(lambda _checked=False, data=payload: self.repair_requested.emit(deepcopy(data)))
                    self.details.addWidget(button)
        if not self._can_apply() and not issues:
            self.details.addWidget(_label('Review is incomplete. Change sources or try again.'))

    def _window_comparison(self, old_rows, new_rows):
        """Expose every supplied current/proposed window in an optional read-only view."""
        def signature(row):
            return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        before, after = Counter(map(signature, old_rows)), Counter(map(signature, new_rows))
        added, removed = sum((after - before).values()), sum((before - after).values())
        self.details.addWidget(_label(f"Window changes: {added} added or changed; {removed} removed or changed."))
        toggle = QToolButton()
        toggle.setText('Compare schedule windows…')
        toggle.setCheckable(True)
        toggle.setAccessibleName('Compare schedule windows')
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setAccessibleName('Current and proposed schedule windows')
        view.setMaximumHeight(max(150, self.fontMetrics().lineSpacing() * 12))
        def describe(row, index):
            if not isinstance(row, Mapping):
                return f"{index}. {row}"
            fields = (('day_utc','Day'), ('recurrence','Recurrence'), ('start_utc','Start UTC'),
                      ('end_utc','End UTC'), ('group_name','Group'), ('frequency','Frequency'),
                      ('band','Band'), ('mode','Mode'), ('source','Source'))
            parts = [f"{label}: {row[key]}" for key, label in fields if row.get(key) not in (None, '')]
            return f"{index}. " + (' · '.join(parts) or 'No window details')
        lines = ['Current windows'] + ([describe(row, i) for i, row in enumerate(old_rows, 1)] or ['None'])
        lines += ['', 'Proposed windows'] + ([describe(row, i) for i, row in enumerate(new_rows, 1)] or ['None'])
        view.setPlainText('\n'.join(lines))
        view.hide()
        toggle.toggled.connect(view.setVisible)
        toggle.toggled.connect(lambda checked, button=toggle: button.setText('Hide schedule comparison' if checked else 'Compare schedule windows…'))
        self.details.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.details.addWidget(view)
