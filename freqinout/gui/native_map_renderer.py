"""Provider-free native QtLocation item-overlay renderer for the Map pop-out.

This module deliberately owns only the rendering boundary.  It does not inspect
configuration, perform I/O, create a top-level window, or import Qt WebEngine.
The owning Map workspace provides already-computed projections and handles the
operator actions emitted here.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Mapping, Sequence

from PySide6.QtCore import QObject, Property, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtQml import QJSValue
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QLabel, QSizePolicy, QStackedLayout, QWidget

from freqinout.gui.map_marker_presentation import marker_tooltip_text
from freqinout.gui.theme import active_app_theme


_QML_SOURCE = Path(__file__).with_name("qml") / "native_map_renderer.qml"
_MAX_MARKERS = 1200
_MAX_PATHS = 600
_MAX_POLYGONS = 180
_MAX_BASE_POLYGONS = 512
_MAX_LABELS = 400
_MAX_FILLS = 300
_MAX_LEGEND_ITEMS = 12


def _surface_color(theme: Mapping[str, object]) -> QColor:
    """Use the shared theme's surface token for the native compositor clear."""
    fallback = active_app_theme()
    return QColor(str(theme.get("surface") or fallback.get("surface")))


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coordinate(value: object) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    lat = _number(value.get("lat", value.get("latitude")))
    lon = _number(value.get("lon", value.get("lng", value.get("longitude"))))
    if lat is None or lon is None or not (-85.05112878 <= lat <= 85.05112878 and -180.0 <= lon <= 180.0):
        return None
    return {"lat": lat, "lon": lon}


def _variant_mapping(value: object) -> dict[str, object] | None:
    """Normalize QVariantMap/QJSValue input arriving from a QML delegate."""
    candidate = value.toVariant() if isinstance(value, QJSValue) else value
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return None


class _NativeMapBridge(QObject):
    """Small QML-facing immutable-snapshot bridge.

    QML receives only normalized value collections.  This prevents a map paint,
    click, or resize from reaching the station database or other live services.
    """

    markers_changed = Signal()
    paths_changed = Signal()
    base_polygons_changed = Signal()
    polygons_changed = Signal()
    grid_lines_changed = Signal()
    grid_labels_changed = Signal()
    state_labels_changed = Signal()
    region_labels_changed = Signal()
    adaptive_grid_changed = Signal()
    city_labels_changed = Signal()
    fills_changed = Signal()
    directions_changed = Signal()
    legend_changed = Signal()
    summary_changed = Signal()
    direction_indicators_changed = Signal()
    theme_changed = Signal()
    view_changed = Signal()
    visible_changed = Signal()
    action_requested = Signal(object)
    view_state_changed = Signal(object)
    viewport_changed = Signal(object)

    def __init__(self, theme: Mapping[str, object]) -> None:
        super().__init__()
        self._markers: list[dict[str, object]] = []
        self._paths: list[dict[str, object]] = []
        self._base_polygons: list[dict[str, object]] = []
        self._polygons: list[dict[str, object]] = []
        self._grid_lines: list[dict[str, object]] = []
        self._grid_labels: list[dict[str, object]] = []
        self._state_labels: list[dict[str, object]] = []
        self._region_labels: list[dict[str, object]] = []
        self._adaptive_grid = False
        self._adaptive_grid_labels = False
        self._city_labels: list[dict[str, object]] = []
        self._fills: list[dict[str, object]] = []
        self._directions: list[dict[str, object]] = []
        self._legend: list[dict[str, object]] = []
        self._summary = ""
        self._direction_indicators = False
        self._theme: dict[str, object] = dict(theme)
        self._view: dict[str, float] = {"lat": 39.5, "lon": -98.35, "zoom": 3.6}
        self._home_view: dict[str, float] = dict(self._view)
        self._has_projection = False
        self._visible = True

    @Property("QVariantList", notify=markers_changed)
    def markers(self) -> list[dict[str, object]]:
        return list(self._markers)

    @Property("QVariantList", notify=markers_changed)
    def visualMarkers(self) -> list[dict[str, object]]:  # noqa: N802 - QML API
        """Transfer rendering fields only; retain full evidence for ID selection.

        QML delegates repeatedly access modelData through QVariant. Nested source
        payloads are inspector evidence and need not cross that hot boundary.
        """
        fields = ("id", "lat", "lon", "min_zoom", "kind", "is_own_station",
                  "is_mesh_self", "qsy_soon", "qsy_text", "color", "color_role",
                  "icon", "topic", "source_kind", "event_count", "label", "tooltip_text")
        return [{key: marker[key] for key in fields if key in marker}
                for marker in self._markers]

    @Property("QVariantList", notify=paths_changed)
    def paths(self) -> list[dict[str, object]]:
        return list(self._paths)

    @Property("QVariantList", notify=base_polygons_changed)
    def basePolygons(self) -> list[dict[str, object]]:  # noqa: N802 - QML property spelling
        return list(self._base_polygons)

    @Property("QVariantList", notify=polygons_changed)
    def polygons(self) -> list[dict[str, object]]:
        return list(self._polygons)

    @Property("QVariantList", notify=grid_lines_changed)
    def gridLines(self) -> list[dict[str, object]]:  # noqa: N802 - QML property spelling
        return list(self._grid_lines)

    @Property("QVariantList", notify=grid_labels_changed)
    def gridLabels(self) -> list[dict[str, object]]:  # noqa: N802 - QML property spelling
        return list(self._grid_labels)

    @Property("QVariantList", notify=state_labels_changed)
    def stateLabels(self) -> list[dict[str, object]]:  # noqa: N802 - QML property spelling
        return list(self._state_labels)

    @Property("QVariantList", notify=region_labels_changed)
    def regionLabels(self) -> list[dict[str, object]]:  # noqa: N802 - QML property spelling
        return list(self._region_labels)

    @Property(bool, notify=adaptive_grid_changed)
    def adaptiveGrid(self) -> bool:  # noqa: N802 - QML property spelling
        return self._adaptive_grid

    @Property(bool, notify=adaptive_grid_changed)
    def adaptiveGridLabels(self) -> bool:  # noqa: N802 - QML property spelling
        return self._adaptive_grid_labels

    @Property("QVariantList", notify=city_labels_changed)
    def cityLabels(self) -> list[dict[str, object]]:  # noqa: N802 - QML property spelling
        return list(self._city_labels)

    @Property("QVariantList", notify=fills_changed)
    def fills(self) -> list[dict[str, object]]:
        return list(self._fills)

    @Property("QVariantList", notify=directions_changed)
    def directions(self) -> list[dict[str, object]]:
        return list(self._directions)

    @Property("QVariantList", notify=legend_changed)
    def legend(self) -> list[dict[str, object]]:
        return list(self._legend)

    @Property(str, notify=summary_changed)
    def summary(self) -> str:
        return self._summary

    @Property(bool, notify=direction_indicators_changed)
    def directionIndicators(self) -> bool:  # noqa: N802 - QML property spelling
        return self._direction_indicators

    @Property("QVariantMap", notify=theme_changed)
    def theme(self) -> dict[str, object]:
        return dict(self._theme)

    @Property("QVariantMap", notify=view_changed)
    def view(self) -> dict[str, float]:
        return dict(self._view)

    @Property(bool, notify=visible_changed)
    def mapVisible(self) -> bool:  # noqa: N802 - QML property spelling
        return self._visible

    def set_projection(
        self,
        markers: Sequence[dict[str, object]],
        paths: Sequence[dict[str, object]],
        view: Mapping[str, object] | None,
    ) -> None:
        for name, values, signal in (
            ("_markers", markers, self.markers_changed),
            ("_paths", paths, self.paths_changed),
        ):
            replacement = [dict(item) for item in values]
            if replacement != getattr(self, name):
                setattr(self, name, replacement)
                signal.emit()
        if view is not None:
            self.set_view(view, report=False)
            if not self._has_projection:
                self._home_view = dict(self._view)
        self._has_projection = True

    def set_view(self, view: Mapping[str, object], *, report: bool) -> None:
        lat = _number(view.get("lat", view.get("latitude")))
        lon = _number(view.get("lon", view.get("lng", view.get("longitude"))))
        zoom = _number(view.get("zoom"))
        if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return
        next_view = {"lat": max(-85.05112878,min(85.05112878,lat)), "lon": lon, "zoom": max(0.0, min(18.0, zoom if zoom is not None else self._view["zoom"]))}
        if all(abs(float(next_view[key]) - float(self._view[key])) < 0.00001 for key in ("lat", "lon", "zoom")):
            return
        self._view = next_view
        self.view_changed.emit()
        if report:
            self.view_state_changed.emit(dict(next_view))

    def set_theme(self, theme: Mapping[str, object]) -> None:
        self._theme = dict(theme)
        self.theme_changed.emit()

    def set_layers(
        self,
        *,
        base_polygons: Sequence[dict[str, object]],
        polygons: Sequence[dict[str, object]],
        grid_lines: Sequence[dict[str, object]],
        grid_labels: Sequence[dict[str, object]],
        state_labels: Sequence[dict[str, object]],
        region_labels: Sequence[dict[str, object]],
        adaptive_grid: bool,
        adaptive_grid_labels: bool,
        city_labels: Sequence[dict[str, object]],
        fills: Sequence[dict[str, object]],
        directions: Sequence[dict[str, object]],
        legend: Sequence[dict[str, object]],
        summary: str,
        direction_indicators: bool,
    ) -> None:
        # Notify only changed layers: replacing an unchanged QVariantList
        # tears down and rebuilds every corresponding MapItemView delegate.
        replacements = {
            "base_polygons": [dict(item) for item in base_polygons],
            "polygons": [dict(item) for item in polygons],
            "grid_lines": [dict(item) for item in grid_lines],
            "grid_labels": [dict(item) for item in grid_labels],
            "state_labels": [dict(item) for item in state_labels],
            "region_labels": [dict(item) for item in region_labels],
            "adaptive_grid": bool(adaptive_grid),
            "city_labels": [dict(item) for item in city_labels],
            "fills": [dict(item) for item in fills],
            "directions": [dict(item) for item in directions],
            "legend": [dict(item) for item in legend],
            "summary": str(summary or ""),
            "direction_indicators": bool(direction_indicators),
        }
        grid_labels_changed = self._adaptive_grid_labels != bool(adaptive_grid_labels)
        self._adaptive_grid_labels = bool(adaptive_grid_labels)
        for name, replacement in replacements.items():
            if replacement != getattr(self, "_" + name) or (name == "adaptive_grid" and grid_labels_changed):
                setattr(self, "_" + name, replacement)
                getattr(self, name + "_changed").emit()

    def set_map_visible(self, visible: bool) -> None:
        value = bool(visible)
        if value == self._visible:
            return
        self._visible = value
        self.visible_changed.emit()

    @Slot()
    def zoomIn(self) -> None:  # noqa: N802 - QML API
        self.set_view({**self._view, "zoom": self._view["zoom"] + 1.0}, report=True)

    @Slot()
    def zoomOut(self) -> None:  # noqa: N802 - QML API
        self.set_view({**self._view, "zoom": self._view["zoom"] - 1.0}, report=True)

    @Slot()
    def resetView(self) -> None:  # noqa: N802 - QML API
        self.set_view(self._home_view, report=True)

    @Slot(float,float)
    def fitWorld(self, width, height):  # noqa: N802
        zoom = max(0.0,min(18.0,math.log2(max(1,min(width-32,height-32))/256)))
        self.set_view({"lat":0,"lon":0,"zoom":zoom},report=True)

    @Slot("QVariant", name="selectMarker")
    def select_marker(self, marker: object) -> None:
        normalized = _variant_mapping(marker)
        if normalized is not None:
            self.action_requested.emit(self._action_payload("select_marker", "marker", normalized))

    @Slot(str)
    def selectMarkerById(self, item_id: str) -> None:  # noqa: N802 - QML API
        """Select a marker without converting a live QML delegate object.

        Passing ``modelData`` back through QJSValue can recursively convert
        nested QVariant maps on some PySide builds.  Stable IDs keep the click
        path small and make dense marker selection reliable.
        """
        self._select_by_id(self._markers, item_id, "select_marker", "marker")

    @Slot("QVariant", name="selectPath")
    def select_path(self, path: object) -> None:
        normalized = _variant_mapping(path)
        if normalized is not None:
            self.action_requested.emit(self._action_payload("select_path", "path", normalized))

    @Slot(str)
    def selectPathById(self, item_id: str) -> None:  # noqa: N802 - QML API
        self._select_by_id(self._paths, item_id, "select_path", "path")

    @Slot("QVariant", name="selectPolygon")
    def select_polygon(self, polygon: object) -> None:
        normalized = _variant_mapping(polygon)
        if normalized is not None:
            self.action_requested.emit(self._action_payload("select_polygon", "polygon", normalized))

    @Slot(str)
    def selectPolygonById(self, item_id: str) -> None:  # noqa: N802 - QML API
        self._select_by_id(self._polygons, item_id, "select_polygon", "polygon")

    @Slot()
    def invalidate_viewport(self):
        if getattr(self,"_viewport",None):
            self._viewport = {}
            self.viewport_changed.emit({})

    @Slot("QVariant")
    def report_viewport(self, value):
        value = _variant_mapping(value)
        if not value:
            return
        try:
            clean={k:float(value[k]) for k in ('west','south','east','north')}
            if (not all(math.isfinite(number) for number in clean.values())
                    or not -180 <= clean['west'] <= 180 or not -180 <= clean['east'] <= 180
                    or not -85.052 <= clean['south'] < clean['north'] <= 85.052
                    or not (int(value['width']) > 0 and int(value['height']) > 0)):
                return
            clean.update(width=int(value['width']),height=int(value['height']),full_world=bool(value.get('full_world')))
        except (KeyError,TypeError,ValueError,OverflowError):
            return
        old={k:v for k,v in getattr(self,'_viewport',{}).items() if k!='revision'}
        if clean != old:
            self._viewport_revision = getattr(self,'_viewport_revision',0)+1
            self._viewport = dict(clean,revision=self._viewport_revision)
            self.viewport_changed.emit(dict(self._viewport))

    @Slot(float, float, float)
    def report_view(self, lat: float, lon: float, zoom: float) -> None:
        self.set_view({"lat": lat, "lon": lon, "zoom": zoom}, report=True)

    def _select_by_id(
        self,
        collection: Sequence[Mapping[str, object]],
        item_id: str,
        action: str,
        item_type: str,
    ) -> None:
        wanted = str(item_id or "")
        for item in collection:
            if str(item.get("id") or "") == wanted:
                payload = self._action_payload(action, item_type, item)
                # Leave the QML pointer callback before transporting the
                # nested Python payload.  Some PySide builds recursively
                # convert QVariant maps when an object signal is emitted from
                # inside the delegate's JS stack.
                QTimer.singleShot(0, lambda value=payload: self.action_requested.emit(value))
                return

    @staticmethod
    def _action_payload(action: str, item_type: str, item: Mapping[str, object]) -> dict[str, object]:
        rendered = dict(item)
        original = rendered.pop("_original_payload", None)
        explicit_payload = rendered.get("payload")
        original_payload = original.get("payload") if isinstance(original, Mapping) else None
        if isinstance(explicit_payload, Mapping):
            selected_payload = dict(explicit_payload)
        elif isinstance(original_payload, Mapping):
            selected_payload = dict(original_payload)
        elif isinstance(original, Mapping):
            selected_payload = dict(original)
        else:
            selected_payload = dict(rendered)
        return {
            "action": action,
            "id": str(rendered.get("id") or ""),
            "item_type": item_type,
            "payload": selected_payload,
            item_type: rendered,
        }


class NativeMapRenderer(QWidget):
    """A QWidget-compatible native map rendering surface.

    The owner may construct this surface inside a persistent pop-out before it
    has a projection.  Applying a projection only replaces value snapshots in
    QML; it never reparents, resizes, or reconstructs its containing window.
    """

    state_changed = Signal(str, str)
    ready = Signal()
    unavailable = Signal(str)
    action_requested = Signal(object)
    view_state_changed = Signal(object)
    viewport_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None, *, theme: Mapping[str, object] | None = None) -> None:
        super().__init__(parent)
        self._closed = False
        self._ready_emitted = False
        self._projection_applied = False
        self.renderer_state = "loading"
        self.failure_reason = "Preparing native map…"
        self._bridge = _NativeMapBridge(theme or active_app_theme())
        # Relay through ordinary callables.  Direct Qt signal-to-signal wiring
        # can recurse when a QML pointer handler invokes the bridge on PySide.
        self._bridge.action_requested.connect(self._relay_action_requested)
        self._bridge.view_state_changed.connect(self._relay_view_state_changed)
        self._bridge.viewport_changed.connect(self.viewport_changed)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMinimumSize(0, 0)

        self._surface_stack = QStackedLayout(self)
        self._surface_stack.setContentsMargins(0, 0, 0, 0)
        self._surface_stack.setStackingMode(QStackedLayout.StackAll)
        self._status = QLabel(self.failure_reason, self)
        self._status.setObjectName("nativeMapRendererStatus")
        self._status.setWordWrap(True)
        self._status.setAutoFillBackground(True)
        self._status.setAlignment(Qt.AlignCenter)
        self._surface_stack.addWidget(self._status)
        self.quick_widget: QQuickWidget | None = None
        try:
            quick = QQuickWidget(self)
            quick.setResizeMode(QQuickWidget.SizeRootObjectToView)
            quick.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            quick.setMinimumSize(0, 0)
            quick.setClearColor(_surface_color(self._bridge.theme))
            # Keep the context object alive for the complete QML scene
            # lifetime. QQuickWidget tears its scene down before deleting its
            # QObject children, avoiding null-context binding churn at exit.
            self._bridge.setParent(quick)
            quick.rootContext().setContextProperty("mapBridge", self._bridge)
            quick.statusChanged.connect(self._on_status_changed)
            self._surface_stack.addWidget(quick)
            self.quick_widget = quick
            # Both children remain visible. The status label covers the scene
            # until a coherent projection arrives, but the graphics child is
            # already present before the Map top-level's first show.
            self._surface_stack.setCurrentWidget(self._status)
            quick.setSource(QUrl.fromLocalFile(str(_QML_SOURCE)))
        except Exception as exc:
            self._set_unavailable(f"Native map unavailable. Review Qt Location support and retry the Map. ({exc})")

    @property
    def bridge(self) -> QObject:
        """Expose the bridge for focused integration tests, not application I/O."""
        return self._bridge

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, 0)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, 0)

    def is_available(self) -> bool:
        return bool(self.quick_widget is not None and not self._closed and self.renderer_state != "unavailable")

    def status_text(self) -> str:
        return str(self.failure_reason or "Native map ready.")

    @Slot(object)
    def _relay_action_requested(self, payload: object) -> None:
        self.action_requested.emit(payload)

    @Slot(object)
    def _relay_view_state_changed(self, payload: object) -> None:
        self.view_state_changed.emit(payload)

    def apply_theme(self, theme: Mapping[str, object] | None = None) -> None:
        if self._closed:
            return
        colors = dict(theme or active_app_theme())
        self._bridge.set_theme(colors)
        if self.quick_widget is not None:
            self.quick_widget.setClearColor(_surface_color(colors))

    def set_map_visible(self, visible: bool) -> None:
        """Pause visual work while the owner is hidden without rebuilding QML."""
        if self._closed:
            return
        self._bridge.set_map_visible(visible)
        if self.quick_widget is not None:
            self.quick_widget.setUpdatesEnabled(bool(visible))

    def apply_projection(self, projection: Mapping[str, object] | None) -> None:
        """Apply one normalized station/path snapshot without side effects."""
        if self._closed:
            return
        payload = projection if isinstance(projection, Mapping) else {}
        markers = self._normalise_markers(payload.get("markers"))
        markers.extend(self._normalise_event_markers(payload.get("weather_events"), "weather"))
        markers.extend(self._normalise_event_markers(payload.get("alert_events"), "alert"))
        markers.extend(self._normalise_event_markers(payload.get("infrastructure_events"), "infrastructure"))
        self._operational_markers = markers[:_MAX_MARKERS]
        if "license_references" in payload:
            self._license_reference_markers = self._normalise_event_markers(payload.get("license_references"), "license_reference")[:200]
        markers = self._operational_markers + list(getattr(self,"_license_reference_markers",()))[:max(0,_MAX_MARKERS-len(self._operational_markers))]
        paths = self._normalise_paths(payload.get("paths", payload.get("links")), self._operational_markers)
        view = payload.get("view", payload.get("center"))
        if not isinstance(view, Mapping) and bool(payload.get("auto_fit")):
            view = self._fit_view(self._operational_markers, paths, self.width(), self.height())
        self._bridge.set_projection(markers, paths, view if isinstance(view, Mapping) else None)
        direction_indicators = bool(
            payload.get("link_direction_markers", payload.get("direction_indicators", False))
            or any(bool(path.get("link_direction_markers")) for path in paths)
        )
        regional = payload.get("regional_summary", payload.get("regional_intelligence"))
        fills = self._normalise_fills(payload.get("regional_fills"), "regional")
        fills.extend(self._normalise_fills(payload.get("propagation_fills"), "propagation"))
        fills.extend(self._regional_fills(regional))
        city_source = payload.get("city_labels", payload.get("cities"))
        show_cities = bool(payload.get("show_cities", city_source is not None))
        show_city_labels = bool(payload.get("show_city_labels", show_cities))
        self._operational_legend = self._normalise_legend(payload.get("legend"), self._operational_markers, fills)[:_MAX_LEGEND_ITEMS]
        self._bridge.set_layers(
            base_polygons=self._normalise_polygons(
                payload.get("base_polygons"),
                limit=_MAX_BASE_POLYGONS,
            ),
            polygons=self._normalise_polygons(payload.get("polygons", payload.get("boundaries"))),
            grid_lines=self._normalise_paths(payload.get("grid_lines"), ())[:_MAX_PATHS],
            grid_labels=self._normalise_labels(payload.get("grid_labels"), "grid")[:_MAX_LABELS],
            state_labels=self._normalise_labels(payload.get("state_labels"), "state")[:64],
            region_labels=self._normalise_labels(payload.get("region_labels"), "fema_region")[:10],
            adaptive_grid=("show_grids" in payload and bool(payload.get("show_grids"))),
            adaptive_grid_labels=bool(payload.get("show_grid_labels", True)),
            city_labels=(self._normalise_city_labels(city_source) if show_cities and show_city_labels else []),
            fills=fills[:_MAX_FILLS],
            directions=(self._direction_indicators(paths) if direction_indicators else []),
            legend=self._combined_reference_legend(bool(len(markers) > len(self._operational_markers))),
            summary=self._projection_summary(payload, regional),
            direction_indicators=direction_indicators,
        )
        # Keep the loading surface visible until all collections for the first
        # snapshot are installed. QML paints after this synchronous call, so
        # the first exposed native frame is coherent rather than empty.
        self._projection_applied = True
        self._reveal_projected_surface()

    def _combined_reference_legend(self, visible):
        items = list(getattr(self,"_operational_legend",()))
        if visible:
            return items[:_MAX_LEGEND_ITEMS-1] + [{"label":"License references (estimated)","color_role":"text_muted","symbol_only":True,
                "tooltip":"Postal estimate, not a station status or operating location. May differ from self-reported location."}]
        return items

    def apply_license_references(self, values):
        """Publish bounded reference values without rebuilding operational layers."""
        if self._closed:
            return
        self._license_reference_markers = self._normalise_event_markers(values,"license_reference")[:200]
        operational = list(getattr(self,"_operational_markers",()))[:_MAX_MARKERS]
        references = self._license_reference_markers[:max(0,_MAX_MARKERS-len(operational))]
        replacement = operational + references
        if replacement != self._bridge._markers:
            self._bridge._markers = replacement
            self._bridge.markers_changed.emit()
        legend = self._combined_reference_legend(bool(references))
        if legend != self._bridge._legend:
            self._bridge._legend = legend
            self._bridge.legend_changed.emit()
        self._projection_applied = True
        self._reveal_projected_surface()

    def apply_geography(self, payload):
        """Replace decorative geography only; preserve live reports and selection."""
        if self._closed:
            return
        for name, values, signal in (
            ("_base_polygons",self._normalise_polygons(payload.get("base_polygons"),limit=_MAX_BASE_POLYGONS),self._bridge.base_polygons_changed),
            ("_city_labels",self._normalise_city_labels(payload.get("city_labels")),self._bridge.city_labels_changed),
        ):
            if getattr(self._bridge,name) != values:
                setattr(self._bridge,name,values)
                signal.emit()
        self._reveal_projected_surface()

    def set_view_center(self, latitude: object, longitude: object, zoom: object | None = None) -> None:
        """Center the native map from cached operator context without I/O."""
        if self._closed:
            return
        view: dict[str, object] = {"lat": latitude, "lon": longitude}
        if zoom is not None:
            view["zoom"] = zoom
        self._bridge.set_view(view, report=False)

    def set_home_view(self, view) -> None:
        from freqinout.gui.map_camera_preferences import validated_view
        value = validated_view(view)
        if value and not self._closed:
            self._bridge._home_view = value

    def viewport_state(self):
        return dict(getattr(self._bridge,'_viewport',{}))

    def view_state(self) -> dict[str, float]:
        return dict(self._bridge.view)

    def zoom_in(self) -> None:
        """Zoom one level using only the current in-memory view state."""
        if not self._closed:
            self._bridge.zoomIn()

    def zoom_out(self) -> None:
        """Zoom out one level using only the current in-memory view state."""
        if not self._closed:
            self._bridge.zoomOut()

    def pan_by(self, latitude_delta: object, longitude_delta: object) -> None:
        """Pan by geographic deltas without requesting provider data."""
        if self._closed:
            return
        lat_delta = _number(latitude_delta)
        lon_delta = _number(longitude_delta)
        if lat_delta is None or lon_delta is None:
            return
        view = self._bridge.view
        self._bridge.set_view(
            {
                "lat": max(-85.0, min(85.0, view["lat"] + lat_delta)),
                "lon": max(-180.0, min(180.0, view["lon"] + lon_delta)),
                "zoom": view["zoom"],
            },
            report=True,
        )

    def center_on(
        self,
        latitude_or_item: object,
        longitude: object | None = None,
        zoom: object | None = None,
    ) -> None:
        """Center on coordinates or an item without changing the window."""
        item = (
            latitude_or_item
            if isinstance(latitude_or_item, Mapping)
            else {"lat": latitude_or_item, "lon": longitude}
        )
        coordinate = _coordinate(item)
        if coordinate is None:
            return
        self.set_view_center(coordinate["lat"], coordinate["lon"], zoom)

    def shutdown(self) -> None:
        """Release QML content once; safe during normal application teardown."""
        if self._closed:
            return
        self._closed = True
        if self.quick_widget is not None:
            self.quick_widget.setUpdatesEnabled(False)
            try:
                self.quick_widget.statusChanged.disconnect(self._on_status_changed)
            except (RuntimeError, TypeError):
                pass
            # Keep the context bridge valid until QWidget ownership destroys
            # the QML tree. Clearing the source first briefly re-evaluates
            # bindings with a null context property and floods shutdown logs.
            # The renderer is final-parented and deleted exactly once by its
            # owning Map window, so explicit source replacement is unnecessary.
        self.renderer_state = "unavailable"
        self.failure_reason = "Native map renderer is closed."

    def _on_status_changed(self, status: QQuickWidget.Status) -> None:
        if self._closed:
            return
        if status == QQuickWidget.Ready:
            self.renderer_state = "ready"
            self.failure_reason = "Native map ready."
            self.state_changed.emit("ready", "")
            if not self._ready_emitted:
                self._ready_emitted = True
                self.ready.emit()
            self._reveal_projected_surface()
            return
        if status == QQuickWidget.Error:
            quick = self.quick_widget
            messages = [str(error.toString() or "").strip() for error in quick.errors()] if quick is not None else []
            detail = "; ".join(message for message in messages if message)
            self._set_unavailable(f"Native map unavailable. Review Qt Location support and retry the Map.{(' ' + detail) if detail else ''}")
            return
        self.renderer_state = "loading"
        self.failure_reason = "Preparing native map…"
        self._status.setText(self.failure_reason)
        self._status.show()
        self._surface_stack.setCurrentWidget(self._status)
        self.state_changed.emit("loading", "")

    def _reveal_projected_surface(self) -> None:
        """Expose the retained QML widget only after its first full snapshot."""
        quick = self.quick_widget
        if (
            self._closed
            or not self._projection_applied
            or quick is None
            or quick.status() != QQuickWidget.Ready
        ):
            return
        if self._surface_stack.currentWidget() is not quick:
            self._surface_stack.setCurrentWidget(quick)
        self._status.hide()

    def _set_unavailable(self, message: str) -> None:
        self.renderer_state = "unavailable"
        self.failure_reason = str(message or "Native map unavailable. Review Qt Location support and retry the Map.")
        self._status.setText(self.failure_reason)
        self._status.show()
        self._surface_stack.setCurrentWidget(self._status)
        self.state_changed.emit("unavailable", self.failure_reason)
        self.unavailable.emit(self.failure_reason)

    @staticmethod
    def _normalise_markers(value: object) -> list[dict[str, object]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return []
        output: list[dict[str, object]] = []
        seen_ids: dict[str, int] = {}
        for index, item in enumerate(value):
            coordinate = _coordinate(item)
            if coordinate is None or not isinstance(item, Mapping):
                continue
            marker = dict(item)
            marker.update(coordinate)
            marker["id"] = NativeMapRenderer._stable_id("station", marker, index, seen_ids)
            marker["kind"] = "station"
            marker["color_role"] = NativeMapRenderer._semantic_color_role(marker, "station")
            marker["_original_payload"] = dict(item)
            marker.setdefault("label", str(marker.get("callsign") or marker.get("name") or ""))
            marker["tooltip_text"] = marker_tooltip_text(marker)
            output.append(marker)
        return output

    @staticmethod
    def _normalise_event_markers(value: object, kind: str) -> list[dict[str, object]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return []
        output: list[dict[str, object]] = []
        seen_ids: dict[str, int] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            coordinate = _coordinate(item)
            if coordinate is None:
                continue
            marker = dict(item)
            marker.update(coordinate)
            marker["id"] = NativeMapRenderer._stable_id(kind, marker, index, seen_ids)
            marker["kind"] = kind
            marker["color_role"] = NativeMapRenderer._semantic_color_role(marker, kind)
            marker["_original_payload"] = dict(item)
            marker.setdefault("label", str(marker.get("title") or marker.get("callsign") or ""))
            marker.setdefault("event_count", int(marker.get("count") or 0))
            marker["tooltip_text"] = marker_tooltip_text(marker)
            output.append(marker)
        return output

    @staticmethod
    def _normalise_paths(value: object, markers: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return []
        lookup: dict[str, dict[str, float]] = {}
        for marker in markers:
            key = str(marker.get("callsign") or marker.get("id") or "").strip().upper()
            coordinate = _coordinate(marker)
            if key and coordinate:
                lookup[key] = coordinate
        output: list[dict[str, object]] = []
        seen_ids: dict[str, int] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            candidates = item.get("points", item.get("coordinates", item.get("path", [])))
            points = [_coordinate(point) for point in candidates] if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes, bytearray)) else []
            normalized = [point for point in points if point is not None]
            if len(normalized) < 2:
                first = _coordinate({"lat": item.get("lat1"), "lon": item.get("lon1")})
                second = _coordinate({"lat": item.get("lat2"), "lon": item.get("lon2")})
                normalized = [point for point in (first, second) if point is not None]
            if len(normalized) < 2:
                origin = lookup.get(str(item.get("origin") or item.get("from") or "").strip().upper())
                destination = lookup.get(str(item.get("destination") or item.get("to") or "").strip().upper())
                normalized = [point for point in (origin, destination) if point is not None]
            if len(normalized) < 2:
                continue
            path = dict(item)
            path["points"] = normalized
            path["id"] = NativeMapRenderer._stable_id("path", path, index, seen_ids)
            path["color_role"] = NativeMapRenderer._semantic_color_role(path, "path")
            # Existing caller-supplied colours remain authoritative.  Normal
            # RF links without one use the original Leaflet's five SNR bins.
            if not str(path.get("color") or "").strip() and path.get("evidence_kind") not in {"cached_route", "observed_packet_path"}:
                path["color"] = NativeMapRenderer._link_snr_color(path.get("snr"))
            path["_original_payload"] = dict(item)
            from freqinout.gui.world_geography import split_path
            for part_index, part in enumerate(split_path(normalized)):
                output.append({**path,"points":part,"id":path["id"] if part_index==0 else f"{path['id']}:wrap:{part_index}"})
        return output[:_MAX_PATHS]

    @staticmethod
    def _normalise_polygons(
        value: object,
        *,
        limit: int = _MAX_POLYGONS,
    ) -> list[dict[str, object]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return []
        output: list[dict[str, object]] = []
        seen_ids: dict[str, int] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            raw_points = item.get("points", item.get("coordinates", item.get("path", [])))
            if not isinstance(raw_points, Sequence) or isinstance(raw_points, (str, bytes, bytearray)):
                continue
            points = [point for point in (_coordinate(raw) for raw in raw_points) if point]
            if len(points) < 3:
                continue
            polygon = dict(item)
            polygon["points"] = points
            polygon["id"] = NativeMapRenderer._stable_id("polygon", polygon, index, seen_ids)
            polygon["color_role"] = NativeMapRenderer._semantic_color_role(polygon, "boundary")
            polygon["_original_payload"] = dict(item)
            from freqinout.gui.world_geography import split_ring
            if any(abs(a["lon"]-b["lon"])>180 for a,b in zip(points,points[1:])):
                for part_index, part in enumerate(split_ring(points)):
                    output.append({**polygon,"points":part,"id":polygon["id"] if part_index==0 else f"{polygon['id']}:wrap:{part_index}"})
            else:
                output.append(polygon)
        return output[: max(0, int(limit))]

    @staticmethod
    def _normalise_labels(value: object, kind: str) -> list[dict[str, object]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return []
        output: list[dict[str, object]] = []
        seen_ids: dict[str, int] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            coordinate = _coordinate(item)
            if coordinate is None:
                continue
            label = dict(item)
            label.update(coordinate)
            label["id"] = NativeMapRenderer._stable_id(kind, label, index, seen_ids)
            label["kind"] = kind
            label["text"] = str(label.get("text") or label.get("label") or label.get("name") or "")
            label["color_role"] = NativeMapRenderer._semantic_color_role(label, kind)
            label["_original_payload"] = dict(item)
            if label["text"]:
                output.append(label)
        return output

    @staticmethod
    def _normalise_city_labels(value: object) -> list[dict[str, object]]:
        """Keep a deterministic, bounded city source list for QML declutter.

        Visibility is decided in QML because it depends on the current map
        projection and zoom.  The bridge intentionally retains the source
        labels so zooming back in restores labels without another worker job.
        """
        labels = NativeMapRenderer._normalise_labels(value, "city")
        for label in labels:
            label["color_role"] = "text_muted"
        labels.sort(
            key=lambda item: (
                -(_number(item.get("population")) or 0.0),
                str(item.get("text") or "").casefold(),
                float(item.get("lat") or 0.0),
                float(item.get("lon") or 0.0),
            )
        )
        # Keep a complete bounded snapshot; source duplicates are harmless
        # because the spatial QML pass accepts only the highest-priority one
        # in a collision cell.  Retaining them also keeps this rendering
        # boundary strictly lossless apart from its documented cap.
        return labels[:_MAX_LABELS]

    @staticmethod
    def _normalise_fills(value: object, kind: str) -> list[dict[str, object]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return []
        output: list[dict[str, object]] = []
        seen_ids: dict[str, int] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            coordinate = _coordinate(item)
            if coordinate is None:
                continue
            fill = dict(item)
            fill.update(coordinate)
            fill["id"] = NativeMapRenderer._stable_id(kind, fill, index, seen_ids)
            fill["kind"] = kind
            fill["radius_m"] = max(5_000.0, min(1_500_000.0, _number(fill.get("radius_m", fill.get("radius"))) or 180_000.0))
            fill["color_role"] = NativeMapRenderer._semantic_color_role(fill, kind)
            fill["_original_payload"] = dict(item)
            output.append(fill)
        return output

    @staticmethod
    def _regional_fills(value: object) -> list[dict[str, object]]:
        if not isinstance(value, Mapping) or not bool(value.get("enabled")):
            return []
        rows: list[dict[str, object]] = []
        for collection_name, radius in (("states", 155_000.0), ("regions", 350_000.0)):
            collection = value.get(collection_name)
            if not isinstance(collection, Mapping):
                continue
            for key, raw in collection.items():
                if not isinstance(raw, Mapping):
                    continue
                row = dict(raw)
                row.setdefault("id", str(key))
                row.setdefault("label", str(raw.get("label") or key))
                row.setdefault("radius_m", radius)
                rows.append(row)
        return NativeMapRenderer._normalise_fills(rows, "regional")

    @staticmethod
    def _direction_indicators(paths: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        def endpoints(path):
            points = path.get("points")
            if not isinstance(points, Sequence) or len(points) < 2:
                return None
            first, last = _coordinate(points[0]), _coordinate(points[-1])
            if first is None or last is None:
                return None
            return ((first["lat"], first["lon"]), (last["lat"], last["lon"]))

        directed_pairs = {pair for path in paths[:_MAX_PATHS] if path.get("direction_known") is not False and (pair := endpoints(path)) is not None}
        for path in paths[:_MAX_PATHS]:
            if path.get("direction_known") is False:
                continue
            raw_points = path.get("points")
            if not isinstance(raw_points, Sequence) or len(raw_points) < 2:
                continue
            # MapPolyline uses straight Web Mercator segments. Place the
            # arrow on the longest segment in that same projection, including
            # wrapped longitudes, rather than averaging geographic latitudes.
            segments = []
            for start, end in zip(raw_points, raw_points[1:]):
                first, last = _coordinate(start), _coordinate(end)
                if first is None or last is None:
                    continue
                dx = (last["lon"] - first["lon"] + 180.0) % 360.0 - 180.0
                ya, yb = (
                    math.degrees(math.asinh(math.tan(math.radians(max(-85.0511, min(85.0511, point["lat"]))))))
                    for point in (first, last)
                )
                dy = yb - ya
                if math.hypot(dx, dy) > 0.000001:
                    segments.append((math.hypot(dx, dy), first, dx, ya, yb))
            if not segments:
                continue
            _, first, dx, ya, yb = max(segments, key=lambda segment: segment[0])
            pair = endpoints(path)
            # Reciprocal traffic gets separate arrowheads along the shared
            # segment; placing both at its midpoint hides one direction.
            fraction = 0.65 if pair is not None and pair[::-1] in directed_pairs else 0.5
            lat = math.degrees(math.atan(math.sinh(math.radians(ya + (yb - ya) * fraction))))
            lon = (first["lon"] + dx * fraction + 180.0) % 360.0 - 180.0
            rotation = math.degrees(math.atan2(-(yb - ya), dx)) % 360.0
            bearing = (rotation + 90.0) % 360.0
            output.append({
                "id": f"direction:{path.get('id')}",
                "kind": "path_direction",
                "lat": lat,
                "lon": lon,
                "bearing": bearing,
                "rotation": rotation,
                "color": str(path.get("color") or ""),
                "color_role": str(path.get("color_role") or "info"),
                "path": dict(path),
            })
        return output

    @staticmethod
    def _normalise_legend(value: object, markers: Sequence[Mapping[str, object]], fills: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            def normalise_entry(raw: object) -> dict[str, object] | None:
                if not isinstance(raw, Mapping):
                    return None
                label = str(raw.get("label") or raw.get("text") or "").strip()
                if not label:
                    return None
                item: dict[str, object] = {
                    "label": label,
                    "color_role": NativeMapRenderer._semantic_color_role(raw, "legend"),
                }
                if raw.get("tooltip"):
                    item["tooltip"] = str(raw["tooltip"])
                if raw.get("symbol_only"):
                    item["symbol_only"] = True
                color = str(raw.get("color") or "").strip()
                if color:
                    item["color"] = color
                if bool(raw.get("heading")):
                    item["heading"] = True
                children = raw.get("items")
                if isinstance(children, Sequence) and not isinstance(children, (str, bytes, bytearray)):
                    normalized_children = [normalise_entry(child) for child in children]
                    item["items"] = [child for child in normalized_children if child is not None][:_MAX_LEGEND_ITEMS]
                return item

            output = [normalise_entry(raw) for raw in value]
            output = [item for item in output if item is not None]
            return output
        roles = {str(item.get("color_role") or "accent") for item in [*markers, *fills]}
        labels = {"danger": "Critical", "warning": "Attention", "success": "Normal", "info": "Information", "accent": "Station"}
        return [{"label": labels.get(role, "Information"), "color_role": role} for role in ("danger", "warning", "success", "info", "accent") if role in roles]

    @staticmethod
    def _link_snr_color(value: object) -> str:
        """Return the historic Leaflet RF link SNR colour without I/O."""
        snr = _number(value)
        if snr is None:
            return "#607d8b"
        if snr >= 5:
            return "#1b5e20"
        if snr >= 0:
            return "#2e7d32"
        if snr >= -5:
            return "#fbc02d"
        if snr >= -10:
            return "#f57c00"
        return "#c62828"

    @staticmethod
    def _projection_summary(payload: Mapping[str, object], regional: object) -> str:
        explicit = str(payload.get("summary") or payload.get("regional_summary_text") or payload.get("sitrep_summary_group") or "").strip()
        if explicit:
            return explicit
        if isinstance(regional, str):
            return regional.strip()
        if isinstance(regional, Mapping):
            summary = str(regional.get("summary") or "").strip()
            if summary:
                return summary
        return ""

    @staticmethod
    def _stable_id(kind: str, item: Mapping[str, object], index: int, seen: dict[str, int]) -> str:
        """Give each render item a repeatable identity without hashing live data."""
        provided = str(item.get("id") or item.get("key") or "").strip()
        if provided:
            base = f"{kind}:{provided}"
        else:
            identity = "|".join(
                str(item.get(key) or "").strip()
                for key in ("callsign", "title", "origin", "destination", "lat", "lon", "latest_ts", "utc_ts")
            )
            base = f"{kind}:{identity or index}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        return base if count == 0 else f"{base}:{count + 1}"

    @staticmethod
    def _semantic_color_role(item: Mapping[str, object], kind: str) -> str:
        explicit = str(item.get("color_role") or "").strip().lower()
        if explicit in {"accent", "danger", "warning", "success", "info", "muted", "text", "text_muted"}:
            return explicit
        status = str(
            item.get("spotter_status_key")
            or item.get("status")
            or item.get("severity")
            or item.get("level")
            or item.get("score_level")
            or ""
        ).strip().lower()
        if status in {"red", "critical", "emergency", "danger", "severe"}:
            return "danger"
        if status in {"yellow", "warning", "watch", "moderate", "caution"}:
            return "warning"
        if status in {"green", "good", "healthy", "ready", "normal", "routine"}:
            return "success"
        if kind == "alert":
            return "danger"
        if kind in {"weather", "infrastructure", "path"}:
            return "info"
        return "accent"

    @staticmethod
    def _fit_view(markers: Sequence[Mapping[str, object]], paths: Sequence[Mapping[str, object]], width=1000, height=700) -> dict[str, float] | None:
        """Return a calm bounded center for an intentional focused projection."""
        points: list[dict[str, float]] = []
        for marker in markers:
            coordinate = _coordinate(marker)
            if coordinate:
                points.append(coordinate)
        for path in paths:
            raw_points = path.get("points") if isinstance(path, Mapping) else None
            if isinstance(raw_points, Sequence):
                points.extend(point for point in (_coordinate(item) for item in raw_points) if point)
        if not points:
            return None
        latitudes = [point["lat"] for point in points]
        longitudes = [point["lon"] for point in points]
        ordered = sorted({(lon+360)%360 for lon in longitudes})
        gaps = [((ordered[(i+1)%len(ordered)]-value)%360,i) for i,value in enumerate(ordered)]
        gap,index = max(gaps)
        lon_span = 0 if len(ordered)==1 else 360-gap
        lon_start = ordered[(index+1)%len(ordered)]
        center_lon = (lon_start+lon_span/2+180)%360-180
        span = max(max(latitudes) - min(latitudes),lon_span)
        if span < 0.25:
            zoom = 9.0
        elif span < 1.0:
            zoom = 7.0
        elif span < 5.0:
            zoom = 5.0
        elif span < 20.0:
            zoom = 4.0
        else:
            zoom = 3.0
        projected_latitudes = [math.asinh(math.tan(math.radians(lat))) for lat in latitudes]
        y_min,y_max = min(projected_latitudes),max(projected_latitudes)
        for extent,available in ((lon_span/360,max(1,width-80)),((y_max-y_min)/(2*math.pi),max(1,height-120))):
            if extent > 0:
                zoom = min(zoom,max(0,math.log2(available/(256*extent))))
        center_lat = math.degrees(math.atan(math.sinh((y_min+y_max)/2)))
        return {"lat": center_lat, "lon": center_lon, "zoom": zoom}
