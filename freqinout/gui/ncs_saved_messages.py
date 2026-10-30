"""Cache-only saved-message table and explicit, separate reusable editor."""
from __future__ import annotations

import copy
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
    QAbstractItemView, QComboBox, QSplitter)


class NcsSavedMessages(QWidget):
    search_requested = Signal(str)
    use_requested = Signal(object)
    save_requested = Signal(object)
    delete_requested = Signal(object)
    draft_changed = Signal()
    editing_started = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.editing = None
        self.generation = 0
        self._filling = False
        self._cancelled = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.editor_splitter = QSplitter(Qt.Horizontal)
        self.editor_splitter.setChildrenCollapsible(False)
        catalog = QWidget()
        self.editor_splitter.addWidget(catalog)
        outer.addWidget(self.editor_splitter, 1)
        layout = QVBoxLayout(catalog)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QHBoxLayout()
        heading.addWidget(QLabel('Saved messages'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('Find message name or content')
        self.search.setAccessibleName('Search saved messages')
        self.search.setClearButtonEnabled(True)
        heading.addWidget(self.search, 1)
        layout.addLayout(heading)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(['MSG_NAME', 'Message content'])
        self.table.setAccessibleName('Saved message name and content')
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, self.fontMetrics().horizontalAdvance('M') * 16)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.use_button = QPushButton('Use message')
        self.use_button.clicked.connect(self.use_selected)
        self.new_button = QPushButton('New')
        self.new_button.clicked.connect(lambda: self.edit({}))
        self.edit_button = QPushButton('Edit')
        self.edit_button.clicked.connect(self.edit_selected)
        self.delete_button = QPushButton('Delete')
        self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self.selected()))
        for button in (self.use_button, self.new_button, self.edit_button, self.delete_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.insertLayout(1, actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.editor = QWidget()
        editor = QVBoxLayout(self.editor)
        editor.setContentsMargins(0, 0, 0, 0)
        self.editor_heading = QLabel('Edit saved message')
        editor.addWidget(self.editor_heading)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name = QLineEdit()
        self.name.setAccessibleName('Saved message MSG_NAME')
        self.content = QTextEdit()
        self.content.setAccessibleName('Saved message content')
        self.content.setMaximumHeight(self.fontMetrics().lineSpacing() * 5)
        self.content.setPlaceholderText('Reusable message text; {TARGET GROUP} inserts the selected group when sending.')
        self.form_code = QLineEdit()
        self.expect = QComboBox()
        self.expect.setProperty('fio_no_auto_fit', True)
        self.expect.addItem('Select saved form', None)
        form.addRow('MSG_NAME', self.name)
        form.addRow('Message content', self.content)
        form.addRow('Form code', self.form_code)
        form.addRow('Saved form', self.expect)
        self.form = form
        editor.addLayout(form)
        commit = QHBoxLayout()
        self.save_button = QPushButton('Save message')
        self.save_button.clicked.connect(self.save)
        self.cancel_button = QPushButton('Cancel edit')
        self.cancel_button.clicked.connect(lambda: self.cancel())
        self.restore_button = QPushButton('Restore cancelled edit')
        self.restore_button.clicked.connect(self.restore_cancelled)
        self.restore_button.hide()
        commit.addWidget(self.save_button)
        commit.addWidget(self.cancel_button)
        commit.addStretch()
        editor.addLayout(commit)
        editor.addStretch(1)
        self.editor_splitter.addWidget(self.editor)
        self.editor_splitter.setStretchFactor(0, 3)
        self.editor_splitter.setStretchFactor(1, 2)
        layout.addWidget(self.restore_button)
        self.editor.hide()
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(lambda: self.search_requested.emit(self.search.text()))
        self.search.textEdited.connect(self._search_edited)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        # Double-click means edit, never transmit or silently replace the draft.
        self.table.cellDoubleClicked.connect(lambda row, column: self.edit_selected(column))
        for field in (self.name, self.form_code):
            field.textChanged.connect(self._changed)
        self.content.textChanged.connect(self._changed)
        self.expect.currentIndexChanged.connect(self._changed)
        self._selection_changed()

    def _search_edited(self, _text):
        self.table.setEnabled(False)
        self.use_button.setEnabled(False)
        self.edit_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.search_timer.start()

    def selected(self):
        index = self.table.currentRow()
        return copy.deepcopy(self.rows[index]) if 0 <= index < len(self.rows) else None

    @staticmethod
    def wording(row):
        definition = row.get('definition') or {}
        if row.get('kind') == 'query':
            return 'E? ' + str(definition.get('form_code') or '')
        if row.get('kind') == 'saved_expect':
            return 'Saved form · ' + str(definition.get('expect_entry_id') or '')
        return str(definition.get('text') or '')

    def publish(self, rows, *, search='', more=False, saved_forms=()):
        # A status or receive refresh must not destroy active search/selection/edit.
        if search != self.search.text().strip()[:512]:
            return
        self.table.setEnabled(True)
        rows = copy.deepcopy(list(rows))
        if rows != self.rows:
            ident = (self.selected() or {}).get('entry_id')
            scroll = self.table.verticalScrollBar().value()
            self.table.blockSignals(True)
            self.table.setRowCount(len(rows))
            self.rows = rows
            self.table.setCurrentCell(-1, -1)
            for index, row in enumerate(rows):
                for column, text in enumerate((str(row.get('name') or ''), self.wording(row))):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    self.table.setItem(index, column, item)
                if row.get('entry_id') == ident:
                    self.table.selectRow(index)
            self.table.resizeRowsToContents()
            self.table.verticalScrollBar().setValue(scroll)
            self.table.blockSignals(False)
        self._selection_changed()
        if more and self.editing is None:
            self.status.setText('First 200 matches. Refine the search to find another message.')
        values = [(r.get('id'), str(r.get('name') or r.get('expect_key') or r.get('id'))) for r in saved_forms]
        if values != getattr(self, '_saved_forms', None):
            current = self.expect.currentData()
            self.expect.blockSignals(True)
            self.expect.clear(); self.expect.addItem('Select saved form', None)
            for ident, label in values:
                self.expect.addItem(label, ident)
            if current is not None and self.expect.findData(current) < 0:
                self.expect.addItem('Saved form unavailable', current)
            self.expect.setCurrentIndex(max(0, self.expect.findData(current)))
            self.expect.blockSignals(False)
            self._saved_forms = values

    def _selection_changed(self):
        selected = bool(self.selected())
        self.use_button.setEnabled(selected)
        edited = self.editing is not None and (self.selected() or {}).get('entry_id') == self.editing.get('entry_id')
        self.use_button.setText('Use edited copy' if edited else 'Use message')
        self.edit_button.setEnabled(selected and self.editing is None)
        self.new_button.setEnabled(self.editing is None)
        self.delete_button.setEnabled(selected and self.editing is None)

    def use_selected(self):
        row = self.selected()
        if row:
            if self.editing is not None and row.get('entry_id') == self.editing.get('entry_id'):
                row = self.draft()
            self.use_requested.emit(row)

    def edit_selected(self, column=0):
        row = self.selected()
        if row:
            self.edit(row)
            if column == 1: self.content.setFocus()

    def edit(self, row, *, announce=True):
        if self.editing is not None:
            self.status.setText('Finish or cancel the current saved-message edit first.')
            self.name.setFocus()
            return
        self.generation += 1
        self.editing = copy.deepcopy(row)
        self._fill(row)
        self.editor.show()
        self.editor_heading.setText('Edit saved message' if row.get('entry_id') else 'New saved message')
        self.status.setText('Changes apply to the saved message. The outgoing draft is separate.')
        self._selection_changed()
        self.name.setFocus()
        self.draft_changed.emit()
        if announce:
            self.editing_started.emit()

    def _fill(self, row):
        self._filling = True
        definition = row.get('definition') or {}
        self.name.setText(str(row.get('name') or ''))
        self.content.setPlainText(str(definition.get('text') or ''))
        self.form_code.setText(str(definition.get('form_code') or ''))
        ident = definition.get('expect_entry_id')
        if ident is not None and self.expect.findData(ident) < 0:
            self.expect.addItem('Saved form unavailable', ident)
        self.expect.setCurrentIndex(max(0, self.expect.findData(ident)))
        kind = row.get('kind') or 'custom'
        self.form.setRowVisible(self.content, kind == 'custom')
        self.form.setRowVisible(self.form_code, kind == 'query')
        self.form.setRowVisible(self.expect, kind == 'saved_expect')
        self._filling = False

    def draft(self):
        if self.editing is None:
            return None
        row = copy.deepcopy(self.editing)
        kind = row.get('kind') or 'custom'
        definition = dict(row.get('definition') or {})
        if kind == 'custom': definition['text'] = self.content.toPlainText()
        elif kind == 'query': definition['form_code'] = self.form_code.text()
        elif kind == 'saved_expect': definition['expect_entry_id'] = self.expect.currentData()
        return dict(row, name=self.name.text(), kind=kind, definition=definition)

    def _changed(self, *_args):
        if not self._filling and self.editing is not None:
            self.status.setText('Unsaved changes to saved message')
            self.draft_changed.emit()

    def save(self):
        row = self.draft()
        if row is not None:
            if not row.get('name', '').strip():
                self.status.setText('Enter MSG_NAME before saving.')
                self.name.setFocus()
                return
            if row.get('kind') == 'custom' and not str(row.get('definition', {}).get('text') or '').strip():
                self.status.setText('Enter message content before saving.')
                self.content.setFocus()
                return
            self.save_requested.emit(dict(row, library_generation=self.generation))

    def saved(self, row, submitted):
        if submitted.get('library_generation') != self.generation or self.editing is None:
            return
        current = self.draft()
        unchanged = all(current.get(key) == submitted.get(key) for key in ('name', 'kind', 'definition'))
        self.editing = copy.deepcopy(row)
        if unchanged:
            self.cancel(retain=False)
            self.status.setText('✓ Saved message')
        else:
            self.status.setText('Saved earlier changes. Newer edits are retained and unsaved.')
            self.draft_changed.emit()

    def cancel(self, *, retain=True):
        if retain and self.editing is not None:
            self._cancelled.append(self.draft())
            self._cancelled = self._cancelled[-10:]
        self.restore_button.setVisible(bool(self._cancelled))
        self.generation += 1
        self.editing = None
        self.editor.hide()
        self.status.clear()
        self._selection_changed()
        self.draft_changed.emit()

    def restore_cancelled(self):
        if self.editing is None and self._cancelled:
            draft = self._cancelled.pop()
            self.edit(draft)
            self.restore_button.setVisible(bool(self._cancelled))

    def restore(self, draft, *, replace=False, recovery=()):
        if replace:
            self.cancel(retain=False)
            self._cancelled = copy.deepcopy(list(recovery))[-10:]
            self.restore_button.setVisible(bool(self._cancelled))
        if self.editing is None and draft:
            self.edit(draft, announce=False)


    def resizeEvent(self, event):
        super().resizeEvent(event)
        orientation = Qt.Vertical if self.width() < self.fontMetrics().horizontalAdvance('M') * 70 else Qt.Horizontal
        if self.editor_splitter.orientation() != orientation:
            self.editor_splitter.setOrientation(orientation)
