"""Rebuildable Census ZCTA representative-point reference, worker I/O only.

Grids describe mailing-area estimates, never reported operating locations.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import os
import re
import sqlite3
import tempfile
import threading
from zipfile import ZipFile


SOURCE_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_zcta_national.zip"
MAX_BYTES = 16 * 1024 * 1024
MIN_ROWS = 20000
MAX_ROWS = 50000
LOOKUP_LIMIT = 256
ESTIMATED_GRID_DESCRIPTION = "Grid estimated from the FCC mailing ZIP. May differ from self-reported location."
US_MAIL_STATES = frozenset((
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI UM"
).split())
_LOCK = threading.RLock()
_SOURCE = re.compile(r"(20[0-9]{2})_Gaz_zcta_national\.(zip|txt)\Z", re.I | re.ASCII)
_HEADER = ("GEOID", "GEOIDFQ", "ALAND", "AWATER", "ALAND_SQMI", "AWATER_SQMI", "INTPTLAT", "INTPTLONG")


def _census_point_valid(lat, lon):
    # The actual national ZCTA file includes Pacific Island Areas despite the
    # Gazetteer page's general exclusion note. Keep all represented territories.
    return math.isfinite(lat) and math.isfinite(lon) and (
        (17 <= lat <= 73 and (-180 <= lon <= -60 or 170 <= lon <= 180))
        or (5 <= lat <= 25 and 130 <= lon <= 170)
        or (-20 <= lat <= -10 and -175 <= lon <= -165))


class ZipGridCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ZipGridStatus:
    rows: int
    vintage: str
    source_name: str
    source_url: str
    sha256: str
    refreshed_at: str


@dataclass(frozen=True)
class ZipGridPoint:
    zip5: str
    grid6: str
    latitude: float
    longitude: float


def normalize_zip(value) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"[0-9]{5}(?:-?[0-9]{4})?", raw, re.ASCII) and raw[:5] != "00000":
        return raw[:5]
    return ""


def grid6_from_latlon(latitude, longitude) -> str:
    lat, lon = float(latitude), float(longitude)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Coordinates are outside Maidenhead bounds")
    # 24 subsquares per square: longitude 1/12 degree, latitude 1/24.
    # Treat the north/east world boundaries as belonging to the final cell.
    x = min(4319, int(math.floor((lon + 180) * 12)))
    y = min(4319, int(math.floor((lat + 90) * 24)))
    return (chr(65 + x // 240) + chr(65 + y // 240) + str(x // 24 % 10)
            + str(y // 24 % 10) + chr(65 + x % 24) + chr(65 + y % 24))


class ZipGridStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def path(self):
        return self.root / "zip_grid.sqlite3"

    @contextmanager
    def _read(self):
        with _LOCK:
            if not self.path.is_file():
                yield None
                return
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("Unsupported ZIP grid cache; refresh ZIP grids")
                yield conn

    @staticmethod
    def _status(conn):
        payload = json.loads(conn.execute("SELECT payload FROM metadata").fetchone()[0])
        status = ZipGridStatus(**payload)
        if (not isinstance(status.rows, int) or not 1 <= status.rows <= MAX_ROWS
                or not re.fullmatch(r"20[0-9]{2}", status.vintage, re.ASCII)
                or not re.fullmatch(r"[0-9a-f]{64}", status.sha256, re.ASCII)):
            raise ValueError("Invalid ZIP grid metadata; refresh ZIP grids")
        datetime.fromisoformat(status.refreshed_at)
        return status

    def lookup_many(self, zip_codes):
        codes = {}
        for index, value in enumerate(zip_codes):
            if index >= LOOKUP_LIMIT:
                raise ValueError(f"ZIP lookup exceeds {LOOKUP_LIMIT} inputs")
            code = normalize_zip(value)
            if code:
                codes[code] = None
        with self._read() as conn:
            if conn is None:
                return None, {}
            status = self._status(conn)
            if not codes:
                return status, {}
            marks = ",".join("?" for _ in codes)
            points = {}
            for row in conn.execute(f"SELECT zip5,grid6,latitude,longitude FROM zcta WHERE zip5 IN ({marks})", tuple(codes)):
                point = ZipGridPoint(*row)
                if point.grid6 != grid6_from_latlon(point.latitude, point.longitude):
                    raise ValueError("Invalid cached ZIP grid; refresh ZIP grids")
                points[point.zip5] = point
            return status, points

    def status(self):
        return self.lookup_many(())[0]

    @staticmethod
    def _resolve(source: Path):
        if source.is_dir():
            candidates = [path for path in source.iterdir() if path.is_file() and _SOURCE.fullmatch(path.name)]
            if len(candidates) != 1:
                raise ValueError("Choose a folder with exactly one Census ZCTA national ZIP or text file")
            return candidates[0]
        if not _SOURCE.fullmatch(source.name):
            raise ValueError("Choose YYYY_Gaz_zcta_national.zip or its canonical .txt file")
        return source

    def refresh(self, source: Path, cancel=None, progress=None):
        cancel = cancel or threading.Event()
        progress = progress or (lambda _text: None)
        def check():
            if cancel.is_set():
                raise ZipGridCancelled("ZIP grid refresh cancelled; previous reference retained")
        check()
        source = self._resolve(Path(source))
        original_stat = source.stat()
        if original_stat.st_size > MAX_BYTES:
            raise ValueError("ZIP grid source exceeds 16 MiB")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(256 * 1024), b""):
                check()
                digest.update(block)
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="zip-grid-build-", suffix=".sqlite3", dir=self.root)
        os.close(descriptor)
        temporary = Path(name)
        conn = None
        try:
            conn = sqlite3.connect(temporary)
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("CREATE TABLE zcta(zip5 TEXT PRIMARY KEY,grid6 TEXT NOT NULL,latitude REAL NOT NULL,longitude REAL NOT NULL)")
            progress("ZIP grids: reading Census representative points")
            if source.suffix.lower() == ".zip":
                with ZipFile(source) as archive:
                    if len(archive.infolist()) > 16:
                        raise ValueError("Too many Census archive members")
                    members = [item for item in archive.infolist() if _SOURCE.fullmatch(item.filename)
                               and item.filename.lower().endswith(".txt")]
                    if len(members) != 1 or members[0].file_size > MAX_BYTES:
                        raise ValueError("Census ZIP must contain exactly one bounded canonical ZCTA text table")
                    member = members[0]
                    vintage = _SOURCE.fullmatch(member.filename).group(1)
                    if vintage != _SOURCE.fullmatch(source.name).group(1):
                        raise ValueError("Census archive and table vintages disagree")
                    with archive.open(member) as stream:
                        count = self._parse(stream, conn, check)
            else:
                vintage = _SOURCE.fullmatch(source.name).group(1)
                with source.open("rb") as stream:
                    count = self._parse(stream, conn, check)
            if count < MIN_ROWS:
                raise ValueError("Census national ZCTA source is incomplete")
            current_stat = source.stat()
            if (original_stat.st_dev, original_stat.st_ino, original_stat.st_size, original_stat.st_mtime_ns) != (
                    current_stat.st_dev, current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns):
                raise ValueError("Census source changed during refresh; retry with a stable file")
            try:
                previous = self.status()
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
                previous = None
            if previous and previous.rows >= 10000 and count < previous.rows * 0.8:
                raise ValueError("ZCTA count fell by more than 20%; previous reference retained")
            status = ZipGridStatus(count, vintage, source.name,
                                   SOURCE_URL if vintage == "2026" else "https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html",
                                   digest.hexdigest(), datetime.now(timezone.utc).isoformat(timespec="seconds"))
            conn.execute("CREATE TABLE metadata(payload TEXT NOT NULL)")
            conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps(status.__dict__),))
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("ZIP grid cache integrity check failed")
            conn.close()
            conn = None
            check()
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            with _LOCK:
                check()
                os.replace(temporary, self.path)
            return status
        except sqlite3.IntegrityError as exc:
            raise ValueError("Duplicate Census ZIP code; previous reference retained") from exc
        finally:
            if conn is not None:
                conn.close()
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _parse(stream, conn, check):
        raw = stream.readline(4097)
        total = len(raw)
        if len(raw) > 4096:
            raise ValueError("Census source line too long")
        header = raw.decode("utf-8-sig").rstrip("\r\n")
        delimiter = "|" if "|" in header else "\t"
        if tuple(part.strip() for part in header.split(delimiter)) != _HEADER:
            raise ValueError("Unsupported Census ZCTA header")
        count = 0
        while True:
            check()
            raw = stream.readline(4097)
            if not raw:
                break
            total += len(raw)
            if len(raw) > 4096 or total > MAX_BYTES:
                raise ValueError("Census source exceeds supported size")
            fields = [part.strip() for part in raw.decode("utf-8").rstrip("\r\n").split(delimiter)]
            if len(fields) != len(_HEADER) or not re.fullmatch(r"[0-9]{5}", fields[0], re.ASCII) or fields[0] == "00000":
                raise ValueError("Malformed Census ZIP row")
            if fields[1] != "860Z200US" + fields[0]:
                raise ValueError("Census geographic identifiers disagree")
            areas = [float(value) for value in fields[2:6]]
            lat, lon = float(fields[6]), float(fields[7])
            if (not all(math.isfinite(value) and value >= 0 for value in areas)
                    or not _census_point_valid(lat, lon)):
                raise ValueError("Invalid Census representative point or area")
            count += 1
            if count > MAX_ROWS:
                raise ValueError("Too many Census ZCTA rows")
            conn.execute("INSERT INTO zcta VALUES (?,?,?,?)", (fields[0], grid6_from_latlon(lat, lon), lat, lon))
        return count

    def download_refresh(self, cancel=None, progress=None):
        import requests
        cancel = cancel or threading.Event()
        progress = progress or (lambda _text: None)
        def check():
            if cancel.is_set():
                raise ZipGridCancelled("ZIP grid download cancelled; previous reference retained")
        check()
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="zip-grid-download-", dir=self.root) as directory:
            path = Path(directory) / "2026_Gaz_zcta_national.zip"
            progress("ZIP grids: contacting U.S. Census Bureau")
            with requests.get(SOURCE_URL, stream=True, timeout=(15, 20), allow_redirects=False,
                              headers={"User-Agent": "CommsPlan offline ZIP grid reference"}) as response:
                if response.status_code != 200:
                    raise ValueError(f"Census download returned HTTP {response.status_code}; import a local source or retry")
                length = response.headers.get("Content-Length", "")
                expected = int(length) if length.isdigit() else None
                if expected is not None and expected > MAX_BYTES:
                    raise ValueError("Census download exceeds 16 MiB")
                size = 0
                with path.open("wb") as output:
                    for block in response.iter_content(chunk_size=256 * 1024):
                        check()
                        size += len(block)
                        if size > MAX_BYTES:
                            raise ValueError("Census download exceeds 16 MiB")
                        output.write(block)
                if expected is not None and size != expected:
                    raise ValueError("Census download incomplete")
            check()
            return self.refresh(path, cancel, progress)
