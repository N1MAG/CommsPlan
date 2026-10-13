"""Qt-free, data-only CommStat brevity models. External I/O belongs to workers."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from itertools import islice

CODE_RE = re.compile(r"(?<![A-Z0-9])#?([0-9][A-Z]{5,31})(?![A-Z0-9])")
_ROLES = {"event", "phase", "severity", "impact", "response", "source", "station", "display"}
_LEGACY = [("event", "emergency_type", "Event", "event"),
           ("phase", "status_codes", "Phase", "phase"),
           ("impact", "shared_impacts", "Impact", "impact"),
           ("response", "public_reaction", "Public response", "response"),
           ("station", "station_response", "Station status", "station")]
_V2 = [_LEGACY[0], _LEGACY[1], ("severity", "severity", "Severity", "severity"),
       _LEGACY[2], ("response", "official_response", "Official response", "response"),
       ("source", "information_codes", "Trust / source", "source"), _LEGACY[-1]]
_TITLE_KEYS = {"event": "emergency", "phase": "status", "impact": "primary",
               "response": "secondary", "station": "station", "source": "source", "severity": "severity"}


def code_candidates(text: object) -> list[str]:
    return list(dict.fromkeys(CODE_RE.findall(str(text or "").upper())))


def model_fields(catalog: dict) -> list[dict]:
    """Unknown schema never falls back to a guessed built-in model."""
    if not isinstance(catalog, dict):
        return []
    if "fio_brevity_model" in catalog:
        model = catalog["fio_brevity_model"]
        if not isinstance(model, dict) or model.get("schema_version") != 1:
            return []
        if not isinstance(model.get("version"), str) or not model["version"].strip():
            return []
        fields = model.get("fields")
        if not isinstance(fields, list) or not 5 <= len(fields) <= 31:
            return []
        out, seen = [], set()
        for item in fields:
            if not isinstance(item, dict):
                return []
            key, section = item.get("key", ""), item.get("section", "")
            if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key)
                    or key in seen or not isinstance(section, str)
                    or not isinstance(catalog.get(section), dict)):
                return []
            seen.add(key)
            role = item.get("role", "display")
            out.append({"key": key, "section": section,
                        "label": str(item.get("label") or key)[:96],
                        "role": role if isinstance(role, str) and role in _ROLES else "display"})
        return out
    if all(isinstance(catalog.get(key), dict) for key in
           ("emergency_type", "status_codes", "severity", "shared_impacts",
            "official_response", "information_codes", "station_response")):
        fields = _V2
    elif all(isinstance(catalog.get(key), dict) for _, key, _, _ in _LEGACY):
        fields = _LEGACY
    else:
        return []
    titles = catalog.get("gui_titles_v3") or catalog.get("gui_titles") or {}
    social = str(catalog.get("summary_style", "")) in {"social", "community"}
    out = []
    for key, section, label, role in fields:
        raw_label = titles.get(_TITLE_KEYS[key], label) if isinstance(titles, dict) else label
        label = re.sub(r"^\s*\d+\.\s*", "", str(raw_label)).strip().rstrip(":")
        out.append({"key": key, "section": section, "label": label,
                    "role": "display" if social and key != "station" else role})
    return out


def catalog_version(catalog: dict) -> str:
    if not isinstance(catalog, dict):
        return "unknown"
    model = catalog.get("fio_brevity_model")
    if isinstance(model, dict):
        return str(model.get("version") or "declared-1")[:96]
    fields = model_fields(catalog)
    return "2.0" if len(fields) == 7 else "legacy-1" if len(fields) == 5 else "unknown"


def section_entries(section: object) -> dict[str, dict]:
    out, pending, visited = {}, [section], 0
    while pending and visited < 256:
        section = pending.pop()
        visited += 1
        if not isinstance(section, dict):
            continue
        for key, value in islice(section.items(), 256):
            if re.fullmatch(r"[A-Z]", str(key)) and isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"].strip():
                out[key] = {"name": value["name"][:512]}
            elif str(key).startswith("***") and isinstance(value, dict):
                pending.append(value)
    return out


def field_section(catalog: dict, field: dict, values: dict) -> dict:
    if field["key"] == "impact":
        overrides = catalog.get("impact_overrides")
        override = overrides.get(values.get("event")) if isinstance(overrides, dict) else None
        if isinstance(override, dict):
            section = override.get("impacts", override)
            return section if isinstance(section, dict) else {}
    return catalog.get(field["section"], {})


def load_catalogs(asset_dir: Path | str) -> dict[str, dict]:
    """Worker-side bounded load; fingerprint includes content, not just folder."""
    out = {}
    try:
        paths = sorted(Path(asset_dir).glob("[0-9]-*.json"))[:32]
    except OSError:
        return out
    for path in paths:
        try:
            if path.stat().st_size > 2_000_000:
                continue
            blob = path.read_bytes()
            if len(blob) > 2_000_000:
                continue
            data = json.loads(blob)
            if not isinstance(data, dict) or path.name[0] in out:
                continue
            data["_fio_fingerprint"] = hashlib.sha256(blob).hexdigest()
            data["_fio_filename"] = path.name
            out[path.name[0]] = data
        except (OSError, ValueError, RecursionError):
            continue
    return out


def analyze_brevity_codes(text: object, catalogs: dict) -> list[dict]:
    records = []
    for code in code_candidates(text):
        catalog = catalogs.get(code[0], {}) if isinstance(catalogs, dict) else {}
        if not isinstance(catalog, dict):
            catalog = {}
        fields = model_fields(catalog)
        record = {"code": code, "catalog_id": code[0], "model_version": catalog_version(catalog),
                  "catalog_fingerprint": catalog.get("_fio_fingerprint", ""),
                  "status": "definition_unavailable", "fields": [],
                  "summary": f"{code}: Definition unavailable"}
        if len(fields) != len(code) - 1:
            # A longer unknown model may change order; never decode its prefix.
            records.append(record)
            continue
        values = {field["key"]: letter for field, letter in zip(fields, code[1:])}
        decoded = []
        for field, letter in zip(fields, code[1:]):
            entry = section_entries(field_section(catalog, field, values)).get(letter, {})
            name = str(entry.get("name") or "")
            known = bool(name and name.strip().lower() != "reserved")
            decoded.append({"key": field["key"], "label": field["label"], "code": letter,
                            "value": name if known else "Definition unavailable", "known": known,
                            "role": field["role"]})
        record["fields"] = decoded
        record["status"] = "decoded" if all(f["known"] for f in decoded) else "partial"
        record["summary"] = code + ": " + " | ".join(f["value"] for f in decoded)
        records.append(record)
    return records


def encode_brevity_code(list_id: str, values: dict, catalog: dict) -> str:
    fields = model_fields(catalog)
    if not re.fullmatch(r"[0-9]", str(list_id)) or not fields:
        raise ValueError("Brevity model definition unavailable")
    values = {key: str(value).upper() for key, value in values.items()}
    letters = []
    for field in fields:
        letter = str(values.get(field["key"], "")).upper()
        entry = section_entries(field_section(catalog, field, values)).get(letter, {})
        if not re.fullmatch(r"[A-Z]", letter) or not entry or entry.get("name", "").strip().lower() == "reserved":
            raise ValueError(f"Choose a defined {field['label']} code")
        letters.append(letter)
    return list_id + "".join(letters)


_cache_lock = threading.Lock()
_catalog_cache = {}


def cached_catalogs(asset_dir, *, force=False):
    """Worker cache; short TTL detects external definition changes without GUI I/O."""
    if not asset_dir:
        return {}
    key = str(asset_dir)
    now = time.monotonic()
    with _cache_lock:
        cached = _catalog_cache.get(key)
        if cached is not None and not force and now - cached[0] < 5.0:
            return cached[1]
    catalogs = load_catalogs(asset_dir)
    with _cache_lock:
        if len(_catalog_cache) >= 16 and key not in _catalog_cache:
            _catalog_cache.pop(next(iter(_catalog_cache)))
        _catalog_cache[key] = (now, catalogs)
    return catalogs


def brevity_metadata(text, asset_dir=None):
    records = analyze_brevity_codes(text, cached_catalogs(asset_dir) if code_candidates(text) else {})
    return {"brevity_models": records,
            "brevity_asset_dir": str(asset_dir) if asset_dir is not None else "",
            "brevity_code": records[-1]["code"] if records else "",
            "brevity_summary": "\n".join(r["summary"] for r in records)}


def intelligence_values(records):
    """Only understood incident fields may enrich existing topic classification."""
    return [str(f["value"]) for r in records if isinstance(r, dict)
            and isinstance(r.get("fields", ()), (list, tuple))
            for f in r.get("fields", ()) if isinstance(f, dict) and f.get("known") and "value" in f
            and f.get("role") in {"event", "impact"}]
