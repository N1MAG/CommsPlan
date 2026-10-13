import QtQuick 2.15
import QtQuick.Controls 2.15
import QtLocation 6.8
import QtPositioning 6.8

Item {
    id: root
    anchors.fill: parent
    focus: true
    Component.onCompleted: gridUpdater.restart()

    function boundedZoom(value) {
        return Math.max(1, Math.min(18, Number(value)))
    }

    function themeColor(role) {
        return mapBridge.theme[role || "accent"] || mapBridge.theme.accent
    }

    function markerSymbol(marker) {
        var kind = String(marker.kind || "station").toLowerCase()
        var icon = String(marker.icon || marker.topic || "").toLowerCase()
        var source = String(marker.source_kind || "").toLowerCase()
        if (kind === "station") return marker.is_own_station ? "⌂" : (marker.is_mesh_self ? "⌖" : "•")
        if (kind === "alert") return "!"
        if (source === "pin") return "⌖"
        if (icon === "storm" || icon === "power") return "ϟ"
        if (icon === "rain") return "☂"
        if (icon === "wind") return "≋"
        if (icon === "snow") return "✣"
        if (icon === "fire" || icon === "heat") return "♨"
        if (icon === "flood" || icon === "water") return "≈"
        if (icon === "medical") return "+"
        if (icon === "comms") return "⌁"
        if (icon === "transport") return "↔"
        if (icon === "shelter") return "⌂"
        if (icon === "evacuation" || icon === "warning") return "!"
        if (kind === "weather") return "☁"
        if (kind === "infrastructure") return "i"
        return "•"
    }

    function markerDetail(marker) {
        if (marker.is_own_station) return "My station · " + String(marker.callsign || "") + "\n" + String(marker.tooltip || marker.title || "")
        return String(marker.tooltip || marker.title || marker.label || marker.callsign || "Map item")
    }

    function markerBorderRole(marker) {
        if (String(marker.kind || "") !== "station")
            return marker.color_role || "accent"
        if (marker.qsy_soon)
            return "accent"
        var qsy = String(marker.qsy_text || "").toLowerCase()
        if (qsy.indexOf("stable") === 0)
            return "success"
        if (qsy.length > 0)
            return "info"
        return marker.color_role || "accent"
    }

    function pathDetail(path) {
        if (path.tooltip || path.title)
            return String(path.tooltip || path.title)
        var origin = String(path.origin || path.from || "")
        var destination = String(path.destination || path.to || "")
        if (origin || destination)
            return origin + " → " + destination
        return "Communication path"
    }

    function polygonDetail(polygon) {
        return String(polygon.tooltip || polygon.title || polygon.label || polygon.name || polygon.area_code || "Map area")
    }

    property bool applyingBridgeView: false
    property var dynamicGridLines: []
    property var dynamicGridLabels: []
    // City source labels remain bounded in the bridge.  This derived view is
    // recomputed after a calm 120ms viewport debounce, so labels reappear as
    // an operator zooms in without a database or projection round trip.
    property var visibleCityLabels: []

    function updateVisibleCityLabels() {
        if (!mapBridge.mapVisible || stationMap.width <= 0 || stationMap.height <= 0) {
            visibleCityLabels = []
            return
        }
        var visible = []
        var occupied = {}
        var zoom = stationMap.zoomLevel
        var labels = mapBridge.cityLabels || []
        // `cityLabels` is ordered by population then name.  The grid keeps
        // the first/highest priority label and only checks its 3x3 neighbors:
        // O(n) bounded work (n <= 400), never pairwise O(n²).
        for (var index = 0; index < labels.length && visible.length < 400; ++index) {
            var label = labels[index]
            if (zoom < Number(label.min_zoom || 1))
                continue
            var point = stationMap.fromCoordinate(
                QtPositioning.coordinate(Number(label.lat), Number(label.lon)), false)
            if (!isFinite(point.x) || !isFinite(point.y) || point.x < -100 || point.y < -24 || point.x > stationMap.width + 100 || point.y > stationMap.height + 24)
                continue
            var text = String(label.text || label.label || "")
            var width = Math.max(30, Math.min(180, text.length * Math.max(7, Qt.application.font.pixelSize * 0.72) + 10))
            var height = Math.max(18, Qt.application.font.pixelSize + 7)
            var cellX = Math.floor(point.x / 72)
            var cellY = Math.floor(point.y / 28)
            var blocked = false
            for (var y = cellY - 1; y <= cellY + 1 && !blocked; ++y) {
                for (var x = cellX - 1; x <= cellX + 1; ++x) {
                    var candidates = occupied[x + ":" + y]
                    if (!candidates)
                        continue
                    for (var candidateIndex = 0; candidateIndex < candidates.length; ++candidateIndex) {
                        var candidate = candidates[candidateIndex]
                        if (Math.abs(point.x - candidate.x) < (width + candidate.width) / 2 && Math.abs(point.y - candidate.y) < (height + candidate.height) / 2) {
                            blocked = true
                            break
                        }
                    }
                    if (blocked)
                        break
                }
            }
            if (blocked)
                continue
            var key = cellX + ":" + cellY
            if (!occupied[key])
                occupied[key] = []
            occupied[key].push({"x": point.x, "y": point.y, "width": width, "height": height})
            visible.push(label)
        }
        visibleCityLabels = visible
    }

    function maidenFromLatLon(lat, lon, level) {
        var adjustedLon = lon + 180.0
        var adjustedLat = lat + 90.0
        var fieldLon = Math.floor(adjustedLon / 20)
        var fieldLat = Math.floor(adjustedLat / 10)
        var value = String.fromCharCode(65 + fieldLon) + String.fromCharCode(65 + fieldLat)
        if (level >= 4) {
            value += String(Math.floor((adjustedLon % 20) / 2))
            value += String(Math.floor(adjustedLat % 10))
        }
        if (level >= 6) {
            value += String.fromCharCode(65 + Math.floor(((adjustedLon % 2) / 2) * 24))
            value += String.fromCharCode(65 + Math.floor((adjustedLat % 1) * 24))
        }
        return value
    }

    function updateAdaptiveGrid() {
        if (!mapBridge.adaptiveGrid || stationMap.width <= 0 || stationMap.height <= 0) {
            dynamicGridLines = []
            dynamicGridLabels = []
            return
        }
        var topLeft = stationMap.toCoordinate(Qt.point(0, 0), false)
        var bottomRight = stationMap.toCoordinate(Qt.point(stationMap.width, stationMap.height), false)
        var west = Math.max(-180, Number(topLeft.longitude))
        var east = Math.min(180, Number(bottomRight.longitude))
        var north = Math.min(85, Number(topLeft.latitude))
        var south = Math.max(-85, Number(bottomRight.latitude))
        if (!isFinite(west) || !isFinite(east) || !isFinite(north) || !isFinite(south) || east <= west || north <= south) {
            dynamicGridLines = []
            dynamicGridLabels = []
            return
        }
        var zoom = stationMap.zoomLevel
        var stepLon = zoom < 5 ? 20 : (zoom < 9 ? 2 : 0.0833333333)
        var stepLat = stepLon / 2
        var level = zoom < 5 ? 2 : (zoom < 9 ? 4 : 6)
        var lonStart = Math.floor(west / stepLon) * stepLon
        var latStart = Math.floor(south / stepLat) * stepLat
        var predictedLines = Math.ceil((east - lonStart) / stepLon) + Math.ceil((north - latStart) / stepLat) + 2
        var lines = []
        if (predictedLines <= 320) {
            for (var lon = lonStart; lon <= east && lines.length < 320; lon += stepLon) {
                lines.push({"points": [{"lat": south, "lon": lon}, {"lat": north, "lon": lon}]})
            }
            for (var lat = latStart; lat <= north && lines.length < 320; lat += stepLat) {
                lines.push({"points": [{"lat": lat, "lon": west}, {"lat": lat, "lon": east}]})
            }
        }
        dynamicGridLines = lines

        var labelsAllowed = mapBridge.adaptiveGridLabels && ((level === 2 && zoom >= 4) || (level === 4 && zoom >= 6) || (level === 6 && zoom >= 10))
        var labels = []
        if (labelsAllowed && lines.length > 0) {
            for (var labelLat = latStart + stepLat / 2; labelLat < north && labels.length < 400; labelLat += stepLat) {
                for (var labelLon = lonStart + stepLon / 2; labelLon < east && labels.length < 400; labelLon += stepLon) {
                    labels.push({
                        "lat": labelLat,
                        "lon": labelLon,
                        "text": maidenFromLatLon(labelLat, labelLon, level),
                        "color_role": "text_muted"
                    })
                }
            }
        }
        dynamicGridLabels = labels
    }

    Connections {
        target: mapBridge
        function onViewChanged() {
            root.applyingBridgeView = true
            stationMap.center = QtPositioning.coordinate(mapBridge.view.lat, mapBridge.view.lon)
            stationMap.zoomLevel = root.boundedZoom(mapBridge.view.zoom)
            root.applyingBridgeView = false
            gridUpdater.restart()
        }
        function onGridLinesChanged() { gridUpdater.restart() }
        function onGridLabelsChanged() { gridUpdater.restart() }
        function onAdaptiveGridChanged() { gridUpdater.restart() }
        function onCityLabelsChanged() { gridUpdater.restart() }
    }

    Timer {
        id: gridUpdater
        interval: 120
        repeat: false
        onTriggered: {
            root.updateAdaptiveGrid()
            root.updateVisibleCityLabels()
        }
    }

    Timer {
        id: viewReporter
        interval: 120
        repeat: false
        onTriggered: mapBridge.report_view(
            stationMap.center.latitude,
            stationMap.center.longitude,
            stationMap.zoomLevel
        )
    }

    Rectangle {
        anchors.fill: parent
        color: mapBridge.theme.bg || mapBridge.theme.surface
    }

    Map {
        id: stationMap
        objectName: "offlineStationMap"
        anchors.fill: parent
        visible: mapBridge.mapVisible
        plugin: Plugin {
            // `itemsoverlay` is a provider-free coordinate canvas.  All map
            // geography and operational layers are bundled FIO vectors; this
            // surface never requests tiles, credentials, or network access.
            name: "itemsoverlay"
        }
        center: QtPositioning.coordinate(mapBridge.view.lat, mapBridge.view.lon)
        zoomLevel: mapBridge.view.zoom
        onCenterChanged: {
            if (!root.applyingBridgeView) {
                viewReporter.restart()
                gridUpdater.restart()
            }
        }
        onZoomLevelChanged: {
            if (!root.applyingBridgeView) {
                viewReporter.restart()
                gridUpdater.restart()
            }
        }

        MapRectangle {
            topLeft: QtPositioning.coordinate(85, -179.999)
            bottomRight: QtPositioning.coordinate(-85, 179.999)
            color: mapBridge.theme.bg || mapBridge.theme.surface
            border.width: 0
        }

        MapItemView {
            model: mapBridge.basePolygons
            delegate: MapPolygon {
                color: mapBridge.theme.surface_alt || mapBridge.theme.surface
                border.color: mapBridge.theme.border
                border.width: 1
                opacity: 0.82
                path: {
                    var result = []
                    var points = modelData.points || []
                    for (var index = 0; index < points.length; ++index) {
                        result.push(QtPositioning.coordinate(Number(points[index].lat), Number(points[index].lon)))
                    }
                    return result
                }
            }
        }

        DragHandler {
            id: panHandler
            target: null
            acceptedButtons: Qt.LeftButton
            property point previousTranslation: Qt.point(0, 0)
            onActiveChanged: {
                previousTranslation = Qt.point(0, 0)
                if (!active)
                    viewReporter.restart()
            }
            onTranslationChanged: {
                var dx = translation.x - previousTranslation.x
                var dy = translation.y - previousTranslation.y
                previousTranslation = translation
                stationMap.pan(-dx, -dy)
            }
        }

        WheelHandler {
            id: wheelZoom
            target: null
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: function(event) {
                var steps = event.angleDelta.y / 120.0
                if (steps !== 0) {
                    stationMap.zoomLevel = root.boundedZoom(stationMap.zoomLevel + steps)
                    viewReporter.restart()
                    event.accepted = true
                }
            }
        }

        PinchHandler {
            id: pinchZoom
            target: null
            property real startingZoom: stationMap.zoomLevel
            onActiveChanged: {
                if (active)
                    startingZoom = stationMap.zoomLevel
                else
                    viewReporter.restart()
            }
            onScaleChanged: {
                stationMap.zoomLevel = root.boundedZoom(startingZoom + Math.log(scale) / Math.LN2)
            }
        }

        MapItemView {
            model: mapBridge.polygons
            delegate: MapPolygon {
                color: modelData.fill_color || modelData.color || root.themeColor(modelData.color_role)
                border.color: modelData.border_color || root.themeColor(modelData.color_role)
                border.width: polygonHover.hovered ? Math.max(2, Number(modelData.line_width || 1)) : Number(modelData.line_width || 1)
                opacity: Number(modelData.fill_opacity === undefined ? 0.22 : modelData.fill_opacity)
                Accessible.role: Accessible.Button
                Accessible.name: root.polygonDetail(modelData)
                path: {
                    var result = []
                    var points = modelData.points || []
                    for (var index = 0; index < points.length; ++index) {
                        result.push(QtPositioning.coordinate(Number(points[index].lat), Number(points[index].lon)))
                    }
                    return result
                }
                TapHandler {
                    onTapped: mapBridge.selectPolygonById(String(modelData.id || ""))
                }
                HoverHandler {
                    id: polygonHover
                }
                ToolTip.visible: polygonHover.hovered
                ToolTip.delay: 350
                ToolTip.text: root.polygonDetail(modelData)
            }
        }

        MapItemView {
            model: mapBridge.fills
            delegate: MapCircle {
                center: QtPositioning.coordinate(Number(modelData.lat), Number(modelData.lon))
                radius: Number(modelData.radius_m || 180000)
                color: modelData.fill_color || root.themeColor(modelData.color_role)
                border.color: modelData.border_color || root.themeColor(modelData.color_role)
                border.width: 1
                opacity: 0.18
            }
        }

        MapItemView {
            model: mapBridge.adaptiveGrid ? root.dynamicGridLines : mapBridge.gridLines
            delegate: MapPolyline {
                referenceSurface: QtLocation.ReferenceSurface.Map
                line.width: 1
                line.color: modelData.color || mapBridge.theme.border
                opacity: 0.65
                path: {
                    var result = []
                    var points = modelData.points || []
                    for (var index = 0; index < points.length; ++index) {
                        result.push(QtPositioning.coordinate(Number(points[index].lat), Number(points[index].lon)))
                    }
                    return result
                }
            }
        }

        MapItemView {
            model: mapBridge.paths
            delegate: MapPolyline {
                referenceSurface: QtLocation.ReferenceSurface.Map
                opacity: modelData.evidence_kind === "cached_route" ? 0.6 : 1.0
                line.width: modelData.evidence_kind === "cached_route" ? 1.5 : 2.5
                line.color: modelData.color || root.themeColor(modelData.color_role)
                path: {
                    var result = []
                    var points = modelData.points || []
                    for (var index = 0; index < points.length; ++index) {
                        result.push(QtPositioning.coordinate(Number(points[index].lat), Number(points[index].lon)))
                    }
                    return result
                }
            }
        }

        MapItemView {
            model: mapBridge.paths
            delegate: MapPolyline {
                referenceSurface: QtLocation.ReferenceSurface.Map
                // One generous, nearly transparent interaction stroke sits on
                // top of the visible 2px path.  Only this layer handles taps,
                // so one operator click can emit only one selection action.
                line.width: pathHover.hovered ? 18 : 14
                line.color: mapBridge.theme.text
                opacity: pathHover.hovered ? 0.10 : 0.015
                Accessible.role: Accessible.Button
                Accessible.name: root.pathDetail(modelData)
                path: {
                    var result = []
                    var points = modelData.points || []
                    for (var index = 0; index < points.length; ++index) {
                        result.push(QtPositioning.coordinate(Number(points[index].lat), Number(points[index].lon)))
                    }
                    return result
                }
                TapHandler {
                    gesturePolicy: TapHandler.WithinBounds
                    onTapped: mapBridge.selectPathById(String(modelData.id || ""))
                }
                HoverHandler {
                    id: pathHover
                }
                ToolTip.visible: pathHover.hovered
                ToolTip.delay: 350
                ToolTip.text: root.pathDetail(modelData)
            }
        }

        MapItemView {
            model: mapBridge.markers
            delegate: MapQuickItem {
                visible: stationMap.zoomLevel >= Number(modelData.min_zoom || 1)
                coordinate: QtPositioning.coordinate(Number(modelData.lat), Number(modelData.lon))
                anchorPoint.x: markerTarget.width / 2
                anchorPoint.y: 17
                sourceItem: Item {
                    id: markerTarget
                    objectName: "offlineMapMarker:" + String(modelData.id || "")
                    width: Math.max(34, stationLabel.visible ? stationLabel.implicitWidth + 8 : 34)
                    height: stationLabel.visible ? 52 : 34
                    Accessible.role: Accessible.Button
                    Accessible.name: root.markerDetail(modelData)
                    Rectangle {
                        id: markerDot
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: (34 - height) / 2
                        width: modelData.kind === "station" ? (modelData.is_own_station ? 25 : (modelData.qsy_soon ? 19 : 16)) : 21
                        height: width
                        radius: modelData.kind === "alert" ? 3 : (modelData.kind === "infrastructure" ? 6 : width / 2)
                        rotation: modelData.kind === "alert" ? 45 : 0
                        color: modelData.color || root.themeColor(modelData.color_role)
                        border.color: root.themeColor(root.markerBorderRole(modelData))
                        border.width: modelData.qsy_soon ? 3 : 2
                        opacity: markerHover.hovered ? 1.0 : 0.96
                    }
                    Rectangle {
                        visible: !!modelData.is_own_station || !!modelData.is_mesh_self
                        anchors.centerIn: markerDot
                        width: markerDot.width + 8
                        height: width
                        radius: width / 2
                        color: "transparent"
                        border.width: 2
                        border.color: mapBridge.theme.text
                    }
                    Text {
                        anchors.centerIn: markerDot
                        text: root.markerSymbol(modelData)
                        color: mapBridge.theme.surface
                        font.family: Qt.application.font.family
                        font.pixelSize: Qt.application.font.pixelSize
                        font.bold: true
                    }
                    Text {
                        visible: Number(modelData.event_count || 0) > 1
                        anchors.left: markerDot.right
                        anchors.bottom: markerDot.top
                        text: Number(modelData.event_count) > 99 ? "99+" : String(modelData.event_count)
                        color: mapBridge.theme.text
                        font.family: Qt.application.font.family
                        font.pixelSize: Math.max(9, Qt.application.font.pixelSize * 0.75)
                        font.bold: true
                    }
                    Text {
                        id: stationLabel
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: modelData.is_mesh_self ? undefined : markerDot.bottom
                        anchors.bottom: modelData.is_mesh_self ? markerDot.top : undefined
                        anchors.topMargin: 2
                        anchors.bottomMargin: 2
                        visible: modelData.kind === "station" && String(modelData.label || "").length > 0
                        text: modelData.label || ""
                        color: mapBridge.theme.text
                        font: Qt.application.font
                        padding: 2
                    }
                    HoverHandler {
                        id: markerHover
                    }
                    TapHandler {
                        acceptedButtons: Qt.LeftButton
                        gesturePolicy: TapHandler.WithinBounds
                        onTapped: mapBridge.selectMarkerById(String(modelData.id || ""))
                    }
                    ToolTip.visible: markerHover.hovered
                    ToolTip.delay: 350
                    ToolTip.text: root.markerDetail(modelData)
                }
            }
        }

        MapItemView {
            model: mapBridge.directions
            delegate: MapQuickItem {
                visible: mapBridge.directionIndicators && stationMap.zoomLevel >= Number(modelData.min_zoom || 1)
                coordinate: QtPositioning.coordinate(Number(modelData.lat), Number(modelData.lon))
                anchorPoint.x: arrowContainer.width / 2
                anchorPoint.y: arrowContainer.height / 2
                // Rotate inside a stable source rectangle. Rotating the
                // sourceItem itself shifts MapQuickItem's geographic anchor.
                sourceItem: Item {
                    id: arrowContainer
                    width: arrow.implicitWidth
                    height: arrow.implicitHeight
                    Text {
                        id: arrow
                        anchors.centerIn: parent
                        text: modelData.path && modelData.path.evidence_kind === "cached_route" ? "›" : "➤"
                        color: modelData.color || root.themeColor(modelData.color_role)
                        font: Qt.application.font
                        rotation: Number(modelData.rotation || 0)
                    }
                    Accessible.role: Accessible.Button
                    Accessible.name: root.pathDetail(modelData.path || {})
                    HoverHandler { id: directionHover }
                    ToolTip.visible: directionHover.hovered
                    ToolTip.text: root.pathDetail(modelData.path || {})
                    MouseArea {
                        anchors.fill: parent
                        onClicked: mapBridge.selectPathById(String((modelData.path || {}).id || ""))
                    }
                }
            }
        }

        MapItemView {
            model: mapBridge.adaptiveGrid ? root.dynamicGridLabels : mapBridge.gridLabels
            delegate: MapQuickItem {
                visible: stationMap.zoomLevel >= Number(modelData.min_zoom || 1)
                coordinate: QtPositioning.coordinate(Number(modelData.lat), Number(modelData.lon))
                anchorPoint.x: gridText.width / 2
                anchorPoint.y: gridText.height / 2
                sourceItem: Text {
                    id: gridText
                    text: modelData.text || ""
                    color: root.themeColor(modelData.color_role || "text_muted")
                    font: Qt.application.font
                }
            }
        }

        MapItemView {
            // State abbreviations are restored only when the States overlay
            // is active.  They are labels, not a replacement hit target: the
            // polygon directly beneath them remains selectable.
            model: mapBridge.stateLabels
            delegate: MapQuickItem {
                visible: stationMap.zoomLevel >= Number(modelData.min_zoom || 1)
                coordinate: QtPositioning.coordinate(Number(modelData.lat), Number(modelData.lon))
                anchorPoint.x: stateText.width / 2
                anchorPoint.y: stateText.height / 2
                sourceItem: Text {
                    id: stateText
                    text: modelData.text || ""
                    color: root.themeColor(modelData.color_role || "text_muted")
                    font.family: Qt.application.font.family
                    font.pixelSize: Math.max(9, Qt.application.font.pixelSize * 0.80)
                    font.bold: true
                }
            }
        }

        MapItemView {
            // Exactly one label per R01–R10 makes the FEMA layer legible at
            // continental zoom without obscuring the state-level geometry.
            model: mapBridge.regionLabels
            delegate: MapQuickItem {
                visible: stationMap.zoomLevel >= Number(modelData.min_zoom || 1)
                coordinate: QtPositioning.coordinate(Number(modelData.lat), Number(modelData.lon))
                anchorPoint.x: regionText.width / 2
                anchorPoint.y: regionText.height / 2
                sourceItem: Text {
                    id: regionText
                    text: String(modelData.text || "") + (String(modelData.band || "").length > 0 ? "\n" + String(modelData.band) : "")
                    color: modelData.color || root.themeColor(modelData.color_role || "info")
                    font.family: Qt.application.font.family
                    font.pixelSize: Math.max(10, Qt.application.font.pixelSize * 0.95)
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    HoverHandler {
                        id: regionLabelHover
                    }
                    ToolTip.visible: regionLabelHover.hovered && String(modelData.tooltip || "").length > 0
                    ToolTip.delay: 350
                    ToolTip.text: String(modelData.tooltip || "")
                }
            }
        }

        MapItemView {
            model: root.visibleCityLabels
            delegate: MapQuickItem {
                visible: stationMap.zoomLevel >= Number(modelData.min_zoom || 1)
                coordinate: QtPositioning.coordinate(Number(modelData.lat), Number(modelData.lon))
                anchorPoint.x: cityText.width / 2
                anchorPoint.y: cityText.height / 2
                sourceItem: Text {
                    id: cityText
                    text: modelData.text || ""
                    color: root.themeColor(modelData.color_role || "text_muted")
                    font: Qt.application.font
                }
            }
        }
    }

    Column {
        id: zoomControls
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: 12
        spacing: 5
        z: 1000

        Repeater {
            model: [
                { "label": "+", "delta": 1, "accessible": "Zoom in" },
                { "label": "⌂", "delta": 0, "accessible": "Return to the saved map view" },
                { "label": "−", "delta": -1, "accessible": "Zoom out" }
            ]
            delegate: Rectangle {
                required property var modelData
                width: Math.max(36, zoomLabel.implicitWidth + 14)
                height: Math.max(36, zoomLabel.implicitHeight + 10)
                radius: 4
                color: zoomTap.pressed ? mapBridge.theme.accent : mapBridge.theme.surface
                border.color: mapBridge.theme.border
                opacity: 0.96
                Accessible.role: Accessible.Button
                Accessible.name: modelData.accessible
                Text {
                    id: zoomLabel
                    anchors.centerIn: parent
                    text: modelData.label
                    color: zoomTap.pressed ? mapBridge.theme.surface : mapBridge.theme.text
                    font.family: Qt.application.font.family
                    font.pixelSize: Qt.application.font.pixelSize
                    font.bold: true
                }
                TapHandler {
                    id: zoomTap
                    acceptedButtons: Qt.LeftButton
                    onTapped: {
                        if (modelData.delta > 0)
                            mapBridge.zoomIn()
                        else if (modelData.delta < 0)
                            mapBridge.zoomOut()
                        else
                            mapBridge.resetView()
                        viewReporter.restart()
                    }
                }
            }
        }
        Rectangle {
            width: 44
            height: Math.max(24, zoomLevelLabel.implicitHeight + 6)
            radius: 4
            color: mapBridge.theme.surface
            border.color: mapBridge.theme.border
            opacity: 0.96
            Accessible.role: Accessible.StaticText
            Accessible.name: "Map zoom level " + stationMap.zoomLevel.toFixed(1)
            Text {
                id: zoomLevelLabel
                anchors.centerIn: parent
                text: "Z " + stationMap.zoomLevel.toFixed(1)
                color: mapBridge.theme.text_muted || mapBridge.theme.text
                font.family: Qt.application.font.family
                font.pixelSize: Math.max(9, Qt.application.font.pixelSize * 0.8)
            }
        }
    }

    Rectangle {
        id: summaryPanel
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 12
        visible: mapBridge.summary.length > 0
        color: mapBridge.theme.surface
        border.color: mapBridge.theme.border
        radius: 4
        opacity: 0.92
        width: Math.min(300, Math.max(180, parent.width - 24))
        implicitHeight: summaryText.implicitHeight + 16

        Text {
            id: summaryText
            anchors.margins: 10
            anchors.fill: parent
            text: mapBridge.summary
            color: mapBridge.theme.text
            font: Qt.application.font
            wrapMode: Text.WordWrap
        }
    }

    Rectangle {
        id: legendPanel
        property int displayUnits: {
            var total = 0
            for (var index = 0; index < mapBridge.legend.length; index += 1) {
                var entry = mapBridge.legend[index] || {}
                total += entry.items && entry.items.length ? entry.items.length : 1
            }
            return total
        }
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottomMargin: 12
        visible: mapBridge.legend.length > 0
        color: mapBridge.theme.surface
        border.color: mapBridge.theme.border
        radius: 4
        opacity: 0.92
        width: Math.min(
            parent.width - 24,
            Math.max(180, Math.min(1040, displayUnits * 145))
        )
        height: Math.max(34, legendRow.implicitHeight + 12)

        Flow {
            id: legendRow
            anchors.centerIn: parent
            width: Math.max(0, parent.width - 20)
            spacing: 14
            Repeater {
                model: mapBridge.legend
                delegate: Item {
                    id: legendEntry
                    property var childItems: modelData.items || []
                    property bool grouped: childItems.length > 0
                    width: grouped ? legendRow.width : simpleEntry.implicitWidth
                    height: grouped ? groupedEntry.implicitHeight : simpleEntry.implicitHeight
                    Accessible.name: modelData.tooltip || modelData.label || ""
                    HoverHandler { id: legendHover }
                    ToolTip.visible: legendHover.hovered && !!modelData.tooltip
                    ToolTip.text: modelData.tooltip || ""

                    Row {
                        id: simpleEntry
                        visible: !legendEntry.grouped
                        spacing: 5
                        Rectangle {
                            width: modelData.heading || modelData.symbol_only ? 0 : 10
                            height: modelData.heading || modelData.symbol_only ? 0 : 10
                            radius: width / 2
                            visible: !modelData.heading && !modelData.symbol_only
                            color: modelData.color || root.themeColor(modelData.color_role)
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                            width: Math.min(140, implicitWidth)
                            text: modelData.label || ""
                            color: mapBridge.theme.text
                            font.bold: !!modelData.heading
                            elide: Text.ElideRight
                        }
                    }

                    Column {
                        id: groupedEntry
                        visible: legendEntry.grouped
                        width: parent.width
                        spacing: 4
                        Text {
                            text: modelData.label || ""
                            color: mapBridge.theme.text
                            font.bold: true
                        }
                        Flow {
                            width: parent.width
                            spacing: 10
                            Repeater {
                                model: legendEntry.childItems
                                delegate: Row {
                                    spacing: 5
                                    Rectangle {
                                        width: modelData.heading ? 0 : 10
                                        height: modelData.heading ? 0 : 10
                                        radius: width / 2
                                        visible: !modelData.heading
                                        color: modelData.color || root.themeColor(modelData.color_role)
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                    Text {
                                        text: modelData.label || ""
                                        color: mapBridge.theme.text
                                        font.bold: !!modelData.heading
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
