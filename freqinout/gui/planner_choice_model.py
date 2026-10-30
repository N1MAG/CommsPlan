"""Lazy Planner choices backed by a worker's detached metadata publication."""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtWidgets import QComboBox


class PlannerChoiceModel(QAbstractListModel):
    def __init__(self, rows, indices, parent):
        super().__init__(parent)
        self.rows = rows
        self.indices = indices
        self.extra = []
        self.overrides = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows) + len(self.extra)

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if not index.isValid() or not 0 <= row < self.rowCount():
            return None
        label, identity = self.rows[row] if row < len(self.rows) else self.extra[row - len(self.rows)]
        if role in (Qt.DisplayRole, Qt.EditRole):
            return self.overrides.get(row, label)
        if role == Qt.UserRole:
            return identity
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if index.isValid() and role in (Qt.EditRole, Qt.DisplayRole):
            self.overrides[index.row()] = str(value)
            self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
            return True
        return False

    def append(self, label, identity):
        row = self.rowCount()
        self.beginInsertRows(QModelIndex(), row, row)
        self.extra.append((label, identity))
        self.endInsertRows()

    def index_for(self, identity):
        result = self.indices.get(identity)
        if result is not None:
            return result
        return next((len(self.rows) + i for i, (_, value) in enumerate(self.extra) if value == identity), -1)


class PlannerChoiceCombo(QComboBox):
    def publish_choices(self, rows, indices):
        model = PlannerChoiceModel(rows, indices, self)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(18)
        self.setModel(model)

    def findData(self, value, role=Qt.UserRole, flags=Qt.MatchExactly | Qt.MatchCaseSensitive):
        model = self.model()
        if isinstance(model, PlannerChoiceModel) and role == Qt.UserRole:
            return model.index_for(value)
        return super().findData(value, role, flags)

    def addItem(self, text, userData=None):
        model = self.model()
        if isinstance(model, PlannerChoiceModel):
            model.append(str(text), userData)
        else:
            super().addItem(text, userData)
