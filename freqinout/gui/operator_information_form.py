"""Content-sized operator identity form; no configuration or device I/O."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QWidget, QGridLayout, QVBoxLayout, QLabel, QSizePolicy


class OperatorInformationForm(QWidget):
    def __init__(self, callsign, name, region, grid, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._cells = []
        self._columns = None
        for title, control in (('Callsign', callsign), ('Operator name', name),
                               ('State / region', region), ('Grid (6 characters)', grid)):
            cell = QWidget(self)
            cell.setMinimumWidth(0)
            cell.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            layout = QVBoxLayout(cell)
            layout.setContentsMargins(0, 0, 0, 0)
            label = QLabel(title, cell)
            label.setWordWrap(True)
            label.setBuddy(control)
            control.setMinimumWidth(0)
            control.setMaximumWidth(16777215)
            control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            layout.addWidget(label)
            layout.addWidget(control)
            self._cells.append(cell)
        self._reflow()

    def _reflow(self):
        metrics = self.fontMetrics()
        spacing = max(4, metrics.height() // 2)
        column_width = metrics.horizontalAdvance('M' * 20) + spacing * 2
        columns = 2 if self.width() >= column_width * 2 + spacing else 1
        self._grid.setHorizontalSpacing(spacing * 2)
        self._grid.setVerticalSpacing(spacing)
        self.setMaximumWidth(column_width * 2 + spacing * 2)
        if columns != self._columns:
            self._columns = columns
            for index, cell in enumerate(self._cells):
                self._grid.addWidget(cell, index // columns, index % columns)
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 1 if columns == 2 else 0)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange) and hasattr(self, '_cells'):
            self._reflow()
