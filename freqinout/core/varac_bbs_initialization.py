from __future__ import annotations

"""Safe, station-scoped initialization for the Managed BBS library."""

from dataclasses import dataclass
import shutil
from pathlib import Path

from freqinout.core.sqlite_utils import connect_sqlite
from freqinout.core.varac_bbs_library_store import (
    list_bbs_locations,
    sync_bbs_location_from_folder,
    upsert_bbs_location,
)
from freqinout.core.varac_bbs_vault import (
    DEFAULT_LOCATION_ID,
    DEFAULT_LOCATION_NAME,
    compute_default_managed_root,
    initialize_managed_root,
)


@dataclass(frozen=True)
class BbsInitializationResult:
    managed_root: str
    default_location_dir: str
    imported_files: int
    cataloged_files: int
    reused: bool = False


def initialize_station_bbs_library(
    db_path: str | Path,
    live_bbs_dir: str | Path,
    *,
    import_existing_files: bool = True,
) -> BbsInitializationResult:
    """Create/reuse the station library without replacing the live BBS folder.

    Existing files are copied only when explicitly requested.  Repeating this
    operation is safe: the managed folder is reused and catalog metadata is
    updated transactionally after filesystem preparation succeeds.
    """

    live_dir = Path(live_bbs_dir).expanduser()
    if not live_dir.exists() or not live_dir.is_dir():
        raise ValueError("The selected VarAC live BBS folder does not exist or is not a folder.")
    with connect_sqlite(Path(db_path)) as conn:
        locations = list_bbs_locations(conn, include_disabled=True)
        meta = dict(conn.execute("SELECT key, value FROM bbs_library_meta").fetchall())
        station_root = str(meta.get("station_managed_root") or "").strip()
        if station_root or locations:
            default_id = str(meta.get("station_default_location_id") or DEFAULT_LOCATION_ID)
            default = next((row for row in locations if row.location_id == default_id), None)
            # A second adapter attaches to the station catalog. Never rebase
            # its locations, reset its access rules or import another live view.
            if not station_root:
                raise ValueError("The station BBS already has locations but no library root. Repair the station library before attaching another radio.")
            if default is None or not str(default.source_dir or "").strip():
                raise ValueError("The station BBS library has no valid Default location. Repair the station library before attaching another radio.")
            return BbsInitializationResult(station_root, default.source_dir, 0, 0, reused=True)
    managed_root = compute_default_managed_root(live_dir)
    if not managed_root:
        raise ValueError("A Managed BBS library location could not be derived from the live BBS folder.")
    paths = initialize_managed_root(managed_root)
    default_dir = paths["default"]
    imported = 0
    if import_existing_files:
        target_dir = Path(default_dir)
        for source in sorted(live_dir.iterdir(), key=lambda item: item.name.lower()):
            if not source.is_file():
                continue
            target = target_dir / source.name
            if target.exists():
                continue
            shutil.copy2(source, target)
            imported += 1
    with connect_sqlite(Path(db_path)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # Filesystem preparation can overlap another administration window.
            # Recheck under the write lock before changing any catalog policy.
            if (conn.execute("SELECT 1 FROM bbs_locations LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM bbs_library_meta WHERE key='station_managed_root' AND value<>'' LIMIT 1").fetchone()):
                raise ValueError("The station BBS catalog changed during initialization. Refresh and attach this radio to the existing library.")
            upsert_bbs_location(
                conn,
                location_id=DEFAULT_LOCATION_ID,
                name=DEFAULT_LOCATION_NAME,
                source_dir=default_dir,
                enabled=True,
                access_rule="public",
                retention_mode="global_default",
                metadata={"initialized_from_live_bbs": str(live_dir)},
            )
            cataloged = sync_bbs_location_from_folder(
                conn,
                location_id=DEFAULT_LOCATION_ID,
                name=DEFAULT_LOCATION_NAME,
                source_dir=default_dir,
                enabled=True,
                metadata={
                    "access_rule": "public",
                    "retention_mode": "global_default",
                    "initialized_from_live_bbs": str(live_dir),
                },
            )
            for key, value in (
                ("station_enabled", "1"),
                ("station_managed_root", str(managed_root)),
                ("station_default_location_id", DEFAULT_LOCATION_ID),
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO bbs_library_meta(key, value) VALUES(?, ?)",
                    (key, value),
                )
    return BbsInitializationResult(
        managed_root=str(managed_root),
        default_location_dir=str(default_dir),
        imported_files=int(imported),
        cataloged_files=int(cataloged),
    )
