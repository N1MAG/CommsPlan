from __future__ import annotations

from pathlib import Path
from typing import Mapping

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from freqinout.core.traffic_actionability import TrafficActionSummary
from freqinout.gui.theme import button_style, resolve_theme


class TrafficActionSummaryWidget(QFrame):
    """Compact, reusable What/Why projection for Ops Center and Messages."""

    bucketActivated = Signal(str)

    _BUCKETS = (
        ("reply", "Reply", "Messages that appear to need a response."),
        ("relay", "Relay", "Impactful reports your duty role may need to distribute."),
        ("review", "Review", "Relevant event traffic without a detected reply or relay action."),
        ("social", "Social", "Direct non-event messages, shown after operational traffic."),
    )

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._summary = TrafficActionSummary()
        self._active_bucket = ""
        self.setObjectName("trafficActionSummary")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.title_label = QLabel("Actionable traffic")
        self.title_label.setStyleSheet("font-weight: 700;")
        row.addWidget(self.title_label)
        self.buttons: dict[str, QPushButton] = {}
        icon_root = Path(__file__).resolve().parents[2] / "assets" / "icons" / "navigation"
        icons = {
            "reply": "messages.svg",
            "relay": "net-control.svg",
            "review": "ops.svg",
            "social": "operators.svg",
        }
        for key, label, tip in self._BUCKETS:
            button = QPushButton(f"{label} 0")
            button.setIcon(QIcon(str(icon_root / icons[key])))
            button.setIconSize(QSize(16, 16))
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, bucket=key: self._activate(bucket))
            self.buttons[key] = button
            row.addWidget(button)
        row.addStretch(1)
        root.addLayout(row)

        self.insight_label = QLabel("No relevant action detected in the current traffic view.")
        self.insight_label.setWordWrap(True)
        self.insight_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        root.addWidget(self.insight_label)
        self.apply_theme()

    def navigation_snapshot(self, bucket: str) -> tuple[tuple[str, ...], int]:
        """Copy identities from the same immutable publication as button counts."""
        ids = tuple(dict.fromkeys(item.stable_id for item in self._summary.items if item.stable_id))
        return ids[:200], self._summary.count(bucket)

    def set_summary(self, summary: TrafficActionSummary) -> None:
        self._summary = summary
        labels = {key: label for key, label, _tip in self._BUCKETS}
        for key, button in self.buttons.items():
            count = summary.count(key)
            button.setText(f"{labels[key]} {count}")
            button.setEnabled(count > 0 or key == self._active_bucket)
        active_items = summary.items_for(self._active_bucket) if self._active_bucket else ()
        lead = active_items[0] if active_items else summary.lead
        if lead is None:
            self.insight_label.setText("No relevant action detected in the current traffic view.")
        else:
            what, why = lead.guidance_for(self._active_bucket)
            self.insight_label.setText(f"What: {what}   •   Why: {why}")
        self.apply_theme()

    def set_active_bucket(self, bucket: object) -> None:
        key = str(bucket or "").strip().lower()
        self._active_bucket = key if key in self.buttons else ""
        self.apply_theme()

    def apply_theme(self, theme: Mapping[str, str] | None = None) -> None:
        colors = dict(theme or resolve_theme(self.settings))
        self.setStyleSheet(
            "QFrame#trafficActionSummary {"
            f" background: {colors.get('surface_alt', colors.get('surface', '#eeeeee'))};"
            f" border: 1px solid {colors.get('border', '#cccccc')};"
            " border-radius: 6px;"
            "}"
        )
        self.insight_label.setStyleSheet(f"color: {colors.get('text_muted', colors.get('text', '#333333'))};")
        roles = {
            "reply": "eligible_danger",
            "relay": "eligible_warning",
            "review": "eligible_info",
            "social": "muted",
        }
        for key, button in self.buttons.items():
            active = key == self._active_bucket
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)
            button.setStyleSheet(button_style("primary" if active else roles[key], colors))

    def _activate(self, bucket: str) -> None:
        selected = "" if self._active_bucket == bucket else bucket
        self.set_active_bucket(selected)
        self.bucketActivated.emit(selected)
