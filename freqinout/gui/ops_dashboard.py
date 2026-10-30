"""Ops Center presentation only. Producers and command ownership stay in host."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import Qt, Signal, QEvent, QSize
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QTabBar, QScrollArea,
    QFrame, QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QSizePolicy, QLabel,
)

from .current_page_stack import CurrentPageStack
from .net_preparation_panel import add_schedule_review_action
from .theme import button_style, control_icon, font_derived_widget_height, mark_text_size_guard_opt_out


def _get(snapshot, name, default=None):
    return snapshot.get(name, default) if isinstance(snapshot, Mapping) else getattr(snapshot, name, default)


class OpsDashboard(QWidget):
    """One selected detail page plus persistent, bounded published summaries."""

    pageChanged = Signal(str)
    scheduleAction = Signal(dict)
    mapRequested = Signal()
    PAGES = (
        ("overview", "Overview"), ("traffic", "Traffic"), ("sop", "SOP Actions"),
        ("peers", "Find Operator"), ("schedule", "Schedule"),
        ("propagation", "Propagation"), ("search", "Search"),
    )

    def __init__(self, parent=None, *, action_role=None, action_tooltip=None):
        super().__init__(parent)
        self._theme = {}
        self._upcoming = ()
        self._upcoming_published = False
        self._attention_role = "secondary"
        self._action_role = action_role or (lambda _kind: "secondary")
        self._action_tooltip = action_tooltip or (lambda _kind: "")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabBar()
        self.tabs.setAccessibleName("Ops Center views")
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(True)
        root.addWidget(self.tabs)
        self.stack = CurrentPageStack()
        self.pages = {}
        self.page_layouts = {}
        for key, label in self.PAGES:
            self.tabs.addTab(label)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignTop)
            scroll.setWidget(content)
            self.stack.addWidget(scroll)
            self.pages[key] = content
            self.page_layouts[key] = layout
        search_hint = QLabel("Search a callsign, topic or SOP event using Search in the toolbar.")
        search_hint.setWordWrap(True)
        self.page_layouts["search"].addWidget(search_hint)
        self.body = QBoxLayout(QBoxLayout.LeftToRight)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(self.stack, 3)
        self.upcoming_box = QGroupBox("Upcoming")
        upcoming_layout = QVBoxLayout(self.upcoming_box)
        self.upcoming_status = QLabel("Awaiting schedule update")
        self.upcoming_status.setWordWrap(True)
        self.upcoming_status.setMinimumHeight(self.upcoming_status.fontMetrics().height() * 2)
        upcoming_layout.addWidget(self.upcoming_status)
        self.upcoming_table = QTableWidget(0, 3)
        self.upcoming_table.setAccessibleName("Next four schedule and SOP actions")
        self.upcoming_table.horizontalHeader().hide()
        self.upcoming_table.verticalHeader().hide()
        self.upcoming_table.setShowGrid(False)
        self.upcoming_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.upcoming_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.upcoming_table.setWordWrap(False)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.upcoming_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        upcoming_layout.addWidget(self.upcoming_table)
        self.schedule_link = QPushButton("Open Schedule")
        self.schedule_link.clicked.connect(lambda: self.select_page("schedule"))
        upcoming_layout.addWidget(self.schedule_link)
        self.upcoming_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.body.addWidget(self.upcoming_box, 2, Qt.AlignTop)
        root.addLayout(self.body, 1)
        self.quick_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.situation_link = QPushButton("Situation · awaiting update")
        self.situation_link.clicked.connect(lambda: self.select_page("traffic"))
        self.map_link = QPushButton("Map")
        self.map_link.clicked.connect(self.mapRequested.emit)
        self.peer_link = QPushButton("Operators · awaiting update")
        self.peer_link.clicked.connect(lambda: self.select_page("peers"))
        self.prop_link = QPushButton("RF · awaiting update")
        self.prop_link.clicked.connect(lambda: self.select_page("propagation"))
        for button in (self.situation_link, self.map_link, self.peer_link, self.prop_link):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.quick_row.addWidget(button)
        root.addLayout(self.quick_row)
        # Header-owned so attention never depends on the selected detail page.
        self.attention_button = QPushButton("SOP · updating")
        self.attention_button.setAccessibleName("Review SOP attention")
        self.attention_button.clicked.connect(lambda: self.select_page("sop"))
        self.tabs.currentChanged.connect(self._page_changed)
        self.set_upcoming((), published=False)

    @property
    def current_page(self):
        return self.PAGES[self.tabs.currentIndex()][0]

    def select_page(self, key):
        index = next((i for i, (name, _) in enumerate(self.PAGES) if name == key), 0)
        self.tabs.setCurrentIndex(index)

    def _page_changed(self, index):
        self.stack.setCurrentIndex(index)
        page = self.PAGES[index][0]
        self.schedule_link.setVisible(page != "schedule")
        self.pageChanged.emit(page)

    def add_panel(self, key, widget):
        widget.setMinimumHeight(0)
        widget.setMaximumHeight(16777215)
        widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.page_layouts[key].addWidget(widget)
        widget.show()

    def set_attention(self, snapshot):
        count = max(0, int(_get(snapshot, "actionable_count", 0) or 0))
        overdue = max(0, int(_get(snapshot, "overdue_count", 0) or 0))
        state = str(_get(snapshot, "state", "pending") or "pending").lower()
        uncertain = next((label for flag, label in (
            (bool(_get(snapshot, "pending", False)) or state == "pending", "updating"),
            (bool(_get(snapshot, "stale", False)) or state == "stale", "stale"),
            (bool(_get(snapshot, "unavailable", False)) or state in {"unavailable", "error"}, "unavailable"),
            (bool(_get(snapshot, "partial", False)) or state == "partial", "partial"),
        ) if flag), "")
        fresh = bool(_get(snapshot, "fresh", False)) or state == "fresh"
        text = f"SOP · {overdue} overdue / {max(0, count-overdue)} due" if count else "SOP"
        if uncertain or not fresh:
            text += f" · {uncertain or 'unverified'}"
        elif not count:
            upcoming = max(0, int(_get(snapshot, "upcoming_count", 0) or 0))
            text += f" · {upcoming} upcoming" if upcoming else " · clear"
        self.attention_button.setText(text)
        self.attention_button.setToolTip(
            "Review SOP attention across all active profiles. Filters and optional guidance do not hide it. " + text
        )
        self._attention_role = "warning" if count or uncertain or not fresh else "secondary"
        self.attention_button.setStyleSheet(button_style(self._attention_role, self._theme))

    def set_upcoming(self, entries: Sequence[dict], *, published=True, status_text=""):
        self.upcoming_status.setText(status_text or ("Schedule inputs checked." if published else "Checking schedule inputs…"))
        upcoming = tuple(dict(entry) for entry in entries[:4])
        if upcoming and upcoming == self._upcoming and self.upcoming_table.rowCount():
            self._upcoming_published = published
            for button in self.upcoming_table.findChildren(QPushButton):
                button.setEnabled(published)
            return
        if upcoming == self._upcoming and published == self._upcoming_published and self.upcoming_table.rowCount():
            self._fit_upcoming_height()
            return
        self._upcoming = upcoming
        self._upcoming_published = published
        table = self.upcoming_table
        table.clearSpans()
        table.setRowCount(0)
        for row, entry in enumerate(self._upcoming):
            table.insertRow(row)
            summary = " · ".join(str(entry.get(k) or "--") for k in ("type", "group", "band_freq"))
            for col, text in enumerate((str(entry.get("when_text") or "--"), summary)):
                item = QTableWidgetItem(text)
                source = str(entry.get('schedule_source') or '').strip()
                item.setToolTip(text + (f"\nSchedule source: {source}" if source else ''))
                table.setItem(row, col, item)
            label = str(entry.get("action") or "")
            kind = str(entry.get("action_kind") or "")
            if label and kind:
                button = QPushButton(label)
                button.setEnabled(published)
                button.setToolTip(self._action_tooltip(kind) or summary + " · " + label)
                button.clicked.connect(lambda _checked=False, payload=dict(entry): self.scheduleAction.emit(payload))
                add_schedule_review_action(button, entry, self.scheduleAction.emit)
                button.setStyleSheet(button_style(self._action_role(kind), self._theme))
                table.setCellWidget(row, 2, button)
        if not self._upcoming:
            table.setRowCount(1)
            text = "None in projected horizon" if published else "Awaiting schedule update"
            table.setItem(0, 0, QTableWidgetItem(text))
            table.setSpan(0, 0, 1, 3)
        self._fit_upcoming_height()

    def _fit_upcoming_height(self):
        table = self.upcoming_table
        row_floor = font_derived_widget_height(table, vertical_padding=12, include_size_hints=False)
        for row in range(table.rowCount()):
            button = table.cellWidget(row, 2)
            if button is not None:
                row_floor = max(row_floor, button.sizeHint().height())
        for row in range(table.rowCount()):
            table.setRowHeight(row, row_floor)
        # Explicit four-row native viewport; metrics are renewed on font changes.
        height = row_floor * max(1, table.rowCount()) + table.frameWidth()*2 + table.horizontalScrollBar().sizeHint().height()
        table.setMinimumHeight(height)
        table.setMaximumHeight(height)
        mark_text_size_guard_opt_out(table)

    def apply_theme(self, theme):
        self._theme = dict(theme)
        for button in (self.schedule_link, self.situation_link, self.map_link, self.peer_link, self.prop_link):
            button.setStyleSheet(button_style("muted", theme))
        self.attention_button.setStyleSheet(button_style(self._attention_role, theme))
        size = self.fontMetrics().height()
        self.attention_button.setIcon(control_icon("sop", theme, size))
        self.attention_button.setIconSize(QSize(size, size))
        self.schedule_link.setIcon(control_icon("schedule", theme, size))
        self.schedule_link.setIconSize(QSize(size, size))
        for row, entry in enumerate(self._upcoming):
            button = self.upcoming_table.cellWidget(row, 2)
            if button is not None:
                button.setStyleSheet(button_style(self._action_role(str(entry.get("action_kind") or "")), theme))
        self._fit_upcoming_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        metrics = QFontMetrics(self.font())
        compact = self.width() < metrics.horizontalAdvance("M") * 58
        self.body.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
        self.quick_row.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and hasattr(self, "upcoming_table"):
            self._fit_upcoming_height()
