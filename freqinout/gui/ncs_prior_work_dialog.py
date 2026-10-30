"""Presentation of cached, retained NCS annotations from earlier evidence."""
from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHeaderView, QHBoxLayout,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)


class NcsPriorWorkDialog(QDialog):
    action_requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Earlier net work')
        self._rows = []
        self._next_cursor = None
        layout = QVBoxLayout(self)
        explanation = QLabel('NCS notes and flags are preserved. Original report text is available in the source reader at its current version.')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Station', 'Follow-up', 'Review', 'Evidence', 'NCS note'])
        self.table.setAccessibleName('Earlier net work')
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.followup_button = QPushButton('Complete follow-up')
        self.review_button = QPushButton('Mark reviewed')
        self.source_button = QPushButton('Open source')
        for button, action in ((self.followup_button, 'resolve_followup'),
                               (self.review_button, 'review'), (self.source_button, 'open_source')):
            button.clicked.connect(lambda checked=False, name=action: self.action_requested.emit(name, self.selected_row()))
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        pages = QHBoxLayout()
        self.previous_button = QPushButton('Previous')
        self.next_button = QPushButton('Next')
        self.close_button = QPushButton('Close')
        self.previous_button.clicked.connect(lambda: self.action_requested.emit('previous_page', None))
        self.next_button.clicked.connect(lambda: self.action_requested.emit('next_page', self._next_cursor))
        self.close_button.clicked.connect(self.close)
        pages.addWidget(self.previous_button)
        pages.addWidget(self.next_button)
        pages.addStretch()
        pages.addWidget(self.close_button)
        layout.addLayout(pages)
        self._selection_changed()
        self.resize(self.fontMetrics().averageCharWidth() * 100, self.fontMetrics().height() * 28)

    def selected_row(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self._rows):
            return None
        row = dict(self._rows[index])
        row['values'] = dict(row.get('values') or {})
        return row

    def _selection_changed(self):
        row = self.selected_row()
        values = (row or {}).get('values') or {}
        self.followup_button.setEnabled(bool(row and values.get('follow_up')))
        self.review_button.setEnabled(bool(row and not values.get('reviewed')))
        self.source_button.setEnabled(bool(row and row.get('message_id')))

    def publish(self, rows, next_cursor=None, has_previous=False, status=''):
        """Display a bounded detached page; no source reads or mutations."""
        self._next_cursor = next_cursor
        self.previous_button.setEnabled(bool(has_previous))
        self.next_button.setEnabled(next_cursor is not None)
        self.status_label.setText(str(status or ('No earlier net work' if not rows else '')))
        rows = [dict(row, values=dict(row.get('values') or {})) for row in rows]
        if rows == self._rows:
            return
        selected = self.selected_row() or {}
        key = (selected.get('entity_id'), selected.get('evidence_version'))
        scrollbar = self.table.verticalScrollBar()
        scroll = scrollbar.value()
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(rows))
        self._rows = rows
        for index, row in enumerate(rows):
            values = row['values']
            evidence = row.get('evidence_version')
            current = row.get('current_content_hash')
            state = ('Current evidence' if evidence and current and evidence == current else
                     'Earlier evidence' if evidence and current and evidence != current else 'Evidence unavailable')
            fields = (row.get('from_call') or 'Unknown station',
                      'Needed' if values.get('follow_up') else 'Complete',
                      'Reviewed' if values.get('reviewed') else 'Not reviewed',
                      state, values.get('note') or '')
            for column, value in enumerate(fields):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.table.setItem(index, column, item)
            if key != (None, None) and key == (row.get('entity_id'), row.get('evidence_version')):
                self.table.selectRow(index)
        scrollbar.setValue(scroll)
        self.table.blockSignals(False)
        self._selection_changed()
