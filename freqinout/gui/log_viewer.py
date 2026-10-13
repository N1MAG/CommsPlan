from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QTextEdit,
    QPushButton,
    QLabel,
    QComboBox,
    QLineEdit,
    QSpinBox,
    QMessageBox,
)
from PySide6.QtCore import QTimer, Signal, Qt
from PySide6.QtGui import QTextCursor, QFont

from freqinout.core.logger import _get_log_file, set_log_level, get_log_level
from freqinout.core.settings_manager import SettingsManager
from freqinout.gui.bounded_snapshot_worker import SnapshotWorkerController
from freqinout.gui.theme import resolve_theme, button_style, label_style


class LogViewerTab(QWidget):
    REFRESH_INTERVAL_MS = 1500
    log_level_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = SettingsManager()
        self.log_file = _get_log_file()
        self._responsive_layout_mode = "wide"
        self._responsive_compact_width = 0
        self._snapshot_generation = 0
        self._last_log_lines: tuple[str, ...] = tuple()
        self._snapshot_worker: SnapshotWorkerController | None = None

        self._build_ui()
        self._apply_theme()
        self._apply_saved_level()
        self._request_refresh()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._request_refresh)
        self.timer.start(self.REFRESH_INTERVAL_MS)
        self.destroyed.connect(self._stop_snapshot_worker)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = QLabel("Logs / Diagnostics")
        self.title_label.setStyleSheet(label_style("text", resolve_theme(self.settings), weight=700))
        header.addWidget(self.title_label, 1)
        layout.addLayout(header)

        self.refresh_btn = QPushButton("Refresh")
        self.clear_btn = QPushButton("Clear")
        self.search_btn = QPushButton("Search")
        self.open_btn = QPushButton("Open Log")
        self.search_label = QLabel("Search:")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search log text...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(180)

        font_group = QWidget()
        font_layout = QHBoxLayout(font_group)
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.setSpacing(4)
        font_layout.addWidget(QLabel("Font:"))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 20)
        self.font_spin.setValue(10)
        font_layout.addWidget(self.font_spin)
        self.font_group = font_group

        level_group = QWidget()
        level_layout = QHBoxLayout(level_group)
        level_layout.setContentsMargins(0, 0, 0, 0)
        level_layout.setSpacing(4)
        level_layout.addWidget(QLabel("Level:"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(["DISABLED", "ERROR", "WARNING", "INFO", "DEBUG", "ALL"])
        level_layout.addWidget(self.level_combo)
        self.level_group = level_group

        self._log_filter_layout = QGridLayout()
        self._log_filter_layout.setContentsMargins(0, 0, 0, 0)
        self._log_filter_layout.setSpacing(8)
        layout.addLayout(self._log_filter_layout)
        self._arrange_log_filter_row(compact=False)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.text.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.text)

        self.status_label = QLabel(f"Log file: {self.log_file}")
        layout.addWidget(self.status_label)

        # connections
        self.refresh_btn.clicked.connect(self._request_refresh)
        self.clear_btn.clicked.connect(lambda: self.text.clear())
        self.search_btn.clicked.connect(self._search)
        self.open_btn.clicked.connect(self._open_file)
        self.search_input.returnPressed.connect(self._search)
        self.search_input.textChanged.connect(lambda _text: self._render_snapshot())
        self.font_spin.valueChanged.connect(self._update_font)
        self.level_combo.currentTextChanged.connect(self._on_level_changed)

        self._update_font()
        self._apply_theme()
        self._responsive_compact_width = self._natural_wide_width()
        self._update_log_responsive_layout()

    def _natural_wide_width(self) -> int:
        """Return the font-derived width needed by the single-row toolbar."""

        controls = (
            self.refresh_btn,
            self.clear_btn,
            self.open_btn,
            self.search_label,
            self.search_btn,
            self.font_group,
            self.level_group,
        )
        control_width = sum(max(widget.sizeHint().width(), widget.minimumSizeHint().width()) for widget in controls)
        search_metrics = self.search_input.fontMetrics()
        search_width = max(
            search_metrics.horizontalAdvance(self.search_input.placeholderText() + "MMMM"),
            search_metrics.averageCharWidth() * 64,
        )
        margins = self.layout().contentsMargins()
        spacing = max(0, self._log_filter_layout.horizontalSpacing())
        return control_width + search_width + spacing * 7 + margins.left() + margins.right()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_log_responsive_layout()

    def _log_responsive_mode_for_width(self, width: int) -> str:
        try:
            return "compact" if int(width) < int(self._responsive_compact_width) else "wide"
        except Exception:
            return "wide"

    def _update_log_responsive_layout(self) -> None:
        if not hasattr(self, "_log_filter_layout"):
            return
        mode = self._log_responsive_mode_for_width(int(self.width() or 0))
        if mode == self._responsive_layout_mode and self._log_filter_layout.count() > 0:
            return
        self._responsive_layout_mode = mode
        self._arrange_log_filter_row(compact=(mode == "compact"))

    def _arrange_log_filter_row(self, *, compact: bool) -> None:
        layout = self._log_filter_layout
        while layout.count():
            layout.takeAt(0)
        for col in range(8):
            layout.setColumnStretch(col, 0)
        if compact:
            self.search_input.setMinimumWidth(0)
            placements = [
                (self.refresh_btn, 0, 0),
                (self.clear_btn, 0, 1),
                (self.open_btn, 0, 2),
                (self.search_label, 1, 0),
                (self.search_input, 1, 1, 1, 3),
                (self.search_btn, 1, 4),
                (self.font_group, 2, 0),
                (self.level_group, 2, 1),
            ]
        else:
            self.search_input.setMinimumWidth(180)
            placements = [
                (self.refresh_btn, 0, 0),
                (self.clear_btn, 0, 1),
                (self.open_btn, 0, 2),
                (self.search_label, 0, 3),
                (self.search_input, 0, 4),
                (self.search_btn, 0, 5),
                (self.font_group, 0, 6),
                (self.level_group, 0, 7),
            ]
        for item in placements:
            widget, row, col, *span = item
            row_span, col_span = span if span else (1, 1)
            layout.addWidget(widget, row, col, row_span, col_span)
        layout.setColumnStretch(4 if not compact else 3, 1)

    def _update_font(self):
        size = self.font_spin.value()
        theme = getattr(self, "_theme", resolve_theme(self.settings))
        # Keep the explicit log-font control, but apply it through QFont so
        # stylesheet font declarations cannot bypass accessibility handling.
        font = QFont(self.text.font())
        font.setFamily("monospace")
        font.setPointSize(size)
        self.text.setFont(font)
        self.text.setStyleSheet(
            f"background-color: {theme['surface']}; color: {theme['text']};"
        )

    def _apply_saved_level(self):
        saved = (self.settings.get("log_level", "") or "DISABLED").upper()
        idx = self.level_combo.findText(saved)
        if idx >= 0:
            self.level_combo.setCurrentIndex(idx)
        else:
            self.level_combo.setCurrentIndex(self.level_combo.findText("DISABLED"))
        if saved != "ALL":
            set_log_level(saved)

    def set_tab_active(self, active: bool) -> None:
        if active:
            if not self.timer.isActive():
                self.timer.start(self.REFRESH_INTERVAL_MS)
            return
        if self.timer.isActive():
            self.timer.stop()

    @staticmethod
    def _read_log_tail_snapshot(log_file: str, max_lines: int = 800, max_bytes: int = 2 * 1024 * 1024) -> tuple[str, ...]:
        """Read a bounded tail without loading an arbitrarily large log file."""

        path = Path(log_file)
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                position = stream.tell()
                chunks: list[bytes] = []
                bytes_read = 0
                newline_count = 0
                while position > 0 and bytes_read < max_bytes and newline_count <= max_lines:
                    chunk_size = min(64 * 1024, position, max_bytes - bytes_read)
                    if chunk_size <= 0:
                        break
                    position -= chunk_size
                    stream.seek(position)
                    chunk = stream.read(chunk_size)
                    chunks.append(chunk)
                    bytes_read += len(chunk)
                    newline_count += chunk.count(b"\n")
            text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
            return tuple(text.splitlines(keepends=True)[-max_lines:])
        except FileNotFoundError:
            return ("No log file yet. Use CommsPlan a bit first.\n",)

    def _ensure_snapshot_worker(self) -> SnapshotWorkerController:
        worker = self._snapshot_worker
        if worker is None:
            worker = SnapshotWorkerController(self, self._on_snapshot_ready)
            self._snapshot_worker = worker
        return worker

    def _stop_snapshot_worker(self) -> None:
        worker = getattr(self, "_snapshot_worker", None)
        if worker is not None:
            worker.stop()
            self._snapshot_worker = None

    def shutdown(self) -> None:
        self.timer.stop()
        self._stop_snapshot_worker()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual
        self.shutdown()
        super().closeEvent(event)

    def _filter_lines(self, lines):
        level = self.level_combo.currentText()
        if level == "ALL":
            filtered = list(lines)
        else:
            token1 = f"[{level}]"
            token2 = f" {level} "
            filtered = [l for l in lines if (token1 in l or token2 in l)]
        term = self.search_input.text().strip() if hasattr(self, "search_input") else ""
        if term:
            term_l = term.lower()
            filtered = [l for l in filtered if term_l in l.lower()]
        return filtered

    def _color_for_line(self, line: str) -> str:
        theme = getattr(self, "_theme", resolve_theme(self.settings))
        if " ERROR " in line or " CRITICAL " in line:
            return theme["danger"]
        if " WARNING " in line:
            return theme["warning"]
        if " DEBUG " in line:
            return theme["info"]
        return theme["text_muted"]

    def apply_theme(self):
        self._apply_theme()

    def _apply_theme(self):
        self._theme = resolve_theme(self.settings)
        self.title_label.setStyleSheet(label_style("text", self._theme, weight=700))
        self.refresh_btn.setStyleSheet(button_style("primary", self._theme))
        self.clear_btn.setStyleSheet(button_style("muted", self._theme))
        self.search_btn.setStyleSheet(button_style("info", self._theme))
        self.open_btn.setStyleSheet(button_style("primary", self._theme))
        self._update_font()
        self._render_snapshot()

    def _request_refresh(self) -> None:
        self._snapshot_generation += 1
        generation = self._snapshot_generation
        log_file = str(self.log_file)
        self.status_label.setText(f"Refreshing log snapshot… Log file: {self.log_file}")
        self._ensure_snapshot_worker().request(
            generation,
            lambda: LogViewerTab._read_log_tail_snapshot(log_file),
        )

    def _on_snapshot_ready(self, generation: int, payload: object, error: object) -> None:
        if int(generation) != self._snapshot_generation:
            return
        if error is not None:
            self.status_label.setText(f"Could not refresh log: {error}. Showing the last snapshot.")
            return
        if not isinstance(payload, tuple) or not all(isinstance(line, str) for line in payload):
            self.status_label.setText("Could not refresh log: invalid snapshot. Showing the last snapshot.")
            return
        self._last_log_lines = payload
        self._render_snapshot()

    def _render_snapshot(self) -> None:
        if not hasattr(self, "text"):
            return
        lines = self._filter_lines(self._last_log_lines)
        self.text.clear()
        for line in lines:
            color = self._color_for_line(line)
            html_line = f'<span style="color:{color}">{html.escape(line.rstrip())}</span>'
            self.text.append(html_line)
        self.text.moveCursor(QTextCursor.End)
        term = self.search_input.text().strip() if hasattr(self, "search_input") else ""
        if term and not lines:
            self.status_label.setText(f"No matches for '{term}'. Log file: {self.log_file}")
        else:
            self.status_label.setText(f"Log file: {self.log_file}")

    def _on_level_changed(self, level: str):
        level = (level or "").upper()
        if level and level != "ALL":
            set_log_level(level)
            try:
                self.settings.set("log_level", level)
            except Exception:
                pass
            try:
                self.log_level_changed.emit(level)
            except Exception:
                pass
        self._render_snapshot()

    def _search(self):
        self._render_snapshot()

    def _open_file(self):
        try:
            import os
            import sys
            import subprocess

            if sys.platform.startswith("win"):
                os.startfile(self.log_file)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.log_file])
            else:
                subprocess.Popen(["xdg-open", self.log_file])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not open log file:\n{e}")
