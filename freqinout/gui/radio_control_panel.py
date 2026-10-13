"""Cache-only radio-card controls for the Station Control Bar.

The panel owns presentation state only.  It deliberately accepts a bounded
snapshot and callbacks supplied by :class:`MainWindow`; it never reads a
database, asks an endpoint for a readback, or invokes scheduler code directly.

Cards are keyed by radio/profile id and are retained while ordinary snapshots
change.  This is important for operator trust: a poll must not reset a target
draft, a hold duration, keyboard focus, or the selected advanced-control
scope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

from PySide6.QtCore import QEvent, QTimer, Qt, Signal, QSize, QRect
from PySide6.QtWidgets import (
    QComboBox,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from freqinout.core.operating_group_frequency import normalize_frequency_hz
from freqinout.gui.theme import button_style, fit_combo_box_to_contents, font_derived_widget_height, get_theme, station_control_card_style, status_chip_style, quiet_control_style, control_icon, icon_button_metrics, icon_button_style
from freqinout.gui.widget_updates import set_widget_value


class ResponsiveAwarenessRail(QFrame):
    """Cache-only, wrapping host for the Station Control Bar awareness chips.

    The rail is deliberately independent of endpoint state.  It only arranges
    the already-created controls when its width, font, or visibility changes.
    At narrow widths the clock receives its own row so a complete time remains
    readable while source and condition controls wrap to content-sized rows.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stationCommandSourceRail")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._controls: list[QWidget] = []
        self._clock: QWidget | None = None
        self._layout_signature: tuple[object, ...] | None = None
        self._reflowing = False
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow)

    def addWidget(self, widget: QWidget) -> None:
        """Small QHBoxLayout-compatible adapter used by the rail builder."""
        if widget.objectName() == "stationCommandClock":
            self._clock = widget
        else:
            self._controls.append(widget)
        widget.setParent(self)
        widget.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._apply_floors()
        self._schedule_reflow()

    def addStretch(self, _stretch: int = 0) -> None:
        """Ignore the old horizontal spacer; wrapping owns all free space."""

    def _schedule_reflow(self) -> None:
        if not self.isVisible() or self._reflow_timer.isActive():
            return
        self._reflow_timer.start(0)

    def _reflow(self) -> None:
        if self._reflowing:
            return
        self._reflowing = True
        try:
            self._apply_floors()
            visible = [widget for widget in self._controls if not widget.isHidden()]
            margins = self._layout.contentsMargins()
            available = max(1, int(self.width()) - margins.left() - margins.right())
            spacing = 6
            hints = [max(1, int(widget.minimumWidth() or widget.sizeHint().width())) for widget in visible]
            rows: list[list[QWidget]] = []
            row_width = 0
            for widget, hint in zip(visible, hints):
                needed = hint if not rows or not rows[-1] else spacing + hint
                if rows and row_width + needed > available:
                    rows.append([])
                    row_width = 0
                    needed = hint
                if not rows:
                    rows.append([])
                rows[-1].append(widget)
                row_width += needed

            clock = self._clock if self._clock is not None and not self._clock.isHidden() else None
            clock_hint = max(1, int(clock.sizeHint().width())) if clock is not None else 0
            if clock is not None and rows and len(rows) == 1 and row_width + spacing + clock_hint <= available:
                rows[-1].append(clock)
                clock = None
            if clock is not None:
                rows.append([clock])

            signature = (
                tuple(tuple(id(item) for item in row) for row in rows),
                tuple((id(widget), int(widget.minimumWidth() or widget.sizeHint().width())) for widget in visible),
            )
            if signature == self._layout_signature:
                return
            self._layout_signature = signature
            while self._layout.count():
                item = self._layout.takeAt(0)
                old_layout = item.layout()
                if old_layout is not None:
                    while old_layout.count():
                        old_layout.takeAt(0)
                    old_layout.deleteLater()
            for row_index, row in enumerate(rows):
                row_layout = QHBoxLayout()
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(spacing)
                for widget in row:
                    if widget is self._clock:
                        row_layout.addStretch(1)
                    row_layout.addWidget(widget)
                if self._clock not in row:
                    row_layout.addStretch(1)
                self._layout.addLayout(row_layout)
        finally:
            self._reflowing = False

    def _apply_floors(self) -> None:
        widgets = self._controls
        for widget in widgets:
            try:
                height = int(widget.property("radioCardSlotHeight") or font_derived_widget_height(widget))
                if height > 0 and widget.minimumHeight() != height:
                    widget.setMinimumHeight(height)
            except (AttributeError, RuntimeError):
                continue
        if self._clock is not None:
            labels = self._clock.findChildren(QLabel)
            text_width = max((label.fontMetrics().horizontalAdvance(label.text()) for label in labels), default=0)
            if text_width:
                self._clock.setMinimumWidth(text_width + 10)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() in (QEvent.Show, QEvent.FontChange, QEvent.StyleChange, QEvent.LayoutRequest):
            self._schedule_reflow()
        return result

    def minimumSizeHint(self) -> QSize:
        # The panel/dock owns width. Never make a compact Dock wide just to fit
        # one unwrapped awareness row; height follows the current wrapping.
        return QSize(0, max(0, super().minimumSizeHint().height()))


def _value(snapshot: object, key: str, default: Any = "") -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _text(value: object, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _radio_id(snapshot: object) -> int:
    try:
        return int(_value(snapshot, "id", _value(snapshot, "device_profile_id", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _frequency_key(meta: object) -> str:
    if isinstance(meta, Mapping):
        row_key = _text(meta.get("operating_group_frequency_key"))
        if row_key:
            return "row:" + row_key
    try:
        hz = normalize_frequency_hz(_value(meta, 'freq', _value(meta, 'frequency', 0)))
        if hz is None:
            return ""
        group = _text(_value(meta, "group", _value(meta, "group_name", ""))).upper()
        band = _text(_value(meta, "band", "")).upper()
        mode = _text(_value(meta, "mode", "")).upper()
        return "|".join((group, band, mode, str(hz), _text(_value(meta, "vfo", "")).upper(),
                         _text(_value(meta, "fldigi_mode", "")).upper(), _text(_value(meta, "fldigi_offset", ""))))
    except (TypeError, ValueError, OverflowError):
        return ""


def _frequency_hz(meta: object) -> int | None:
    try:
        return normalize_frequency_hz(_value(meta, "freq", _value(meta, "frequency", 0)))
    except (TypeError, ValueError, OverflowError):
        return None


def _frequency_dial_matches(left: object, right: object) -> bool:
    """Match the observed dial while treating absent group/band as unknown."""
    left_hz, right_hz = _frequency_hz(left), _frequency_hz(right)
    if left_hz is None or right_hz is None or left_hz != right_hz:
        return False
    for keys in (("group", "group_name"), ("band",)):
        left_value = next((_text(_value(left, key, "")).upper() for key in keys if _text(_value(left, key, ""))), "")
        right_value = next((_text(_value(right, key, "")).upper() for key in keys if _text(_value(right, key, ""))), "")
        if left_value and right_value and left_value != right_value:
            return False
    return True


_FREQUENCY_VARIANT_FIELDS = (
    ("mode",),
    ("vfo",),
    ("fldigi_mode",),
    ("fldigi_offset", "fldigi_offset_hz"),
    ("js8_offset", "js8_offset_hz"),
)


def _frequency_variant_matches(observed: object, configured: object) -> bool:
    """Return whether every known mode/VFO/offset observation fits a row."""
    for keys in _FREQUENCY_VARIANT_FIELDS:
        observed_value = next((_text(_value(observed, key, "")).upper() for key in keys if _text(_value(observed, key, ""))), "")
        configured_value = next((_text(_value(configured, key, "")).upper() for key in keys if _text(_value(configured, key, ""))), "")
        if observed_value and observed_value != configured_value:
            return False
    return True


def _frequency_variant_text(meta: Mapping[str, object]) -> str:
    parts: list[str] = []
    mode = _text(meta.get("mode"))
    fldigi_mode = _text(meta.get("fldigi_mode"))
    vfo = _text(meta.get("vfo"))
    offset = _text(meta.get("fldigi_offset")) or _text(meta.get("fldigi_offset_hz"))
    js8_offset = _text(meta.get("js8_offset")) or _text(meta.get("js8_offset_hz"))
    if mode:
        parts.append(mode)
    if fldigi_mode and fldigi_mode.upper() != mode.upper():
        parts.append(fldigi_mode)
    if vfo:
        parts.append(f"VFO {vfo}")
    if offset:
        parts.append(f"offset {offset}")
    if js8_offset:
        parts.append(f"JS8 {js8_offset}")
    if not parts:
        stable = _text(meta.get("operating_group_frequency_key"))
        if stable:
            parts.append(f"variant {stable[-12:]}")
        else:
            parts.append("configured variant")
    return " · ".join(parts)


def _frequency_text(value: object) -> str:
    try:
        rendered = f"{float(value):.6f}".rstrip("0")
        whole, _, decimals = rendered.partition(".")
        rendered = whole + "." + decimals.ljust(3, "0")
        return f"{rendered} MHz"
    except (TypeError, ValueError):
        return "Frequency unavailable"


@dataclass(frozen=True)
class RadioCardDisplay:
    """GUI-facing evidence supplied by the parent cache projection."""

    observed_text: str = "Frequency unavailable"
    observed_hz: int | None = None
    observed_meta: Mapping[str, object] | None = None
    display_state: str = "unavailable"
    current_group: str = ""
    current_band: str = ""
    authority: str = "On schedule"
    authority_role: str = "info"
    next_text: str = "No next destination"
    notice: str = ""
    qsy_progress: str = ""
    app_rows: tuple[tuple[str, str, str], ...] = ()
    frequency_controls_available: bool = True
    hold_remaining_minutes: int | None = None
    next_tooltip: str = ""
    net_active: bool = False
    sop_tuning_enabled: bool = True
    routine_schedule_enabled: bool = True
    timer_policy: Mapping[str, str] | None = None
    cat_verified: bool = False
    cat_status: str = "Unknown · delayed"

    @classmethod
    def from_mapping(cls, value: object) -> "RadioCardDisplay":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        rows: list[tuple[str, str, str]] = []
        raw_rows = value.get("app_rows", value.get("applications", ()))
        if isinstance(raw_rows, Mapping):
            raw_rows = tuple((name, text, "") for name, text in raw_rows.items())
        for raw in raw_rows or ():
            if isinstance(raw, Mapping):
                rows.append((_text(raw.get("name")), _text(raw.get("text")), _text(raw.get("tooltip"))))
            elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                parts = list(raw)
                rows.append((_text(parts[0] if parts else ""), _text(parts[1] if len(parts) > 1 else ""), _text(parts[2] if len(parts) > 2 else "")))
        remaining_minutes = None
        try:
            raw_minutes = value.get("hold_remaining_minutes")
            parsed = float(raw_minutes)
            if not isinstance(raw_minutes, bool) and math.isfinite(parsed) and parsed >= 0:
                remaining_minutes = int(math.ceil(parsed))
        except (TypeError, ValueError, OverflowError):
            pass
        return cls(
            observed_text=_text(value.get("observed_text"), "Frequency unavailable"),
            observed_hz=value.get("observed_hz"),
            observed_meta=(dict(value.get("observed_meta")) if isinstance(value.get("observed_meta"), Mapping) else None),
            display_state=_text(value.get("display_state"), "unavailable").lower(),
            current_group=_text(value.get("current_group")),
            current_band=_text(value.get("current_band")),
            authority=_text(value.get("authority"), "On schedule"),
            authority_role=_text(value.get("authority_role"), "info"),
            next_text=_text(value.get("next_text"), "No next destination"),
            notice=_text(value.get("notice")),
            qsy_progress=_text(value.get("qsy_progress")),
            app_rows=tuple(rows),
            frequency_controls_available=bool(value.get("frequency_controls_available", True)),
            hold_remaining_minutes=remaining_minutes,
            next_tooltip=_text(value.get("next_tooltip")),
            net_active=bool(value.get("net_active", False)),
            sop_tuning_enabled=bool(value.get("sop_tuning_enabled", True)),
            routine_schedule_enabled=bool(value.get("routine_schedule_enabled", True)),
            timer_policy=(
                {str(key): str(item) for key, item in value.get("timer_policy", {}).items()}
                if isinstance(value.get("timer_policy"), Mapping)
                else None
            ),
            cat_verified=bool(value.get("cat_verified", False)),
            cat_status=_text(
                value.get("cat_status"),
                "CAT verified" if bool(value.get("cat_verified", False)) else "Unknown · delayed",
            ),
        )


@dataclass(frozen=True)
class RadioCardCallbacks:
    """Actions stay in MainWindow so existing command guards remain authoritative."""

    select: Callable[[int], None] | None = None
    qsy: Callable[[int, Mapping[str, object] | None, int | None], None] | None = None
    hold: Callable[[int, int | None], None] | None = None
    resume: Callable[[int], None] | None = None
    sop: Callable[[int], None] | None = None
    net: Callable[[int], None] | None = None
    sources: Callable[[int], None] | None = None
    policy: Callable[[int], None] | None = None
    advanced: Callable[[int], None] | None = None
    health: Callable[[int], None] | None = None
    plan: Callable[[int], None] | None = None
    launch: Callable[[int], None] | None = None
    js8_offset: Callable[[int], None] | None = None
    navigation: Callable[[str], None] | None = None
    dock: Callable[[], None] | None = None


class DestinationComboBox(QComboBox):
    """Keep complete popup data; shorten only secondary closed-control copy."""

    def closed_display_text(self) -> str:
        full = self.currentText()
        meta = self.currentData()
        if not isinstance(meta, Mapping):
            return full
        frequency = _frequency_text(meta.get("freq", meta.get("frequency")))
        group = _text(meta.get("group"))
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, self)
        width = max(1, rect.width() - 6)
        for candidate in (full, " · ".join(filter(None, (frequency, group))), frequency, frequency.removesuffix(" MHz")):
            if self.fontMetrics().horizontalAdvance(candidate) <= width:
                return candidate
        return frequency.removesuffix(" MHz")

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        compact = bool(self.property("radioCardCompactDestination"))
        option.currentText = "" if compact else self.closed_display_text()
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        painter.drawControl(QStyle.CE_ComboBoxLabel, option)
        if compact:
            rect = self.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, self).adjusted(4, 0, -4, 0)
            meta = self.currentData()
            if isinstance(meta, Mapping):
                frequency = _frequency_text(meta.get("freq", meta.get("frequency")))
                if self.fontMetrics().horizontalAdvance(frequency) > rect.width():
                    frequency = frequency.removesuffix(" MHz")
                context = " ".join(filter(None, (_text(meta.get("group", meta.get("group_name"))), _text(meta.get("band")))))
                line = self.fontMetrics().lineSpacing()
                top = rect.center().y() - line
                painter.drawText(QRect(rect.x(), top, rect.width(), line), Qt.AlignLeft | Qt.AlignVCenter, frequency)
                painter.drawText(QRect(rect.x(), top + line, rect.width(), line), Qt.AlignLeft | Qt.AlignVCenter,
                                 self.fontMetrics().elidedText(context, Qt.ElideRight, rect.width()))
            else:
                painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, self.currentText())


class RadioControlCard(QFrame):
    """One retained, radio-scoped card."""

    selected = Signal(int)
    controls_requested = Signal(int)

    def __init__(
        self,
        radio_id: int,
        *,
        callbacks: RadioCardCallbacks | None = None,
        theme: Mapping[str, object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.radio_id = int(radio_id or 0)
        self.callbacks = callbacks or RadioCardCallbacks()
        self._theme = dict(theme or get_theme("light"))
        self._draft_key = ""
        self._duration_by_value: dict[int, int | None] = {}
        self._updating = False
        self._style_signature: tuple[object, ...] | None = None
        self._action_style_signature: tuple[object, ...] | None = None
        self._semantic_signature: tuple[object, ...] | None = None
        self._minimized = False
        self._sop_full_text = "SOP actions"
        self._sop_urgency = "quiet"
        self._name_full_text = f"Radio {self.radio_id}"
        self._authority_full_text = "On schedule"
        self._identity_layout_signature: bool | None = None
        self._fields_layout_signature: bool | None = None
        self._actions_layout_signature: bool | None = None
        self._app_evidence_available = False
        self.setObjectName("radioControlCard")
        self.setMinimumWidth(0)
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._build()
        self._selector_geometry_timer = QTimer(self)
        self._selector_geometry_timer.setSingleShot(True)
        self._selector_geometry_timer.timeout.connect(self._sync_selector_heights)
        self.frequency_combo.installEventFilter(self)
        self.duration_combo.installEventFilter(self)
        self.sop_button.installEventFilter(self)
        self._selector_geometry_timer.start(0)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 10, 11, 10)
        layout.setSpacing(4)

        identity = QWidget(self)
        identity.setObjectName("radioCardIdentity")
        identity_layout = QGridLayout(identity)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(6)
        self._identity_layout = identity_layout
        self.name_button = QPushButton(identity)
        self.name_button.setObjectName("radioCardName")
        self.name_button.setCheckable(True)
        self.name_button.setFlat(False)
        self.name_button.setMinimumWidth(0)
        self.name_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.name_button.setMinimumHeight(font_derived_widget_height(self.name_button))
        self.name_button.setCursor(Qt.PointingHandCursor)
        self.name_button.clicked.connect(self._select)
        self.group_label = QLabel(identity)
        self.group_label.setObjectName("radioCardGroup")
        self.group_label.setWordWrap(True)
        self.group_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.authority_label = QPushButton(identity)
        self.authority_label.setObjectName("radioCardAuthority")
        self.authority_label.clicked.connect(lambda: self.callbacks.health(self.radio_id) if self.callbacks.health else None)
        self.authority_label.setAccessibleName("Schedule information for this radio")
        self.authority_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.cat_label = QLabel(identity)
        self.cat_label.setObjectName("radioCardCatStatus")
        self.cat_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.cat_label.setAccessibleName("CAT evidence status")
        self._identity_separator = QLabel("·", identity)
        identity_layout.addWidget(self.name_button, 0, 0)
        identity_layout.addWidget(self._identity_separator, 0, 1)
        identity_layout.addWidget(self.group_label, 0, 2)
        identity_layout.addWidget(self.authority_label, 0, 3)
        identity_layout.addWidget(self.cat_label, 0, 4)
        identity_layout.setColumnStretch(0, 1)
        layout.addWidget(identity)
        self.status_widget = QWidget(self)
        self.status_widget.setObjectName("radioCardStatusRow")
        self._status_layout = QGridLayout(self.status_widget)
        self._status_layout.setContentsMargins(0, 0, 0, 0)
        self._status_layout.setSpacing(6)
        self._status_layout_signature = None
        layout.addWidget(self.status_widget)

        self.compact_frequency_label = QLabel(identity)
        self.compact_frequency_label.setObjectName("radioCardCompactFrequency")
        self.compact_frequency_label.setWordWrap(True)
        self.compact_frequency_label.setVisible(False)
        layout.addWidget(self.compact_frequency_label)

        fields = QWidget(self)
        fields.setObjectName("radioCardFields")
        fields_layout = QGridLayout(fields)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.setHorizontalSpacing(8)
        fields_layout.setVerticalSpacing(3)
        frequency = QLabel("Frequency", fields)
        frequency.setObjectName("radioCardFrequencyCaption")
        hold = QLabel("Hold after action", fields)
        hold.setObjectName("radioCardDurationCaption")
        self._fields_layout = fields_layout
        self._frequency_caption = frequency
        self._duration_caption = hold
        self.observed_label = QLabel(fields)
        self.observed_label.setObjectName("radioCardObserved")
        self.observed_label.setAccessibleName("Observed frequency")
        self.frequency_combo = DestinationComboBox(fields)
        self.frequency_combo.setObjectName("radioCardFrequency")
        self.frequency_combo.setProperty("radioCardAllFrequencies", True)
        self.frequency_combo.setAccessibleName("All configured frequencies for this radio")
        self.frequency_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.frequency_combo.currentIndexChanged.connect(self._draft_changed)
        self.duration_combo = QComboBox(fields)
        self.duration_combo.setObjectName("radioCardHoldDuration")
        self.duration_combo.setAccessibleName("Hold after action duration")
        self.duration_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.duration_combo.currentIndexChanged.connect(self._duration_changed)
        fields_layout.addWidget(frequency, 0, 0, 1, 2)
        fields_layout.addWidget(hold, 0, 2)
        fields_layout.addWidget(self.frequency_combo, 1, 0, 1, 2)
        fields_layout.addWidget(self.duration_combo, 1, 2)
        self.observed_label.hide()
        fields_layout.setColumnStretch(0, 1)
        fields_layout.setColumnStretch(1, 2)
        fields_layout.setColumnStretch(2, 1)
        layout.addWidget(fields)

        actions = QWidget(self)
        actions.setObjectName("radioCardActions")
        actions_layout = QGridLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        # Icon-only controls should retain content-sized widths even in the
        # narrow Dock; the tighter gap keeps one card inside the viewport.
        actions_layout.setSpacing(4)
        self._actions_layout = actions_layout
        self.qsy_button = QPushButton(actions)
        self.qsy_button.setObjectName("radioCardQsy")
        self.qsy_button.setToolTip("Choose a configured destination, then QSY this radio.")
        self.qsy_button.clicked.connect(self._qsy)
        self.qsy_button.setAccessibleName("QSY to the selected configured destination")
        self.hold_button = QToolButton(actions)
        self.hold_button.setText("")
        self.hold_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.hold_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.hold_menu = QMenu(self.hold_button)
        self.hold_menu.aboutToShow.connect(self._sync_hold_menu)
        self.hold_button.setMenu(self.hold_menu)
        self.hold_button.setObjectName("radioCardHold")
        self.hold_button.clicked.connect(self._hold_or_resume)
        self.sop_button = QPushButton(actions)
        self.sop_button.setObjectName("radioCardSop")
        self.sop_button.setCheckable(True)
        self.sop_button.setToolTip("Toggle SOP tuning for this radio.")
        self.sop_button.setAccessibleName("SOP tuning")
        self.sop_button.clicked.connect(self._sop)
        self.net_button = QToolButton(actions)
        self.net_button.setObjectName("radioCardNet")
        self.net_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.net_button.setCheckable(True)
        self.net_button.setAccessibleName("Net control")
        self.net_button.setToolTip("Enter or end Net control for this radio.")
        self.net_button.clicked.connect(self._net)
        self.sources_button = QToolButton(actions)
        self.sources_button.setObjectName("radioCardSources")
        self.sources_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.sources_button.setAccessibleName("Schedule sources")
        self.sources_button.setToolTip("Review schedule sources for this radio.")
        self.sources_button.clicked.connect(self._sources)
        self.policy_button = QToolButton(actions)
        self.policy_button.setObjectName("radioCardPolicy")
        self.policy_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.policy_button.setAccessibleName("Schedule policy")
        self.policy_button.setToolTip("Review quick schedule policy for this radio.")
        self.policy_button.clicked.connect(self._policy)
        for button in (self.qsy_button, self.net_button, self.hold_button, self.sop_button, self.sources_button, self.policy_button):
            button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            button.setMinimumHeight(font_derived_widget_height(button))
        actions_layout.addWidget(self.qsy_button, 0, 0)
        actions_layout.addWidget(self.net_button, 0, 1)
        actions_layout.addWidget(self.hold_button, 0, 2)
        actions_layout.addWidget(self.sop_button, 0, 3)
        actions_layout.addWidget(self.sources_button, 0, 4)
        actions_layout.addWidget(self.policy_button, 0, 5)
        layout.addWidget(actions)

        self.app_frame = ResponsiveAwarenessRail(self)
        self.app_frame.setObjectName("radioCardAppEvidence")
        self.app_layout = self.app_frame
        layout.insertWidget(layout.indexOf(actions), self.app_frame)

        next_row = QWidget(self.status_widget)
        self.next_row = next_row
        next_row.setObjectName("radioCardNextRow")
        next_layout = QHBoxLayout(next_row)
        next_layout.setContentsMargins(0, 0, 0, 0)
        next_layout.setSpacing(4)
        self.next_icon = QLabel(next_row)
        self.next_icon.setAlignment(Qt.AlignTop)
        self.next_label = QLabel(next_row)
        self.next_label.setObjectName("radioCardNext")
        self.next_label.setWordWrap(True)
        next_layout.addWidget(self.next_icon)
        next_layout.addWidget(self.next_label, 1)
        self._status_layout.addWidget(next_row, 0, 1)
        self.notice_label = QLabel(self)
        self.notice_label.setObjectName("radioCardNotice")
        self.notice_label.setWordWrap(True)
        self.notice_label.setMinimumHeight(self.notice_label.fontMetrics().lineSpacing() + 4)
        layout.addWidget(self.notice_label)

        self._advanced_button = QToolButton(self)
        self._advanced_button.setObjectName("radioCardAdvanced")
        self._advanced_button.setText("More controls")
        self._advanced_button.clicked.connect(self._advanced)
        self._advanced_button.setText("Controls…")
        self._advanced_button.setAccessibleName("Additional controls for this radio")
        self._advanced_button.setStyleSheet(button_style("muted", dict(self._theme)))
        self._advanced_button.setMinimumHeight(font_derived_widget_height(self._advanced_button))
        self._advanced_button.hide()
        self._install_selection_filters()

        # Keep the three functional areas addressable when the shell is
        # minimized.  The panel's Expand controls restores them in place.
        fields.hide()
        self._frequency_caption.hide()
        self._duration_caption.hide()
        self._compact_widgets = (actions,)
        self._identity_layout_signature = None
        self._reflow_card_layouts()
        self.duration_combo.hide()
        self._fields_widget = fields
        self._actions_widget = actions

    def set_minimized(self, minimized: bool) -> None:
        minimized = bool(minimized)
        if minimized == self._minimized:
            return
        self._minimized = minimized
        self._update_actions()
        # The selector remains visible in minimized cards, so a second current
        # frequency line would duplicate the same destination context.
        self.compact_frequency_label.hide()
        self._update_compact_frequency()

    def _update_compact_frequency(self) -> None:
        if not hasattr(self, "compact_frequency_label"):
            return
        # Compact mode is an observation summary. Never use the selected
        # destination draft here, even while the operator edits a target.
        text = _text(self.observed_label.property("radioCardObservedText"), "Frequency unavailable")
        set_widget_value(self.compact_frequency_label, "text", text)

    def set_theme(self, theme: Mapping[str, object]) -> None:
        self._theme = dict(theme or get_theme("light"))
        self._style()
        self._update_cat_status(
            verified=bool(self.property("radioCardCatVerified")),
            status=str(self.property("radioCardCatStatus") or "CAT unknown/delayed"),
        )
        self._sync_control_icons()
        for chip in self.app_frame.findChildren(QFrame, "statusChip"):
            set_widget_value(chip, "styleSheet", status_chip_style(dict(self._theme), chip.fontMetrics()))

    def _style(self) -> None:
        theme = self._theme
        selected = bool(self.property("selected"))
        signature = (
            selected,
            str(self.authority_label.property("radioCardAuthorityRole") or "info"),
            bool(self.net_button.isChecked()), bool(self.sop_button.isChecked()), self._sop_urgency,
            bool(self.hold_button.property("radioCardHeld")), self.qsy_button.isEnabled(),
            self.fontMetrics().lineSpacing(),
            tuple(sorted((str(key), repr(value)) for key, value in theme.items())),
        )
        if signature == self._style_signature:
            return
        self._style_signature = signature
        self.setStyleSheet(station_control_card_style(dict(theme), selected=selected))
        set_widget_value(self.authority_label, "styleSheet", quiet_control_style(dict(theme), "text" if signature[1] in {"info", "muted"} else signature[1]))
        for button, role in ((self.name_button, "muted"), (self._advanced_button, "muted"),
                             (self.qsy_button, "primary" if self.qsy_button.isEnabled() else "muted"),
                             (self.hold_button, "warning" if self.hold_button.property("radioCardHeld") else "muted"),
                             (self.net_button, "primary" if self.net_button.isChecked() else "muted"),
                             (self.sop_button, "danger" if self._sop_urgency == "due" else "warning" if self._sop_urgency == "approaching" else "primary" if self.sop_button.isChecked() else "muted"),
                             (self.sources_button, "muted"), (self.policy_button, "muted")):
            button.setStyleSheet(button_style(role, dict(theme)) if button in (self.name_button, self._advanced_button)
                                else icon_button_style(role, dict(theme), button))

    def _sync_control_icons(self) -> None:
        held = bool(self.hold_button.property("radioCardHeld"))
        net_active = bool(self.net_button.property("radioCardNetActive"))
        sop_enabled = bool(self.sop_button.property("radioCardSopTuningEnabled"))
        role = str(self.authority_label.property("radioCardAuthorityRole") or "info")
        size = icon_button_metrics(self.qsy_button)[0]
        scheduled = self._authority_full_text.lower() in {"on schedule", "on schedule · verified"}
        signature = (held, net_active, sop_enabled, self._sop_urgency, role, scheduled, self.qsy_button.isEnabled(), size, self.devicePixelRatioF(),
                     tuple(sorted(self._theme.items())))
        if signature == getattr(self, "_icons_signature", None):
            return
        self._icons_signature = signature
        for button, name, icon_role in ((self.qsy_button, "arrow-right", "on_accent" if self.qsy_button.isEnabled() else "text"),
                (self.hold_button, "resume" if held else "pause", "on_warning" if held else "text"),
                (self.net_button, "headset", "on_accent" if net_active else "text"),
                (self.sop_button, "list-checks" if sop_enabled else "list-x", "on_danger" if self._sop_urgency == "due" else "on_warning" if self._sop_urgency == "approaching" else "on_accent" if sop_enabled else "text"),
                (self.sources_button, "calendar-days", "text"),
                (self.policy_button, "sliders-horizontal", "text"),
                (self.authority_label, "headset" if net_active else "pause" if held else "schedule" if scheduled else "issue", "warning" if held else "success" if scheduled and role == "info" else "text" if role == "info" else role)):
            button.setIcon(control_icon(name, self._theme, size, self.devicePixelRatioF(), icon_role))
            button.setIconSize(QSize(size, size))
        self.next_icon.setPixmap(control_icon("clock", self._theme, size, self.devicePixelRatioF(), "text_muted").pixmap(QSize(size, size)))
        for chip in self.app_frame.findChildren(QFrame, "statusChip"):
            if hasattr(chip, "_fio_icon"):
                name = str(chip.property("radioCardAppName"))
                chip._fio_icon.setPixmap(control_icon(name, self._theme, size, self.devicePixelRatioF()).pixmap(QSize(size, size)))
            activity = chip._fio_activity.text()
            set_widget_value(chip._fio_activity, "styleSheet", "color:" + str(self._theme.get("danger" if activity == "TX" else "info" if activity == "RX" else "warning" if activity.startswith("QSO") else "text", self._theme["text"])) + ";")

    def _select(self) -> None:
        self.name_button.setChecked(True)
        if not self.property("selected"):
            self.selected.emit(self.radio_id)

    def _draft_changed(self, _index: int = -1) -> None:
        if self._updating:
            return
        meta = self.frequency_combo.currentData()
        selected_key = _frequency_key(meta)
        observed_key = _frequency_key(self.frequency_combo.property("radioCardObservedMeta"))
        self._draft_key = selected_key if selected_key and selected_key != observed_key else ""
        self.frequency_combo.setProperty("radioCardDraft", bool(self._draft_key))
        set_widget_value(self.frequency_combo, "toolTip", self.frequency_combo.currentText())
        self._update_actual_readback()
        self._update_actions()

    def _update_actual_readback(self) -> None:
        observed_key = _frequency_key(self.frequency_combo.property("radioCardObservedMeta"))
        selected_key = _frequency_key(self.frequency_combo.currentData())
        draft = bool(observed_key and selected_key and observed_key != selected_key)
        actual = str(self.observed_label.property("radioCardObservedText") or "")
        self.observed_label.hide()
        set_widget_value(self.observed_label, "text", "")
        text = self.frequency_combo.currentText()
        tooltip = f"On radio: {actual}\nDestination: {text}" if draft and actual else text
        set_widget_value(self.frequency_combo, "toolTip", tooltip)

    def _update_sop_button_text(self) -> None:
        if not hasattr(self, "sop_button"):
            return
        full_text = _text(self._sop_full_text, "SOP actions")
        # ``sop_text`` remains a compatibility input for the cached due/upcoming
        # context, but the action is a stable icon-only tuning toggle.
        set_widget_value(self.sop_button, "text", "")
        set_widget_value(self.sop_button, "toolTip", f"{self._name_full_text} — {full_text}\nSOP tuning {'on' if self.sop_button.isChecked() else 'off'}. Click to {'disable' if self.sop_button.isChecked() else 'enable'} tuning.\nAction reminders remain active when tuning is off.")
        set_widget_value(self.sop_button, "accessibleName", f"SOP tuning {'on' if self.sop_button.isChecked() else 'off'} for {self._name_full_text}. {full_text}")

    def _update_name_text(self) -> None:
        if not hasattr(self, "name_button"):
            return
        full_text = _text(self._name_full_text, f"Radio {self.radio_id}")
        name_width = min(self.name_button.fontMetrics().horizontalAdvance(full_text) + 24, self.fontMetrics().horizontalAdvance("FTDX-101D") + 24)
        frequency_floor = 0 if self._identity_layout_signature else self._frequency_closed_width_floor()
        available = self.width()
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                available = min(available, ancestor.viewport().width())
                break
            ancestor = ancestor.parentWidget()
        budget = max(self.fontMetrics().horizontalAdvance("MMM") + 12,
                     available - frequency_floor - self.qsy_button.sizeHint().width() - 36)
        self.name_button.setFixedWidth(min(name_width, budget))
        available = max(50, int(self.name_button.width() or 0))
        shown = self.name_button.fontMetrics().elidedText(full_text, Qt.ElideRight, available - 24)
        set_widget_value(self.name_button, "text", shown)
        set_widget_value(self.name_button, "toolTip", full_text)
        set_widget_value(self.name_button, "accessibleName", full_text)

    def _update_authority_text(self) -> None:
        if not hasattr(self, "authority_label"):
            return
        full_text = _text(self._authority_full_text, "On schedule")
        if self.net_button.property("radioCardNetActive"):
            text = "Net mode"
        elif self.hold_button.property("radioCardHeld"):
            minutes = self.property("radioCardHoldRemainingMinutes")
            text = f"Paused · {minutes}m" if minutes is not None else "Paused"
        elif self.property("radioCardRoutineScheduleEnabled") is False:
            text = "Daily / Net off"
        elif full_text.lower() in {"on schedule", "on schedule · verified"}:
            text = "Scheduled"
        else:
            text = full_text
        self.authority_label.setProperty("radioCardAuthorityText", text)
        shown = self.authority_label.fontMetrics().elidedText(text, Qt.ElideRight, max(80, self.authority_label.maximumWidth() - 36))
        set_widget_value(self.authority_label, "text", shown)
        tooltip_suffix = ""
        if self.net_button.property("radioCardNetActive"):
            tooltip_suffix = "\nOperator controls tuning; automatic corrections are suspended. Use the headset for Net options."
        elif self.hold_button.property("radioCardHeld"):
            tooltip_suffix = (f"\nResume schedule · {self.property('radioCardHoldRemainingMinutes')}m remaining"
                              if self.property("radioCardHoldRemainingMinutes") is not None else "\nUntil Resume")
        elif self.property("radioCardRoutineScheduleEnabled") is False:
            tooltip_suffix = "\nRoutine schedule destinations are disabled for this radio."
        set_widget_value(self.authority_label, "toolTip", full_text + tooltip_suffix)
        set_widget_value(self.authority_label, "accessibleName", f"{self._name_full_text}: {text}. {full_text}")

    def _update_cat_status(self, *, verified: bool, status: str) -> None:
        label = getattr(self, "cat_label", None)
        if label is None:
            return
        text = "CAT verified" if verified else _text(status, "unknown · delayed")
        if not verified and "delayed" in text.casefold():
            text = "CAT delayed"
        if not verified and not text.lower().startswith("cat"):
            text = "CAT " + text
        set_widget_value(label, "text", text)
        set_widget_value(
            label,
            "toolTip",
            "Fresh frequency evidence from the configured CAT endpoint."
            if verified
            else "CAT status is unknown or delayed; fresh frequency evidence is required for verification.",
        )
        set_widget_value(label, "accessibleName", text)
        color = self._theme.get("success" if verified else "text_muted", self._theme.get("text", "#202124"))
        set_widget_value(label, "styleSheet", f"QLabel {{ color:{color}; background:transparent; }}")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_card_layouts()
        self._update_name_text()
        self._update_authority_text()
        self._update_sop_button_text()

    def _reflow_card_layouts(self) -> None:
        em = max(1, self.fontMetrics().horizontalAdvance("M"))
        narrow = self.width() < max(420, em * 38)
        if narrow != self._identity_layout_signature:
            self._identity_layout_signature = narrow
            self._actions_layout_signature = None
            self._status_layout_signature = None
            for layout in (self._identity_layout, self._fields_layout, self._actions_layout):
                while layout.count():
                    layout.takeAt(0)
                for column in range(layout.columnCount()):
                    layout.setColumnStretch(column, 0)
                    layout.setColumnMinimumWidth(column, 0)
            self._identity_separator.hide()
            self.group_label.hide()
            self._identity_layout.addWidget(self.name_button, 0, 0)
            if narrow:
                self._identity_layout.addWidget(self.frequency_combo, 1, 0, 1, 2)
            else:
                self._identity_layout.addWidget(self.frequency_combo, 0, 1)
            self.frequency_combo.setProperty("radioCardCompactDestination", narrow)
            if hasattr(self, "_selector_geometry_timer") and not self._selector_geometry_timer.isActive():
                self._selector_geometry_timer.start(0)
            self._identity_layout.setColumnStretch(1, 1)
            self.duration_combo.hide()
            # In narrow cards keep essential actions above wrapping app
            # evidence, so Large Text never pushes them below the Dock fold.
            actions = self.qsy_button.parentWidget()
            self.layout().removeWidget(actions)
            app_index = self.layout().indexOf(self.app_frame)
            self.layout().insertWidget(app_index if narrow else app_index + 1, actions)
        # Wrap the icon row before its natural width can force a compact card
        # beyond the viewport. Never shrink icons to accommodate Large Text.
        buttons = (self.qsy_button, self.net_button, self.hold_button,
                   self.sop_button, self.sources_button, self.policy_button)
        available = self.width()
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                available = min(available, ancestor.viewport().width())
                break
            ancestor = ancestor.parentWidget()
        widths = tuple(icon_button_metrics(button)[2] + (icon_button_metrics(button)[3] if button is self.hold_button else 0)
                       for button in buttons)
        columns = 3 if sum(widths) + 5 * self._actions_layout.horizontalSpacing() > available - 24 else 6
        action_signature = (columns, widths)
        if action_signature != self._actions_layout_signature:
            self._actions_layout_signature = action_signature
            while self._actions_layout.count():
                self._actions_layout.takeAt(0)
            for column in range(self._actions_layout.columnCount()):
                self._actions_layout.setColumnStretch(column, 0)
                self._actions_layout.setColumnMinimumWidth(column, 0)
            for index, button in enumerate(buttons):
                self._actions_layout.addWidget(button, index // columns, index % columns)
            self._actions_layout.setColumnStretch(columns, 1)
        self._reflow_status_row(available - 24)
        self._fit_app_slots()

    def _reflow_status_row(self, available: int) -> None:
        """Keep Where/When evidence together, wrapping before it can clip."""
        text = str(self.authority_label.property("radioCardAuthorityText") or "Scheduled")
        metrics = self.authority_label.fontMetrics()
        authority_width = min(metrics.horizontalAdvance(text) + 36,
                              metrics.horizontalAdvance("On schedule · verified") + 24)
        cat_width = self.cat_label.sizeHint().width()
        next_width = (self.next_label.fontMetrics().horizontalAdvance(self.next_label.text())
                      + self.next_icon.sizeHint().width() + 4)
        inline = authority_width + next_width + cat_width + 12 <= max(0, available)
        stacked = authority_width + cat_width + 12 > max(0, available)
        signature = (inline, stacked, authority_width, cat_width, bool(self.next_label.text()))
        if signature == self._status_layout_signature:
            return
        self._status_layout_signature = signature
        self.authority_label.setFixedWidth(authority_width)
        while self._status_layout.count():
            self._status_layout.takeAt(0)
        for column in range(3):
            self._status_layout.setColumnStretch(column, 0)
        if stacked:
            self._status_layout.addWidget(self.authority_label, 0, 0, 1, 3)
            self._identity_layout.addWidget(self.cat_label, 0, 1, Qt.AlignRight)
            self._status_layout.addWidget(self.next_row, 1, 0, 1, 3)
        else:
            self._status_layout.addWidget(self.authority_label, 0, 0)
            self._status_layout.addWidget(self.cat_label, 0, 2)
        if inline:
            self._status_layout.addWidget(self.next_row, 0, 1)
            self._status_layout.setColumnStretch(1, 1)
        elif not stacked:
            self._status_layout.addWidget(self.next_row, 1, 0, 1, 3)
            self._status_layout.setColumnStretch(1, 1)
        self.next_row.setVisible(bool(self.next_label.text()))
        self._update_authority_text()

    def _fit_app_slots(self) -> None:
        available = max(1, self.width() - 22 - 2 * self.frameWidth())
        minimum = self.fontMetrics().horizontalAdvance("FLDigi RX") + 24
        columns = max(1, min(3, (available + 6) // (minimum + 6)))
        width = max(1, (available - (columns - 1) * 6) // columns)
        for chip in getattr(self.app_frame, "_controls", ()):
            if chip.minimumWidth() != width:
                chip.setFixedWidth(width)
            height = 2 * self.fontMetrics().lineSpacing() + 3 * (max(2, self.fontMetrics().lineSpacing() // 5) + 1)
            chip.setProperty("radioCardSlotHeight", height)
            if chip.minimumHeight() != height or chip.maximumHeight() != height:
                chip.setFixedHeight(height)
            info = chip._fio_info
            text = str(info.property("radioCardFullText") or "")
            if chip.property("radioCardAppName") == "JS8" and text.endswith(" · last confirmed"):
                text = "Last " + text.removesuffix(" · last confirmed")
            room = max(1, width - 18)
            set_widget_value(info, "text", info.fontMetrics().elidedText(text, Qt.ElideRight, room))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange) and hasattr(self, "frequency_combo"):
            timer = getattr(self, "_selector_geometry_timer", None)
            if timer is not None and not timer.isActive():
                timer.start(0)
            self._identity_layout_signature = None
            self._fields_layout_signature = None
            self._actions_layout_signature = None
            self._reflow_card_layouts()

    def _frequency_closed_width_floor(self) -> int:
        return self.frequency_combo.fontMetrics().horizontalAdvance("00.000000") + self.frequency_combo.style().pixelMetric(QStyle.PM_ScrollBarExtent) + 12

    def _sync_selector_heights(self) -> None:
        self._style()
        self._sync_control_icons()
        for button in (self.name_button, self.authority_label, self._advanced_button,
                       self.qsy_button, self.net_button, self.hold_button,
                       self.sop_button, self.sources_button, self.policy_button):
            height = font_derived_widget_height(button)
            button.setMinimumHeight(height)
            if button in (self.qsy_button, self.net_button, self.hold_button,
                          self.sop_button, self.sources_button, self.policy_button):
                _, _, extent, menu = icon_button_metrics(button)
                button.setMinimumHeight(extent)
                # A split-menu button needs the same icon hit target PLUS its
                # independent arrow area; a square would clip the main icon.
                button.setFixedWidth(extent + (menu if button is self.hold_button else 0))
        for combo in (self.frequency_combo, self.duration_combo):
            fit_combo_box_to_contents(combo)
            # The card owns responsive closed-control width. Preserve the
            # shared popup fitting without forcing a narrow Dock to grow.
            combo.setMinimumWidth(self._frequency_closed_width_floor() if combo is self.frequency_combo else 0)
            combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            if combo is self.duration_combo:
                set_widget_value(combo, "toolTip", combo.currentText())
            height = font_derived_widget_height(combo) + (combo.fontMetrics().lineSpacing() if combo is self.frequency_combo and self._identity_layout_signature else 0)
            if combo.maximumHeight() < height:
                combo.setMaximumHeight(height)
            if combo.minimumHeight() != height:
                combo.setMinimumHeight(height)
        self._reflow_card_layouts()
        self._update_actual_readback()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            if watched.property("radioCardSelectSurface"):
                self._select()
        if watched is getattr(self, "sop_button", None) and event.type() == QEvent.Resize:
            self._update_sop_button_text()
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            timer = getattr(self, "_selector_geometry_timer", None)
            if timer is not None and not timer.isActive():
                timer.start(0)
        return super().eventFilter(watched, event)

    def _install_selection_filters(self) -> None:
        for widget in (self, *self.findChildren(QWidget)):
            if isinstance(widget, QLabel) or widget is self or widget.objectName() in {
                "radioCardIdentity", "radioCardFields", "radioCardActions", "radioCardAppEvidence", "radioCardAppRow"}:
                widget.setProperty("radioCardSelectSurface", True)
                widget.installEventFilter(self)

    def _duration_changed(self, _index: int = -1) -> None:
        if not self._updating:
            self.duration_combo.setProperty("radioCardDurationDraft", self.duration_combo.currentData())
            set_widget_value(self.duration_combo, "toolTip", self.duration_combo.currentText())
            self._update_actions()

    def _sync_hold_menu(self) -> None:
        self.hold_menu.clear()
        for index in range(self.duration_combo.count()):
            action = self.hold_menu.addAction("Until resumed" if self.duration_combo.itemData(index) is None else self.duration_combo.itemText(index))
            action.triggered.connect(lambda _checked=False, choice=index: self._start_hold(choice))

    def _start_hold(self, index: int) -> None:
        if not self.hold_button.isEnabled():
            return
        self.duration_combo.setCurrentIndex(index)
        if self.callbacks.hold:
            self.callbacks.hold(self.radio_id, self._duration())

    def _selected_meta(self) -> Mapping[str, object] | None:
        meta = self.frequency_combo.currentData()
        return dict(meta) if isinstance(meta, Mapping) else None

    def _duration(self) -> int | None:
        value = self.duration_combo.currentData()
        try:
            return int(value) if value not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            return None

    def _qsy(self) -> None:
        if self.callbacks.qsy:
            self.callbacks.qsy(self.radio_id, self._selected_meta(), self._duration())

    def _hold_or_resume(self) -> None:
        if self.hold_button.property("radioCardHeld"):
            if self.callbacks.resume:
                self.callbacks.resume(self.radio_id)
        elif self.callbacks.hold:
            # The primary pause affordance is always indefinite.  Timed holds
            # remain explicit menu choices so a prior duration draft cannot
            # silently change the meaning of a direct pause click.
            self.callbacks.hold(self.radio_id, None)

    def _sop(self) -> None:
        previous = bool(self.sop_button.property("radioCardSopTuningEnabled"))
        if self.callbacks.sop:
            self.callbacks.sop(self.radio_id)
        # The callback owns the guarded state transition.  Until its next
        # cached publication, keep the toggle at the last observed value.
        self.sop_button.setChecked(bool(self.sop_button.property("radioCardSopTuningEnabled")) if self.sop_button.property("radioCardSopTuningEnabled") != previous else previous)
        self._sync_control_icons()
        self._style()

    def _net(self) -> None:
        # MainWindow owns the end-Net choice and any guarded command.  The
        # card only reports the explicit radio-scoped request.
        previous = bool(self.net_button.property("radioCardNetActive"))
        if self.callbacks.net:
            self.callbacks.net(self.radio_id)
        self.net_button.setChecked(bool(self.net_button.property("radioCardNetActive")) if self.net_button.property("radioCardNetActive") != previous else previous)

    def _sources(self) -> None:
        if self.callbacks.sources:
            self.callbacks.sources(self.radio_id)

    def _policy(self) -> None:
        if self.callbacks.policy:
            self.callbacks.policy(self.radio_id)

    def _advanced(self) -> None:
        self.controls_requested.emit(self.radio_id)

    def update_hold_state(self, held: bool, remaining_minutes: int | None) -> None:
        signature = (bool(held), remaining_minutes)
        if signature == (bool(self.hold_button.property("radioCardHeld")), self.property("radioCardHoldRemainingMinutes")):
            return
        self.hold_button.setProperty("radioCardHeld", bool(held))
        self.setProperty("radioCardHoldRemainingMinutes", remaining_minutes)
        self._semantic_signature = None
        self._update_actions()

    def _update_actions(self) -> None:
        selected_key = _frequency_key(self.frequency_combo.currentData())
        observed_key = _frequency_key(self.frequency_combo.property("radioCardObservedMeta"))
        allowed = bool(self.frequency_combo.count() and self.frequency_combo.currentData() is not None)
        delayed = _text(self.property("radioCardDisplayState")).lower() in {"delayed", "unavailable", "invalidated"}
        held = bool(self.hold_button.property("radioCardHeld"))
        qsy_enabled = bool(allowed and selected_key and selected_key != observed_key and not delayed)
        controls_available = bool(self.property("radioCardFrequencyControlsAvailable"))
        qsy_enabled = qsy_enabled and controls_available
        set_widget_value(self.qsy_button, "enabled", qsy_enabled)
        set_widget_value(self.qsy_button, "toolTip", f"QSY {self._name_full_text} to the selected configured destination." if qsy_enabled else "Choose a different configured destination before QSY." if controls_available else "QSY is unavailable for this radio.")
        set_widget_value(self.hold_button, "accessibleName", f"{'Resume' if held else 'Hold'} schedule for {self._name_full_text}")
        set_widget_value(self.hold_button, "toolTip", "Resume scheduled changes." if held else "Pause scheduled changes until Resume. The menu offers optional durations.")
        set_widget_value(self.hold_button, "enabled", controls_available)
        # Net, hold/resume, SOP tuning, source and policy actions are the
        # always-visible card controls in both full and minimized modes.
        set_widget_value(self.net_button, "enabled", controls_available)
        set_widget_value(self.sources_button, "enabled", True)
        set_widget_value(self.policy_button, "enabled", True)
        self.hold_button.setVisible(True)
        self.sop_button.setVisible(True)
        self.net_button.setVisible(True)
        self.sources_button.setVisible(True)
        self.policy_button.setVisible(True)
        self.qsy_button.setVisible(True)
        self._actions_widget.setVisible(True)
        self.sop_button.setChecked(bool(self.sop_button.property("radioCardSopTuningEnabled")))
        self.net_button.setChecked(bool(self.net_button.property("radioCardNetActive")))
        self._update_authority_text()
        self._sync_control_icons()
        action_signature = (held, qsy_enabled, tuple(sorted((str(key), repr(value)) for key, value in self._theme.items())))
        if action_signature != self._action_style_signature:
            self._action_style_signature = action_signature
            self.hold_button.setStyleSheet(icon_button_style("warning" if held else "muted", dict(self._theme), self.hold_button))
            self.qsy_button.setStyleSheet(icon_button_style("primary" if qsy_enabled else "muted", dict(self._theme), self.qsy_button))

    def _set_combo_options(self, options: Sequence[object], observed_meta: Mapping[str, object] | None) -> None:
        previous_draft = self._draft_key
        normalized_options: list[dict[str, object]] = []
        if isinstance(options, (str, bytes)) or not isinstance(options, Sequence):
            options = ()
        for raw in options or ():
            if isinstance(raw, Mapping):
                normalized_options.append(dict(raw))
        observed_for_combo = dict(observed_meta or {})
        observed_identity = dict(observed_for_combo)
        matched_option: dict[str, object] | None = None
        if observed_for_combo:
            stable_key = _text(observed_for_combo.get("operating_group_frequency_key"))
            if stable_key:
                stable_matches = [item for item in normalized_options
                                  if _text(item.get("operating_group_frequency_key")) == stable_key
                                  and _frequency_dial_matches(observed_for_combo, item)
                                  and _frequency_variant_matches(observed_for_combo, item)]
                if len(stable_matches) == 1:
                    matched_option = stable_matches[0]
                else:
                    # A historical configured key is not evidence that an
                    # off-schedule dial/mode still matches that destination.
                    observed_for_combo.pop("operating_group_frequency_key", None)
                    observed_identity = dict(observed_for_combo)
            else:
                dial_matches = [item for item in normalized_options
                                if _frequency_dial_matches(observed_for_combo, item)]
                variant_matches = [item for item in dial_matches
                                   if _frequency_variant_matches(observed_for_combo, item)]
                if len(variant_matches) == 1:
                    matched_option = variant_matches[0]
            if matched_option is not None:
                merged = dict(matched_option)
                # Retain observed fields such as the exact readback text while
                # preserving configured identity (especially stablekey).
                merged.update({key: value for key, value in observed_for_combo.items()
                               if value not in (None, "") or key not in merged})
                configured_stable_key = _text(matched_option.get("operating_group_frequency_key"))
                if configured_stable_key:
                    merged["operating_group_frequency_key"] = configured_stable_key
                observed_for_combo = merged
                observed_identity = dict(merged)
        observed_key = _frequency_key(observed_identity)
        dedupe_keys = {_frequency_key(matched_option)} if matched_option is not None else set()
        if self._draft_key and self._draft_key == observed_key:
            self._draft_key = ""
        option_signature = (
            observed_key,
            tuple(tuple(sorted((str(key), repr(value)) for key, value in item.items())) for item in normalized_options),
        )
        plan_structure = tuple(_frequency_key(item) for item in normalized_options if _frequency_key(item))
        old_signature = self.frequency_combo.property("radioCardOptionsSignature")
        old_structure = self.frequency_combo.property("radioCardPlanStructureSignature")
        if option_signature == old_signature:
            self.frequency_combo.setProperty("radioCardObservedMeta", dict(observed_identity))
            self._update_actual_readback()
            self._update_compact_frequency()
            return

        variant_counts: dict[int, int] = {}
        for raw in normalized_options:
            frequency_hz = _frequency_hz(raw)
            if frequency_hz is not None:
                variant_counts[frequency_hz] = variant_counts.get(frequency_hz, 0) + 1

        def option_entries() -> list[tuple[str, dict[str, object] | None]]:
            entries: list[tuple[str, dict[str, object] | None]] = []
            if observed_for_combo:
                label = " · ".join(filter(None, (_frequency_text(_value(observed_for_combo, "freq", _value(observed_for_combo, "frequency", ""))), _text(observed_for_combo.get("group")), _text(observed_for_combo.get("band")))))
                if matched_option is None and any(_frequency_dial_matches(observed_for_combo, item) for item in normalized_options):
                    label = "Current · " + label
                entries.append((label, dict(observed_for_combo)))
            for raw in normalized_options:
                key = _frequency_key(raw)
                if not key or key in dedupe_keys:
                    continue
                label = _text(raw.get("label"))
                if not label or label[0].isdigit():
                    label = " · ".join(filter(None, (_frequency_text(raw.get("freq", raw.get("frequency"))), _text(raw.get("group", raw.get("group_name", ""))), _text(raw.get("band")))))
                if (variant_counts.get(_frequency_hz(raw), 0) > 1
                        or (matched_option is None and _frequency_dial_matches(observed_for_combo, raw))):
                    label = f"{label} · {_frequency_variant_text(raw)}"
                next_minutes = raw.get("next_minutes")
                is_next = bool(raw.get("is_next"))
                try:
                    is_next = is_next or (0 <= int(next_minutes) <= 15)
                except (TypeError, ValueError):
                    pass
                if is_next:
                    label = "Next · " + label
                entries.append((label, dict(raw)))
            return entries

        entries = option_entries()
        # A readback update changes the synthetic current choice frequently.
        # Update that row (and changed mode/offset metadata) in place while
        # retaining the popup, draft, and keyboard focus.  Rebuild only when
        # target order/count changes.
        same_structure = old_structure == plan_structure and self.frequency_combo.count() == len(entries)
        if same_structure and entries:
            if self._draft_key and self._draft_key == observed_key:
                self._draft_key = ""
            selected_key = self._draft_key or observed_key or _frequency_key(self.frequency_combo.currentData())
            self._updating = True
            try:
                self.frequency_combo.setProperty("radioCardObservedMeta", dict(observed_identity))
                self.frequency_combo.setProperty("radioCardOptionsSignature", option_signature)
                self.frequency_combo.setProperty("radioCardPlanStructureSignature", plan_structure)
                for index, (label, meta) in enumerate(entries):
                    self.frequency_combo.setItemText(index, label)
                    self.frequency_combo.setItemData(index, meta)
                target_idx = next(
                    (index for index in range(self.frequency_combo.count()) if _frequency_key(self.frequency_combo.itemData(index)) == selected_key),
                    -1,
                )
                if target_idx < 0 and observed_for_combo:
                    target_idx = 0
                if target_idx < 0 and not previous_draft and not observed_for_combo and len(normalized_options) == 1:
                    target_idx = 0
                self.frequency_combo.setCurrentIndex(target_idx)
            finally:
                self._updating = False
            self._update_actual_readback()
            self._update_compact_frequency()
            self._update_actions()
            return
        self._updating = True
        try:
            self.frequency_combo.clear()
            self.frequency_combo.setProperty("radioCardObservedMeta", dict(observed_identity))
            self.frequency_combo.setProperty("radioCardOptionsSignature", option_signature)
            self.frequency_combo.setProperty("radioCardPlanStructureSignature", plan_structure)
            for label, meta in entries:
                self.frequency_combo.addItem(label, meta)
            if not entries:
                self.frequency_combo.addItem("Frequency unavailable", None)
            target_idx = -1
            if previous_draft:
                for index in range(self.frequency_combo.count()):
                    if _frequency_key(self.frequency_combo.itemData(index)) == previous_draft:
                        target_idx = index
                        break
            if target_idx < 0 and observed_for_combo:
                target_idx = 0
            if target_idx < 0 and not previous_draft and not observed_for_combo and len(normalized_options) == 1:
                target_idx = 0
            self.frequency_combo.setCurrentIndex(target_idx)
        finally:
            self._updating = False
        self._update_actions()
        self._update_compact_frequency()
        if not self._selector_geometry_timer.isActive():
            self._selector_geometry_timer.start(0)

    def _set_durations(self, durations: Sequence[object] | None) -> None:
        values: list[int] = []
        for raw in durations or ():
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in values:
                values.append(value)
        duration_signature = tuple(values)
        if duration_signature == self.duration_combo.property("radioCardDurationSignature"):
            return
        previous = self.duration_combo.currentData()
        self._updating = True
        try:
            self.duration_combo.clear()
            self.duration_combo.addItem("Until resumed", 0)
            self.duration_combo.setProperty("radioCardDurationSignature", duration_signature)
            for value in values:
                if value < 60:
                    label = f"{value} minutes"
                elif value % 60:
                    label = f"{value // 60} hour {value % 60} minutes"
                else:
                    label = f"{value // 60} hour" + ("s" if value >= 120 else "")
                self.duration_combo.addItem(label, value)
            idx = self.duration_combo.findData(previous)
            self.duration_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._updating = False

        if not self._selector_geometry_timer.isActive():
            self._selector_geometry_timer.start(0)

    def _set_app_rows(
        self,
        rows: Sequence[tuple[str, str, str]],
        *,
        cat_verified: bool = False,
        cat_status: str = "Unknown · delayed",
    ) -> None:
        signature = (tuple(rows), bool(cat_verified), str(cat_status))
        if signature == getattr(self, "_app_rows_signature", None):
            return
        self._app_rows_signature = signature
        existing = {str(widget.property("radioCardAppName")): widget for widget in self.app_frame.findChildren(QWidget) if widget.property("radioCardAppName")}
        wanted = {str(row[0]): row for row in rows if row and str(row[0]).strip()}
        for name in ("FLDigi", "JS8", "VarAC", "Activity"):
            wanted.setdefault(name, (name, "", ""))
        for name, row in wanted.items():
            widget = existing.get(name)
            if widget is None:
                widget = QFrame(self.app_frame)
                widget.setObjectName("statusChip")
                widget.setProperty("radioCardAppName", name)
                row_layout = QVBoxLayout(widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(6)
                title = QLabel(name, widget)
                title.setObjectName("radioCardAppName")
                title.setMinimumWidth(title.fontMetrics().horizontalAdvance(name))
                info = QToolButton(widget) if name == "JS8" else QLabel(widget)
                info.setObjectName("radioCardAppInfo")
                if name == "JS8":
                    info.setCursor(Qt.PointingHandCursor)
                    info.clicked.connect(lambda: self.callbacks.js8_offset(self.radio_id) if self.callbacks.js8_offset else None)
                info.setMinimumWidth(0)
                info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                activity = QLabel(widget)
                activity.setObjectName("radioCardAppActivity")
                activity.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
                top_layout = QHBoxLayout()
                top_layout.setContentsMargins(0, 0, 0, 0)
                glyph = QLabel(widget)
                glyph.setObjectName("radioCardAppIcon")
                top_layout.addWidget(glyph)
                top_layout.addWidget(title)
                top_layout.addStretch(1)
                top_layout.addWidget(activity)
                row_layout.addLayout(top_layout)
                row_layout.addWidget(info)
                widget._fio_icon = glyph
                self.app_layout.addWidget(widget)
                widget._fio_title = title
                widget._fio_info = info  # type: ignore[attr-defined]
                widget._fio_activity = activity  # type: ignore[attr-defined]
            info_text = row[1]
            if name.lower() != "activity":
                prefix = f"{name} · "
                if info_text.startswith(prefix):
                    info_text = info_text[len(prefix):]
            activity_text = row[1] if name.lower() == "activity" else ""
            for suffix in ("RX", "TX", "Busy", "NET", "QSO", "QSO-BBS"):
                if info_text.endswith(" · " + suffix):
                    info_text = info_text[:-(len(suffix) + 3)]
                    activity_text = suffix
                    break
            widget._fio_activity.setVisible(bool(activity_text))
            widget._fio_info.setProperty("radioCardFullText", "" if name.lower() == "activity" else info_text)
            set_widget_value(widget._fio_activity, "text", activity_text)  # type: ignore[attr-defined]
            set_widget_value(widget._fio_activity, "styleSheet", "color:" + str(self._theme.get("danger" if activity_text == "TX" else "info" if activity_text == "RX" else "warning" if activity_text.startswith("QSO") else "text", self._theme["text"])) + ";")
            set_widget_value(widget, "toolTip", f"{row[1]}\n{row[2]}" + ("\nClick JS8 to edit and save this radio’s default offset." if name == "JS8" else ""))  # type: ignore[attr-defined]
            set_widget_value(widget, "accessibleName", f"{self._name_full_text}: {name} {info_text} {activity_text}".strip())
            set_widget_value(widget, "styleSheet", status_chip_style(dict(self._theme), widget.fontMetrics()))
            visible = bool(row[1])
            if widget.isHidden() == visible:
                widget.setVisible(visible)
        for name, widget in existing.items():
            if name not in wanted:
                widget.hide()
        self._app_evidence_available = any(bool(row[1]) for row in wanted.values())
        app_visible = self._app_evidence_available
        if self.app_frame.isHidden() == app_visible:
            self.app_frame.setVisible(app_visible)
        self._fit_app_slots()
        self._sync_control_icons()
        self._install_selection_filters()

    def update(
        self,
        snapshot: object,
        *,
        display: RadioCardDisplay | Mapping[str, object] | None = None,
        selected: bool = False,
        plan_options: Sequence[object] | None = None,
        hold_durations: Sequence[object] | None = None,
        held: bool = False,
        sop_text: str = "SOP actions",
        sop_minutes: float | None = None,
    ) -> None:
        """Publish cached state into existing widgets."""

        evidence = RadioCardDisplay.from_mapping(display)
        name_value = _text(_value(snapshot, "name", _value(snapshot, "label", f"Radio {self.radio_id}")), f"Radio {self.radio_id}")
        group_value = " ".join(filter(None, (evidence.current_group, evidence.current_band))) or _text(_value(snapshot, "group"), "Group unavailable")
        # The selector is intentionally a draft over every configured
        # destination visible to this radio.  ``plan_options`` remains the
        # compatibility name; newer projections may publish allfrequencies.
        normalized_options = plan_options
        if normalized_options is None:
            normalized_options = _value(snapshot, "allfrequencies", _value(snapshot, "all_frequencies", _value(snapshot, "plan_options", ())))
        if isinstance(normalized_options, Mapping):
            normalized_options = tuple(normalized_options.values())
        if isinstance(normalized_options, (str, bytes)) or not isinstance(normalized_options, Sequence):
            normalized_options = ()
        duration_values = tuple(hold_durations or _value(snapshot, "hold_durations", ()))
        semantic_signature = (
            name_value,
            group_value,
            evidence.observed_text,
            evidence.observed_hz,
            evidence.display_state,
            evidence.current_group,
            evidence.current_band,
            evidence.authority,
            evidence.authority_role,
            evidence.next_text,
            evidence.notice,
            evidence.qsy_progress,
            evidence.app_rows,
            evidence.frequency_controls_available,
            evidence.hold_remaining_minutes,
            evidence.next_tooltip,
            evidence.net_active,
            evidence.sop_tuning_enabled,
            evidence.routine_schedule_enabled,
            tuple(sorted((str(key), str(value)) for key, value in (evidence.timer_policy or {}).items())),
            evidence.cat_verified,
            evidence.cat_status,
            tuple(
                tuple(sorted((str(key), repr(value)) for key, value in item.items()))
                for item in normalized_options
                if isinstance(item, Mapping)
            ),
            duration_values,
            bool(held),
            sop_text,
            sop_minutes,
            bool(selected),
        )
        if semantic_signature == self._semantic_signature:
            return
        self._semantic_signature = semantic_signature
        focus_before = QApplication.focusWidget()
        restore_frequency_focus = focus_before is self.frequency_combo
        restore_duration_focus = focus_before is self.duration_combo
        self._updating = True
        try:
            name = name_value
            group = group_value
            self._name_full_text = name
            self._update_name_text()
            set_widget_value(self.group_label, "text", group)
            self._authority_full_text = evidence.authority
            self._update_authority_text()
            self._update_cat_status(verified=evidence.cat_verified, status=evidence.cat_status)
            self.observed_label.setProperty("radioCardObservedText", evidence.observed_text)
            self.observed_label.setToolTip("Last confirmed observed frequency. A destination draft does not change this value.")
            self.setProperty("radioCardDisplayState", evidence.display_state)
            self.setProperty("radioCardFrequencyControlsAvailable", bool(evidence.frequency_controls_available))
            self.setProperty("radioCardNetActive", bool(evidence.net_active))
            self.net_button.setProperty("radioCardNetActive", bool(evidence.net_active))
            self.setProperty("radioCardSopTuningEnabled", bool(evidence.sop_tuning_enabled))
            self.sop_button.setProperty("radioCardSopTuningEnabled", bool(evidence.sop_tuning_enabled))
            self.setProperty("radioCardRoutineScheduleEnabled", bool(evidence.routine_schedule_enabled))
            self.setProperty("radioCardTimerPolicy", dict(evidence.timer_policy or {}))
            self.setProperty("radioCardCatVerified", bool(evidence.cat_verified))
            self.setProperty("radioCardCatStatus", evidence.cat_status)
            self.net_button.setChecked(bool(evidence.net_active))
            self.sop_button.setChecked(bool(evidence.sop_tuning_enabled))
            policy_text = ", ".join(f"{key}: {value}" for key, value in (evidence.timer_policy or {}).items())
            set_widget_value(self.policy_button, "toolTip", "Review quick schedule policy for this radio." + (f"\n{policy_text}" if policy_text else ""))
            set_widget_value(self.sources_button, "toolTip", "Review schedule sources for this radio." + ("\nRoutine schedule enabled." if evidence.routine_schedule_enabled else "\nRoutine schedule disabled."))
            self.authority_label.setProperty("radioCardAuthorityRole", evidence.authority_role or "info")
            self.setProperty("selected", bool(selected))
            self.name_button.setChecked(bool(selected))
            combo_enabled = bool(evidence.frequency_controls_available) and evidence.display_state not in {"delayed", "unavailable", "invalidated"}
            set_widget_value(self.frequency_combo, "enabled", combo_enabled)
            blocked_tip = "Frequency control is unavailable for this radio." if not evidence.frequency_controls_available else ""
            set_widget_value(self.frequency_combo, "toolTip", blocked_tip)
            set_widget_value(self.qsy_button, "toolTip", blocked_tip)
            set_widget_value(self.hold_button, "toolTip", blocked_tip)
            raw_current = evidence.observed_meta or _value(snapshot, "observed_meta", None)
            current_meta = dict(raw_current) if isinstance(raw_current, Mapping) else None
            if current_meta is not None:
                current_meta.setdefault("group", evidence.current_group)
                current_meta.setdefault("band", evidence.current_band)
            self._set_combo_options(normalized_options, current_meta)
            self._set_durations(duration_values)
            self.hold_button.setProperty("radioCardHeld", bool(held))
            self.setProperty("radioCardHoldRemainingMinutes", evidence.hold_remaining_minutes)
            set_widget_value(self.hold_button, "accessibleName", f"{'Resume' if held else 'Hold'} schedule for {name}")
            self._sop_full_text = _text(sop_text, "SOP actions")
            # Typed, radio-scoped cached timing; never infer urgency from an action name.
            self._sop_urgency = "quiet"
            if not isinstance(sop_minutes, bool):
                try:
                    minutes = float(sop_minutes)
                    if math.isfinite(minutes):
                        self._sop_urgency = "due" if minutes <= 0 else "approaching" if minutes <= 15 else "quiet"
                except (TypeError, ValueError, OverflowError):
                    pass
            self.sop_button.setProperty("radioCardSopUrgency", self._sop_urgency)
            self._update_sop_button_text()
            set_widget_value(self.next_label, "text", evidence.next_text)
            set_widget_value(self.next_label, "toolTip", evidence.next_tooltip or evidence.next_text)
            # Command progress/failure is operator-critical and must remain
            # visible even when a delayed readback notice is also present.
            notice = evidence.qsy_progress or evidence.notice
            if not evidence.qsy_progress and evidence.cat_verified and notice.casefold() == "cat verified":
                notice = ""
            set_widget_value(self.notice_label, "text", notice)
            self.notice_label.setVisible(bool(notice))
            self._set_app_rows(
                evidence.app_rows,
                cat_verified=evidence.cat_verified,
                cat_status=evidence.cat_status,
            )
        finally:
            self._updating = False
        self._update_actions()
        self._update_actual_readback()
        self._update_compact_frequency()
        self._reflow_card_layouts()
        self._style()
        if restore_frequency_focus:
            self.frequency_combo.setFocus(Qt.OtherFocusReason)
        elif restore_duration_focus:
            self.duration_combo.setFocus(Qt.OtherFocusReason)

    def focus_target(self) -> None:
        self.frequency_combo.setFocus(Qt.OtherFocusReason)


class RadioControlPanel(QWidget):
    """Retained multi-radio card collection with full and Dock layouts."""

    navigation_requested = Signal(str)
    dock_requested = Signal()
    geometry_changed = Signal()
    full_requested = Signal()
    minimized_changed = Signal(bool)

    def __init__(
        self,
        *,
        callbacks: RadioCardCallbacks | None = None,
        theme: Mapping[str, object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.callbacks = callbacks or RadioCardCallbacks()
        self._theme = dict(theme or {})
        self._cards: dict[int, RadioControlCard] = {}
        self._selected_id = 0
        self._page = 0
        self._radio_menu_signature = None
        self._pager_layout_signature = None
        self._dock_mode = False
        self._drafts: dict[int, str] = {}
        self._layout_signature: tuple[object, ...] | None = None
        self._navigation_layout_signature: tuple[object, ...] | None = None
        self._advanced_layout_signature: tuple[object, ...] | None = None
        self._minimized = False
        self.setObjectName("radioControlPanel")
        self._build()

    @property
    def cards(self) -> Mapping[int, RadioControlCard]:
        return self._cards

    @property
    def selected_id(self) -> int:
        return self._selected_id

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)
        self.navigation_button = QPushButton("Go to", self)
        self.navigation_button.setObjectName("radioControlNavigation")
        self.dock_button = QPushButton("Dock view", self)
        self.dock_button.setObjectName("radioControlDock")
        self.dock_button.clicked.connect(self._toggle_dock)
        self.navigation_button.hide()
        self.dock_button.hide()
        self.global_context = QWidget(self)
        self.global_context.setObjectName("radioControlGlobalContext")
        self.global_context_layout = QHBoxLayout(self.global_context)
        self.global_context_layout.setContentsMargins(0, 0, 0, 0)
        self.global_context_layout.setSpacing(6)
        toolbar = QGridLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        self._toolbar_layout = toolbar
        self._toolbar_stacked = None
        self.utility_rail = ResponsiveAwarenessRail(self)
        self.utility_rail.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.controls_button = QPushButton("Controls ▴", self)
        self.controls_button.setObjectName("radioControlDisclosure")
        self.controls_button.setCheckable(True)
        self.controls_button.setChecked(True)
        self.controls_button.setToolTip("Collapse operational controls; keep radio and application activity visible.")
        self.controls_button.setAccessibleName("Collapse radio controls")
        self.controls_button.clicked.connect(lambda: self.set_minimized(not self._minimized))
        self.utility_rail.addWidget(self.controls_button)
        self.controls_button.show()
        self.view_button = QPushButton("Dock view", self)
        self.view_button.setAccessibleName("Switch to Dock view")
        self.view_button.clicked.connect(self._toggle_dock)
        self.view_menu = QMenu(self.view_button)
        self._dock_action = self.view_menu.addAction("Dock view", self._toggle_dock)
        self._expand_action = self.view_menu.addAction("Collapse controls", lambda: self.set_minimized(not self._minimized))
        self.navigation_menu = QMenu(self.navigation_button)
        for route in ("Ops Center", "Messages", "BBS", "Schedules", "Settings"):
            self.navigation_menu.addAction(route, lambda _checked=False, value=route: self._request_navigation(value))
        self.navigation_button.setMenu(self.navigation_menu)
        self.utility_rail.addWidget(self.view_button)
        self.view_button.show()
        self.utility_rail.addWidget(self.navigation_button)
        toolbar.addWidget(self.global_context, 0, 0)
        toolbar.addWidget(self.utility_rail, 0, 1)
        toolbar.setColumnStretch(0, 1)
        root.addLayout(toolbar)
        self.radio_pages = QWidget(self)
        self.radio_pages.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        pages_layout = QGridLayout(self.radio_pages)
        pages_layout.setContentsMargins(0, 0, 0, 0)
        pages_layout.setSpacing(6)
        self._pages_layout = pages_layout
        self.previous_radios = QPushButton("Previous", self.radio_pages)
        self.previous_radios.setAccessibleName("Show previous two radios")
        self.previous_radios.clicked.connect(lambda: self._set_page(self._page - 1))
        self.radio_picker = QPushButton("Radios", self.radio_pages)
        self.radio_menu = QMenu(self.radio_picker)
        self.radio_picker.setMenu(self.radio_menu)
        self.next_radios = QPushButton("Next", self.radio_pages)
        self.next_radios.setAccessibleName("Show next two radios")
        self.next_radios.clicked.connect(lambda: self._set_page(self._page + 1))
        pages_layout.addWidget(self.previous_radios, 0, 0)
        pages_layout.addWidget(self.radio_picker, 0, 1)
        pages_layout.addWidget(self.next_radios, 0, 2)
        self.radio_pages.hide()
        root.addWidget(self.radio_pages)
        self.navigation_row = QWidget(self)
        navigation_layout = QGridLayout(self.navigation_row)
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.setSpacing(5)
        self._navigation_layout = navigation_layout
        self._navigation_buttons: list[QPushButton] = []
        for route in ("Ops Center", "Messages", "BBS", "Schedules", "Settings"):
            button = QPushButton(route, self.navigation_row)
            button.setObjectName("radioControlNavigationRoute")
            button.clicked.connect(lambda _checked=False, value=route: self._request_navigation(value))
            self._navigation_buttons.append(button)
        self.navigation_row.setVisible(False)
        root.addWidget(self.navigation_row)
        self.cards_widget = QWidget(self)
        self.cards_widget.setMinimumWidth(0)
        self.cards_layout = QGridLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.cards_layout.setHorizontalSpacing(10)
        self.cards_layout.setVerticalSpacing(10)
        self.cards_scroll = QScrollArea(self)
        self.cards_scroll.setObjectName("radioControlCardsScroll")
        self.cards_scroll.setFrameShape(QFrame.NoFrame)
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.cards_scroll.setWidget(self.cards_widget)
        self.cards_scroll.viewport().installEventFilter(self)
        root.addWidget(self.cards_scroll, 1)
        self.selected_label = QLabel("No radio selected", self)
        self.selected_label.setObjectName("radioControlSelected")
        self.more_button = QPushButton("More controls", self)
        self.more_button.setObjectName("radioControlMore")
        self.more_button.clicked.connect(self._advanced)
        self.selected_label.hide()
        self.more_button.hide()
        self.advanced_row = QWidget(self)
        self.advanced_row.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        advanced_layout = QGridLayout(self.advanced_row)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(6)
        self._advanced_layout = advanced_layout
        self._advanced_buttons: dict[str, QPushButton] = {}
        for key, label in (("health", "Radio health"), ("plan", "Assigned plan"), ("launch", "Launch Control"), ("js8_offset", "JS8 offset")):
            button = QPushButton(label, self.advanced_row)
            button.setObjectName("radioControlAdvanced" + key.title())
            button.clicked.connect(lambda _checked=False, action=key: self._advanced_action(action))
            self._advanced_buttons[key] = button
        self.advanced_row.hide()
        self._reflow_navigation(force=True)
        self._reflow_advanced(force=True)

    @staticmethod
    def _grid_reflow(layout: QGridLayout, widgets: Sequence[QWidget], columns: int) -> None:
        while layout.count():
            layout.takeAt(0)
        for column in range(layout.columnCount()):
            layout.setColumnStretch(column, 0)
            layout.setColumnMinimumWidth(column, 0)
        for index, widget in enumerate(widgets):
            layout.addWidget(widget, index // columns, index % columns)
        for column in range(columns):
            layout.setColumnStretch(column, 1)

    def _reflow_navigation(self, *, force: bool = False) -> None:
        route_width = max((button.sizeHint().width() for button in self._navigation_buttons), default=90)
        columns = 3 if self.width() >= route_width * 3 + 10 else 2
        signature = (columns, tuple(self._navigation_buttons))
        if not force and signature == self._navigation_layout_signature:
            return
        self._navigation_layout_signature = signature
        self._grid_reflow(self._navigation_layout, self._navigation_buttons, columns)

    def _reflow_advanced(self, *, force: bool = False) -> None:
        # Four wide action buttons should wrap before their labels become
        # unreadable. At the narrowest Dock width, give each action a full row.
        widgets = list(self._advanced_buttons.values())
        button_width = max((button.sizeHint().width() for button in widgets), default=110)
        available = max(1, int(self.width()))
        if available < button_width * 2 + 12:
            columns = 1
        elif available >= button_width * 4 + 18:
            columns = 4
        else:
            columns = 2
        signature = (columns, tuple(widgets))
        if not force and signature == self._advanced_layout_signature:
            return
        self._advanced_layout_signature = signature
        self._grid_reflow(self._advanced_layout, widgets, columns)

    def _request_navigation(self, route: str) -> None:
        self.navigation_requested.emit(route)
        if self.callbacks.navigation:
            self.callbacks.navigation(route)

    def _toggle_navigation(self) -> None:
        self.navigation_row.setVisible(not self.navigation_row.isVisible())

    def _toggle_dock(self) -> None:
        self.set_dock_mode(not self._dock_mode)
        if self._dock_mode:
            self.dock_requested.emit()
            if self.callbacks.dock:
                self.callbacks.dock()
        else:
            self.full_requested.emit()

    def _advanced(self) -> None:
        if self._minimized:
            self.set_minimized(False)
            return
        self.advanced_row.hide()
        self.more_button.setText("Hide controls" if self.advanced_row.isVisible() else "More controls")
        for card in self._cards.values():
            set_widget_value(card._advanced_button, "text", "Hide controls" if self.advanced_row.isVisible() and card.radio_id == self._selected_id else "Controls…")
        self.geometry_changed.emit()

    def _card_controls(self, radio_id: int) -> None:
        same_scope = self._selected_id == radio_id
        was_open = self.advanced_row.isVisible()
        self._card_selected(radio_id)
        if self._minimized:
            self.set_minimized(False)
        if was_open and not same_scope:
            return
        self._advanced()

    def _advanced_action(self, action: str) -> None:
        ident = int(self._selected_id or 0)
        if not ident:
            return
        callback = getattr(self.callbacks, action, None)
        if callable(callback):
            callback(ident)

    @property
    def visible_radio_ids(self) -> tuple[int, ...]:
        ids = tuple(self._cards)
        return ids[self._page * 2:self._page * 2 + 2]

    def _set_page(self, page: int) -> None:
        page = max(0, min(int(page), max(0, (len(self._cards) - 1) // 2)))
        if page == self._page:
            return
        self._page = page
        # Keep advanced actions visibly scoped; paging itself never selects.
        if self._selected_id not in self.visible_radio_ids and self.advanced_row.isVisible():
            self._advanced()
        self._reflow()
        self.cards_scroll.verticalScrollBar().setValue(0)
        self.geometry_changed.emit()

    def _reveal_radio(self, radio_id: int) -> None:
        ids = list(self._cards)
        if radio_id in self._cards:
            self._set_page(ids.index(radio_id) // 2)

    def _reflow_radio_pages(self) -> None:
        buttons = (self.previous_radios, self.radio_picker, self.next_radios)
        available = max(1, self.width() - self.layout().contentsMargins().left() - self.layout().contentsMargins().right())
        stacked = available < sum(button.minimumSizeHint().width() for button in buttons) + 12
        if stacked == self._pager_layout_signature:
            return
        self._pager_layout_signature = stacked
        layout = self._pages_layout
        while layout.count():
            layout.takeAt(0)
        for column in range(layout.columnCount()):
            layout.setColumnStretch(column, 0)
            layout.setColumnMinimumWidth(column, 0)
        if stacked:
            layout.addWidget(self.radio_picker, 0, 0, 1, 2)
            layout.addWidget(self.previous_radios, 1, 0)
            layout.addWidget(self.next_radios, 1, 1)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 1)
        else:
            for index, button in enumerate(buttons):
                layout.addWidget(button, 0, index)
            layout.setColumnStretch(1, 1)

    def _sync_radio_pages(self) -> None:
        count = len(self._cards)
        self._page = min(self._page, max(0, (count - 1) // 2))
        self.radio_pages.setVisible(count > 2)
        set_widget_value(self.previous_radios, "enabled", self._page > 0)
        set_widget_value(self.next_radios, "enabled", (self._page + 1) * 2 < count)
        off_page_attention = sum(
            1 for ident, card in self._cards.items()
            if ident not in self.visible_radio_ids and (
                card.authority_label.property("radioCardAuthorityRole") in {"warning", "danger"}
                or bool(card.notice_label.text())
            )
        )
        text = f"Radios {self._page * 2 + 1}–{min(count, self._page * 2 + 2)}/{count}"
        if off_page_attention:
            text += f" · !{off_page_attention}"
        set_widget_value(self.radio_picker, "text", text)
        set_widget_value(self.radio_picker, "toolTip", f"Select a radio or receiver. {off_page_attention} off-page radios have notices.")
        self._reflow_radio_pages()
        signature = tuple((ident, card._name_full_text, card._authority_full_text,
                           card.notice_label.text()) for ident, card in self._cards.items())
        if signature == self._radio_menu_signature or self.radio_menu.isVisible():
            return
        self._radio_menu_signature = signature
        self.radio_menu.clear()
        for ident, name, authority, notice in signature:
            label = f"{name} · {authority}" if authority else name
            if notice:
                label += f" · {notice}"
            self.radio_menu.addAction(label, lambda _checked=False, value=ident: self._card_selected(value))

    def _card_selected(self, radio_id: int) -> None:
        if int(radio_id or 0) not in self._cards:
            # A menu opened before a profile removal must not select its stale
            # action or retarget a shared tray to a missing radio.
            return
        self._selected_id = int(radio_id or 0)
        self._reveal_radio(self._selected_id)
        self.advanced_row.hide()
        self.advanced_row.setToolTip(f"Controls for {self._cards[self._selected_id]._name_full_text}" if self._selected_id in self._cards else "")
        set_widget_value(
            self.selected_label,
            "text",
            self._cards[self._selected_id].name_button.text() if self._selected_id in self._cards else "No radio selected",
        )
        for ident, card in self._cards.items():
            card.setProperty("selected", ident == self._selected_id)
            card.name_button.setChecked(ident == self._selected_id)
            card._style()
            set_widget_value(card._advanced_button, "text", "Hide controls" if self.advanced_row.isVisible() and ident == self._selected_id else "Controls…")
        if self.callbacks.select:
            self.callbacks.select(self._selected_id)

    def _reflow(self) -> None:
        stacked = self.width() < max(600, self.fontMetrics().horizontalAdvance("M") * 60)
        utility_width = sum(button.sizeHint().width() for button in (self.controls_button, self.view_button)) + 12
        if not self.navigation_button.isHidden():
            utility_width += self.navigation_button.sizeHint().width() + 6
        self.utility_rail.setMinimumWidth(0 if stacked else utility_width)
        if stacked != self._toolbar_stacked:
            self._toolbar_stacked = stacked
            self._toolbar_layout.removeWidget(self.global_context)
            self._toolbar_layout.removeWidget(self.utility_rail)
            self._toolbar_layout.addWidget(self.global_context, 0, 0, 1, 2 if stacked else 1)
            self._toolbar_layout.addWidget(self.utility_rail, 1 if stacked else 0, 0 if stacked else 1, 1, 2 if stacked else 1)
        self._sync_radio_pages()
        ids = list(self.visible_radio_ids)
        viewport_width = int(self.cards_scroll.viewport().width() or self.width()) if hasattr(self, "cards_scroll") else self.width()
        em = max(1, self.fontMetrics().horizontalAdvance("M"))
        minimum_card_width = max(300, em * 32)
        columns = 1 if len(ids) < 2 or self._dock_mode or viewport_width < minimum_card_width * 2 + 10 else 2
        signature = (tuple(ids), columns, bool(self._dock_mode))
        if signature == self._layout_signature:
            return
        self._layout_signature = signature
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
        for column in range(self.cards_layout.columnCount()):
            self.cards_layout.setColumnStretch(column, 0)
            self.cards_layout.setColumnMinimumWidth(column, 0)
        for ident, card in self._cards.items():
            card.setVisible(ident in ids)
        for index, ident in enumerate(ids):
            # A responsive card changes its child rows after receiving width.
            # QWidgetItem queries that current height-for-width; addWidget's
            # cached item can retain the earlier stacked height at this width.
            card_item = QWidgetItem(self._cards[ident])
            card_item.setAlignment(Qt.AlignTop)
            self.cards_layout.addItem(card_item, index // columns, index % columns, 1, 1, Qt.AlignTop)
        self.cards_layout.setColumnStretch(0, 1)
        if columns > 1:
            self.cards_layout.setColumnStretch(1, 1)
        rows = (len(ids) + columns - 1) // columns
        for row in range(rows + 1):
            self.cards_layout.setRowStretch(row, 0)
        self.cards_layout.setRowStretch(rows, 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_navigation()
        self._reflow_advanced()
        self._reflow()

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() == QEvent.LayoutRequest and hasattr(self, "cards_scroll"):
            timer = getattr(self, "_geometry_publication_timer", None)
            if timer is None:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(self._publish_geometry)
                self._geometry_publication_timer = timer
            if not timer.isActive():
                timer.start(0)
        return result

    def _publish_geometry(self) -> None:
        # Publish only changed natural geometry after child wrapping settles.
        # Status wording and repeated LayoutRequests never resize the host.
        signature = (self._minimized, self._toolbar_layout.sizeHint().height(),
            tuple((ident, card.sizeHint().height(), card.minimumSizeHint().height(),
                card.heightForWidth(card.width()) if card.hasHeightForWidth() else 0)
                for ident, card in self._cards.items() if not card.isHidden()))
        if signature != getattr(self, "_geometry_publication_signature", None):
            self._geometry_publication_signature = signature
            self.geometry_changed.emit()

    def preferred_dock_width(self) -> int:
        """Fit one card column and station controls, without a spare grid column."""
        self.ensurePolished()
        margins = self.layout().contentsMargins()
        card_width = max((card.minimumSizeHint().width() for card in self._cards.values()), default=0)
        rail_width = self.global_context.minimumSizeHint().width()
        scrollbar = self.cards_scroll.verticalScrollBar().sizeHint().width()
        return max(320, max(card_width + scrollbar, rail_width) + margins.left() + margins.right())

    def eventFilter(self, watched, event) -> bool:
        if hasattr(self, "cards_scroll") and watched is self.cards_scroll.viewport() and event.type() == QEvent.Resize:
            self._reflow()
        return super().eventFilter(watched, event)

    def set_theme(self, theme: Mapping[str, object]) -> None:
        self._theme = dict(theme or {})
        for card in self._cards.values():
            card.set_theme(self._theme)

    def set_minimized(self, minimized: bool) -> None:
        """Show the compact identity/attention view while retaining cards."""

        minimized = bool(minimized)
        if minimized == self._minimized:
            return
        self._minimized = minimized
        self._expand_action.setText("Expand controls" if minimized else "Collapse controls")
        set_widget_value(self.controls_button, "text", "Controls ▾" if minimized else "Controls ▴")
        self.controls_button.setChecked(not minimized)
        self.controls_button.setAccessibleName("Expand radio controls" if minimized else "Collapse radio controls")
        self.controls_button.setToolTip("Show Hold duration, Hold/Resume and SOP actions for each radio." if minimized else
                                       "Collapse operational controls; keep radio and application activity visible.")
        self.setProperty("minimized", minimized)
        self.minimized_changed.emit(minimized)
        for card in self._cards.values():
            card.set_minimized(minimized)
        self.cards_scroll.verticalScrollBar().setValue(0)
        self._layout_signature = None
        self._reflow()
        self.cards_layout.activate()
        set_widget_value(self.more_button, "text", "Expand controls" if minimized else "More controls")
        self.advanced_row.hide()
        self._reflow()
        self.geometry_changed.emit()

    def update_cards(
        self,
        snapshots: Sequence[object],
        *,
        selected_id: int = 0,
        display_by_id: Mapping[int, object] | None = None,
        options_by_id: Mapping[int, Sequence[object]] | None = None,
        hold_durations: Sequence[object] | None = None,
        held_by_id: Mapping[int, bool] | None = None,
        sop_text_by_id: Mapping[int, str] | None = None,
        sop_minutes_by_id: Mapping[int, float | None] | None = None,
    ) -> None:
        """Update cards from an immutable bounded projection.

        Existing card instances are reused by id. Removed profiles are the
        only normal condition that deletes a card widget.
        """
        previous_selected_id = self._selected_id
        incoming: dict[int, object] = {}
        for snapshot in snapshots or ():
            ident = _radio_id(snapshot)
            if ident > 0:
                incoming[ident] = snapshot
        effective_selected_id = int(selected_id or 0)
        if effective_selected_id not in incoming:
            effective_selected_id = self._selected_id if self._selected_id in incoming else next(iter(incoming), 0)
        for ident in tuple(self._cards):
            if ident not in incoming:
                card = self._cards.pop(ident)
                card.setParent(None)
                card.deleteLater()
        for ident, snapshot in incoming.items():
            card = self._cards.get(ident)
            if card is None:
                card = RadioControlCard(ident, callbacks=self.callbacks, theme=self._theme, parent=self.cards_widget)
                card.selected.connect(self._card_selected)
                card.controls_requested.connect(self._card_controls)
                self._cards[ident] = card
            card.update(
                snapshot,
                display=(display_by_id or {}).get(ident),
                selected=ident == effective_selected_id,
                plan_options=(options_by_id or {}).get(ident),
                hold_durations=hold_durations,
                held=bool((held_by_id or {}).get(ident, False)),
                sop_text=(sop_text_by_id or {}).get(ident, "SOP actions"),
                sop_minutes=(sop_minutes_by_id or {}).get(ident),
            )
        self._selected_id = effective_selected_id
        if self._selected_id != previous_selected_id:
            self._reveal_radio(self._selected_id)
        selected_card = self._cards.get(self._selected_id)
        self.advanced_row.setToolTip(f"Controls for {selected_card._name_full_text}" if selected_card else "")
        if selected_card is None:
            self.advanced_row.hide()
        for ident, card in self._cards.items():
            set_widget_value(card._advanced_button, "text", "Hide controls" if self.advanced_row.isVisible() and ident == self._selected_id else "Controls…")
        set_widget_value(
            self.selected_label,
            "text",
            selected_card.name_button.text() if selected_card is not None else "No radio selected",
        )
        self._reflow()
        for card in self._cards.values():
            card.set_minimized(self._minimized)

    def focus_selected_target(self) -> None:
        card = self._cards.get(self._selected_id)
        if card is not None:
            card.focus_target()

    def set_dock_mode(self, enabled: bool) -> None:
        """Synchronize the visual toggle when the host closes the Dock."""

        self._dock_mode = bool(enabled)
        self.setProperty("dockMode", self._dock_mode)
        self.dock_button.setText("Full view" if self._dock_mode else "Dock view")
        self._dock_action.setText(self.dock_button.text())
        set_widget_value(self.view_button, "text", self.dock_button.text())
        self.view_button.setAccessibleName("Switch to " + self.dock_button.text())
        self.navigation_button.setVisible(self._dock_mode)
        # The card viewport is the sole vertical scroll owner in both the
        # full shell and the detached Dock. The host summary scroll is
        # disabled while this retained panel is active.
        self.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._reflow()


class RadioControlDockWindow(QMainWindow):
    """Ordinary resizable host for the same card panel."""

    closed = Signal()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        event.accept()
