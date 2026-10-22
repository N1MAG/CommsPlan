"""Independent GeoNames full Canadian postal points; worker I/O only.

A cell identifies an estimated mailing representative point, never a station fix.
No region/FSA guessing and no operational database writes.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from zipfile import ZipFile

from freqinout.core.zip_grid import grid6_from_latlon, LOOKUP_LIMIT, ZipGridCancelled

SOURCE_URL = "https://download.geonames.org/export/zip/CA_full.csv.zip"
SOURCE_NOTICE = "GeoNames Canadian postal codes — CC BY 4.0; https://www.geonames.org/; https://creativecommons.org/licenses/by/4.0/"
ESTIMATED_GRID_DESCRIPTION = "Grid estimated from the mailing postal code. May differ from self-reported location."
MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 96 * 1024 * 1024
MIN_ROWS, MAX_ROWS = 500000, 1500000
_LOCK = threading.RLock()
_POSTAL = re.compile(r"[ABCEGHJ-NPRSTVXY][0-9][ABCEGHJ-NPRSTVWXYZ] ?[0-9][ABCEGHJ-NPRSTVWXYZ][0-9]\Z", re.ASCII)
_PREFIX_PROVINCES = dict(A=("NL",), B=("NS",), C=("PE",), E=("NB",), G=("QC",), H=("QC",), J=("QC",),
                         K=("ON",), L=("ON",), M=("ON",), N=("ON",), P=("ON",), R=("MB",), S=("SK",),
                         T=("AB",), V=("BC",), X=("NT", "NU"), Y=("YT",))


def normalize_postal(value):
    raw = str(value or "").strip().upper()
    return raw.replace(" ", "") if _POSTAL.fullmatch(raw) else ""


@dataclass(frozen=True)
class CanadaPostalGridStatus:
    rows: int
    vintage: str  # Snapshot retrieval date, not a GeoNames survey/effective date.
    source_name: str
    source_url: str
    sha256: str
    refreshed_at: str


@dataclass(frozen=True)
class CanadaPostalGridPoint:
    postal_code: str
    province: str
    grid6: str  # Legacy display contract: 6, 4, or empty for ambiguous points.
    latitude: float
    longitude: float
    accuracy: int
    south: float
    west: float
    north: float
    east: float
    candidates: int


def _point_valid(postal, province, lat, lon, accuracy):
    return (bool(postal) and (province in _PREFIX_PROVINCES.get(postal[0], ())
                             or (province == "QC" and postal.startswith("K1A")))
            and math.isfinite(lat) and math.isfinite(lon)
            and 41 <= lat <= 84 and -142 <= lon <= -52 and accuracy in range(1, 7))


def grid_for_bounds(south, west, north, east):
    """Shared cell of all supplied points; no geographic coverage claim."""
    low, high = grid6_from_latlon(south, west), grid6_from_latlon(north, east)
    if low == high:
        return low
    return low[:4] if low[:4] == high[:4] else ""


class CanadaPostalGridStore:
    def __init__(self, root):
        self.root = Path(root)

    @property
    def path(self):
        return self.root / "canada_postal_grid.sqlite3"

    @contextmanager
    def _read(self):
        # Build is independent; serialize only publication/open/query/close for
        # cross-platform replacement while a reader has a handle open.
        with _LOCK:
            if not self.path.is_file():
                yield None
                return
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("Unsupported Canada postal-grid cache; refresh Canada postal grids")
                yield conn

    @staticmethod
    def _status(conn):
        status = CanadaPostalGridStatus(**json.loads(conn.execute("SELECT payload FROM metadata").fetchone()[0]))
        if (type(status.rows) is not int or not 1 <= status.rows <= MAX_ROWS
                or not re.fullmatch(r"[0-9a-f]{64}", status.sha256, re.ASCII)
                or status.source_url != SOURCE_URL):
            raise ValueError("Invalid Canada postal-grid metadata; refresh Canada postal grids")
        datetime.fromisoformat(status.refreshed_at)
        datetime.strptime(status.vintage, "%Y-%m-%d")
        return status

    def lookup_many(self, postal_codes):
        codes = {}
        for index, value in enumerate(postal_codes):
            if index >= LOOKUP_LIMIT:
                raise ValueError(f"Postal lookup exceeds {LOOKUP_LIMIT} inputs")
            code = normalize_postal(value)
            if code:
                codes[code] = None
        with self._read() as conn:
            if conn is None:
                return None, {}
            status = self._status(conn)
            points = {}
            if codes:
                marks = ",".join("?" for _ in codes)
                for row in conn.execute(f"SELECT postal_code,province,grid6,latitude,longitude,accuracy,south,west,north,east,candidates FROM postal WHERE postal_code IN ({marks})", tuple(codes)):
                    point = CanadaPostalGridPoint(*row)
                    if (not _point_valid(point.postal_code, point.province, point.latitude, point.longitude, point.accuracy)
                            or not (41 <= point.south <= point.latitude <= point.north <= 84
                                    and -142 <= point.west <= point.longitude <= point.east <= -52)
                            or type(point.candidates) is not int or not 1 <= point.candidates <= MAX_ROWS
                            or point.grid6 != grid_for_bounds(point.south, point.west, point.north, point.east)):
                        raise ValueError("Invalid cached Canada postal point; refresh Canada postal grids")
                    points[point.postal_code] = point
            return status, points

    def status(self):
        return self.lookup_many(())[0]

    @staticmethod
    def _resolve(source):
        source = Path(source)
        if source.is_dir():
            candidates = [p for p in source.iterdir() if p.is_file() and p.name.lower() in ("ca_full.csv.zip", "ca_full.txt")]
            if len(candidates) != 1:
                raise ValueError("Choose a folder with exactly one CA_full.csv.zip or CA_full.txt")
            source = candidates[0]
        if source.name.lower() not in ("ca_full.csv.zip", "ca_full.txt"):
            raise ValueError("Choose GeoNames CA_full.csv.zip or CA_full.txt (full postal codes)")
        return source

    def refresh(self, source, cancel=None, progress=None):
        cancel = cancel or threading.Event()
        progress = progress or (lambda _message: None)
        def check():
            if cancel.is_set():
                raise ZipGridCancelled("Canada postal-grid refresh cancelled; previous reference retained")
        check()
        source = self._resolve(source)
        before = source.stat()
        limit = MAX_BYTES if source.suffix.lower() == ".zip" else MAX_TEXT_BYTES
        if before.st_size > limit:
            raise ValueError("Canada postal-grid source exceeds supported size")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(256 * 1024), b""):
                check()
                digest.update(block)
        self.root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="canada-postal-build-", suffix=".sqlite3", dir=self.root)
        os.close(fd)
        temporary = Path(name)
        conn = None
        try:
            conn = sqlite3.connect(temporary)
            conn.executescript("PRAGMA synchronous=OFF; CREATE TABLE postal(postal_code TEXT PRIMARY KEY,province TEXT NOT NULL,grid6 TEXT NOT NULL,latitude REAL NOT NULL,longitude REAL NOT NULL,accuracy INTEGER NOT NULL,south REAL NOT NULL,west REAL NOT NULL,north REAL NOT NULL,east REAL NOT NULL,candidates INTEGER NOT NULL) WITHOUT ROWID;")
            conn.executescript("""CREATE TEMP TRIGGER postal_conflict BEFORE INSERT ON postal
              WHEN EXISTS (SELECT 1 FROM postal WHERE postal_code=NEW.postal_code AND
                (province!=NEW.province OR accuracy!=NEW.accuracy))
              BEGIN SELECT RAISE(ABORT, 'Conflicting Canadian postal province/accuracy attributes; previous reference retained'); END;""")
            progress("Canada postal grids: reading GeoNames full postal points")
            if source.suffix.lower() == ".zip":
                with ZipFile(source) as archive:
                    members = [i for i in archive.infolist() if i.filename.lower() == "ca_full.txt"]
                    if len(archive.infolist()) > 16 or len(members) != 1 or members[0].file_size > MAX_TEXT_BYTES:
                        raise ValueError("GeoNames archive must contain exactly one bounded CA_full.txt")
                    with archive.open(members[0]) as stream:
                        count = self._parse(stream, conn, check, progress)
            else:
                with source.open("rb") as stream:
                    count = self._parse(stream, conn, check, progress)
            if count < MIN_ROWS:
                raise ValueError("Canadian full postal source is incomplete")
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("Canadian postal source changed during refresh")
            try:
                previous = self.status()
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
                previous = None
            if previous and count < previous.rows * 0.8:
                raise ValueError("Canadian postal count fell by more than 20%; previous reference retained")
            now = datetime.now(timezone.utc)
            status = CanadaPostalGridStatus(count, now.date().isoformat(), source.name, SOURCE_URL,
                                           digest.hexdigest(), now.isoformat(timespec="seconds"))
            conn.execute("CREATE TABLE metadata(payload TEXT NOT NULL)")
            conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps(status.__dict__),))
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Canada postal-grid integrity check failed")
            conn.close()
            conn = None
            check()
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            with _LOCK:
                check()
                os.replace(temporary, self.path)
            return status
        finally:
            if conn is not None:
                conn.close()
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _parse(stream, conn, check, progress):
        count = total = 0
        batch = []
        def flush():
            conn.executemany("""INSERT INTO postal VALUES (?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(postal_code) DO UPDATE SET
                south=min(south,excluded.south), west=min(west,excluded.west),
                north=max(north,excluded.north), east=max(east,excluded.east),
                candidates=candidates+1""", batch)
            batch.clear()
        while True:
            check()
            raw = stream.readline(4097)
            if not raw:
                break
            total += len(raw)
            if len(raw) > 4096 or total > MAX_TEXT_BYTES:
                raise ValueError("Canadian postal text exceeds supported size")
            fields = raw.decode("utf-8-sig" if count == 0 else "utf-8").rstrip("\r\n").split("\t")
            if len(fields) != 12 or fields[0] != "CA":
                raise ValueError("Unsupported Canadian postal row; use full GeoNames source")
            postal, province = normalize_postal(fields[1]), fields[4]
            lat, lon, accuracy = float(fields[9]), float(fields[10]), int(fields[11])
            if not _point_valid(postal, province, lat, lon, accuracy):
                raise ValueError("Invalid Canadian postal code, province, coordinates or accuracy")
            count += 1
            if count > MAX_ROWS:
                raise ValueError("Too many Canadian postal rows")
            batch.append((postal, province, grid6_from_latlon(lat, lon), lat, lon, accuracy, lat, lon, lat, lon, 1))
            if len(batch) == 4096:
                flush()
                if count % 65536 == 0:
                    progress(f"Canada postal grids: processed {count:,} points")
        if batch:
            flush()
        # Only multiply-located codes need a second grid calculation. Keep the
        # full extent of supplied points; no arbitrary first/last-row winner.
        cursor = conn.execute("SELECT postal_code,south,west,north,east FROM postal WHERE south!=north OR west!=east")
        while True:
            check()
            rows = cursor.fetchmany(4096)
            if not rows:
                break
            updates = [(grid_for_bounds(s,w,n,e), (s+n)/2, (w+e)/2, code) for code,s,w,n,e in rows]
            conn.executemany("UPDATE postal SET grid6=?,latitude=?,longitude=? WHERE postal_code=?", updates)
        return conn.execute("SELECT count(*) FROM postal").fetchone()[0]

    def download_refresh(self, cancel=None, progress=None):
        import requests
        cancel = cancel or threading.Event()
        progress = progress or (lambda _message: None)
        deadline = time.monotonic() + 180
        def check():
            if cancel.is_set():
                raise ZipGridCancelled("Canada postal-grid download cancelled; previous reference retained")
            if time.monotonic() > deadline:
                raise ValueError("Canada postal-grid download timed out")
        check()
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="canada-postal-download-", dir=self.root) as directory:
            path = Path(directory) / "CA_full.csv.zip"
            progress("Canada postal grids: contacting GeoNames")
            with requests.get(SOURCE_URL, stream=True, timeout=(15, 20), allow_redirects=False,
                              headers={"User-Agent": "CommsPlan offline postal grid reference"}) as response:
                if response.status_code != 200:
                    raise ValueError(f"GeoNames returned HTTP {response.status_code}; import a local source or retry")
                length = response.headers.get("Content-Length", "")
                expected = int(length) if length.isdigit() else None
                if expected is not None and expected > MAX_BYTES:
                    raise ValueError("Canadian postal archive exceeds 16 MiB")
                size = 0
                with path.open("wb") as output:
                    for block in response.iter_content(256 * 1024):
                        check()
                        size += len(block)
                        if size > MAX_BYTES:
                            raise ValueError("Canadian postal archive exceeds 16 MiB")
                        output.write(block)
                if not size or (expected is not None and size != expected):
                    raise ValueError("Canadian postal archive download is incomplete")
            check()
            return self.refresh(path, cancel, progress)
