"""Bounded, source-first review dialogs for manual Mesh/JS8 forwarding.

The dialogs in this module own presentation and validation preview only.  The
``submit`` callback is supplied by the owning service and is the sole boundary
for background work, persistence, and transport dispatch.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid
import math
import time
import re
import weakref
from typing import Any, Callable, Mapping

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.manual_forward_payload import prepare_forward_payload
from freqinout.gui.theme import (
    active_app_theme,
    button_style,
    control_height_for_font,
    fit_child_combo_boxes,
    fit_text_action_button,
    label_style,
    multiline_height_for_font,
)


SubmitCallback = Callable[[str, dict[str, Any], Callable[[dict[str, Any]], None]], None]


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _transport(destination: Mapping[str, Any]) -> str:
    value = _text(destination.get("transport") or destination.get("kind")).strip().casefold()
    if value in {"meshcore", "meshtastic", "js8"}:
        return value
    return ""


class _SourceSummaryLabel(QLabel):
    """Keep source context on one line, with full text retained for inspection."""

    def __init__(self, text, parent):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(text)

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(self.contentsRect(), Qt.AlignLeft | Qt.AlignVCenter,
                         self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width()))


class _ForwardPreviewEdit(QPlainTextEdit):
    """Prefer three readable lines; let the dialog allocate any extra space."""

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(multiline_height_for_font(self, visible_lines=3, vertical_padding=16))
        return hint


class ManualForwardDialog(QDialog):
    """Review one source message and explicitly save or dispatch a relay draft."""

    completed = Signal(object)

    def __init__(self, source: dict, submit: SubmitCallback, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review manual forwarding")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._alive = True
        self._generation = 0
        self._submit = submit
        self.source = deepcopy(dict(source or {}))
        self._draft = deepcopy(self.source)
        self._picker_options = []
        self._suggestions = []
        self._lookup_offset = 0
        self._lookup_busy = False
        self._has_more = False
        self._route = None
        self._observed_js8_target = ""
        self._selection_key = None
        self._selection_available = False
        self._requested_source_id = _text(self.source.get("source_id")).strip()
        source_relay = _text(self.source.get("relay_id") or self.source.get("draft_relay_id")).strip()
        self.relay_id = source_relay or uuid.uuid4().hex[:12]
        self._resumed = bool(source_relay)
        try:
            expiry = float(self.source.get("draft_expiry"))
            self._draft_expiry = expiry if math.isfinite(expiry) and expiry > 0 else None
        except (TypeError, ValueError, OverflowError):
            self._draft_expiry = None
        try:
            saved_ttl = float(self.source.get("draft_expires_seconds"))
            self._draft_ttl = saved_ttl if math.isfinite(saved_ttl) and 60 <= saved_ttl <= 86400 else None
        except (TypeError, ValueError, OverflowError):
            self._draft_ttl = None
        self._context_busy = True
        self._busy = False
        self._context_reason = ""
        self._destinations: list[dict[str, Any]] = []
        self._selected_destination: dict[str, Any] | None = None
        self._preview_result: Any = None

        self._build_ui()
        self._apply_theme()
        self._set_controls_enabled(False)
        self._set_status("Loading available destinations…", "info")
        self._request_context()

    # ----- construction and presentation -------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("manualForwardScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content = QWidget(self.scroll)
        content.setObjectName("manualForwardContent")
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 2, 8)
        layout.setSpacing(6)

        source_group = QWidget(content)
        source_group.setObjectName("sourceSummary")
        source_layout = QVBoxLayout(source_group)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(4)
        source_header = QHBoxLayout()
        source_header.setContentsMargins(0, 0, 0, 0)
        source_header.setSpacing(6)
        self.source_label = _SourceSummaryLabel(_text(self.source.get("source_label") or self.source.get("source_id") or "Source"), source_group)
        source_identity = " · ".join(
            part for part in (
                _text(self.source.get("family")).strip().upper(),
                _text(self.source.get("source_id")).strip(),
            ) if part
        ) or "—"
        self.source_identity = QLabel(source_identity, source_group)
        self.source_identity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.source_identity.setVisible(False)
        self.source_identity.setToolTip(f"Reference: {source_identity}")
        self.source_sender = _SourceSummaryLabel("From " + (_text(self.source.get("sender")) or "unknown sender"), source_group)
        self.show_source_button = QPushButton("View original", source_group)
        self.show_source_button.setObjectName("showOriginalBodyButton")
        self.show_source_button.clicked.connect(self._toggle_source_body)
        source_header.addWidget(self.source_label, 2)
        source_header.addWidget(self.source_sender, 1)
        source_header.addWidget(self.show_source_button)
        source_layout.addLayout(source_header)
        self.source_body = QPlainTextEdit(source_group)
        self.source_body.setObjectName("sourceBody")
        self.source_body.setReadOnly(True)
        self.source_body.setPlainText(_text(self.source.get("body")))
        self.source_body.setMinimumHeight(multiline_height_for_font(self.source_body, visible_lines=4, vertical_padding=16))
        self.source_body.setMaximumHeight(multiline_height_for_font(self.source_body, visible_lines=10, vertical_padding=18))
        self.source_body.hide()
        source_layout.addWidget(self.source_body)
        layout.addWidget(source_group)

        route_group = QGroupBox("Destination", content)
        route_layout = QFormLayout(route_group)
        route_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.destination_combo = QComboBox(route_group)
        self.destination_combo.setObjectName("destinationCombo")
        self.destination_combo.setAccessibleName("Forward destination")
        self.destination_combo.addItem("Choose a destination…", None)
        self.destination_combo.currentIndexChanged.connect(self._destination_changed)
        self.type_combo = QComboBox(route_group)
        self.type_combo.setAccessibleName("Recipient type")
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        self.channel_combo = QComboBox(route_group)
        self.channel_combo.setAccessibleName("Direct message channel")
        self.channel_combo.setToolTip("This selects the sending channel. Node channel membership is not recorded; node choices are scoped by device.")
        self.channel_combo.currentIndexChanged.connect(self._route_changed)
        self.scope_combo = QComboBox(route_group)
        self.scope_combo.setAccessibleName("Observed JS8 group")
        self.scope_combo.currentIndexChanged.connect(self._filter_candidates)
        self.recipient_edit = QLineEdit(route_group)
        self.recipient_edit.setObjectName("recipientEdit")
        self.recipient_edit.setAccessibleName("Filter recipients or enter JS8 recipient")
        self.recipient_edit.setPlaceholderText("Type a name, callsign or node ID")
        self.recipient_edit.textChanged.connect(self._search_text_changed)
        self.recipient_edit.returnPressed.connect(self._find_nodes)
        self.find_button = QPushButton("Find", route_group)
        self.find_button.clicked.connect(self._find_nodes)
        self.more_button = QPushButton("More", route_group)
        self.more_button.clicked.connect(lambda: self._find_nodes(more=True))
        search_row = QHBoxLayout()
        search_row.addWidget(self.channel_combo, 1)
        search_row.addWidget(self.scope_combo, 1)
        self.channel_combo.hide()
        self.scope_combo.hide()
        self.scope_combo.setToolTip("Filter callsign suggestions by observed group; this is not a membership roster.")
        search_row.addWidget(self.recipient_edit, 3)
        search_row.addWidget(self.find_button)
        search_row.addWidget(self.more_button)
        self.recipient_list = QListWidget(route_group)
        self.recipient_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.recipient_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recipient_list.itemClicked.connect(self._choose_recipient)
        self.recipient_list.itemActivated.connect(self._choose_recipient)
        self.recipient_list.setAccessibleName("Matching recipients; activate to choose")
        self.candidate_count = QLabel(route_group)
        self.candidate_count.setWordWrap(True)
        self.recipient_confirmation = QLabel(route_group)
        self.recipient_confirmation.setWordWrap(True)
        self.recipient_confirmation.setTextFormat(Qt.PlainText)
        self.recipient_confirmation.setMinimumWidth(0)
        self.recipient_confirmation.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.recipient_row_label = QLabel("Recipient:", route_group)
        self.route_controls_host = QWidget(route_group)
        self.route_controls_layout = QBoxLayout(QBoxLayout.LeftToRight, self.route_controls_host)
        self.route_controls_layout.setContentsMargins(0, 0, 0, 0)
        self.route_controls_layout.setSpacing(6)
        connection_host = QWidget(self.route_controls_host)
        connection_layout = QHBoxLayout(connection_host)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.addWidget(QLabel("Send through:", connection_host))
        connection_layout.addWidget(self.destination_combo, 1)
        self.type_host = QWidget(self.route_controls_host)
        type_layout = QHBoxLayout(self.type_host)
        type_layout.setContentsMargins(0, 0, 0, 0)
        type_layout.addWidget(QLabel("Send to:", self.type_host))
        type_layout.addWidget(self.type_combo, 1)
        self.route_controls_layout.addWidget(connection_host, 2)
        self.route_controls_layout.addWidget(self.type_host, 1)
        route_layout.addRow(self.route_controls_host)
        route_layout.addRow(self.recipient_row_label, search_row)
        route_layout.addRow(self.recipient_list)
        route_layout.addRow(self.candidate_count)
        route_layout.addRow(self.recipient_confirmation)
        self.js8_selection_label = QLabel("Check selection in JS8Call to clear it here.", route_group)
        self.js8_selection_label.setWordWrap(True)
        self.js8_selection_label.setTextFormat(Qt.PlainText)
        self.check_js8_button = QPushButton("Recheck selection", route_group)
        self.clear_js8_button = QPushButton("Clear JS8 selection", route_group)
        self.check_js8_button.clicked.connect(lambda: self._selection_action(False))
        self.clear_js8_button.clicked.connect(lambda: self._selection_action(True))
        selection_actions = QHBoxLayout()
        selection_actions.setContentsMargins(0, 0, 0, 0)
        selection_actions.addWidget(self.js8_selection_label, 1)
        selection_actions.addWidget(self.check_js8_button)
        selection_actions.addWidget(self.clear_js8_button)
        self.selection_actions_widget = QWidget(route_group)
        self.selection_actions_widget.setLayout(selection_actions)
        route_layout.addRow(self.selection_actions_widget)
        self._route_layout = route_layout
        self._search_row = search_row
        for row in (search_row, self.recipient_list, self.candidate_count, self.recipient_confirmation, self.selection_actions_widget):
            route_layout.setRowVisible(row, False)
        layout.addWidget(route_group)
        self.destination_context = QLabel(content)
        self.destination_context.setWordWrap(True)
        self.destination_context.hide()

        preview_group = QGroupBox("Message to send", content)
        self.preview_group = preview_group
        # The editor's default size hint must not become a scroll-body minimum.
        # Its explicit three-line minimum still protects readable preview text.
        preview_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        preview_group.setToolTip("Exact addressed preview, including forwarding attribution.")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_text = _ForwardPreviewEdit(preview_group)
        self.preview_text.setObjectName("wirePreview")
        self.preview_text.setReadOnly(True)
        self.preview_text.setPlaceholderText("Choose a recipient to preview the addressed message.")
        self.preview_text.setMinimumHeight(multiline_height_for_font(self.preview_text, visible_lines=3, vertical_padding=16))
        preview_layout.addWidget(self.destination_context)
        preview_layout.addWidget(self.preview_text, 1)
        evidence_row = QHBoxLayout()
        self.bytes_label = QLabel("Bytes: —", preview_group)
        self.bytes_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.notices_label = QLabel(preview_group)
        self.notices_label.setWordWrap(True)
        evidence_row.addWidget(self.bytes_label)
        evidence_row.addWidget(self.notices_label, 1)
        preview_layout.addLayout(evidence_row)
        layout.addWidget(preview_group, 1)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("forwardStatus")
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.footer_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.footer_layout.setSpacing(6)
        self.expiry_host = QWidget(self)
        expiry_row = QHBoxLayout(self.expiry_host)
        expiry_row.setContentsMargins(0, 0, 0, 0)
        expiry_row.setSpacing(6)
        expiry_row.addWidget(QLabel("Expires:", self))
        self.expiry_combo = QComboBox(self)
        self.expiry_combo.setObjectName("expiryCombo")
        self.expiry_combo.addItem("1 hour", 3600)
        self.expiry_combo.addItem("4 hours", 4 * 3600)
        self.expiry_combo.addItem("24 hours", 24 * 3600)
        if self._resumed and self._draft_ttl is not None:
            index = self.expiry_combo.findData(self._draft_ttl)
            if index < 0:
                self.expiry_combo.addItem("Keep saved duration", self._draft_ttl)
                index = self.expiry_combo.count() - 1
            self.expiry_combo.setCurrentIndex(index)
        self.expiry_deadline_label = QLabel(self)
        self.expiry_deadline_label.setWordWrap(True)
        if self._resumed:
            if self._draft_expiry is None:
                self.expiry_deadline_label.setText("Saved deadline unavailable; reopen activity to review.")
            else:
                try:
                    deadline = datetime.fromtimestamp(self._draft_expiry, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                except (ValueError, OverflowError, OSError):
                    self._draft_expiry = None
                    deadline = "unavailable"
                remaining = max(0, int((self._draft_expiry or 0) - time.time()))
                self.expiry_deadline_label.setText(f"Saved deadline: {deadline} ({remaining // 60} min remaining at review)")
        expiry_row.addWidget(self.expiry_combo)
        expiry_row.addWidget(self.expiry_deadline_label)
        self.footer_layout.addWidget(self.expiry_host)
        self.footer_action_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.footer_action_layout.setSpacing(6)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Save draft", self)
        self.save_button.setObjectName("saveDraftButton")
        self.save_button.clicked.connect(self._save)
        self.send_button = QPushButton("Send now", self)
        self.send_button.setObjectName("sendNowButton")
        self.send_button.clicked.connect(self._send)
        self.footer_action_layout.addStretch(1)
        self.footer_action_layout.addWidget(self.cancel_button)
        self.footer_action_layout.addWidget(self.save_button)
        self.footer_action_layout.addWidget(self.send_button)
        self.footer_layout.addLayout(self.footer_action_layout, 1)
        outer.addLayout(self.footer_layout)

        for widget in (self.destination_combo, self.type_combo, self.channel_combo, self.scope_combo, self.recipient_edit, self.find_button, self.more_button, self.check_js8_button, self.clear_js8_button, self.expiry_combo, self.cancel_button, self.save_button, self.send_button, self.show_source_button):
            widget.setMinimumHeight(control_height_for_font(widget))
        for button in (self.find_button, self.more_button, self.check_js8_button, self.clear_js8_button, self.cancel_button, self.save_button, self.send_button, self.show_source_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        self._update_compact_geometry()
        self.resize(max(self.fontMetrics().horizontalAdvance("M") * 72, 790), max(self.fontMetrics().lineSpacing() * 30, 450))
        self._apply_footer_direction()

    def _apply_footer_direction(self) -> None:
        controls = (self.cancel_button, self.save_button, self.send_button)
        required = sum(max(control.sizeHint().width(), control.minimumSizeHint().width(), control.minimumWidth()) for control in controls)
        required += 3 * max(6, int(self.fontMetrics().horizontalAdvance("M") / 2))
        direction = QBoxLayout.TopToBottom if self.width() < required + 80 else QBoxLayout.LeftToRight
        self.footer_action_layout.setDirection(direction)
        footer_required = required + self.expiry_host.sizeHint().width() + 40
        self.footer_layout.setDirection(QBoxLayout.TopToBottom if self.width() < footer_required else QBoxLayout.LeftToRight)

    def _update_compact_geometry(self) -> None:
        # Geometry uses cached widgets only; resizing must never request context.
        if not hasattr(self, "route_controls_layout"):
            return
        wide = self.width() >= self.fontMetrics().horizontalAdvance("M") * 68
        self.route_controls_layout.setDirection(QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom)
        rows = 4 if self.height() >= self.fontMetrics().lineSpacing() * 38 else 2
        self.recipient_list.setFixedHeight(multiline_height_for_font(self.recipient_list, visible_lines=rows, vertical_padding=8))

    def resizeEvent(self, event) -> None:
        self._apply_footer_direction()
        self._update_compact_geometry()
        super().resizeEvent(event)

    def _apply_theme(self) -> None:
        theme = active_app_theme()
        self.status_label.setStyleSheet(label_style(getattr(self, "_status_role", "info"), theme))
        self.notices_label.setStyleSheet(label_style("warning", theme))
        self.bytes_label.setStyleSheet(label_style("muted", theme))
        self.cancel_button.setStyleSheet(button_style("muted", theme))
        self.save_button.setStyleSheet(button_style("secondary", theme))
        self.send_button.setStyleSheet(button_style("primary", theme))
        self.show_source_button.setStyleSheet(button_style("muted", theme))
        self.find_button.setStyleSheet(button_style("secondary", theme))
        self.more_button.setStyleSheet(button_style("muted", theme))
        self.check_js8_button.setStyleSheet(button_style("muted", theme))
        self.clear_js8_button.setStyleSheet(button_style("secondary", theme))
        for button in (self.find_button, self.more_button, self.check_js8_button, self.clear_js8_button, self.cancel_button, self.save_button, self.send_button, self.show_source_button):
            fit_text_action_button(button)
        self._update_compact_geometry()
        self.preview_text.setMinimumHeight(multiline_height_for_font(self.preview_text, visible_lines=3, vertical_padding=16))
        self.source_body.setMinimumHeight(multiline_height_for_font(self.source_body, visible_lines=4, vertical_padding=16))
        self.source_body.setMaximumHeight(multiline_height_for_font(self.source_body, visible_lines=10, vertical_padding=18))
        self._apply_footer_direction()
        fit_child_combo_boxes(self)

    def _toggle_source_body(self) -> None:
        visible = not self.source_body.isVisible()
        self.source_body.setVisible(visible)
        self.show_source_button.setText("Hide original body" if visible else "View original")

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (QEvent.FontChange, QEvent.PaletteChange, QEvent.ApplicationFontChange):
            self._apply_theme()
            for widget in (self.destination_combo, self.type_combo, self.channel_combo, self.scope_combo, self.recipient_edit, self.find_button, self.more_button, self.check_js8_button, self.clear_js8_button, self.expiry_combo, self.cancel_button, self.save_button, self.send_button, self.show_source_button):
                widget.setMinimumHeight(control_height_for_font(widget))
        super().changeEvent(event)

    # ----- context, preview, and commit --------------------------------------------------
    def _request_context(self) -> None:
        self._invoke_submit("context", {"source": deepcopy(self.source)}, self._context_complete)

    def _invoke_submit(self, operation: str, payload: dict[str, Any], handler: Callable[[dict[str, Any]], None]) -> None:
        self._generation += 1
        generation = self._generation
        ref = weakref.ref(self)

        def callback(result: dict[str, Any] | None) -> None:
            obj = ref()
            if obj is None or not obj._is_live(generation):
                return
            obj._callback_safe(handler, result)

        try:
            self._submit(operation, payload, callback)
        except Exception as exc:  # the owner remains responsible for logging; UI stays usable
            if self._is_live(generation):
                handler({"ok": False, "state": "error", "detail": str(exc)})

    def _is_live(self, generation: int | None = None) -> bool:
        if not self._alive or (generation is not None and generation != self._generation):
            return False
        try:
            from shiboken6 import isValid

            return bool(isValid(self))
        except Exception:
            return True

    def _callback_safe(self, handler: Callable[[dict[str, Any]], None], result: dict[str, Any] | None) -> None:
        if not self._alive:
            return
        try:
            handler(dict(result or {}))
        except RuntimeError:
            return

    def _context_complete(self, result: dict[str, Any]) -> None:
        if not self._alive:
            return
        self._context_busy = False
        self._context_reason = _text(result.get("reason") or (result.get("detail") if result.get("ok") is False else "")).strip()
        resolved = result.get("source")
        if isinstance(resolved, Mapping):
            candidate = deepcopy(dict(resolved))
            candidate_id = _text(candidate.get("source_id")).strip()
            if self._requested_source_id and candidate_id and candidate_id != self._requested_source_id:
                self._context_reason = self._context_reason or "The resolved source changed; review the current message again."
            else:
                self.source = candidate
                self.source_label.setText(_text(self.source.get("source_label") or self.source.get("source_id") or "Source"))
                identity = " · ".join(
                    part for part in (
                        _text(self.source.get("family")).strip().upper(),
                        _text(self.source.get("source_id")).strip(),
                    ) if part
                ) or "—"
                self.source_identity.setText(identity)
                self.source_identity.setToolTip(f"Reference: {identity}")
                self.source_sender.setText("From " + (_text(self.source.get("sender")) or "unknown sender"))
                self.source_body.setPlainText(_text(self.source.get("body")))
        self._destinations = [dict(d) for d in result.get("destinations", []) if isinstance(d, Mapping)]
        self._suggestions = [dict(d) for d in result.get("js8_suggestions", []) if isinstance(d, Mapping)]
        self.destination_combo.blockSignals(True)
        self.destination_combo.clear()
        self.destination_combo.addItem("Choose a connection…", None)
        seen = set()
        for destination in self._destinations:
            key = self._connection_key(destination)
            if key not in seen:
                seen.add(key)
                self.destination_combo.addItem(destination.get("connection_label") or destination.get("label") or key, destination)
        self.destination_combo.blockSignals(False)
        self._set_controls_enabled(True)
        requested = self._draft.get("draft_destination_id") or self._draft.get("destination_id")
        saved = next((d for d in self._destinations if d.get("id") == requested), None)
        if saved:
            for index in range(1, self.destination_combo.count()):
                if self._connection_key(self.destination_combo.itemData(index)) == self._connection_key(saved):
                    self.destination_combo.setCurrentIndex(index)
                    break
            mode = ("group" if str(self._draft.get("draft_recipient") or self._draft.get("recipient") or "").startswith("@") else "callsign") if _transport(saved) == "js8" else self._kind(saved)
            self.type_combo.setCurrentIndex(self.type_combo.findData(mode))
            channel = (saved.get("identity") or {}).get("channel_id")
            for index in range(self.channel_combo.count()):
                option = self.channel_combo.itemData(index)
                if isinstance(option, dict) and (option.get("identity") or {}).get("channel_id") == channel:
                    self.channel_combo.setCurrentIndex(index)
                    break
            if _transport(saved) == "js8":
                self.recipient_edit.setText(str(self._draft.get("draft_recipient") or self._draft.get("recipient") or ""))
            else:
                self._selected_destination = saved
                self._refresh_preview()
        else:
            self._destination_changed(0)

    @staticmethod
    def _connection_key(option):
        return str((option or {}).get("connection_id") or (option or {}).get("id") or "")

    @staticmethod
    def _kind(option):
        return str((option.get("identity") or {}).get("kind") or option.get("recipient_kind") or "channel")

    def _destination_changed(self, index):
        value = self.destination_combo.itemData(index)
        self._route = dict(value) if isinstance(value, Mapping) else None
        self._observed_js8_target = ""
        self.js8_selection_label.setText("Check selection in JS8Call to clear it here.")
        self.type_combo.blockSignals(True)
        self.type_combo.clear()
        if self._route:
            for label, kind in (("Group", "group"), ("Callsign", "callsign")) if _transport(self._route) == "js8" else (("Channel", "channel"), ("Direct node", "direct")):
                self.type_combo.addItem(label, kind)
        self.type_combo.blockSignals(False)
        self._type_changed()

    def _type_changed(self, *_args):
        self._generation += 1
        self._selected_destination = None
        self.recipient_edit.blockSignals(True)
        self.recipient_edit.clear()
        self.recipient_edit.blockSignals(False)
        self._selection_key = None
        self._selection_available = False
        self._observed_js8_target = ""
        mode = self.type_combo.currentData()
        js8 = bool(self._route and _transport(self._route) == "js8")
        channels = [d for d in self._destinations if not js8 and self._connection_key(d) == self._connection_key(self._route) and self._kind(d) == "channel"]
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.addItem("Choose a channel…", None)
        for option in channels:
            self.channel_combo.addItem(option.get("recipient_label") or option.get("label") or "Channel", option)
        self.channel_combo.blockSignals(False)
        self.scope_combo.blockSignals(True)
        self.scope_combo.clear()
        self.scope_combo.addItem("All observed groups", "")
        for group in sorted({g for d in self._suggestions for g in d.get("groups", [])}):
            self.scope_combo.addItem(group, group)
        self.scope_combo.blockSignals(False)
        self.type_host.setVisible(bool(self._route))
        self._route_layout.setRowVisible(self._search_row, bool(self._route))
        self._route_layout.setRowVisible(self.recipient_confirmation, bool(self._route))
        self.channel_combo.setVisible(bool(self._route and _transport(self._route) == "meshtastic" and mode == "direct"))
        self.scope_combo.setVisible(js8 and mode == "callsign")
        self._route_layout.setRowVisible(self.selection_actions_widget, False)
        self.find_button.setVisible(js8 or mode == "direct")
        self.find_button.setText("Use recipient" if js8 else "Find")
        self.more_button.setVisible(not js8 and mode == "direct")
        self.recipient_edit.setPlaceholderText("Enter or filter @group" if mode == "group" else "Enter or filter callsign" if mode == "callsign" else "Filter channels" if mode == "channel" else "Type to filter; Find searches all stored nodes")
        self._route_changed()

    def _route_changed(self, *_args):
        self._generation += 1
        self._selected_destination = None
        self._lookup_offset = 0
        self._lookup_busy = False
        self._has_more = False
        self._picker_options = [d for d in self._destinations if self._connection_key(d) == self._connection_key(self._route) and self._kind(d) == self.type_combo.currentData()]
        if self._route and _transport(self._route) == "meshtastic" and self.type_combo.currentData() == "direct":
            channel = self.channel_combo.currentData()
            self._picker_options = [d for d in self._picker_options if channel and (d.get("identity") or {}).get("channel_id") == (channel.get("identity") or {}).get("channel_id")]
        self._filter_candidates()

    def _search_text_changed(self, *_args):
        self._selection_key = None
        self._selection_available = False
        self._observed_js8_target = ""
        self._route_layout.setRowVisible(self.selection_actions_widget, False)
        if self._route and _transport(self._route) == "js8":
            self.find_button.setVisible(True)
        self._lookup_offset = 0
        self._lookup_busy = False
        self._has_more = False
        self._filter_candidates()

    def _filter_candidates(self, *_args):
        self._generation += 1
        self._selected_destination = self._route if self._route and _transport(self._route) == "js8" else None
        self.recipient_list.clear()
        self._update_compact_geometry()
        query = self.recipient_edit.text().strip().casefold()
        mode = self.type_combo.currentData()
        if mode in {"group", "callsign"}:
            scope = self.scope_combo.currentData() or ""
            options = [d for d in self._suggestions if d.get("kind") == mode and (not scope or scope in d.get("groups", []))]
        else:
            options = self._picker_options
        matched = [d for d in options if query in str(d.get("search_text") or d.get("recipient_label") or d.get("value") or d.get("label") or "").casefold()]
        for option in matched[:100]:
            text = str(option.get("recipient_label") or option.get("value") or option.get("label") or "Recipient")
            native = str((option.get("identity") or {}).get("destination_id") or "")
            if native:
                name = text.removesuffix(" · " + native)
                short_id = native if len(native) <= 24 else native[:8] + "…" + native[-12:]
                text = name + "\n" + short_id
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, option)
            item.setToolTip(str(option.get("search_text") or option.get("label") or option.get("value") or ""))
            self.recipient_list.addItem(item)
        self.candidate_count.setText((f"{min(100, len(matched))} of {len(matched)} matches" if len(matched) > 100 else f"{len(matched)} match(es)") + (" · More available" if self._has_more else ""))
        self.candidate_count.setToolTip("Find searches all stored nodes on this device." if mode == "direct" else "Valid manual entry is also allowed." if mode in {"group", "callsign"} else "Select a channel to continue.")
        self.more_button.setEnabled(self._has_more)
        self._refresh_preview()

    def _choose_recipient(self, item):
        value = item.data(Qt.UserRole)
        if not isinstance(value, dict):
            return
        if self.type_combo.currentData() in {"group", "callsign"}:
            self.recipient_edit.setText(value["value"])
            self._recipient_committed()
        else:
            self._selected_destination = dict(value)
            self._refresh_preview()

    def _find_nodes(self, _checked=False, *, more=False):
        if self._route and _transport(self._route) == "js8":
            self._recipient_committed()
            return
        if self._busy or self._context_busy or self._lookup_busy or not self._route or self.type_combo.currentData() != "direct":
            return
        base = self._route
        if _transport(base) == "meshtastic":
            base = self.channel_combo.currentData()
            if not base:
                self._set_status("Choose a channel before finding direct nodes.", "warning")
                return
        offset = self._lookup_offset if more else 0
        payload = dict(source=deepcopy(self.source), destination=deepcopy(base), query=self.recipient_edit.text().strip(), offset=offset)
        self._selected_destination = None
        self._lookup_busy = True
        self._refresh_preview()
        self._set_status("Finding stored nodes…", "info")
        self._invoke_submit("lookup", payload, lambda result: self._nodes_complete(result, more))

    def _nodes_complete(self, result, more):
        self._lookup_busy = False
        self._update_action_state()
        if not result.get("ok"):
            self._set_status(str(result.get("detail") or "Node lookup failed."), "danger")
            return
        options = [dict(d) for d in result.get("options", []) if isinstance(d, Mapping)]
        self._picker_options = options
        self._lookup_offset = int(result.get("next_offset") or 0)
        self._has_more = bool(result.get("has_more"))
        self._filter_candidates()

    def _recipient_committed(self):
        if self._busy or self._context_busy or not self._route or _transport(self._route) != "js8" or not self._preview_result or not self._preview_result.ok:
            return
        self._selection_key = (self._connection_key(self._route), self.recipient_edit.text().strip().upper())
        self.find_button.hide()
        self._route_layout.setRowVisible(self.selection_actions_widget, True)
        self._selection_action(False)

    def _selection_action(self, clear):
        if self._busy or self._context_busy or not self._route or _transport(self._route) != "js8":
            return
        if clear and not self._observed_js8_target:
            return
        self._busy = True
        self._set_controls_enabled(False)
        self._set_status("Clearing the observed JS8Call selection…" if clear else "Checking JS8Call selection…", "info")
        payload = dict(source=deepcopy(self.source), destination=deepcopy(self._route), expected_target=self._observed_js8_target)
        self._invoke_submit("target_clear" if clear else "target_check", payload, self._selection_complete)

    def _selection_complete(self, result):
        self._busy = False
        self._selection_available = bool(result.get("available"))
        self._observed_js8_target = str(result.get("target") or "") if result.get("available") else ""
        self.js8_selection_label.setText("In JS8Call: " + (self._observed_js8_target or ("No selection" if result.get("available") else "Selection unavailable")))
        self._set_controls_enabled(True)
        detail = str(result.get("detail") or "JS8 selection check completed.")
        if self._observed_js8_target:
            detail = "Clear the selection in JS8Call before sending this fully addressed forward."
        self._set_status(detail, "warning" if self._observed_js8_target or not result.get("ok") else "info")

    def _refresh_preview(self, *_args: object) -> None:
        destination = self._selected_destination
        context = _text((destination or self._route or {}).get("context"))
        self.destination_context.setText(context)
        self.destination_context.setVisible(bool(context) and _transport(destination or self._route or {}) == "js8")
        self.destination_combo.setToolTip(context)
        self.preview_group.setToolTip("Exact addressed preview. " + context)
        target = self.recipient_edit.text().strip() if destination and _transport(destination) == "js8" else _text((destination or {}).get("recipient_label") or (destination or {}).get("label"))
        native = str(((destination or {}).get("identity") or {}).get("destination_id") or "")
        if native and native not in target:
            target += " · " + native
        connection = _text((destination or {}).get("connection_label"))
        display = re.sub(r"[A-Fa-f0-9]{32,}", lambda match: " ".join(match.group()[i:i+16] for i in range(0, len(match.group()), 16)), target)
        self.recipient_confirmation.setText("Recipient: " + display if target else "")
        self._route_layout.setRowVisible(self.recipient_confirmation, bool(target))
        chosen = bool(destination and target)
        if destination and _transport(destination) == "js8":
            chosen = chosen and (self.recipient_list.count() == 0 or (self.recipient_list.count() == 1 and self.recipient_list.item(0).text().casefold() == target.casefold()))
        self._route_layout.setRowVisible(self.recipient_list, bool(self._route) and not chosen)
        self._route_layout.setRowVisible(self.candidate_count, bool(self._route) and not chosen)
        self.recipient_confirmation.setToolTip(target + (" · " + connection if connection else ""))
        self._preview_result = None
        if not destination:
            self.preview_text.clear()
            self.bytes_label.setText("Bytes: —")
            self.bytes_label.setToolTip("")
            self.notices_label.clear()
            self.notices_label.setToolTip("")
            self._set_status(self._context_reason or ("Choose a recipient to continue." if self._route else "Choose a connection to continue."), "warning" if self._context_reason else "info")
            self._update_action_state()
            return
        recipient = self.recipient_edit.text().strip() if _transport(destination) == "js8" else ""
        if _transport(destination) == "js8" and not recipient:
            self.preview_text.clear()
            self.bytes_label.setText("Bytes: —")
            self.bytes_label.setToolTip("")
            self.notices_label.clear()
            self.notices_label.setToolTip("")
            self._set_status(self._context_reason or "Choose a recipient to continue.", "warning" if self._context_reason else "info")
            self._update_action_state()
            return
        try:
            result = prepare_forward_payload(
                transport=_transport(destination),
                sender=_text(self.source.get("sender")),
                body=_text(self.source.get("body")),
                relay_id=self.relay_id,
                recipient=recipient,
                max_bytes=destination.get("max_bytes"),
            )
        except Exception as exc:
            self._set_status(f"Unable to prepare preview: {exc}", "danger")
            self._update_action_state()
            return
        self._preview_result = result
        self.preview_text.setPlainText(_text(getattr(result, "wire_text", "")))
        self.bytes_label.setText(f"{result.wire_bytes} / {result.max_bytes} bytes")
        self.bytes_label.setToolTip(f"Complete addressed UTF-8 wire size; {result.payload_bytes} payload bytes. " + ("CommsPlan forwarding ceiling, not a universal JS8Call limit." if _transport(destination) == "js8" else "Qualified transport limit."))
        issues = tuple(getattr(result, "issues", ()) or ())
        mode = self.type_combo.currentData()
        if _transport(destination) == "js8" and recipient and ((mode == "group") != recipient.startswith("@")):
            issues += ("Recipient does not match the selected Group/Callsign type.",)
            self._preview_result = None
        notices = tuple(getattr(result, "notices", ()) or ())
        self.notices_label.setText("Uppercase for JS8" if notices == ("JS8 wire preview is uppercase",) else "; ".join(notices))
        self.notices_label.setToolTip("; ".join(notices))
        destination_reason = _text(destination.get("reason")).strip()
        if issues:
            self._set_status("Blocked: " + "; ".join(issues), "danger")
        elif destination_reason:
            self._set_status(destination_reason, "warning")
        elif self._context_reason:
            self._set_status(self._context_reason, "warning")
        else:
            self._set_status("Ready for explicit operator review.", "success")
        self._update_action_state()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.destination_combo, self.type_combo, self.channel_combo, self.scope_combo, self.recipient_edit, self.recipient_list, self.find_button, self.more_button, self.check_js8_button, self.clear_js8_button, self.expiry_combo, self.cancel_button, self.save_button, self.send_button):
            widget.setEnabled(bool(enabled))
        self._update_action_state()

    def _update_action_state(self) -> None:
        busy = self._busy or self._context_busy
        selected = self._selected_destination is not None
        preview_ok = bool(self._preview_result is not None and getattr(self._preview_result, "ok", False))
        qualified = not self._context_reason and bool(self._selected_destination is None or self._selected_destination.get("qualified", True) is not False)
        if self._selected_destination is not None and _text(self._selected_destination.get("reason")).strip():
            qualified = False
        self.check_js8_button.setEnabled(not busy and bool(self._route and _transport(self._route) == "js8"))
        self.clear_js8_button.setEnabled(not busy and bool(self._observed_js8_target))
        self.find_button.setEnabled(not busy and not self._lookup_busy)
        self.more_button.setEnabled(not busy and not self._lookup_busy and self._has_more)
        expiry_ok = not self._resumed or (self._draft_expiry is not None and self._draft_ttl is not None and self._draft_expiry > time.time())
        self.save_button.setEnabled(not busy and selected and expiry_ok)
        js8_ready = not self._selected_destination or _transport(self._selected_destination) != "js8" or (self._selection_key == (self._connection_key(self._route), self.recipient_edit.text().strip().upper()) and self._selection_available and not self._observed_js8_target)
        self.send_button.setEnabled(not busy and selected and preview_ok and qualified and js8_ready and expiry_ok)
        self.cancel_button.setEnabled(not busy)
        if busy:
            self.send_button.setEnabled(False)

    def _commit_payload(self) -> dict[str, Any]:
        return {
            "source": deepcopy(self.source),
            "relay_id": self.relay_id,
            "destination": deepcopy(self._selected_destination or {}),
            "recipient_type": self.type_combo.currentData(),
            "recipient": self.recipient_edit.text().strip() if self._selected_destination and _transport(self._selected_destination) == "js8" else "",
            "expires_seconds": self.expiry_combo.currentData() or 3600,
        }

    def _save(self) -> None:
        self._update_action_state()
        if self._busy or not self.save_button.isEnabled():
            return
        self._start_commit("save")

    def _send(self) -> None:
        self._update_action_state()
        if self._busy or not self.send_button.isEnabled():
            return
        self._start_commit("send")

    def _start_commit(self, operation: str) -> None:
        self._busy = True
        self._set_controls_enabled(False)
        self._set_status("Saving draft…" if operation == "save" else "Sending…", "info")
        self._invoke_submit(operation, self._commit_payload(), lambda result: self._commit_complete(operation, result))

    def _commit_complete(self, operation: str, result: dict[str, Any]) -> None:
        if not self._alive:
            return
        self._busy = False
        if bool(result.get("ok")):
            payload = dict(result)
            payload.setdefault("relay_id", self.relay_id)
            payload.setdefault("operation", operation)
            self.completed.emit(payload)
            self._alive = False
            self.accept()
            return
        detail = _text(result.get("detail") or result.get("state") or "The request could not be completed.")
        self._set_status(detail, "danger")
        self._set_controls_enabled(not self._context_busy)

    def _set_status(self, text: str, role: str = "info") -> None:
        self._status_role = role
        self.status_label.setText(text)
        self.status_label.setStyleSheet(label_style(role, active_app_theme()))

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            self._set_status("A background request is still running; wait for its result.", "warning")
            return
        self._alive = False
        self._generation += 1
        super().closeEvent(event)

    def reject(self) -> None:
        if self._busy:
            self._set_status("A background request is still running; wait for its result.", "warning")
            return
        self._alive = False
        self._generation += 1
        super().reject()
class ForwardActivityDialog(QDialog):
    """Bounded activity/history view for saved manual forwarding records."""

    resumeRequested = Signal(object)

    def __init__(self, submit: SubmitCallback, parent: QWidget | None = None, source_id: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Forwarding activity")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._submit = submit
        self._alive = True
        self._generation = 0
        self._cancel_busy = False
        self.source_id = str(source_id or "")
        self._records: list[dict[str, Any]] = []
        self._build_activity_ui()
        self._apply_theme()
        self._request_activity()

    def _build_activity_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        title = QLabel("Saved relay drafts and forwarding history", self)
        outer.addWidget(title)
        self.status_label = QLabel("Loading activity…", self)
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        self.records_table = QTableWidget(0, 3, self)
        self.records_table.setObjectName("forwardRecordsTable")
        self.records_table.setHorizontalHeaderLabels(["Status", "Route", "Detail"])
        self.records_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.records_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.records_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.records_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.records_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.records_table.itemSelectionChanged.connect(self._record_selected)
        outer.addWidget(self.records_table, 1)
        self.detail_label = QLabel("Select a record to view its detail.", self)
        self.detail_label.setWordWrap(True)
        outer.addWidget(self.detail_label)
        footer = QHBoxLayout()
        self.review_button = QPushButton("Review draft…", self)
        self.review_button.clicked.connect(self._review)
        self.cancel_draft_button = QPushButton("Cancel saved draft", self)
        self.cancel_draft_button.clicked.connect(self._cancel_draft)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.review_button)
        footer.addWidget(self.cancel_draft_button)
        footer.addStretch(1)
        footer.addWidget(self.close_button)
        outer.addLayout(footer)
        for widget in (self.review_button, self.cancel_draft_button, self.close_button):
            widget.setMinimumHeight(control_height_for_font(widget))
        self.review_button.setEnabled(False)
        self.cancel_draft_button.setEnabled(False)
        self.resize(max(self.fontMetrics().horizontalAdvance("M") * 80, 700), self.fontMetrics().lineSpacing() * 30)

    def _apply_theme(self) -> None:
        theme = active_app_theme()
        self.status_label.setStyleSheet(label_style("info", theme))
        self.detail_label.setStyleSheet(label_style("muted", theme))
        self.review_button.setStyleSheet(button_style("primary", theme))
        self.cancel_draft_button.setStyleSheet(button_style("warning", theme))
        self.close_button.setStyleSheet(button_style("muted", theme))

    def _request_activity(self) -> None:
        self._generation += 1
        generation = self._generation
        ref = weakref.ref(self)

        def callback(result: dict[str, Any] | None) -> None:
            obj = ref()
            if obj is None or not obj._is_live(generation):
                return
            obj._activity_complete(dict(result or {}))

        try:
            self._submit("activity", {"source_id": self.source_id}, callback)
        except Exception as exc:
            self._activity_complete({"ok": False, "detail": str(exc)})

    def _is_live(self, generation: int | None = None) -> bool:
        if not self._alive or (generation is not None and generation != self._generation):
            return False
        try:
            from shiboken6 import isValid

            return bool(isValid(self))
        except Exception:
            return True

    def _activity_complete(self, result: dict[str, Any]) -> None:
        self._records = [dict(item) for item in (result.get("records") or []) if isinstance(item, Mapping)][:200]
        self.records_table.setRowCount(0)
        for record in self._records[:200]:
            row = self.records_table.rowCount()
            self.records_table.insertRow(row)
            values = (
                _text(record.get("status") or record.get("state") or "Unknown"),
                self._human_route(record.get("route") or record.get("destination") or "—"),
                _text(record.get("detail") or record.get("reason") or ""),
            )
            for column, value in enumerate(values):
                self.records_table.setItem(row, column, QTableWidgetItem(value))
        self.status_label.setText(_text(result.get("detail")) or (f"{len(self._records)} record(s)" if self._records else "No forwarding activity."))
        self._record_selected()

    def _selected_record(self) -> dict[str, Any] | None:
        row = self.records_table.currentRow()
        return self._records[row] if 0 <= row < len(self._records) else None

    def _record_selected(self) -> None:
        record = self._selected_record()
        reviewable = bool(record and record.get("relay_id") and record.get("state") in {"draft", "failed"})
        self.review_button.setEnabled(reviewable)
        self.cancel_draft_button.setEnabled(reviewable)
        if record:
            self.detail_label.setText(self._history_detail(record))
        else:
            self.detail_label.setText("Select a record to view its detail.")

    def _review(self) -> None:
        record = self._selected_record()
        if record is not None and record.get("relay_id"):
            self.resumeRequested.emit(deepcopy(record))

    @staticmethod
    def _human_source(value: object) -> str:
        if isinstance(value, Mapping):
            label = _text(value.get("source_label") or value.get("label") or value.get("source_id") or "Source")
            sender = _text(value.get("sender"))
            return f"{label} · {sender}" if sender else label
        return _text(value) or "Source"

    @staticmethod
    def _human_route(value: object) -> str:
        if isinstance(value, Mapping):
            label = _text(value.get("label") or value.get("name") or value.get("id") or "Destination")
            transport = _text(value.get("transport") or value.get("kind")).strip().casefold()
            target = _text(value.get("channel") or value.get("node") or value.get("recipient") or value.get("target"))
            parts = [label]
            if transport:
                parts.append(transport.upper())
            if target:
                parts.append(target)
            return " · ".join(parts)
        return _text(value) or "Destination"

    def _history_detail(self, record: Mapping[str, Any]) -> str:
        request = dict(record.get("request") or {})
        evidence = dict(request.get("payload_evidence") or {})
        def stamp(value):
            try:
                return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            except (ValueError, TypeError, OverflowError, OSError):
                return _text(value) or "—"
        lines = [
            f"State: {_text(record.get('status') or record.get('state') or 'Unknown')}",
            f"When: {stamp(record.get('updated') or record.get('timestamp') or record.get('created_at') or record.get('updated_at'))}",
            f"Source: {self._human_source(record.get('source') or record.get('source_label') or self.source_id)}",
            f"Route: {self._human_route(record.get('route') or record.get('destination') or '—')}",
            f"Detail: {_text(record.get('detail') or record.get('reason') or 'No detail recorded.')}",
        ]
        wire = _text(request.get("wire_text") or record.get("wire_text") or record.get("preview"))
        if wire:
            lines.append(f"Wire preview: {wire}")
        if evidence or record.get("wire_bytes") is not None or record.get("max_bytes") is not None:
            lines.append(f"Wire bytes: {_text(evidence.get('wire_bytes', record.get('wire_bytes', '—')))}/{_text(evidence.get('max_bytes', record.get('max_bytes', '—')))}")
        for event in (record.get("history") or ())[-20:]:
            lines.append(f"{stamp(event.get('event_time'))} · {event.get('state', '')} · {event.get('detail', '')}")
        return "\n".join(lines)

    def _cancel_draft(self) -> None:
        record = self._selected_record()
        relay_id = _text(record.get("relay_id")) if record else ""
        if not relay_id:
            return
        self.review_button.setEnabled(False)
        self.cancel_draft_button.setEnabled(False)
        self.status_label.setText("Cancelling saved draft…")
        self._cancel_busy = True
        self._generation += 1
        generation = self._generation
        ref = weakref.ref(self)

        def callback(result: dict[str, Any] | None) -> None:
            obj = ref()
            if obj is None or not obj._is_live(generation):
                return
            obj._cancel_complete(result)

        try:
            self._submit("cancel", {"relay_id": relay_id}, callback)
        except Exception as exc:
            self._cancel_complete({"ok": False, "detail": str(exc)})

    def _cancel_complete(self, result: dict[str, Any] | None) -> None:
        result = dict(result or {})
        self._cancel_busy = False
        if result.get("ok"):
            self.status_label.setText(_text(result.get("detail") or "Draft cancelled."))
        else:
            self.status_label.setText(_text(result.get("detail") or "Could not cancel draft."))

    def closeEvent(self, event) -> None:
        if self._cancel_busy:
            event.ignore()
            self.status_label.setText("A background cancellation is still running; wait for its result.")
            return
        self._alive = False
        self._generation += 1
        super().closeEvent(event)

    def reject(self) -> None:
        if self._cancel_busy:
            self.status_label.setText("A background cancellation is still running; wait for its result.")
            return
        self._alive = False
        self._generation += 1
        super().reject()
