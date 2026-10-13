"""Bounded presentation widgets for SOP attention and evidence search.

These widgets only render immutable snapshots supplied by the host. They do
not read operational stores, settings, or network resources.
"""
from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QSignalBlocker, Signal, Qt, QRect, QSize
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from freqinout.core.ops_intelligence import EvidenceItem, SearchBrief as SearchBriefResult
from freqinout.core.sop_awareness import SopAttentionItem, SopAttentionSnapshot
from .theme import active_app_theme, button_style, control_icon, label_style


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class SopAttentionWidget(QWidget):
    """Persistent guidance preference plus a separate attention review."""

    guidanceChanged = Signal(bool)
    contextRequested = Signal(str, dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._snapshot: Any = None
        self._theme: Mapping[str, str] = active_app_theme()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self.guidance_checkbox = QCheckBox("Show optional SOP guidance")
        self.guidance_checkbox.toggled.connect(self.guidanceChanged.emit)
        self.guidance_checkbox.toggled.connect(self._guidance_toggled)
        root.addWidget(self.guidance_checkbox)
        self.guidance_detail = QLabel()
        self.guidance_detail.setWordWrap(True)
        self.guidance_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.guidance_detail.hide()
        root.addWidget(self.guidance_detail)
        self.preference_status = QLabel()
        self.preference_status.setWordWrap(True)
        self.preference_status.setTextFormat(Qt.TextFormat.PlainText)
        self.preference_status.hide()
        root.addWidget(self.preference_status)
        self.attention_frame = QWidget()
        attention = QVBoxLayout(self.attention_frame)
        attention.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.attention_header = QLabel("Review SOP attention")
        self.attention_header.setToolTip("Scheduled reminders across all active SOP profiles. Dashboard filters do not hide these attention items.")
        self.attention_header.setWordWrap(True)
        self.attention_header.setTextFormat(Qt.TextFormat.PlainText)
        head.addWidget(self.attention_header, 1)
        self.review_button = QPushButton("Review")
        self.review_button.setCheckable(True)
        self.review_button.toggled.connect(self._show_items)
        head.addWidget(self.review_button)
        attention.addLayout(head)
        self.builder_button = QPushButton("Open SOP Builder")
        self.builder_button.clicked.connect(lambda: self.contextRequested.emit("sop", {}))
        attention.addWidget(self.builder_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.attention_state = QLabel()
        self.attention_state.setWordWrap(True)
        self.attention_state.setTextFormat(Qt.TextFormat.PlainText)
        attention.addWidget(self.attention_state)
        self.items_frame = QFrame()
        self.items_layout = QVBoxLayout(self.items_frame)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        attention.addWidget(self.items_frame)
        root.addWidget(self.attention_frame)
        self._show_items(False)
        self.set_snapshot(SopAttentionSnapshot())
        self.apply_theme()

    def set_guidance_enabled(self, enabled: bool) -> None:
        blocker = QSignalBlocker(self.guidance_checkbox)
        self.guidance_checkbox.setChecked(bool(enabled))
        del blocker
        self._guidance_toggled(bool(enabled))

    def set_preference_error(self, text: str) -> None:
        self.preference_status.setText(f"Could not save SOP guidance preference: {text}")
        self.preference_status.setVisible(bool(text))

    def _guidance_toggled(self, enabled: bool) -> None:
        upcoming = max(0, int(_value(self._snapshot, "upcoming_count", 0) or 0))
        visible = bool(enabled and upcoming)
        preview = tuple(item for item in (_value(self._snapshot, "items", ()) or ())
            if _value(item, "status", "") == "Upcoming")[:3]
        lines = [f"SOP guidance · {upcoming} upcoming scheduled action{'s' if upcoming != 1 else ''}"]
        for item in preview:
            lines.append(f"{_text(_value(item, 'group_name', ''))} · {_text(_value(item, 'label', ''))} · {_text(_value(item, 'due_utc', ''))}")
        self.guidance_detail.setText("\n".join(lines) if upcoming else "")
        self.guidance_detail.setVisible(visible)

    def set_snapshot(self, snapshot: Any) -> None:
        self._snapshot = snapshot
        state = _text(_value(snapshot, "state", "pending")).lower()
        pending = bool(_value(snapshot, "pending", False)) or state == "pending"
        stale = bool(_value(snapshot, "stale", False)) or state == "stale"
        unavailable = bool(_value(snapshot, "unavailable", False)) or state in {"error", "unavailable"}
        fresh = bool(_value(snapshot, "fresh", False)) or state == "fresh"
        partial = bool(_value(snapshot, "partial", False)) or state == "partial"
        count = max(0, int(_value(snapshot, "actionable_count", 0) or 0))
        overdue = max(0, int(_value(snapshot, "overdue_count", 0) or 0))
        # Fresh, complete zero is the only state allowed to remove the attention banner.
        show = not (fresh and not partial and not pending and not stale and not unavailable and count == 0)
        self.attention_frame.setVisible(show)
        self._guidance_toggled(self.guidance_checkbox.isChecked())
        if not show:
            return
        if count:
            self.attention_header.setText(f"Review SOP attention · {count} due/overdue")
        else:
            self.attention_header.setText("Review SOP attention")
        if pending:
            status = "SOP status updating."
        elif stale:
            status = "Last-known SOP attention may be stale. Refreshing status is pending."
        elif unavailable:
            status = "SOP attention is unavailable; this is not an all-clear."
        elif partial:
            status = "Counts cover active SOP reminders; only the first 256 details are retained."
        elif fresh and count == 0:
            status = "No due actions in the fresh checked scope."
        else:
            status = f"{overdue} overdue; {max(0, count - overdue)} due now."
        self.attention_state.setText(status)
        self._render_items(tuple(_value(snapshot, "items", ()) or ())[:8])

    def _render_items(self, items: tuple[Any, ...]) -> None:
        while self.items_layout.count():
            item = self.items_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not items:
            label = QLabel("No item details are available in this snapshot.")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            self.items_layout.addWidget(label)
            return
        for item in items:
            group = _text(_value(item, "group_name", ""))
            label = _text(_value(item, "label", "SOP action"))
            status = _text(_value(item, "status", "unverified"))
            due = _text(_value(item, "due_utc", ""))
            description = _text(_value(item, "description", ""))
            lines = [f"{group} · {label}" if group else label, f"Status: {status}" + (f" · Due: {due}" if due else "")]
            if description:
                lines.append(description)
            row = QLabel("\n".join(lines))
            row.setWordWrap(True)
            row.setTextFormat(Qt.TextFormat.PlainText)
            self.items_layout.addWidget(row)
        total = max(len(items), int(_value(self._snapshot, "actionable_count", len(items)) or 0))
        if total > len(items):
            remaining = QLabel(f"{total - len(items)} additional attention items are not shown here.")
            remaining.setWordWrap(True)
            remaining.setTextFormat(Qt.TextFormat.PlainText)
            self.items_layout.addWidget(remaining)

    def _show_items(self, visible: bool) -> None:
        self.items_frame.setVisible(bool(visible))
        self.review_button.setText("Hide" if visible else "Review")

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        self._theme = dict(theme or active_app_theme())
        colors = self._theme
        self.attention_header.setStyleSheet(label_style("text", colors, weight=700))
        self.attention_state.setStyleSheet(label_style("muted", colors))
        self.review_button.setStyleSheet(button_style("warning", colors))
        self.builder_button.setStyleSheet(button_style("muted", colors))
        self.preference_status.setStyleSheet(label_style("danger", colors))
        self.guidance_detail.setStyleSheet(label_style("muted", colors))
        self.review_button.setIcon(control_icon("clock", dict(colors), 16, self.devicePixelRatioF(), "warning"))
        self.builder_button.setIcon(control_icon("sop", dict(colors), 16, self.devicePixelRatioF(), "text_muted"))


class SearchBriefWidget(QWidget):
    """Bounded evidence view; summaries remain explicitly attributed."""

    contextRequested = Signal(str, dict)
    closed = Signal()
    PAGE_SIZE = 50
    MAX_EVIDENCE = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme: Mapping[str, str] = active_app_theme()
        self._result: Any = None
        self._items: tuple[Any, ...] = ()
        self._page = 0
        self._selected: Any = None
        self._error = False
        self._loading = False
        self._truncated = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        heading = QHBoxLayout()
        self.title = QLabel("Situation search")
        self.title.setStyleSheet(label_style("text", self._theme, weight=700))
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.title, 1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self._close)
        heading.addWidget(self.close_button)
        root.addLayout(heading)
        self.status = QLabel("Search has not been run.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        self.coverage_button = QPushButton("Sources and coverage")
        self.coverage_button.setCheckable(True)
        self.coverage_detail = QLabel()
        self.coverage_detail.setWordWrap(True)
        self.coverage_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.coverage_detail.hide()
        self.coverage_button.toggled.connect(self.coverage_detail.setVisible)
        root.addWidget(self.coverage_button, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self.coverage_detail)
        self.summary_title = QLabel("Reported information in this scope")
        self.summary_title.setStyleSheet(label_style("text", self._theme, weight=600))
        root.addWidget(self.summary_title)
        self.summary_lines = QLabel()
        self.summary_lines.setWordWrap(True)
        self.summary_lines.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lines.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        root.addWidget(self.summary_lines)
        self.sop_match_label = QLabel()
        self.sop_match_label.setWordWrap(True)
        self.sop_match_label.setTextFormat(Qt.TextFormat.PlainText)
        self.sop_match_label.hide()
        root.addWidget(self.sop_match_label)
        self.open_sop_button = QPushButton("Open SOP")
        self.open_sop_button.clicked.connect(lambda: self.contextRequested.emit("sop", {}))
        self.open_sop_button.hide()
        root.addWidget(self.open_sop_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.evidence_label = QLabel("Evidence · 0–0 of 0")
        root.addWidget(self.evidence_label)
        self.evidence_list = QListWidget()
        self.evidence_list.setWordWrap(True)
        self.evidence_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.evidence_list.setUniformItemSizes(False)
        self.evidence_list.currentRowChanged.connect(self._select_row)
        root.addWidget(self.evidence_list)
        pager = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        self.page_label = QLabel("Page 0 of 0")
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(lambda: self._change_page(1))
        pager.addWidget(self.previous_button)
        pager.addWidget(self.page_label, 1)
        pager.addWidget(self.next_button)
        root.addLayout(pager)
        routes = QHBoxLayout()
        self.inbox_button = QPushButton("Open in Inbox")
        self.map_button = QPushButton("View on Map")
        self.compose_button = QPushButton("Compose")
        routes.addWidget(self.inbox_button)
        routes.addWidget(self.map_button)
        routes.addWidget(self.compose_button)
        root.addLayout(routes)
        self.inbox_button.clicked.connect(lambda: self._route("inbox"))
        self.map_button.clicked.connect(lambda: self._route("map"))
        self.compose_button.clicked.connect(lambda: self._route("compose"))
        self.apply_theme()

    def set_loading(self, kind: str, query: str) -> None:
        self._error = False
        self._loading = True
        self._truncated = False
        self._result = None
        self._items = ()
        self._selected = None
        self._page = 0
        self.title.setText(f"{_text(kind) or 'Situation'} search")
        self.status.setText(f"Searching the selected scope for: {query}\nSearch is bounded and may not cover every source.")
        self._render()

    def set_result(self, result: Any) -> None:
        self._error = False
        self._loading = False
        self._result = result
        self._page = 0
        self.title.setText(f"{_text(_value(result, 'kind', 'Situation')) or 'Situation'} search")
        supplied_items = tuple(_value(result, "evidence", ()) or ())
        self._truncated = len(supplied_items) > self.MAX_EVIDENCE
        items = supplied_items[: self.MAX_EVIDENCE]
        self._items = items
        self._selected = items[0] if items else None
        self._render()

    def set_error(self, text: str) -> None:
        self._error = True
        self._loading = False
        self._result = None
        self._items = ()
        self._selected = None
        self.status.setText(f"Search unavailable: {text}\nEvidence status is unverified; no conclusion can be drawn from this search.")
        self._render()

    def _render(self) -> None:
        result = self._result
        if result is not None:
            query = _text(_value(result, "query", ""))
            scope = _text(_value(result, "scope_label", "selected scope")) or "selected scope"
            limited = bool(_value(result, "limited", False))
            coverage = tuple(_value(result, "coverage", ()) or ())
            coverage_text = "; ".join(_text(part) for part in coverage[:8])
            self.status.setText(
                f"Query: {query}\nScope: {scope}\n"
                + ("Results are limited. " if limited or self._truncated else "")
                + "Reports may be unverified; review sources and coverage before drawing conclusions."
            )
            self.coverage_detail.setText(coverage_text or "Coverage details are not provided.")
        self.coverage_button.setVisible(result is not None)
        self.coverage_detail.setVisible(result is not None and self.coverage_button.isChecked())
        summaries = tuple(_value(result, "summaries", ()) or ())[:8] if result is not None else ()
        if self._error:
            summary_lines = "Search failed. This does not establish absence of an incident or duty."
        elif self._loading:
            summary_lines = "Search in progress; evidence is not available yet."
        elif not self._items:
            summary_lines = "No matching evidence in the selected scope. This does not establish absence of an incident or duty."
        elif summaries:
            summary_lines = "\n".join(f"Reported information: {_text(summary)}" for summary in summaries)
        else:
            summary_lines = "Matching evidence is available; no known-information summary was supplied."
        self.summary_lines.setText(summary_lines)
        matches = tuple(_value(result, "sop_matches", ()) or ())[:8] if result is not None else ()
        if matches:
            match_lines = [
                f"Matching active SOP instructions · {_text(_value(result, 'sop_state', 'unverified'))}",
                "Instruction text matches; these are not event-trigger confirmations.",
            ]
            for match in matches:
                label = _text(_value(match, "label", "SOP instruction"))
                group = _text(_value(match, "group_name", ""))
                description = _text(_value(match, "description", ""))
                due = _text(_value(match, "due_utc", ""))
                details = " · ".join(value for value in (group, due) if value)
                match_lines.append(f"{group + ' · ' if group else ''}{label}" + (f" ({details})" if details else ""))
                if description:
                    match_lines.append(description)
            self.sop_match_label.setText("\n".join(match_lines))
            self.sop_match_label.show()
            self.open_sop_button.show()
        else:
            self.sop_match_label.clear()
            self.sop_match_label.hide()
            self.open_sop_button.hide()
        self.evidence_list.clear()
        start = self._page * self.PAGE_SIZE
        visible = self._items[start : start + self.PAGE_SIZE]
        for item in visible:
            summary = _text(_value(item, "summary", "")) or "No summary supplied"
            source = _text(_value(item, "source_family", "unknown"))
            source_ref = _text(_value(item, "source_ref", ""))
            received = _text(_value(item, "received_utc", "time unavailable"))
            who = _text(_value(item, "callsign", "")) or _text(_value(item, "group_name", "")) or "Source"
            status = _text(_value(item, "status", "unverified")) or "unverified"
            provenance = " · ".join(part for part in (who, source, source_ref, received, status) if part)
            entry = QListWidgetItem(f"{provenance}\nReported: {summary}")
            self.evidence_list.addItem(entry)
        total = len(self._items)
        end = min(total, start + len(visible))
        self.evidence_label.setText(f"Evidence · {start + 1 if total else 0}–{end} of {total}" + (" (maximum 200 shown)" if total == self.MAX_EVIDENCE else ""))
        pages = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        self.page_label.setText(f"Page {self._page + 1 if pages else 0} of {pages}")
        self.previous_button.setEnabled(self._page > 0)
        self.next_button.setEnabled(self._page + 1 < pages)
        self.evidence_list.setCurrentRow(0 if visible else -1)
        self._select_row(0 if visible else -1)
        self._fit_evidence_rows()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_evidence_rows()

    def _fit_evidence_rows(self) -> None:
        width = max(1, self.evidence_list.viewport().width() - 8)
        metrics = self.evidence_list.fontMetrics()
        for row in range(self.evidence_list.count()):
            item = self.evidence_list.item(row)
            bounds = metrics.boundingRect(QRect(0, 0, width, 10000), Qt.TextFlag.TextWordWrap, item.text())
            item.setSizeHint(QSize(width, max(metrics.lineSpacing(), bounds.height()) + 8))
        wanted = sum(self.evidence_list.item(i).sizeHint().height() for i in range(min(3, self.evidence_list.count()))) + metrics.lineSpacing()
        height = min(metrics.lineSpacing() * 12, max(metrics.lineSpacing() * 3, wanted))
        if self.evidence_list.maximumHeight() != height:
            self.evidence_list.setMaximumHeight(height)

    def _select_row(self, row: int) -> None:
        index = self._page * self.PAGE_SIZE + row
        self._selected = self._items[index] if row >= 0 and 0 <= index < len(self._items) else None
        for button in (self.inbox_button, self.map_button, self.compose_button):
            button.setEnabled(self._selected is not None)

    def _change_page(self, delta: int) -> None:
        pages = (len(self._items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        self._page = min(max(0, self._page + delta), max(0, pages - 1))
        self._render()

    def _route(self, kind: str) -> None:
        item = self._selected
        result = self._result
        context = {
            "callsign": _text(_value(item, "callsign", "")),
            "group_filter": _text(_value(item, "group_name", "")),
            "source_family": _text(_value(item, "source_family", "")),
            "source_ref": _text(_value(item, "source_ref", "")),
            "search_query": _text(_value(result, "query", "")),
        }
        if result is not None and _value(result, "kind", "") == "topic":
            context["topic_filter"] = _text(_value(result, "query", ""))
        if item is not None:
            self.contextRequested.emit(kind, context)

    def _close(self) -> None:
        self.hide()
        self.closed.emit()

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        colors = dict(theme or active_app_theme())
        self.title.setStyleSheet(label_style("text", colors, weight=700))
        self.summary_title.setStyleSheet(label_style("text", colors, weight=600))
        self.close_button.setStyleSheet(button_style("muted", colors))
        self.coverage_button.setStyleSheet(button_style("muted", colors))
        self.coverage_detail.setStyleSheet(label_style("muted", colors))
        for button in (self.previous_button, self.next_button, self.inbox_button, self.map_button, self.compose_button):
            button.setStyleSheet(button_style("secondary", colors))
        self.status.setStyleSheet(label_style("muted", colors))
        self.summary_lines.setStyleSheet(label_style("text", colors))
        self.sop_match_label.setStyleSheet(label_style("muted", colors))
        self.open_sop_button.setStyleSheet(button_style("muted", colors))
        self.open_sop_button.setIcon(control_icon("sop", dict(colors), 16, self.devicePixelRatioF(), "text_muted"))
        self.evidence_label.setStyleSheet(label_style("muted", colors, weight=600))
