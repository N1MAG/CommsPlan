from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen, QWidget

from freqinout.core.product_identity import DISPLAY_NAME
from freqinout.gui.theme import active_app_theme


class StartupSplash:
    """Small, early startup status surface for slow first-window builds."""

    DEDICATION = (
        "Dedicated to my Dad (SK), a U.S. Navy Radioman who learned "
        "HF Digital Tri-Mode at age 86."
    )
    SUPPORT_MESSAGE = (
        f"If {DISPLAY_NAME} benefits your station, please encourage its continued "
        "development. There's more to come."
    )
    SUPPORT_URL = "buymeacoffee.com/n1mag"

    def __init__(self, app: QApplication, *, version: str = "") -> None:
        self._app = app
        self._splash = QSplashScreen(self._build_pixmap(version))
        self._splash.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._splash.setWindowFlag(Qt.FramelessWindowHint, True)
        self._splash.setAccessibleName(f"Starting {DISPLAY_NAME}")
        self._splash.setAccessibleDescription(
            f"{self.DEDICATION} {self.SUPPORT_MESSAGE} {self.SUPPORT_URL}"
        )
        self._theme = active_app_theme()
        self._last_message = ""

    @staticmethod
    def _build_pixmap(version: str) -> QPixmap:
        app = QApplication.instance()
        base_font = QFont(app.font()) if app is not None else QFont()
        base_size = max(9.0, float(base_font.pointSizeF() or 9.0))
        scale = max(1.0, base_size / 10.0)
        pixmap = QPixmap(int(540 * scale), int(270 * scale))
        theme = active_app_theme()
        pixmap.fill(QColor(theme["surface"]))

        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.scale(scale, scale)
            # Coordinates and canvas scale together.  Font point sizes stay in
            # logical units so the painter transform does not apply text scale
            # twice when Large Text is enabled.
            logical_base_size = base_size / scale
            painter.setPen(QColor(theme["text"]))
            title_font = QFont(base_font)
            title_font.setPointSizeF(logical_base_size * 2.4)
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.drawText(30, 54, f"Starting {DISPLAY_NAME}")

            subtitle_font = QFont(base_font)
            subtitle_font.setPointSizeF(logical_base_size)
            painter.setFont(subtitle_font)
            painter.setPen(QColor(theme["text_muted"]))
            subtitle = DISPLAY_NAME
            if version:
                subtitle = f"{subtitle} {version}"
            painter.drawText(32, 82, subtitle)

            painter.setPen(QColor(theme["border"]))
            painter.drawLine(32, 102, 508, 102)

            dedication_font = QFont(base_font)
            dedication_font.setPointSizeF(logical_base_size)
            dedication_font.setItalic(True)
            painter.setFont(dedication_font)
            painter.setPen(QColor(theme["text"]))
            painter.drawText(
                32,
                116,
                476,
                42,
                int(Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap),
                StartupSplash.DEDICATION,
            )

            support_font = QFont(base_font)
            support_font.setPointSizeF(logical_base_size * 0.9)
            painter.setFont(support_font)
            painter.setPen(QColor(theme["text"]))
            painter.drawText(
                32,
                166,
                476,
                38,
                int(Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap),
                StartupSplash.SUPPORT_MESSAGE,
            )

            support_font.setBold(True)
            painter.setFont(support_font)
            painter.setPen(QColor(theme["info"]))
            painter.drawText(32, 218, StartupSplash.SUPPORT_URL)

            painter.setPen(QColor(theme["border"]))
            painter.drawLine(32, 232, 508, 232)
        finally:
            painter.end()
        return pixmap

    def show(self, message: str = f"Starting {DISPLAY_NAME}...") -> None:
        # Prepare the complete first frame before Windows maps the native
        # surface.  Showing first can expose an unpainted top-level window for
        # a frame on slower packaged starts.
        self.update_status_without_event_pump(message)
        self._splash.show()
        self._process_events()

    def update_status(self, message: str) -> None:
        self.update_status_without_event_pump(message)
        self._process_events()

    def update_status_without_event_pump(self, message: str) -> None:
        """Update splash text without dispatching application timers.

        MainWindow construction creates deferred timers intentionally. Pumping
        the global event queue from a progress callback allowed those expensive
        post-construction jobs to run before the window was shown, turning a
        splash repaint into a multi-second startup stall.
        """

        text = str(message or "").strip() or f"Starting {DISPLAY_NAME}..."
        self._last_message = text
        self._splash.showMessage(
            text,
            int(Qt.AlignLeft | Qt.AlignBottom),
            QColor(self._theme["text"]),
        )
        self._splash.repaint()

    def finish(self, widget: Optional[QWidget]) -> None:
        try:
            if widget is not None:
                self._splash.finish(widget)
            else:
                self._splash.close()
        finally:
            self._process_events()

    def close(self) -> None:
        self._splash.close()
        self._process_events()

    def _process_events(self) -> None:
        self._app.processEvents(QEventLoop.ExcludeUserInputEvents)
