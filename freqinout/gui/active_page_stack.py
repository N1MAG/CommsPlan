"""Shared stack whose geometry follows its active page, not hidden forms."""
from PySide6.QtCore import QEvent, QSize, QTimer
from PySide6.QtWidgets import QStackedWidget


class ActivePageStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._height_pending=False
        self.currentChanged.connect(self._schedule_height)

    def addWidget(self, widget):  # noqa: N802
        widget.installEventFilter(self)
        return super().addWidget(widget)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.currentWidget() and event.type() in (QEvent.LayoutRequest,QEvent.FontChange,QEvent.StyleChange):
            self._schedule_height()
        return super().eventFilter(watched,event)

    def _schedule_height(self,*_args):
        if not self._height_pending:
            self._height_pending=True
            QTimer.singleShot(0,self._publish_height)

    def _publish_height(self):
        self._height_pending=False
        page=self.currentWidget()
        height=max(0,page.minimumSizeHint().height(),page.minimumHeight()) if page else 0
        if height != self.minimumHeight():
            self.setMinimumHeight(height)
            self.updateGeometry()

    def sizeHint(self):  # noqa: N802
        page=self.currentWidget()
        return page.sizeHint().expandedTo(page.minimumSize()) if page else QSize(0,0)

    def minimumSizeHint(self):  # noqa: N802
        page=self.currentWidget()
        return page.minimumSizeHint().expandedTo(page.minimumSize()) if page else QSize(0,0)
