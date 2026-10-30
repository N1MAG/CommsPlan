"""Cache-only awareness of independently owned protocol nets."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


def cached_net_activity(protocol, tab):
    """Read widget/controller memory only; never discover a radio or source."""
    if tab is None or not getattr(tab, '_net_in_progress', False):
        return None
    radio_id = getattr(tab, '_ncs_session_radio_id', None)
    token = str(getattr(tab, '_ncs_session_id', '') or '')
    if not radio_id or not token:
        return None
    scope = getattr(tab, '_preparation_scope', None)
    name = str(getattr(scope, 'radio_name', '') or f'Radio {radio_id}')
    controller = getattr(tab, 'workbench', None)
    run = getattr(controller, 'run', None)
    if protocol == 'JS8' and run and run.get('run_id') == token:
        label = str(run.get('label') or '')
        try:
            name = str(json.loads(run['source_json']).get('radio_name') or name)
        except (ValueError, KeyError, TypeError):
            pass
        # This is a loaded-page count, deliberately not a claimed net total.
        rows = getattr(controller, 'snapshot', {}).get('roster', ())
        attention = sum(bool(row.get('follow_up') or row.get('older_follow_up')) for row in rows)
        detail = f'{attention} loaded stations need follow-up'
    else:
        field = getattr(tab, 'net_name_edit' if protocol == 'JS8' else 'net_name_combo', None)
        label = str(field.text() if protocol == 'JS8' else field.currentText()) if field is not None else ''
        detail = 'Attention not reported'
    return dict(protocol=protocol, radio_id=radio_id, radio_name=name, label=label,
                session_id=token, state='Active', attention=detail)


class NcsPeerAwareness(QWidget):
    open_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signature = None
        self._protocol = ''
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.open_button = QPushButton('Open net')
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self._protocol))
        layout.addWidget(self.open_button)
        self.hide()

    def publish(self, own, peer):
        signature = (own, peer)
        if signature == self._signature:
            return
        self._signature = signature
        self.setVisible(bool(peer))
        if not peer:
            self._protocol = ''
            return
        self._protocol = peer['protocol']
        same = bool(own and own['label'].strip() and own['label'].strip().casefold() == peer['label'].strip().casefold())
        relationship = ' · Same incident label' if same else ''
        self.label.setText(f"Also active: {peer['protocol']} · {peer['label'] or 'Unnamed net'} · {peer['radio_name']}{relationship}")
        self.label.setToolTip(f"{peer['state']} · {peer['attention']}. Labels are descriptive; each net keeps its own radio, messages and Start/End.")
        self.open_button.setAccessibleName(f"Open {peer['protocol']} net {peer['label']}")
        self.open_button.setToolTip(f"Open {peer['protocol']} on {peer['radio_name']}; listening and ownership stay unchanged.")
