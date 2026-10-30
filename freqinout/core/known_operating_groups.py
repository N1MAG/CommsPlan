from __future__ import annotations

import json
from itertools import chain
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from freqinout.core.config_paths import get_config_dir
from freqinout.core.mode_utils import normalize_operating_group_mode
from freqinout.core.operating_group_frequency import normalize_frequency_hz
from freqinout.core.resource_catalog_migration import (
    read_legacy_compatibility_rows,
    resource_catalog_authority_state,
)


REMOVED_KNOWN_GROUPS = {"AHRN", "RATPACK"}

BUILTIN_STANDARD_GROUPS: tuple[Dict[str, Any], ...] = (
    {
        "group": "JS8CALL STANDARD",
        "configs": [
            {"band": "160M", "mode": "Digi", "frequency": "1.842", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "80M", "mode": "Digi", "frequency": "3.578", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "40M", "mode": "Digi", "frequency": "7.078", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "30M", "mode": "Digi", "frequency": "10.130", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "20M", "mode": "Digi", "frequency": "14.078", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "17M", "mode": "Digi", "frequency": "18.104", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "15M", "mode": "Digi", "frequency": "21.078", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "12M", "mode": "Digi", "frequency": "24.922", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "10M", "mode": "Digi", "frequency": "28.078", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "6M", "mode": "Digi", "frequency": "50.318", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "2M", "mode": "Digi", "frequency": "144.178", "fldigi_mode": "", "fldigi_offset": ""},
        ],
        "resource_sets": ["Built-in Standards"],
        "net_names": ["JS8Call default calling frequencies"],
        "status": "standard",
        "source_note": "Built-in conventional JS8Call defaults.",
    },
    {
        "group": "VARAC STANDARD",
        "configs": [
            {"band": "160M", "mode": "Digi", "frequency": "1.995", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "80M", "mode": "Digi", "frequency": "3.595", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "40M", "mode": "Digi", "frequency": "7.105", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "30M", "mode": "Digi", "frequency": "10.133", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "20M", "mode": "Digi", "frequency": "14.105", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "17M", "mode": "Digi", "frequency": "18.107", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "15M", "mode": "Digi", "frequency": "21.105", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "12M", "mode": "Digi", "frequency": "24.927", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "10M", "mode": "Digi", "frequency": "28.105", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "6M", "mode": "Digi", "frequency": "50.330", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "2M", "mode": "Digi", "frequency": "144.170", "fldigi_mode": "", "fldigi_offset": ""},
        ],
        "resource_sets": ["Built-in Standards"],
        "net_names": ["VarAC USB calling frequencies"],
        "status": "standard",
        "source_note": "VarAC USB calling frequencies; regional and FM channels excluded.",
    },
    {
        "group": "FT8 STANDARD",
        "configs": [
            {"band": "160M", "mode": "Digi", "frequency": "1.840", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "80M", "mode": "Digi", "frequency": "3.573", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "60M", "mode": "Digi", "frequency": "5.357", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "40M", "mode": "Digi", "frequency": "7.074", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "30M", "mode": "Digi", "frequency": "10.136", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "20M", "mode": "Digi", "frequency": "14.074", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "17M", "mode": "Digi", "frequency": "18.100", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "15M", "mode": "Digi", "frequency": "21.074", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "12M", "mode": "Digi", "frequency": "24.915", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "10M", "mode": "Digi", "frequency": "28.074", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "6M", "mode": "Digi", "frequency": "50.313", "fldigi_mode": "", "fldigi_offset": ""},
        ],
        "resource_sets": ["Built-in Standards"],
        "net_names": ["WSJT-X FT8 working frequencies"],
        "status": "standard",
        "source_note": "Built-in conventional WSJT-X FT8 defaults.",
    },
    {
        "group": "WSPR STANDARD",
        "configs": [
            {"band": "160M", "mode": "Digi", "frequency": "1.836600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "80M", "mode": "Digi", "frequency": "3.568600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "60M", "mode": "Digi", "frequency": "5.364700", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "40M", "mode": "Digi", "frequency": "7.038600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "30M", "mode": "Digi", "frequency": "10.138700", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "20M", "mode": "Digi", "frequency": "14.095600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "17M", "mode": "Digi", "frequency": "18.104600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "15M", "mode": "Digi", "frequency": "21.094600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "12M", "mode": "Digi", "frequency": "24.924600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "10M", "mode": "Digi", "frequency": "28.124600", "fldigi_mode": "", "fldigi_offset": ""},
            {"band": "6M", "mode": "Digi", "frequency": "50.293000", "fldigi_mode": "", "fldigi_offset": ""},
        ],
        "resource_sets": ["Built-in Standards"],
        "net_names": ["WSJT-X WSPR working frequencies"],
        "status": "standard",
        "source_note": "Built-in conventional WSJT-X WSPR defaults.",
    },
)

# Supplemental delivered choices are independent of seasonal net membership.
# Resource rows are processed first so their overlapping modem metadata wins.
DELIVERED_GROUP_FREQUENCIES = {
    "MAGNET": (("20M", "14.115"), ("40M", "7.115"), ("80M", "3.585"),
               ("30M", "10.115"), ("10M", "28.115")),
    "AMRRON": (("20M", "14.110"), ("40M", "7.110"), ("80M", "3.588")),
}


def _delivered_group_rows():
    for group, frequencies in DELIVERED_GROUP_FREQUENCIES.items():
        for band, frequency in frequencies:
            yield {"group_name": group, "band": band, "mode": "Digi", "frequency": frequency,
                   "fldigi_mode": "Cont-4/250", "fldigi_offset": "900", "source_type": "builtin",
                   "source_ref": "delivered-group-presets", "resource_set": "Built-in Presets"}


WEFAX_STATIONS: tuple[Dict[str, Any], ...] = (
    {
        "name": "Boston",
        "call": "NMF",
        "lat": 41.7,
        "lon": -70.5,
        "assigned_khz": (4235.0, 6340.5, 9110.0, 12750.0),
        "states": {"CT", "DE", "MA", "MD", "ME", "NH", "NJ", "NY", "PA", "RI", "VA", "VT", "WV"},
    },
    {
        "name": "New Orleans",
        "call": "NMG",
        "lat": 29.9,
        "lon": -90.1,
        "assigned_khz": (4317.9, 8503.9, 12789.9, 17146.4),
        "states": {"AL", "AR", "FL", "GA", "IA", "IL", "IN", "KS", "KY", "LA", "MI", "MN", "MO", "MS", "NC", "ND", "NE", "OH", "OK", "SC", "SD", "TN", "TX", "WI"},
    },
    {
        "name": "Pt. Reyes",
        "call": "NMC",
        "lat": 38.1,
        "lon": -122.8,
        "assigned_khz": (4346.0, 8682.0, 12786.0, 17151.2, 22527.0),
        "states": {"AZ", "CA", "CO", "ID", "MT", "NM", "NV", "OR", "UT", "WA", "WY"},
    },
    {
        "name": "Kodiak",
        "call": "NOJ",
        "lat": 57.8,
        "lon": -152.4,
        "assigned_khz": (2054.0, 4298.0, 8459.0, 12410.6),
        "states": {"AK"},
    },
    {
        "name": "Honolulu",
        "call": "KVM70",
        "lat": 21.3,
        "lon": -157.9,
        "assigned_khz": (9982.5, 11090.0, 16135.0),
        "states": {"HI"},
    },
)


def net_resources_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout_nets.db"


def bundled_net_resource_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "net_resources"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _maidenhead_to_latlon(grid: str) -> tuple[float, float] | None:
    grid = (grid or "").strip().upper()
    if len(grid) < 4:
        return None
    try:
        lon = (ord(grid[0]) - ord("A")) * 20.0 + int(grid[2]) * 2.0 + 1.0 / 24.0
        lat = (ord(grid[1]) - ord("A")) * 10.0 + int(grid[3]) * 1.0 + 1.0 / 48.0
        if len(grid) >= 6:
            lon += (ord(grid[4]) - ord("A")) / 12.0
            lat += (ord(grid[5]) - ord("A")) / 24.0
        lon -= 180.0
        lat -= 90.0
    except Exception:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _select_wefax_station(
    *,
    station_grid6: str = "",
    station_state: str = "",
    timezone_name: str = "",
    station_call_override: str = "",
) -> tuple[Dict[str, Any], str]:
    override = _clean_text(station_call_override).upper()
    if override:
        for station in WEFAX_STATIONS:
            if str(station.get("call", "") or "").upper() == override:
                return station, "selected by station override"

    latlon = _maidenhead_to_latlon(station_grid6)
    if latlon is not None:
        lat, lon = latlon
        station = min(
            WEFAX_STATIONS,
            key=lambda item: _haversine_km(lat, lon, float(item["lat"]), float(item["lon"])),
        )
        return station, f"selected from station grid {station_grid6.strip().upper()}"

    state = _clean_text(station_state).upper()
    if state:
        for station in WEFAX_STATIONS:
            states = station.get("states", set())
            if isinstance(states, set) and state in states:
                return station, f"selected from station state {state}"

    tz = _clean_text(timezone_name)
    if "Alaska" in tz:
        return WEFAX_STATIONS[3], f"selected from station timezone {tz}"
    if "Hawaii" in tz or "Honolulu" in tz:
        return WEFAX_STATIONS[4], f"selected from station timezone {tz}"
    if "Pacific" in tz or "Mountain" in tz or "Denver" in tz or "Los_Angeles" in tz:
        return WEFAX_STATIONS[2], f"selected from station timezone {tz}"
    if "Central" in tz or "Chicago" in tz:
        return WEFAX_STATIONS[1], f"selected from station timezone {tz}"
    if "Eastern" in tz or "New_York" in tz:
        return WEFAX_STATIONS[0], f"selected from station timezone {tz}"

    return WEFAX_STATIONS[2], "selected as the default western U.S. HF radiofax station"


def _wefax_band_label(freq_mhz: float) -> str:
    return f"{max(1, int(round(freq_mhz)))}MHZ"


def _wefax_station_entry(
    *,
    station_grid6: str = "",
    station_state: str = "",
    timezone_name: str = "",
    station_call_override: str = "",
) -> Dict[str, Any]:
    station, reason = _select_wefax_station(
        station_grid6=station_grid6,
        station_state=station_state,
        timezone_name=timezone_name,
        station_call_override=station_call_override,
    )
    configs: List[Dict[str, Any]] = []
    for assigned_khz in station["assigned_khz"]:
        carrier_mhz = (float(assigned_khz) - 1.9) / 1000.0
        configs.append(
            {
                "band": _wefax_band_label(carrier_mhz),
                "mode": "SSB",
                "frequency": f"{carrier_mhz:.6f}",
                "vfo": "A",
                "fldigi_mode": "WEFAX576",
                "fldigi_offset": "",
                "auto_tune": False,
                "use_condition_levels": False,
                "condition_level": 5,
                "source_type": "builtin",
                "source_ref": "NOAA/NWS Marine Radiofax",
                "resource_set": "Built-in Standards",
                "resource_id": None,
                "net_name": f"{station['call']} HF Radiofax",
            }
        )
    return {
        "group": f"FLDIGI WEFAX {station['call']}",
        "configs": configs,
        "resource_sets": ["Built-in Standards"],
        "net_names": [f"{station['name']} {station['call']} HF Radiofax"],
        "status": "standard",
        "source_note": (
            f"Built-in NOAA/NWS HF radiofax preset for {station['name']} ({station['call']}), {reason}. "
            "USB radio dial frequencies are 1.9 kHz below assigned frequencies; FLDigi starts in WEFAX576."
        ),
    }


def _resource_rows_from_db(db_path: Path | None = None) -> List[Dict[str, Any]]:
    path = Path(db_path) if db_path is not None else net_resources_db_path()
    if not path.exists():
        return []
    if resource_catalog_authority_state(path) == "canonical":
        rows = read_legacy_compatibility_rows(path)
        return [
            {
                "resource_id": int(row.get("id") or 0) or None,
                "resource_set": _clean_text(row.get("resource_set")) or "Custom",
                "source_type": _clean_text(row.get("source_type")) or "net_resource",
                "source_ref": _clean_text(row.get("source_ref")),
                "group_name": _clean_text(row.get("group_name")),
                "band": _clean_text(row.get("band")).upper(),
                "mode": _clean_text(row.get("mode")),
                "frequency": _clean_text(row.get("frequency")),
                "net_name": _clean_text(row.get("net_name")),
                "fldigi_mode": _clean_text(row.get("fldigi_mode")),
                "fldigi_offset": _clean_text(row.get("fldigi_offset")),
            }
            for row in rows
        ]
    conn = sqlite3.connect(path)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='net_resources'"
        ).fetchone()
        if not exists:
            return []
        cur = conn.execute(
            """
            SELECT id, resource_set, source_type, source_ref, group_name, band, mode, frequency,
                   net_name, fldigi_mode, fldigi_offset
              FROM net_resources
            """
        )
        return [
            {
                "resource_id": int(row[0]),
                "resource_set": _clean_text(row[1]) or "Custom",
                "source_type": _clean_text(row[2]) or "net_resource",
                "source_ref": _clean_text(row[3]),
                "group_name": _clean_text(row[4]),
                "band": _clean_text(row[5]).upper(),
                "mode": _clean_text(row[6]),
                "frequency": _clean_text(row[7]),
                "net_name": _clean_text(row[8]),
                "fldigi_mode": _clean_text(row[9]),
                "fldigi_offset": _clean_text(row[10]),
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def _resource_rows_from_bundled_json(resource_dir: Path | None = None) -> List[Dict[str, Any]]:
    root = Path(resource_dir) if resource_dir is not None else bundled_net_resource_dir()
    rows: List[Dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("sitrepnets-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        resource_set = _clean_text(payload.get("resource_set")) if isinstance(payload, dict) else ""
        for row in payload.get("rows", []) if isinstance(payload, dict) else []:
            if not isinstance(row, Mapping):
                continue
            rows.append(
                {
                    "resource_id": None,
                    "resource_set": resource_set or path.stem.replace("sitrepnets-", "").title(),
                    "source_type": "builtin",
                    "source_ref": path.name,
                    "group_name": _clean_text(row.get("group_name")),
                    "band": _clean_text(row.get("band")).upper(),
                    "mode": _clean_text(row.get("mode")),
                    "frequency": _clean_text(row.get("frequency")),
                    "net_name": _clean_text(row.get("net_name")),
                    "fldigi_mode": _clean_text(row.get("fldigi_mode")),
                    "fldigi_offset": _clean_text(row.get("fldigi_offset")),
                }
            )
    return rows


def _normalize_catalog_config(row: Mapping[str, Any]) -> Dict[str, Any] | None:
    group = _clean_text(row.get("group_name")).upper()
    band = _clean_text(row.get("band")).upper()
    mode = normalize_operating_group_mode(row.get("mode", ""), band)
    frequency = _clean_text(row.get("frequency"))
    if not (group and band and mode and frequency):
        return None
    return {
        "group": group,
        "band": band,
        "mode": mode,
        "frequency": frequency,
        "vfo": "A",
        "fldigi_mode": _clean_text(row.get("fldigi_mode")),
        "fldigi_offset": _clean_text(row.get("fldigi_offset")),
        "auto_tune": False,
        "use_condition_levels": False,
        "condition_level": 5,
        "source_type": _clean_text(row.get("source_type")) or "net_resource",
        "source_ref": _clean_text(row.get("source_ref")),
        "resource_set": _clean_text(row.get("resource_set")) or "Custom",
        "resource_id": row.get("resource_id"),
        "net_name": _clean_text(row.get("net_name")),
    }


def build_known_operating_group_catalog(
    rows: Iterable[Mapping[str, Any]],
    *,
    station_grid6: str = "",
    station_state: str = "",
    timezone_name: str = "",
    wefax_station_override: str = "",
) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    seen_configs: set[tuple[str, str, str, int | str]] = set()
    for row in chain(rows, _delivered_group_rows()):
        config = _normalize_catalog_config(row)
        if config is None:
            continue
        group = str(config["group"])
        if group in REMOVED_KNOWN_GROUPS:
            continue
        key = (
            group,
            str(config["mode"]),
            str(config["band"]),
            normalize_frequency_hz(config["frequency"]) or str(config["frequency"]),
        )
        if key in seen_configs:
            continue
        seen_configs.add(key)
        entry = groups.setdefault(
            group,
            {"group": group, "configs": [], "resource_sets": set(), "net_names": set(), "status": "resource"},
        )
        entry["configs"].append(config)
        if config.get("resource_set"):
            entry["resource_sets"].add(str(config["resource_set"]))
        if config.get("net_name"):
            entry["net_names"].add(str(config["net_name"]))

    catalog: List[Dict[str, Any]] = []
    for group, entry in groups.items():
        configs = sorted(entry["configs"], key=lambda cfg: (str(cfg.get("band", "")), str(cfg.get("frequency", ""))))
        catalog.append(
            {
                "group": group,
                "configs": configs,
                "resource_sets": sorted(entry["resource_sets"]),
                "net_names": sorted(entry["net_names"]),
                "status": str(entry.get("status", "resource")),
                "source_note": "",
            }
        )
    builtin = [{**entry, "configs": [dict(cfg) for cfg in entry["configs"]]}
               for entry in BUILTIN_STANDARD_GROUPS]
    # An imported resource may use a delivered standard's name. Keep one group,
    # retain additional resource frequencies, and leave standard defaults intact.
    for standard in builtin:
        matching = next((entry for entry in catalog if entry["group"] == standard["group"]), None)
        if matching is None:
            continue
        seen = {(cfg["band"], cfg["mode"], normalize_frequency_hz(cfg["frequency"]) or str(cfg["frequency"]))
                for cfg in standard["configs"]}
        for cfg in matching["configs"]:
            identity = (cfg["band"], cfg["mode"], normalize_frequency_hz(cfg["frequency"]) or str(cfg["frequency"]))
            if identity not in seen:
                standard["configs"].append(cfg)
                seen.add(identity)
        standard["resource_sets"] = sorted(set(standard["resource_sets"]) | set(matching["resource_sets"]))
        standard["net_names"] = sorted(set(standard["net_names"]) | set(matching["net_names"]))
        catalog.remove(matching)
    builtin.append(
        _wefax_station_entry(
            station_grid6=station_grid6,
            station_state=station_state,
            timezone_name=timezone_name,
            station_call_override=wefax_station_override,
        )
    )
    return sorted(builtin + catalog, key=lambda item: str(item.get("group", "")).lower())


def load_known_operating_group_catalog(
    *,
    db_path: Path | None = None,
    resource_dir: Path | None = None,
    station_grid6: str = "",
    station_state: str = "",
    timezone_name: str = "",
    wefax_station_override: str = "",
) -> List[Dict[str, Any]]:
    rows = _resource_rows_from_db(db_path)
    if not rows:
        rows = _resource_rows_from_bundled_json(resource_dir)
    return build_known_operating_group_catalog(
        rows,
        station_grid6=station_grid6,
        station_state=station_state,
        timezone_name=timezone_name,
        wefax_station_override=wefax_station_override,
    )
