"""Map-owned reference workflow. Presentation paths consume bounded snapshots."""
import json
from PySide6.QtWidgets import QComboBox, QPushButton
from PySide6.QtCore import QTimer
from freqinout.core.call_finder import reference_key
from freqinout.core.license_map_reference import MapBounds
from freqinout.gui.map_reference_controller import MapReferenceController


class MapLicenseReferencePresenter:
    def __init__(self,host,panel):
        self.host,self.panel=host,panel
        self.controller=MapReferenceController(host)
        self.hits=()
        self.viewport={}
        self.kind=''
        self.active=False
        self.controller.set_active(False)
        self.scope=QComboBox(host)
        self.scope.addItem('Current map','current')
        self.scope.addItem('License references','references')
        self.scope.addItem('Both','both')
        self.scope.setAccessibleName('Map search scope')
        self.scope.setToolTip('Reference searches run only on Enter or Search. Group and Activity window apply to operational evidence.')
        self.submit=QPushButton('Search',host)
        field=host._map_search_field.layout()
        field.addWidget(self.scope)
        field.addWidget(self.submit)
        self.submit.clicked.connect(self.submit_search)
        host._map_search_edit.returnPressed.connect(self.submit_search)
        self.scope.currentIndexChanged.connect(self.search_scope_changed)
        panel.callsign_edit.hide();panel.search_button.hide()
        panel.build_requested.connect(self.build)
        panel.area_requested.connect(self.area)
        panel.callsign_requested.connect(self.callsign)
        panel.overlay_toggled.connect(lambda *_:self.publish())
        panel.show_requested.connect(self.show)
        panel.add_requested.connect(self.add)
        panel.cancel_requested.connect(self.cancel)
        panel.country_combo.currentIndexChanged.connect(self.scope_changed)
        panel.service_combo.currentIndexChanged.connect(self.scope_changed)
        panel.region_edit.textChanged.connect(self.scope_changed)
        self.controller.busy_changed.connect(panel.set_busy)
        self.controller.progress.connect(panel.set_status)
        self.controller.failed.connect(self.failed)
        self.controller.ready.connect(self.ready)
        self.controller.invalidated.connect(lambda:self.scope_changed(message='Reference source changed. Search again; rebuild the area index if stale.'))
        self.panel.set_status('Optional postal estimates. Search a callsign, or build the map index for area searches.')
        self._operational_search=''

    def set_active(self,active):
        active=bool(active)
        if active != self.active:
            self.active=active
            self.controller.set_active(active)

    def dispose(self):
        self.controller.dispose()

    def search_scope_changed(self,*_args):
        self.controller.invalidate()
        if self.scope.currentData()!='current':
            self.open_tools()
            self.panel.set_status('Press Enter or Search to look up offline license references.')
        self.host._on_map_search_text_changed(self.host._map_search_edit.text())

    def open_tools(self):
        self.host._map_selected_panel.hide()
        self.host._open_map_tool_category('Layers')

    def filters(self):
        return dict(self.panel.filters(),geographic_country=getattr(self.host,'_map_geographic_country',''))

    def scope_changed(self,*_args,message='Reference scope changed. Search again.'):
        self.controller.invalidate()
        self.hits=()
        self.panel.set_results((),message=message)
        self.publish()

    def cancel(self):
        self.controller.invalidate()
        self.panel.set_status('Cancelling reference work; retained index is preserved.')

    def build(self):
        self.scope_changed(message='Building map reference index…')
        self.kind='build'
        self.controller.request('build',**self.panel.filters())

    def viewport_changed(self,value):
        self.viewport=dict(value or {})
        self.controller.viewport_changed()
        self.panel.set_area_available(bool(self.viewport),changed=bool(self.viewport and self.hits))

    def area(self):
        renderer=getattr(self.host,'_native_map_renderer',None)
        value=dict(self.viewport)
        if not renderer or not value or value.get('width')!=renderer.width() or value.get('height')!=renderer.height():
            self.panel.set_status('Wait for the map area to settle, then Search this area.')
            return
        try:
            bounds=MapBounds(*(float(value[k]) for k in ('west','south','east','north')),bool(value.get('full_world')))
            bounds.cells()  # Pure early feedback; SQL still enforces its own cap.
        except (ValueError,KeyError,TypeError) as exc:
            self.panel.set_status(str(exc));return
        self.kind='area'
        self.panel.set_status('Searching this area…')
        self.controller.request('area',bounds=bounds,viewport_revision=int(value['revision']),**self.filters())

    def submit_search(self):
        if self.scope.currentData() in ('references','both'):
            self.callsign(self.host._map_search_edit.text())
        else:
            self.host._on_map_search_timeout()

    def callsign(self,query):
        query=str(query).strip().upper()
        if not query:
            self.panel.set_status('Enter a callsign, then Search.');return
        self.open_tools()
        self.kind='callsign'
        self.panel.set_status('Searching offline license references…')
        self.controller.request('callsign',query=query,**self.filters())

    def failed(self,message):
        # Retained prior results are explicitly a previous snapshot; a failed
        # area query never replaces them with a successful-looking empty list.
        self.panel.set_status(str(message)+' Previous results, if shown, are retained.')

    def ready(self,result):
        if self.kind=='build':
            self.panel.set_status(result.message)
            return
        self.hits=tuple(result.hits)
        message=result.message
        if not message:
            message=f'{len(self.hits)} license references. Locations are postal estimates.'
        self.panel.set_results(tuple(hit.record for hit in self.hits),has_more=result.has_more,message=message)
        if self.kind=='area':
            # Search this area is explicit intent to display its bounded result.
            self.panel.overlay_checkbox.blockSignals(True)
            self.panel.overlay_checkbox.setChecked(True)
            self.panel.overlay_checkbox.blockSignals(False)
        self.publish()
        self.reveal(self.panel.table)

    def publish(self):
        renderer=getattr(self.host,'_native_map_renderer',None)
        if renderer is None:
            return
        values=[]
        geographic=getattr(self.host,'_map_geographic_country','')
        managed=getattr(self.host,'operator_index',{})
        own=str(self.host.settings.get('operator_callsign','') or '').upper() if self.host.settings else ''
        if self.panel.overlay_checkbox.isChecked():
            for hit in self.hits:
                record=hit.record
                if (hit.latitude is None or hit.longitude is None or not record.estimated_grid6
                        or record.callsign in managed or record.callsign==own):
                    continue
                if geographic and hit.geographic_country != ('' if geographic=='unknown' else geographic):
                    continue
                key=reference_key(record)
                summary=f'{record.callsign} · License reference\n{record.name} · {record.state}\n{record.estimated_grid6} · estimated from mailing postal code\nMay differ from self-reported location.'
                values.append(dict(id='license-reference:'+json.dumps(key,separators=(',',':')),kind='license_reference',type='license_reference',
                    reference_key=list(key),callsign=record.callsign,label='',lat=hit.latitude,lon=hit.longitude,color_role='text_muted',
                    tooltip=summary,summary=summary))
        renderer.apply_license_references(values[:200])
        if values and len(getattr(renderer,'_operational_markers',()))+len(values)>1200:
            self.panel.set_status('Some reference markers are hidden by the 1,200-item map limit. Narrow operational layers or reference scope.')

    def show(self,record):
        hit=next((hit for hit in self.hits if reference_key(hit.record)==reference_key(record)),None)
        if not hit or hit.latitude is None:
            self.panel.set_status('No qualified postal estimate is available for this reference.');return
        geographic=getattr(self.host,'_map_geographic_country','')
        if geographic and hit.geographic_country != ('' if geographic=='unknown' else geographic):
            self.panel.set_status('This reference is outside Country scope. Select All countries to plot it.');return
        renderer=getattr(self.host,'_native_map_renderer',None)
        if renderer is None:
            return
        managed=getattr(self.host,'operator_index',{})
        own=str(self.host.settings.get('operator_callsign','') or '').upper() if self.host.settings else ''
        if record.callsign in managed or record.callsign==own:
            from freqinout.gui.stations_map_tab import maidenhead_to_latlon
            row=managed.get(record.callsign,{})
            grid=row.get('grid6') or row.get('grid') or ''
            if record.callsign==own:
                grid=self.host.settings.get('operator_grid6','') or self.host.settings.get('operator_grid','') or grid
            point=maidenhead_to_latlon(grid) if grid else None
            self.panel.set_status('Already managed; the map uses the operational location, not this postal estimate.')
            if point:
                self.center(point[0],point[1],6)
            return
        self.panel.overlay_checkbox.setChecked(True)
        self.publish()
        self.center(hit.latitude,hit.longitude,6 if len(record.estimated_grid6)==4 else 9)

    def center(self,lat,lon,zoom):
        renderer=getattr(self.host,'_native_map_renderer',None)
        if renderer is not None:
            renderer.set_view_center(lat,lon,zoom)
            self.host._on_native_map_view_state_changed(dict(lat=lat,lon=lon,zoom=zoom))

    def select(self,payload):
        key=tuple(payload.get('reference_key') or ())
        hit=next((hit for hit in self.hits if reference_key(hit.record)==key),None)
        if not hit:
            return
        self.open_tools()
        for index,record in enumerate(self.panel._rows):
            if reference_key(record)==key:
                self.panel.table.selectRow(index)
                self.reveal(self.panel.details)
                break

    def reveal(self,widget):
        def apply():
            if self.active and self.host.isVisible() and self.host._map_control_requested_category=='Layers':
                self.host._controls_scroll.ensureWidgetVisible(widget,0,widget.fontMetrics().lineSpacing())
        QTimer.singleShot(0,apply)

    def add(self,record):
        window=self.host._application_window()
        review=getattr(window,'_review_fcc_operator',None) if window else None
        if callable(review):
            self.host._bring_application_window_forward()
            review(record)
        else:
            self.panel.set_status('Open Call Finder in the main window to review adding this reference to Operators.')
