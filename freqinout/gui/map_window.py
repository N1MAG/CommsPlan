from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.logger import log
from freqinout.gui.theme import active_app_theme, button_style


MAP_WINDOW_PLACEMENT_KEY = "map_window_placement_v1"


class _StableMapContentStack(QStackedWidget):
    """A permanent pop-out surface whose children cannot resize its window.

    The Map's status page and native Qt Quick page intentionally share this one
    central widget for the lifetime of the pop-out. Returning neutral size
    hints prevents a cold renderer from participating in top-level window size
    negotiation.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, 0)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, 0)


@dataclass(frozen=True)
class MapWindowPlacement:
    geometry: QRect
    maximized: bool
    screen_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "x": int(self.geometry.x()),
            "y": int(self.geometry.y()),
            "width": int(self.geometry.width()),
            "height": int(self.geometry.height()),
            "screen": str(self.screen_id or ""),
            "maximized": bool(self.maximized),
        }


def _screen_id(screen: object | None) -> str:
    if screen is None:
        return ""
    try:
        value = screen.name()
    except Exception:
        value = getattr(screen, "name", "")
    return str(value or "").strip()


def _screen_serial(screen: object | None) -> str:
    if screen is None:
        return ""
    try:
        value = screen.serialNumber()
    except Exception:
        value = getattr(screen, "serial_number", "")
    return str(value or "").strip()


def _available_geometry(screen: object | None) -> QRect:
    if screen is None:
        return QRect(0, 0, 1200, 760)
    try:
        rect = screen.availableGeometry()
    except Exception:
        rect = getattr(screen, "available_geometry", QRect())
    if isinstance(rect, QRect) and rect.width() > 0 and rect.height() > 0:
        return QRect(rect)
    return QRect(0, 0, 1200, 760)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _strict_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _saved_screen_geometry(saved: Mapping[str, object]) -> QRect:
    value = saved.get("screen_geometry")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return QRect()
    parts = tuple(_coerce_int(part) for part in value)
    if any(part is None for part in parts):
        return QRect()
    x, y, width, height = (int(part) for part in parts if part is not None)
    if width <= 0 or height <= 0:
        return QRect()
    return QRect(x, y, width, height)


def _bounded_default_geometry(available: QRect) -> QRect:
    width = min(int(available.width()), max(640, min(1180, int(available.width() * 0.82))))
    height = min(int(available.height()), max(480, min(760, int(available.height() * 0.82))))
    width = max(1, width)
    height = max(1, height)
    x = int(available.x()) + max(0, (int(available.width()) - width) // 2)
    y = int(available.y()) + max(0, (int(available.height()) - height) // 2)
    return QRect(x, y, width, height)


def _clamp_geometry(rect: QRect, available: QRect) -> QRect:
    min_width = min(640, max(1, int(available.width())))
    min_height = min(480, max(1, int(available.height())))
    width = min(int(available.width()), max(min_width, int(rect.width())))
    height = min(int(available.height()), max(min_height, int(rect.height())))
    max_x = int(available.right()) - width + 1
    max_y = int(available.bottom()) - height + 1
    x = min(max(int(rect.x()), int(available.x())), max_x)
    y = min(max(int(rect.y()), int(available.y())), max_y)
    return QRect(x, y, width, height)


def resolve_map_window_placement(
    saved: object,
    screens: Sequence[object],
    fallback_screen: object | None,
) -> MapWindowPlacement:
    """Resolve persisted Map placement without consulting or changing a window."""
    connected = [screen for screen in screens if screen is not None]
    fallback = fallback_screen if fallback_screen in connected else None
    if fallback is None and connected:
        fallback_name = _screen_id(fallback_screen)
        fallback = next((screen for screen in connected if _screen_id(screen) == fallback_name), connected[0])
    fallback_available = _available_geometry(fallback)
    fallback_id = _screen_id(fallback)
    default = MapWindowPlacement(_bounded_default_geometry(fallback_available), False, fallback_id)
    if not isinstance(saved, Mapping):
        return default

    saved_serial = str(saved.get("screen_serial", "") or "").strip()
    target = next((screen for screen in connected if saved_serial and _screen_serial(screen) == saved_serial), None)
    saved_screen_id = str(saved.get("screen", "") or "").strip()
    if target is None:
        target = next((screen for screen in connected if _screen_id(screen) == saved_screen_id), None)
    if target is None:
        prior_available = _saved_screen_geometry(saved)
        if not prior_available.isNull():
            target = next(
                (screen for screen in connected if _available_geometry(screen) == prior_available),
                None,
            )
    if target is None:
        return default
    values = tuple(_coerce_int(saved.get(key)) for key in ("x", "y", "width", "height"))
    if any(value is None for value in values):
        return default
    x, y, width, height = (int(value) for value in values if value is not None)
    if width <= 0 or height <= 0:
        return default
    available = _available_geometry(target)
    requested = QRect(x, y, width, height)
    if not requested.intersects(available):
        return default
    geometry = _clamp_geometry(requested, available)
    return MapWindowPlacement(
        geometry,
        _strict_bool(saved.get("maximized", False)),
        _screen_id(target),
    )


class PersistentMapWindow(QMainWindow):
    """One reusable, nonmodal owner for the native Map workspace."""

    content_ready = Signal(object)
    content_failed = Signal(str)
    work_visibility_changed = Signal(bool)

    def __init__(
        self,
        application_host: QWidget,
        settings: object,
        map_tab_factory: Callable[[QWidget], QWidget],
    ) -> None:
        super().__init__(application_host, Qt.Window)
        self.setObjectName("persistentMapWindow")
        self.setWindowTitle("CommsPlan Map")
        try:
            self.setWindowIcon(application_host.windowIcon())
        except Exception:
            pass
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setMinimumSize(0, 0)
        self._application_host = application_host
        self._settings = settings
        self._map_tab_factory = map_tab_factory
        self._theme: dict[str, object] = active_app_theme()
        self._map_tab: QWidget | None = None
        self._shutdown_started = False
        self._content_shutdown = False
        self._allow_final_close = False
        self._placement_restore_pending = True
        self._normal_geometry_updates_blocked = False
        self._placement_write_signature: tuple[object, ...] | None = None
        self._normal_geometry = QRect()
        self._placement_timer = QTimer(self)
        self._placement_timer.setSingleShot(True)
        self._placement_timer.setInterval(600)
        self._placement_timer.timeout.connect(self._persist_placement)
        self._normal_geometry_block_timer = QTimer(self)
        self._normal_geometry_block_timer.setSingleShot(True)
        self._normal_geometry_block_timer.setInterval(250)
        self._normal_geometry_block_timer.timeout.connect(self._release_normal_geometry_update_block)

        self._content_stack = _StableMapContentStack(self)
        self._content_stack.setObjectName("mapWindowContentStack")
        self._content_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._content_stack.setMinimumSize(0, 0)
        self.setCentralWidget(self._content_stack)

        self._status_widget = QWidget(self._content_stack)
        status_layout = QVBoxLayout(self._status_widget)
        status_layout.setContentsMargins(24, 24, 24, 24)
        status_layout.addStretch(1)
        self._status_label = QLabel("Opening Map…", self._status_widget)
        self._status_label.setObjectName("mapWindowLoadingLabel")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setWordWrap(True)
        status_layout.addWidget(self._status_label)
        self._retry_button = QPushButton("Retry Map", self._status_widget)
        self._retry_button.setObjectName("mapWindowRetryButton")
        self._retry_button.setAccessibleName("Retry opening Map")
        self._retry_button.clicked.connect(self.ensure_content)
        self._retry_button.setVisible(False)
        status_layout.addWidget(self._retry_button, 0, Qt.AlignHCenter)
        status_layout.addStretch(1)
        self._content_stack.addWidget(self._status_widget)
        self._content_stack.setCurrentWidget(self._status_widget)
        self.apply_theme(self._theme)

        try:
            saved = settings.get(MAP_WINDOW_PLACEMENT_KEY, {})
        except Exception:
            saved = {}
        app = QApplication.instance()
        screens = list(app.screens()) if app is not None else []
        try:
            fallback_screen = application_host.screen()
        except Exception:
            fallback_screen = app.primaryScreen() if app is not None else None
        placement = resolve_map_window_placement(saved, screens, fallback_screen)
        self.setGeometry(placement.geometry)
        self._normal_geometry = QRect(placement.geometry)
        self._restore_maximized = bool(placement.maximized)
        canonical = placement.as_dict()
        saved_matches = isinstance(saved, Mapping) and all(saved.get(key) == value for key, value in canonical.items())
        self._placement_write_signature = self._placement_signature(placement) if saved_matches else None
        self._placement_restore_pending = False

    @property
    def map_tab(self) -> QWidget | None:
        return self._map_tab

    @staticmethod
    def _placement_signature(placement: MapWindowPlacement) -> tuple[object, ...]:
        rect = placement.geometry
        return (
            int(rect.x()),
            int(rect.y()),
            int(rect.width()),
            int(rect.height()),
            str(placement.screen_id or ""),
            bool(placement.maximized),
        )

    def _current_placement(self) -> MapWindowPlacement:
        geometry = QRect(self._normal_geometry)
        if geometry.width() <= 0 or geometry.height() <= 0:
            try:
                geometry = QRect(self.normalGeometry())
            except Exception:
                geometry = QRect(self.geometry())
        screen_id = ""
        try:
            screen_id = _screen_id(self.screen())
        except Exception:
            pass
        return MapWindowPlacement(geometry, bool(self.isMaximized()), screen_id)

    def _schedule_placement_persist(self) -> None:
        if self._shutdown_started or self._placement_restore_pending:
            return
        self._placement_timer.start()

    def _persist_placement(self) -> None:
        if self._placement_restore_pending:
            return
        placement = self._current_placement()
        signature = self._placement_signature(placement)
        if signature == self._placement_write_signature:
            return
        try:
            payload = placement.as_dict()
            try:
                screen = self.screen()
            except Exception:
                screen = None
            serial = _screen_serial(screen)
            if serial:
                payload["screen_serial"] = serial
            available = _available_geometry(screen)
            payload["screen_geometry"] = [
                int(available.x()),
                int(available.y()),
                int(available.width()),
                int(available.height()),
            ]
            setter = getattr(self._settings, "set", None)
            if callable(setter):
                setter(MAP_WINDOW_PLACEMENT_KEY, payload)
            elif isinstance(self._settings, dict):
                self._settings[MAP_WINDOW_PLACEMENT_KEY] = payload
            else:
                return
            self._placement_write_signature = signature
        except Exception as exc:
            log.debug("Map window placement persistence failed: %s", exc)

    def ensure_content(self) -> QWidget | None:
        if self._shutdown_started:
            return None
        if self._map_tab is not None:
            return self._map_tab
        self._status_label.setText("Opening Map…")
        self._retry_button.setVisible(False)
        try:
            tab = self._map_tab_factory(self._content_stack)
            if not isinstance(tab, QWidget):
                raise TypeError("Map factory did not return a QWidget")
            tab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            tab.setMinimumSize(0, 0)
            self._content_stack.addWidget(tab)
            self._map_tab = tab
            if hasattr(tab, "apply_theme"):
                tab.apply_theme(dict(self._theme))
            self._content_stack.setCurrentWidget(tab)
            if self.is_available_for_work() and hasattr(tab, "set_map_visible"):
                tab.set_map_visible(True)
            self.content_ready.emit(tab)
            return tab
        except Exception as exc:
            log.exception("Map window content creation failed")
            self._status_label.setText("Map unavailable. Review configuration or retry without restarting CommsPlan.")
            self._retry_button.setVisible(True)
            self.content_failed.emit(str(exc))
            return None

    def apply_theme(self, theme: Mapping[str, object]) -> None:
        """Apply one owner-resolved palette without touching window geometry.

        The Map is a separate top-level window, but its palette authority is
        still MainWindow.  Forwarding the immutable snapshot prevents a child
        SettingsManager cache from reverting QML or selected-detail colors.
        """
        colors = dict(theme or active_app_theme())
        self._theme = colors
        try:
            self._status_widget.setStyleSheet(
                "QWidget {"
                f" background-color: {colors.get('bg', '#E6E8EA')};"
                f" color: {colors.get('text', '#1C1F21')};"
                "}"
            )
            self._status_label.setStyleSheet(
                f"color: {colors.get('text', '#1C1F21')}; background: transparent;"
            )
            self._retry_button.setStyleSheet(button_style("secondary", colors))
        except Exception:
            log.debug("Map window chrome theme update failed", exc_info=True)
        tab = self._map_tab
        if tab is not None and hasattr(tab, "apply_theme"):
            try:
                tab.apply_theme(colors)
            except Exception:
                log.debug("Map window content theme update failed", exc_info=True)

    def present(self) -> None:
        if self._shutdown_started:
            return
        # Build the native scene while this already-positioned top-level window
        # is still hidden. The first visible frame is therefore the final Map
        # hierarchy; no child attachment can resize, repaint, or move a visible
        # window. Failures retain the calm status page in the same final parent.
        if self._map_tab is None:
            self.ensure_content()
        if self.isMinimized():
            if bool(getattr(self, "_restore_maximized", False)):
                self.showMaximized()
            else:
                self.showNormal()
        elif not self.isVisible() and bool(getattr(self, "_restore_maximized", False)):
            self.showMaximized()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def showMaximized(self) -> None:  # noqa: N802 - Qt virtual method name
        # Some platform plugins send transition move/resize events before they
        # report the maximized state. Those events describe the maximized frame,
        # not the operator's restorable normal rectangle.
        self._normal_geometry_updates_blocked = True
        super().showMaximized()
        self._normal_geometry_block_timer.start()

    def _release_normal_geometry_update_block(self) -> None:
        self._normal_geometry_updates_blocked = False

    def is_available_for_work(self) -> bool:
        return bool(self.isVisible() and not self.isMinimized() and not self._shutdown_started)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        available = self.is_available_for_work()
        tab = self._map_tab
        if tab is not None and hasattr(tab, "set_map_visible"):
            tab.set_map_visible(available)
        self.work_visibility_changed.emit(available)

    def hideEvent(self, event) -> None:
        tab = self._map_tab
        if tab is not None and hasattr(tab, "set_map_visible"):
            tab.set_map_visible(False)
        self.work_visibility_changed.emit(False)
        self._persist_placement()
        super().hideEvent(event)

    def moveEvent(self, event) -> None:
        if (
            not bool(getattr(self, "_placement_restore_pending", True))
            and not bool(getattr(self, "_normal_geometry_updates_blocked", False))
            and not self.isMaximized()
            and not self.isFullScreen()
        ):
            self._normal_geometry = QRect(self.geometry())
            self._schedule_placement_persist()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        if (
            not bool(getattr(self, "_placement_restore_pending", True))
            and not bool(getattr(self, "_normal_geometry_updates_blocked", False))
            and not self.isMaximized()
            and not self.isFullScreen()
        ):
            self._normal_geometry = QRect(self.geometry())
            self._schedule_placement_persist()
        super().resizeEvent(event)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() != QEvent.WindowStateChange or bool(getattr(self, "_placement_restore_pending", True)):
            return
        self._restore_maximized = bool(self.isMaximized())
        self._schedule_placement_persist()
        self.work_visibility_changed.emit(self.is_available_for_work())

    def closeEvent(self, event) -> None:
        self._placement_timer.stop()
        self._persist_placement()
        if not self._allow_final_close:
            event.ignore()
            self.hide()
            return
        event.accept()
        super().closeEvent(event)

    def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._placement_timer.stop()
        self._normal_geometry_block_timer.stop()
        self._persist_placement()
        tab = self._map_tab
        if tab is not None:
            try:
                if hasattr(tab, "set_map_visible"):
                    tab.set_map_visible(False)
            except Exception:
                pass
            if not self._content_shutdown:
                self._content_shutdown = True
                try:
                    if hasattr(tab, "shutdown"):
                        tab.shutdown()
                except Exception as exc:
                    log.debug("Map window content shutdown failed: %s", exc)
        self._allow_final_close = True
        self.close()
        self.deleteLater()
