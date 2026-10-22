"""Disposable, provider-qualified postal map index. All I/O belongs on a worker.

This never writes a roster, log, check-in, location preference or source database.
Preparation is explicit; area queries cannot build or download anything.
"""
from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

from freqinout.core.call_finder import CallFinderStore, reference_key, SearchResult
from freqinout.core.geographic_scope import classify_point
from freqinout.core.zip_grid import ZipGridStore, ESTIMATED_GRID_DESCRIPTION
from freqinout.core.canada_postal_grid import CanadaPostalGridStore

MAX_CELLS, MAX_CANDIDATES, MAX_RESULTS = 256, 2048, 200
_PROVIDERS = {'Amateur': ('amateur.sqlite3','zip_grid.sqlite3','US'),
              'GMRS': ('gmrs.sqlite3','zip_grid.sqlite3','US'),
              'Canada': ('ised_amateur.sqlite3','canada_postal_grid.sqlite3','CA')}


@dataclass(frozen=True)
class ReferenceHit:
    record: object
    latitude: float | None = None
    longitude: float | None = None
    geographic_country: str = ''


@dataclass(frozen=True)
class ReferenceResult:
    hits: tuple = ()
    has_more: bool = False
    message: str = ''
    source_revision: tuple = ()
    viewport_revision: int = 0


@dataclass(frozen=True)
class MapBounds:
    west: float
    south: float
    east: float
    north: float
    full_world: bool = False

    def intervals(self):
        values=(self.west,self.south,self.east,self.north)
        if not all(math.isfinite(x) for x in values) or not (-180 <= self.west <= 180 and -180 <= self.east <= 180 and -85.052 <= self.south < self.north <= 85.052):
            raise ValueError('Map area is not valid; wait for the map to settle')
        if self.full_world:
            raise ValueError('Zoom in before searching this area (maximum 256 map cells)')
        return ((self.west,self.east),) if self.west < self.east else ((self.west,180),(-180,self.east))

    def cells(self):
        cells=[]
        for west,east in self.intervals():
            xs=range(min(179,math.floor(west)),min(179,math.floor(east))+1)
            ys=range(math.floor(self.south),math.floor(self.north)+1)
            if len(cells)+len(xs)*len(ys) > MAX_CELLS:
                raise ValueError('Zoom in before searching this area (maximum 256 map cells)')
            cells.extend((y+90)*360+x+180 for y in ys for x in xs)
        return tuple(dict.fromkeys(cells))

    def contains(self, lat, lon):
        return self.south <= lat <= self.north and any(w <= lon <= e for w,e in self.intervals())


def _ro(path):
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0.25)
    conn.row_factory=sqlite3.Row
    return conn


def _fingerprint(path):
    if not path.is_file():
        return None
    st=path.stat()
    with closing(_ro(path)) as conn:
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        meta=conn.execute('SELECT payload FROM metadata').fetchone()[0]
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, version, hashlib.sha256(meta.encode()).hexdigest())


class LicenseMapReferenceStore:
    def __init__(self, root=None):
        self.calls=CallFinderStore(root)

    @property
    def root(self):
        return self.calls.root

    def services(self,country='',service='',**_kwargs):
        return tuple(s for s,(_,_,c) in _PROVIDERS.items() if (not country or c==country)
                     and (not service or ('Amateur' if s=='Canada' else s)==service))

    def revision(self,services):
        return tuple((s,_fingerprint(self.root/_PROVIDERS[s][0]),_fingerprint(self.root/_PROVIDERS[s][1])) for s in services)

    def _index(self,service):
        return self.root/('map_reference_'+service.lower()+'.sqlite3')

    def _points(self,records):
        us=[r.mail_zip for r in records if r.provider!='ised_amateur' and r.mail_zip and not r.zip_reason]
        ca=[r.postal_code for r in records if r.provider=='ised_amateur' and r.postal_code]
        _,uspoints=ZipGridStore(self.root).lookup_many(us) if us else (None,{})
        _,capoints=CanadaPostalGridStore(self.root).lookup_many(ca) if ca else (None,{})
        output={}
        from freqinout.core.canada_postal_grid import normalize_postal
        for r in records:
            point=capoints.get(normalize_postal(r.postal_code)) if r.provider=='ised_amateur' else uspoints.get(r.mail_zip)
            if point and point.grid6 and (r.provider!='ised_amateur' or not r.state or r.state==point.province):
                output[reference_key(r)]=point
        return output

    def build(self, *, country='',service='',cancel=None,progress=None,**_kwargs):
        cancel=cancel or threading.Event()
        progress=progress or (lambda _text:None)
        services=self.services(country,service)
        self.root.mkdir(parents=True,exist_ok=True)
        messages=[]
        for svc in services:
            before=self.revision((svc,))
            if not all(before[0][1:]):
                messages.append(f'{svc}: load licenses and postal grids in Call Finder Sources first')
                continue
            fd,name=tempfile.mkstemp(prefix='map-reference-',suffix='.sqlite3',dir=self.root)
            os.close(fd)
            staging=Path(name)
            try:
                with closing(sqlite3.connect(staging)) as out:
                    out.executescript('PRAGMA synchronous=OFF; PRAGMA user_version=1; CREATE TABLE reference(refkey TEXT PRIMARY KEY,callsign TEXT,region TEXT,grid TEXT,latitude REAL,longitude REAL,geo_country TEXT,cell INTEGER) WITHOUT ROWID; CREATE TABLE metadata(payload TEXT NOT NULL);')
                    last,count,seen='',0,0
                    grid_source='GeoNames full Canadian postal representative points (CC BY 4.0)' if svc=='Canada' else 'U.S. Census Bureau ZCTA representative point'
                    grid_vintage=''
                    # Each bounded batch releases source handles/locks. A source
                    # replacement during the build makes final publication fail.
                    while True:
                        if cancel.is_set():
                            raise InterruptedError('Map index build cancelled; previous index retained')
                        with self.calls._read(svc) as conn:
                            if conn is None:
                                raise ValueError('Source changed during map index build')
                            meta=self.calls._status(conn)
                            raw_rows=conn.execute('SELECT * FROM licensee WHERE callsign>? ORDER BY callsign LIMIT 256',(last,)).fetchall()
                        records=tuple(self.calls._record(row,meta) for row in raw_rows)
                        if not records:
                            break
                        last=records[-1].callsign
                        seen+=len(records)
                        points=self._points(records)
                        batch=[]
                        for record_index,record in enumerate(records):
                            if record_index % 32 == 0:
                                time.sleep(0)  # Frequent GIL yield for interactive identity/UI work.
                            point=points.get(reference_key(record))
                            if not point:
                                continue
                            lat,lon=point.latitude,point.longitude
                            if not (-85.05112878 <= lat <= 85.05112878 and -180 <= lon <= 180):
                                continue
                            key=json.dumps(reference_key(record),separators=(',',':'))
                            cell=(math.floor(lat)+90)*360+min(359,math.floor(lon)+180)
                            batch.append((key,record.callsign,record.state,point.grid6,lat,lon,classify_point(lat,lon),cell))
                        out.executemany('INSERT INTO reference VALUES(?,?,?,?,?,?,?,?)',batch)
                        count+=len(batch)
                        if seen%8192 < 256:
                            progress(f'{svc}: prepared {count:,} map locations from {seen:,} records')
                        time.sleep(0.001)  # Yield between batches; NCS has its own lane.
                    out.execute('CREATE INDEX reference_cells ON reference(cell,latitude,longitude)')
                    out.execute('CREATE INDEX reference_country_cells ON reference(geo_country,cell,latitude,longitude)')
                    out.execute('CREATE INDEX reference_region_cells ON reference(region,cell,latitude,longitude)')
                    grid_store=CanadaPostalGridStore(self.root) if svc=='Canada' else ZipGridStore(self.root)
                    status,_=grid_store.lookup_many(())
                    grid_vintage=status.vintage if status else ''
                    metadata=dict(revision=before,rows=count,service=svc,grid_source=grid_source,grid_vintage=grid_vintage)
                    out.execute('INSERT INTO metadata VALUES(?)',(json.dumps(metadata),))
                    out.commit()
                if cancel.is_set():
                    raise InterruptedError('Map index build cancelled; previous index retained')
                if self.revision((svc,)) != before:
                    raise ValueError('Reference source changed during build; rebuild the map index')
                os.replace(staging,self._index(svc))
                messages.append(f'{svc}: {count:,} estimated locations ready')
            finally:
                staging.unlink(missing_ok=True)
        return ReferenceResult(message=' · '.join(messages) or 'No compatible source selected',source_revision=self.revision(services))

    def callsign(self,query, *, country='',service='',state='',geographic_country='',**_kwargs):
        services=self.services(country,service)
        before=self.revision(services)
        result=self.calls.search(query,service,state,limit=50,country=country)
        records=result.rows
        points=self._points(records)
        hits=[]
        for record in records:
            point=points.get(reference_key(record))
            geo=classify_point(point.latitude,point.longitude) if point else ''
            hit=ReferenceHit(record,point.latitude if point else None,point.longitude if point else None,geo)
            # Identity search keeps unlocated records visible. Geographic scope
            # affects plotting, never fabricates an operating location.
            hits.append(hit)
        if before != self.revision(services):
            raise ValueError('Reference source changed during search; search again')
        return ReferenceResult(tuple(hits),result.has_more,' · '.join(result.errors),before)

    def area(self,bounds, *, country='',service='',state='',geographic_country='',viewport_revision=0,**_kwargs):
        if not isinstance(bounds,MapBounds):
            raise ValueError('Wait for a settled map area')
        cells=bounds.cells()
        services=self.services(country,service)
        before=self.revision(services)
        candidates=[]
        messages=[]
        for svc in services:
            revision=next(r for r in before if r[0]==svc)
            if not all(revision[1:]):
                messages.append(f'{svc}: source or postal grids missing')
                continue
            path=self._index(svc)
            if not path.is_file():
                messages.append(f'{svc}: Prepare map references first')
                continue
            with closing(_ro(path)) as conn:
                metadata=json.loads(conn.execute('SELECT payload FROM metadata').fetchone()[0])
                if metadata.get('revision') != json.loads(json.dumps((revision,))):
                    messages.append(f'{svc}: map references are stale; prepare them again')
                    continue
                # Mandatory portable cell/bbox B-tree. No national COUNT or join.
                marks=','.join('?' for _ in cells)
                intervals=bounds.intervals()
                lon_sql=' OR '.join('(longitude>=? AND longitude<=?)' for _ in intervals)
                sql=f'SELECT * FROM reference WHERE cell IN ({marks}) AND latitude>=? AND latitude<=? AND ({lon_sql})'
                args=[*cells,bounds.south,bounds.north,*[v for interval in intervals for v in interval]]
                if state:
                    sql+=' AND region=?';args.append(state.upper())
                if geographic_country:
                    sql+=' AND geo_country=?';args.append('' if geographic_country=='unknown' else geographic_country)
                sql+=' LIMIT ?';args.append(MAX_CANDIDATES+1-len(candidates))
                steps=[0]
                def budget():
                    steps[0]+=1000
                    return steps[0]>250000
                conn.set_progress_handler(budget,1000)
                try:
                    rows=conn.execute(sql,args).fetchall()
                except sqlite3.OperationalError as exc:
                    if 'interrupted' in str(exc):
                        raise ValueError('This area is too busy; zoom in or narrow license country/service') from exc
                    raise
                candidates.extend((svc,dict(row),metadata) for row in rows)
                if len(candidates)>MAX_CANDIDATES:
                    raise ValueError('This area has more than 2,048 candidates; zoom in or narrow license country/service')
        if geographic_country:
            expected='' if geographic_country=='unknown' else geographic_country
            candidates=[c for c in candidates if c[1]['geo_country']==expected]
        center_lon=(bounds.west+bounds.east)/2 if bounds.west<bounds.east else ((bounds.west+bounds.east+360)/2+180)%360-180
        center_lat=(bounds.south+bounds.north)/2
        def distance(item):
            row=item[1]
            delta=(row['longitude']-center_lon+180)%360-180
            return ((row['latitude']-center_lat)**2+(delta*math.cos(math.radians(center_lat)))**2,row['refkey'])
        candidates.sort(key=distance)
        selected=candidates[:MAX_RESULTS]
        hits=[]
        for svc in services:
            selected_svc=[c for c in selected if c[0]==svc]
            if not selected_svc:
                continue
            with self.calls._read(svc) as conn:
                if conn is None:
                    raise ValueError('Source changed; search again')
                meta=self.calls._status(conn)
                marks=','.join('?' for _ in selected_svc)
                records={r['callsign']:self.calls._record(r,meta) for r in conn.execute(f'SELECT * FROM licensee WHERE callsign IN ({marks})',tuple(c[1]['callsign'] for c in selected_svc))}
            for _,row,index_meta in selected_svc:
                record=records.get(row['callsign'])
                if record is None or list(reference_key(record)) != json.loads(row['refkey']):
                    raise ValueError('Source identity changed; rebuild map index')
                record=replace(record,estimated_grid6=row['grid'],grid_source=index_meta['grid_source'],grid_vintage=index_meta['grid_vintage'],grid_reason='Grid estimated from the mailing postal code. May differ from self-reported location.')
                hits.append(ReferenceHit(record,row['latitude'],row['longitude'],row['geo_country']))
        rank={c[1]['refkey']:i for i,c in enumerate(selected)}
        hits.sort(key=lambda h:rank[json.dumps(reference_key(h.record),separators=(',',':'))])
        if before != self.revision(services):
            raise ValueError('Source changed during area search; search again')
        return ReferenceResult(tuple(hits),len(candidates)>MAX_RESULTS,' · '.join(messages),before,viewport_revision)
