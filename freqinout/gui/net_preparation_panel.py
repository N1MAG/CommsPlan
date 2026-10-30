"""Compact, presentation-only status and choices for net preparation."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QComboBox, QFrame, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from freqinout.gui.responsive_geometry import WrappingControlLayout
from freqinout.gui.theme import active_app_theme, button_style, label_style, resolve_theme


def add_schedule_review_action(button, entry, dispatch):
    """Keep exact schedule review reachable beside the Prepare primary action."""
    if entry.get('action_kind') != 'prepare_net':
        return
    action = QAction('Review schedule', button)
    payload = dict(entry, action_kind='open_net', action='Review schedule')
    action.triggered.connect(lambda _checked=False: dispatch(payload))
    button.addAction(action)
    button.setContextMenuPolicy(Qt.ActionsContextMenu)
    button.setAccessibleDescription('Prepare net. Review schedule is available in the context menu.')
    button.setToolTip(button.toolTip() + '\nRight-click or press Shift+F10 for Review schedule.')


class NetPreparationPanel(QFrame):
    """Display a preparation result; the owning controller handles every action."""

    action_requested = Signal(str, object)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._generation = 0
        self.settings = settings
        self.setObjectName('netPreparationPanel')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        self.title_label = self._label()
        self.detail_label = self._label()
        self.context_label = self._label()
        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.context_label)
        self.choice_combo = QComboBox(self)
        self.choice_combo.setAccessibleName('Radio and service')
        self.choice_combo.setPlaceholderText('Select a radio and service')
        self.choice_combo.currentIndexChanged.connect(self._choice_changed)
        layout.addWidget(self.choice_combo)
        self.buttons_layout = WrappingControlLayout(spacing=6, alignment=Qt.AlignLeft)
        layout.addLayout(self.buttons_layout)
        self.choice_button = None
        self.apply_theme()
        self.display('')

    def _label(self):
        label = QLabel(self)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    def apply_theme(self):
        theme = active_app_theme(resolve_theme(self.settings))
        self._theme = theme
        self.setStyleSheet(
            'QFrame#netPreparationPanel {'
            f"background: {theme['surface_alt']}; border: 1px solid {theme['border']};"
            'border-radius: 6px;}'
        )
        self.title_label.setStyleSheet(label_style('text', theme, weight=700))
        self.detail_label.setStyleSheet(label_style('text', theme))
        self.context_label.setStyleSheet(label_style('muted', theme))
        for i in range(self.buttons_layout.count()):
            button = self.buttons_layout.itemAt(i).widget()
            if button is not None:
                button.setStyleSheet(button_style(button.property('semanticRole') or 'secondary', theme))

    def display(self, title, detail='', context='', actions=(), choices=()):
        """Replace the visible projection without choosing, routing, or writing."""
        self._generation += 1
        for label, text in ((self.title_label, title), (self.detail_label, detail),
                            (self.context_label, context)):
            label.setText(str(text or ''))
            label.setVisible(bool(text))
        while self.buttons_layout.count():
            item = self.buttons_layout.takeAt(0)
            button = item.widget()
            if button is not None:
                button.setEnabled(False)
                button.hide()
                button.deleteLater()
        self.choice_combo.clear()
        for text, payload in choices:
            self.choice_combo.addItem(str(text), payload)
        self.choice_combo.setCurrentIndex(-1)
        self.choice_combo.setVisible(self.choice_combo.count() > 0)
        self.choice_button = None
        if self.choice_combo.count():
            self.choice_button = self._add_button('Use selected radio/service', 'primary')
            self.choice_button.setEnabled(False)
            self.choice_button.clicked.connect(lambda _checked=False, generation=self._generation: self._choose(generation))
        for text, action_id, payload, role in actions:
            button = self._add_button(str(text), role)
            button.clicked.connect(lambda _checked=False, action=action_id, value=payload:
                                   self.action_requested.emit(action, value))
        self.updateGeometry()

    def _add_button(self, text, role):
        button = QPushButton(text, self)
        button.setProperty('semanticRole', role or 'secondary')
        button.setStyleSheet(button_style(role or 'secondary', self._theme))
        self.buttons_layout.addWidget(button)
        return button

    def _choice_changed(self, index):
        if self.choice_button is not None:
            self.choice_button.setEnabled(index >= 0)

    def _choose(self, generation=None):
        if generation is not None and generation != self._generation:
            return
        index = self.choice_combo.currentIndex()
        if index >= 0:
            self.action_requested.emit('choose', self.choice_combo.itemData(index))
