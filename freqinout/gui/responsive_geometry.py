"""Cache-only sizing primitives for screen work areas and bounded tables."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer
from PySide6.QtWidgets import QHeaderView, QLayout, QTableWidget


class ResponsiveContentTable(QTableWidget):
    """Fit bounded content widths; preserve operator widths across publications."""

    def __init__(self, rows, columns, parent=None, *, column_em_limits):
        super().__init__(rows, columns, parent)
        self._column_em_limits = tuple(column_em_limits)
        if len(self._column_em_limits) != columns:
            raise ValueError("Every table column needs a bounded width policy.")
        self._preferred_widths = []
        self._last_viewport_width = -1
        self._manual_widths = False
        self._fitting = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_columns)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().sectionResized.connect(self._section_resized)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideRight)

    def _section_resized(self, *_args):
        if not self._fitting:
            self._manual_widths = True

    def request_column_fit(self):
        if not self._fit_timer.isActive():
            self._fit_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_fit_timer"):
            self.request_column_fit()

    def showEvent(self, event):
        super().showEvent(event)
        self.request_column_fit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange) and hasattr(self, "_fit_timer"):
            self._preferred_widths = []
            self._last_viewport_width = -1
            self.request_column_fit()

    def _fit_columns(self):
        if not self.isVisible():
            return
        available = self.viewport().width()
        if available == self._last_viewport_width and self._manual_widths:
            return
        if available != self._last_viewport_width:
            self._manual_widths = False
        metrics = self.fontMetrics()
        em = max(1, metrics.horizontalAdvance("M"))
        padding = 2 * em
        floors, preferred = [], []
        for column in range(self.columnCount()):
            header = self.horizontalHeaderItem(column)
            heading = header.text() if header else ""
            cap = int(self._column_em_limits[column] * em + padding)
            floor = min(cap, self.horizontalHeader().fontMetrics().horizontalAdvance(heading) + padding)
            floors.append(max(self.horizontalHeader().minimumSectionSize(), floor))
            body = max((metrics.horizontalAdvance(self.item(row, column).text())
                        for row in range(min(self.rowCount(), 64))
                        if self.item(row, column) is not None), default=0)
            prior = self._preferred_widths[column] if len(self._preferred_widths) == self.columnCount() else 0
            preferred.append(max(floors[-1], prior, min(cap, body + padding)))
        self._preferred_widths = preferred
        widths = list(preferred)
        surplus = sum(widths) - available
        room = sum(width - floor for width, floor in zip(widths, floors))
        if surplus > 0 and room > 0:
            fraction = min(1.0, surplus / room)
            widths = [max(floor, int(width - (width - floor) * fraction))
                      for width, floor in zip(widths, floors)]
        self._fitting = True
        try:
            for column, width in enumerate(widths):
                if self.columnWidth(column) != width:
                    self.setColumnWidth(column, width)
        finally:
            self._fitting = False
        self._last_viewport_width = available


def fitted_client_geometry(work_area: QRect, size: QSize, frame_extra: QSize) -> QRect:
    """Center a client rectangle whose native frame fits the logical work area."""
    width = max(1, min(size.width(), work_area.width() - max(0, frame_extra.width())))
    height = max(1, min(size.height(), work_area.height() - max(0, frame_extra.height())))
    return QRect(work_area.x() + (work_area.width() - width) // 2,
                 work_area.y() + (work_area.height() - height) // 2, width, height)


class WindowScreenFitController(QObject):
    """Fit at first show/work-area changes, never on ordinary user resize."""

    def __init__(self, window, reflow=None):
        super().__init__(window)
        self._window = window
        self._reflow = reflow
        self._screen = None
        self._handle = None
        self._started = False
        self._fit_pending = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Show and not self._started:
            self._started = True
            self._handle = self._window.windowHandle()
            if self._handle is not None:
                self._handle.screenChanged.connect(self._screen_changed)
            self._screen_changed(self._window.screen())
        elif event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange):
            self._schedule(False)
        elif event.type() == QEvent.Close:
            self._timer.stop()
        return False

    def _screen_changed(self, screen):
        if self._screen is not None:
            for name in ("availableGeometryChanged", "logicalDotsPerInchChanged"):
                try:
                    getattr(self._screen, name).disconnect(self._work_area_changed)
                except (RuntimeError, TypeError):
                    pass
        self._screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._work_area_changed)
            screen.logicalDotsPerInchChanged.connect(self._work_area_changed)
        self._schedule(True)

    def _work_area_changed(self, *_args):
        self._schedule(True)

    def _schedule(self, fit):
        self._fit_pending = self._fit_pending or fit
        if not self._timer.isActive():
            self._timer.start(0)

    def _flush(self):
        window = self._window
        fit, self._fit_pending = self._fit_pending, False
        if not window.isVisible():
            return
        if fit and not window.isMaximized() and not window.isFullScreen() and self._screen is not None:
            area = self._screen.availableGeometry()
            frame = window.frameGeometry()
            if not area.contains(frame):
                extra = frame.size() - window.size()
                client = fitted_client_geometry(area, window.size(), extra)
                window.resize(client.size())
                # move() positions the native frame, not the client origin.
                window.move(area.x() + max(0, (area.width() - window.frameGeometry().width()) // 2),
                            area.y() + max(0, (area.height() - window.frameGeometry().height()) // 2))
        if callable(self._reflow):
            self._reflow()


def restore_full_window_geometry(window, preferences) -> bool:
    """Apply profile-owned geometry before show; Dock never owns this key."""
    screen = window.screen()
    area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1100, 760)
    saved = preferences.value("full_window_geometry")
    try:
        if saved and window.restoreGeometry(saved):
            normal = window.normalGeometry().size()
            usable = normal.width() >= min(640, max(1, area.width() - 24)) and normal.height() >= min(360, max(1, area.height() - 48))
            if usable or window.isMaximized() or window.isFullScreen():
                return True
    except (TypeError, ValueError):
        pass
    # A useful initial workspace is independent of compact child size hints.
    window.resize(min(1100, max(1, area.width() - 24)), min(760, max(1, area.height() - 48)))
    return False


def save_full_window_geometry(window, preferences) -> None:
    """Persist only on explicit shutdown, never on resize or status polling."""
    preferences.setValue("full_window_geometry", window.saveGeometry())
    preferences.sync()


class WrappingControlLayout(QLayout):
    """Wrapping controls with a font-derived, single-control width floor.

    Qt height-for-width negotiation owns reflow; no resize callbacks, timers or
    screen measurements are needed. Buttons keep their complete text/hit size.
    """

    def __init__(self, parent=None, *, spacing=6, alignment=Qt.AlignRight, max_columns=None):
        super().__init__(parent)
        self._alignment = alignment
        self._max_columns = max_columns
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout_rows(QRect(0, 0, width, 0), measure=True)

    def minimumSize(self):
        size = QSize(0, 0)
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def sizeHint(self):
        return self.minimumSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout_rows(rect, measure=False)

    def _layout_rows(self, rect, *, measure):
        margins = self.contentsMargins()
        available = max(0, rect.width() - margins.left() - margins.right())
        gap = max(0, self.spacing())
        rows, row, width, height = [], [], 0, 0
        for item in self._items:
            if item.isEmpty():
                continue
            size = item.sizeHint().expandedTo(item.minimumSize())
            if row and (width + gap + size.width() > available
                        or (self._max_columns is not None and len(row) >= self._max_columns)):
                rows.append((row, width, height))
                row, width, height = [], 0, 0
            width += (gap if row else 0) + size.width()
            height = max(height, size.height())
            row.append((item, size))
        if row:
            rows.append((row, width, height))
        y = rect.y() + margins.top()
        for row, width, height in rows:
            offset = max(0, available - width) if self._alignment & Qt.AlignRight else 0
            x = rect.x() + margins.left() + offset
            for item, size in row:
                if not measure:
                    item.setGeometry(QRect(x, y, size.width(), height))
                x += size.width() + gap
            y += height + gap
        return y - rect.y() - (gap if rows else 0) + margins.bottom()
