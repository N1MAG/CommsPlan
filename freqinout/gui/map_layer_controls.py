"""Responsive, policy-free controls for map layer visibility."""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QEvent, QSize, Signal
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLayout, QSizePolicy, QStyle, QWidget


class MapLayerControls(QWidget):
    """A compact responsive grid of map layer checkboxes.

    This widget only reports user choices. It deliberately owns no map policy,
    persistence, or data access so the host view can decide how each layer is
    applied.
    """

    layer_toggled = Signal(str, bool)

    LAYERS = (
        ("stations", "Stations", "Show station locations and their current status."),
        ("paths", "Paths", "Show logged directional traffic from sender to addressee; this does not confirm delivery."),
        ("traffic", "Traffic", "Show traffic report locations."),
        ("regional", "Regional Intel", "Show regional intelligence summaries."),
        ("propagation", "RF Planning", "Open the RF planning view with stations, paths, and planning tools."),
        ("pins", "Pins", "Show saved reference pins."),
        ("mesh_nodes", "Mesh", "Show mesh network nodes."),
        ("peer", "Peer Schedule", "Show peers whose schedules align; this does not verify current availability."),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._grid = QGridLayout(self)
        # Let the grid reflow to its host instead of making its initial column
        # count a permanent minimum-width constraint.
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._checks: dict[str, QCheckBox] = {}
        self._columns = 0
        self._visible_keys = {key for key, _label, _description in self.LAYERS}

        for key, label, description in self.LAYERS:
            checkbox = QCheckBox(label, self)
            checkbox.setToolTip(description)
            checkbox.setAccessibleName(label)
            checkbox.setAccessibleDescription(description)
            checkbox.toggled.connect(
                lambda checked, layer_key=key: self.layer_toggled.emit(layer_key, checked)
            )
            self._checks[key] = checkbox

        self._reflow()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        # A provisional narrow grid must not force the whole window taller
        # before its first layout assigns the actual available width.
        margins = self._grid.contentsMargins()
        width = max(box.minimumSizeHint().width() for box in self._checks.values())
        height = max(box.minimumSizeHint().height() for box in self._checks.values())
        return QSize(width + margins.left() + margins.right(), height + margins.top() + margins.bottom())

    def set_visible_layers(self, keys: Iterable[str]) -> None:
        """Keep compatibility state; one visible owner for each control."""
        self._visible_keys = set(keys) & set(self._checks)
        for key, checkbox in self._checks.items():
            checkbox.setVisible(key in self._visible_keys)
        self._columns = 0
        self._reflow()

    def selection(self) -> set[str]:
        """Return the keys of currently selected layers."""
        return {key for key, checkbox in self._checks.items() if checkbox.isChecked()}

    def set_selection(self, keys: Iterable[str]) -> None:
        """Update checked layers without emitting user-action signals."""
        selected = {str(key) for key in keys}
        for key, checkbox in self._checks.items():
            previous = checkbox.blockSignals(True)
            checkbox.setChecked(key in selected)
            checkbox.blockSignals(previous)

    def checkbox(self, key: str) -> QCheckBox:
        """Return the checkbox for a layer key, primarily for host wiring."""
        return self._checks[key]

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange, QEvent.Type.StyleChange}:
            self._columns = 0
            self._reflow()

    def _reflow(self) -> None:
        margins = self._grid.contentsMargins()
        available = max(1, self.contentsRect().width() - margins.left() - margins.right())
        widest = max(
            (checkbox.sizeHint().width() for key, checkbox in self._checks.items() if key in self._visible_keys),
            default=1,
        )
        spacing = self._grid.horizontalSpacing()
        if spacing < 0:
            spacing = self.style().pixelMetric(QStyle.PixelMetric.PM_LayoutHorizontalSpacing)
        cell_width = max(1, widest)
        columns = max(1, (available + spacing) // (cell_width + spacing))
        columns = max(1, min(columns, len(self._visible_keys)))
        if columns == self._columns:
            return

        while self._grid.count():
            self._grid.takeAt(0)
        for index, checkbox in enumerate(box for key, box in self._checks.items() if key in self._visible_keys):
            self._grid.addWidget(checkbox, index // columns, index % columns)
        for col in range(columns):
            self._grid.setColumnStretch(col, 0)
        self._columns = columns


__all__ = ["MapLayerControls"]
