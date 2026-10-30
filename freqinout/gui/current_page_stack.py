"""A stacked widget whose geometry follows the page that is actually shown.

Qt's default :class:`QStackedWidget` asks its layout for the aggregate size of
all pages.  That is normally harmless, but one deferred or legacy page can
have a very large hint and make an unrelated, visible page expand its parent.
The active page is the only page that can affect the requested geometry here.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QStackedWidget, QTabWidget, QStyle, QWidget


class CurrentPageStack(QStackedWidget):
    """Use only the current page when reporting stack geometry.

    The implementation deliberately performs no child iteration and does not
    impose a fixed height.  A large *current* page may therefore still be
    placed in a scroll area, while hidden pages cannot inflate the stack.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.currentChanged.connect(self._current_page_changed)

    def _current_size(self, method_name: str) -> QSize:
        page = self.currentWidget()
        if page is None:
            return QSize(0, 0)
        size = getattr(page, method_name)()
        if not size.isValid():
            size = QSize(0, 0)
        # A caller may intentionally set a page minimum.  Preserve that
        # contract without consulting any of the hidden pages.
        return size.expandedTo(page.minimumSize())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self._current_size("sizeHint")

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self._current_size("minimumSizeHint")

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        page = self.currentWidget()
        # An editor with its own content scroll fits the outer viewport. Its
        # wrapped content must not request a second outer scrolling height.
        return bool(page is not None and not page.property("fioOwnsContentScroll") and page.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        page = self.currentWidget()
        if page is not None and page.property("fioOwnsContentScroll"):
            return -1
        if page is None or not page.hasHeightForWidth():
            return super().heightForWidth(width)
        return page.heightForWidth(width)

    def addWidget(self, widget: QWidget) -> int:  # noqa: N802 - Qt override
        index = super().addWidget(widget)
        self.updateGeometry()
        return index

    def insertWidget(self, index: int, widget: QWidget) -> int:  # noqa: N802 - Qt override
        actual_index = super().insertWidget(index, widget)
        self.updateGeometry()
        return actual_index

    def removeWidget(self, widget: QWidget) -> None:  # noqa: N802 - Qt override
        super().removeWidget(widget)
        self.updateGeometry()

    def _current_page_changed(self, _index: int) -> None:
        # currentChanged is emitted for both deferred page construction and
        # navigation.  One synchronous invalidation keeps the surrounding
        # layout coherent without a timer-driven resize feedback loop.
        self.updateGeometry()


# Keep the intent discoverable for callers that describe the same primitive
# as an active-page stack.
ActivePageStack = CurrentPageStack

__all__ = ["ActivePageStack", "CurrentPageStack", "CurrentPageTabs"]


class CurrentPageTabs(QTabWidget):
    """Tabbed counterpart of CurrentPageStack; hidden editors cannot grow a list.

    Native tab styling and keyboard navigation remain owned by QTabWidget.
    Geometry reads only cached widget hints and the current font/style.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def _page_hint(self, method):
        page = self.currentWidget()
        size = (getattr(page, method)().expandedTo(page.minimumSize())
                if page is not None else QSize(0, 0))
        bar = self.tabBar().sizeHint()
        frame = 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth, None, self)
        return QSize(max(size.width(), bar.width()) + frame,
                     max(0, size.height()) + bar.height() + frame)

    def sizeHint(self):  # noqa: N802
        return self._page_hint("sizeHint")

    def minimumSizeHint(self):  # noqa: N802
        return self._page_hint("minimumSizeHint")

    def hasHeightForWidth(self):  # noqa: N802
        page = self.currentWidget()
        return bool(page is not None and page.hasHeightForWidth())

    def heightForWidth(self, width):  # noqa: N802
        page = self.currentWidget()
        if page is None or not page.hasHeightForWidth():
            return -1
        frame = 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth, None, self)
        return page.heightForWidth(max(0, width - frame)) + self.tabBar().sizeHint().height() + frame
