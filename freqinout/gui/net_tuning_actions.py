"""Cache-only operator actions for the active NET FLDigi tuning preset.

The scheduler owns all hardware work.  This module only renders the published
snapshot and submits an explicitly requested action to the scheduler.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from freqinout.gui.theme import active_app_theme, button_height_for_font, button_style


DEFAULT_LABEL = "Center + Narrow"


def _snapshot_value(snapshot: Mapping[str, Any], key: str, default: Any = "") -> Any:
    value = snapshot.get(key, default)
    return default if value is None else value


def _format_hz(value: Any, *, unavailable: str = "Unknown") -> str:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return unavailable
    return f"{number:,} Hz"


def _format_filter_width(value: Any) -> str:
    if value in (None, ""):
        return "Leave unchanged"
    return _format_hz(value)


def _state_value(state: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = state.get(key)
        if value not in (None, ""):
            return value
    return None


def _format_state(state: Any) -> str:
    if not isinstance(state, Mapping):
        return str(state or "Unknown")
    frequency = _state_value(state, "frequency_hz", "rig_hz", "dial_hz", "frequency")
    audio = _state_value(state, "audio_offset_hz", "fldigi_offset_hz", "fldigi_offset")
    width = _state_value(state, "filter_width_hz", "fldigi_filter_width_hz", "filter_width")
    frequency_text = _format_hz(frequency) if isinstance(frequency, (int, float)) else str(frequency or "Unknown")
    return f"frequency {frequency_text}; audio {_format_hz(audio)}; filter {_format_filter_width(width)}"


class NetTuningActionDialog(QDialog):
    """Operator dialog for one radio and one scheduler-published NET snapshot."""

    def __init__(
        self,
        scheduler: object,
        radio_id: int,
        *,
        radio_name: str = "",
        net_name: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.scheduler = scheduler
        self.radio_id = int(radio_id or 0)
        self.radio_name = str(radio_name or f"Radio {self.radio_id}").strip()
        self.net_name = str(net_name or "").strip()
        self._signal = getattr(scheduler, "net_tuning_changed", None)
        self._signal_connected = False
        self._active_entry_signal = getattr(scheduler, "active_entry_changed", None)
        self._active_entry_signal_connected = False
        self._request_pending = False
        self._snapshot: dict[str, Any] = {}
        self._build_ui()
        self._connect_signal()
        self._refresh_from_cache()

    def _build_ui(self) -> None:
        self.setObjectName("netTuningActionDialog")
        self.setWindowTitle("NET FLDigi Tuning")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        font_width = max(1, self.fontMetrics().horizontalAdvance("M"))
        self.setMinimumWidth(font_width * 36)
        self.setMaximumWidth(font_width * 58)

        layout = QVBoxLayout(self)
        line_height = max(1, self.fontMetrics().lineSpacing())
        layout.setContentsMargins(line_height, line_height, line_height, line_height)
        layout.setSpacing(max(2, line_height // 3))

        self.title_label = QLabel(DEFAULT_LABEL, self)
        self.title_label.setObjectName("netTuningTitle")
        self.title_label.setWordWrap(True)
        title_font = QFont(self.title_label.font())
        title_font.setWeight(QFont.Weight.Bold)
        self.title_label.setFont(title_font)
        self.title_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(self.title_label)

        self.context_label = QLabel(self)
        self.context_label.setObjectName("netTuningContext")
        self.context_label.setWordWrap(True)
        self.context_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(self.context_label)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.offset_label = QLabel(self)
        self.offset_label.setObjectName("netTuningAudioOffset")
        self.width_label = QLabel(self)
        self.width_label.setObjectName("netTuningFilterWidth")
        self.preserve_label = QLabel(self)
        self.preserve_label.setObjectName("netTuningPreserveSignal")
        for label in (self.offset_label, self.width_label, self.preserve_label):
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        form.addRow("Audio offset:", self.offset_label)
        form.addRow("Filter width:", self.width_label)
        form.addRow("Signal behavior:", self.preserve_label)
        layout.addLayout(form)

        self.result_label = QLabel(self)
        self.result_label.setObjectName("netTuningResult")
        self.result_label.setWordWrap(True)
        self.result_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.result_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        self.apply_button = QPushButton(DEFAULT_LABEL, self)
        self.apply_button.setObjectName("netTuningApply")
        self.apply_button.setAccessibleName("Apply FLDigi Center and Narrow tuning")
        self.restore_button = QPushButton("Restore previous tuning", self)
        self.restore_button.setObjectName("netTuningRestore")
        self.restore_button.setAccessibleName("Restore previous FLDigi tuning")
        buttons.addButton(self.apply_button, QDialogButtonBox.AcceptRole)
        buttons.addButton(self.restore_button, QDialogButtonBox.ResetRole)
        self.close_button = buttons.button(QDialogButtonBox.Close)
        self.close_button.setObjectName("netTuningClose")
        self.close_button.setAccessibleName("Close NET FLDigi tuning")
        theme = active_app_theme()
        self.apply_button.setStyleSheet(button_style("primary", theme))
        self.restore_button.setStyleSheet(button_style("secondary", theme))
        self.close_button.setStyleSheet(button_style("secondary", theme))
        for button in (self.apply_button, self.restore_button, self.close_button):
            button.setMinimumHeight(button_height_for_font(button))
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(buttons)
        layout.addStretch(1)
        layout.removeWidget(buttons)
        layout.addWidget(buttons)

        self.apply_button.clicked.connect(self._apply)
        self.restore_button.clicked.connect(self._restore)
        self.close_button.clicked.connect(self.close)

    def _connect_signal(self) -> None:
        signal = self._signal
        connect = getattr(signal, "connect", None)
        if callable(connect):
            try:
                connect(self._on_net_tuning_changed)
                self._signal_connected = True
            except (RuntimeError, TypeError):
                self._signal_connected = False
        active_signal = self._active_entry_signal
        active_connect = getattr(active_signal, "connect", None)
        if callable(active_connect):
            try:
                active_connect(self._on_active_entry_changed)
                self._active_entry_signal_connected = True
            except (RuntimeError, TypeError):
                self._active_entry_signal_connected = False

    def _disconnect_signal(self) -> None:
        if self._signal_connected:
            disconnect = getattr(self._signal, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect(self._on_net_tuning_changed)
                except (RuntimeError, TypeError):
                    pass
            self._signal_connected = False
        if self._active_entry_signal_connected:
            disconnect = getattr(self._active_entry_signal, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect(self._on_active_entry_changed)
                except (RuntimeError, TypeError):
                    pass
            self._active_entry_signal_connected = False

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._disconnect_signal()
        super().closeEvent(event)

    def _cached_snapshot(self) -> Mapping[str, Any]:
        getter = getattr(self.scheduler, "net_tuning_snapshot", None)
        if not callable(getter):
            return {"available": False, "detail": "NET tuning is unavailable."}
        try:
            value = getter(self.radio_id)
        except Exception as exc:
            return {"available": False, "detail": f"NET tuning is unavailable: {exc}"}
        return dict(value) if isinstance(value, Mapping) else {"available": False, "detail": "NET tuning is unavailable."}

    def _refresh_from_cache(self) -> None:
        self._snapshot = dict(self._cached_snapshot())
        self._render_snapshot()

    def _on_net_tuning_changed(self, radio_id: int) -> None:
        try:
            if int(radio_id or 0) != self.radio_id:
                return
        except (TypeError, ValueError):
            return
        self._request_pending = False
        self._refresh_from_cache()

    def _on_active_entry_changed(self, *_args) -> None:
        self._refresh_from_cache()

    def _render_snapshot(self) -> None:
        snapshot = self._snapshot
        restore_filter_unknown = bool(
            _snapshot_value(snapshot, "restore_filter_unknown", False)
            or _snapshot_value(snapshot, "previous_filter_width_unknown", False)
        )
        label = str(_snapshot_value(snapshot, "label", DEFAULT_LABEL) or DEFAULT_LABEL).strip()
        self.title_label.setText(label)
        self.apply_button.setText("Apply")
        self.apply_button.setToolTip(f"Apply {label}")
        active_net = str(_snapshot_value(snapshot, "net_name", self.net_name) or self.net_name).strip()
        context = self.radio_name
        if active_net:
            context = f"{context} · {active_net}"
        detail = str(_snapshot_value(snapshot, "detail", "") or "").strip()
        self.context_label.setText(context)

        self.offset_label.setText(_format_hz(_snapshot_value(snapshot, "audio_offset_hz")))
        width = _snapshot_value(snapshot, "filter_width_hz")
        self.width_label.setText(_format_filter_width(width))
        preserve = bool(_snapshot_value(snapshot, "preserve_signal", False))
        self.preserve_label.setText(
            "Preserve on-air signal while changing the dial"
            if preserve
            else "Leave dial unchanged; change audio/filter only"
        )

        result = _snapshot_value(snapshot, "result", {})
        pending = bool(_snapshot_value(snapshot, "pending", False)) or self._request_pending
        if pending:
            result_text = "Applying; waiting for endpoint readback…"
        elif isinstance(result, Mapping) and result:
            result_ok = result.get("ok")
            result_detail = str(result.get("detail") or "").strip()
            if result_ok is True:
                status = "Applied and verified."
            elif result_ok is False:
                status = "The action did not complete."
            else:
                status = "Action status unavailable."
            result_text = f"{status} {result_detail}".strip()
            before = result.get("before")
            after = result.get("after")
            if isinstance(before, Mapping) or isinstance(after, Mapping):
                result_text = (
                    f"{result_text}\nBefore: {_format_state(before)}"
                    f"\nAfter: {_format_state(after)}"
                ).strip()
        elif detail:
            result_text = detail
        else:
            result_text = "No action requested."

        if restore_filter_unknown:
            result_text = f"{result_text}\nPrevious filter width is unknown; restore cannot verify that field."
        self.result_label.setText(result_text)

        available = bool(_snapshot_value(snapshot, "available", False))
        self.apply_button.setEnabled(available and not pending)
        self.restore_button.setEnabled(available and not pending and bool(_snapshot_value(snapshot, "can_restore", False)))

        if restore_filter_unknown:
            self.restore_button.setToolTip("The previous filter width was not observed; restore cannot verify that field.")
        else:
            self.restore_button.setToolTip("Restore the cached previous FLDigi tuning when available.")

    def _submit(self, *, restore: bool) -> None:
        apply_action = getattr(self.scheduler, "apply_net_tuning", None)
        if not callable(apply_action):
            self._snapshot = {"available": False, "detail": "NET tuning is unavailable."}
            self._render_snapshot()
            return
        self._request_pending = True
        self.apply_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        try:
            result = apply_action(self.radio_id, restore=restore)
        except Exception as exc:
            self._request_pending = False
            self.result_label.setText(f"NET tuning request failed: {exc}")
            self._refresh_from_cache()
            return
        status = str(result or "").strip().lower()
        if status == "blocked":
            self._request_pending = False
            self.result_label.setText("NET tuning was blocked by the scheduler safety state.")
        elif status == "queued":
            self.result_label.setText("Applying; waiting for endpoint readback…")
        else:
            self._request_pending = False
            self.result_label.setText(f"NET tuning request: {result}")
        self._refresh_from_cache()

    def _apply(self) -> None:
        self._submit(restore=False)

    def _restore(self) -> None:
        self._submit(restore=True)


def show_net_tuning_dialog(
    parent,
    scheduler: object,
    radio_id: int,
    *,
    radio_name: str = "",
    net_name: str = "",
) -> NetTuningActionDialog | None:
    """Show or focus one dialog per parent/radio without performing I/O."""

    ident = int(radio_id or 0)
    if ident <= 0:
        return None
    dialogs = getattr(parent, "_net_tuning_dialogs", None)
    if not isinstance(dialogs, dict):
        dialogs = {}
        setattr(parent, "_net_tuning_dialogs", dialogs)
    existing = dialogs.get(ident)
    if existing is not None:
        try:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return existing
        except RuntimeError:
            dialogs.pop(ident, None)
    dialog = NetTuningActionDialog(
        scheduler,
        ident,
        radio_name=radio_name,
        net_name=net_name,
        parent=parent,
    )
    dialogs[ident] = dialog
    dialog.finished.connect(lambda _result, key=ident, owner=parent: getattr(owner, "_net_tuning_dialogs", {}).pop(key, None))
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
