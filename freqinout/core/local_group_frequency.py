"""Lossless, Qt-free catalog snapshots in legacy Local Comms Group profiles.

The Settings owner publishes these dictionaries. This module never opens a
database, mutates a catalog, creates schedules, or commands a radio.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from typing import Iterable, Mapping

from freqinout.core.frequency_reference import frequency_programming_text
from freqinout.core.resource_catalog_models import FrequencyResource

LINK_FIELDS = ("frequency_resource_key", "accepted_frequency_version_hash", "accepted_frequency_snapshot")
MAX_ASSIGNMENTS = 2000
MAX_SNAPSHOT_BYTES = 131072


def normalize_local_profile(row: Mapping, default_resource: str = "VHF Simplex") -> dict[str, str]:
    result = {
        "group": str(row.get("group", row.get("name", "")) or "").strip(),
        "resource": str(row.get("resource", row.get("service", "")) or "").strip() or default_resource,
        "mode": str(row.get("mode") or "").strip(),
        "target": str(row.get("target") or "").strip(),
        "notes": str(row.get("notes") or "").strip(),
    }
    # Keep malformed references intact for repair instead of silently erasing
    # their provenance. Consumers validate the snapshot before using it.
    for name in LINK_FIELDS:
        if row.get(name) not in (None, ""):
            value = row[name]
            result[name] = json.dumps(value, sort_keys=True, separators=(",", ":")) if isinstance(value, dict) else str(value)
    return result


def local_profile_key(row: Mapping) -> tuple[str, str, str, str]:
    group = str(row.get("group") or "").strip().upper()
    key = str(row.get("frequency_resource_key") or "").strip()
    if key:
        return group, "CATALOG", key, ""
    return (group, str(row.get("resource") or "").strip().upper(),
            str(row.get("mode") or "").strip().upper(), str(row.get("target") or "").strip().upper())


def snapshot_for_profile(profile: Mapping) -> FrequencyResource | None:
    raw = profile.get("accepted_frequency_snapshot")
    if not isinstance(raw, (str, dict)) or not raw:
        return None
    if isinstance(raw, str) and len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            return None
        resource = FrequencyResource(**{k: v for k, v in data.items() if k in FrequencyResource.__dataclass_fields__})
        return resource if resource.frequency_resource_key == str(profile.get("frequency_resource_key") or "").strip() else None
    except (ValueError, TypeError, OverflowError):
        return None


def catalog_reference_state(profile: Mapping, current: FrequencyResource | None) -> str:
    if current is None:
        return "missing"
    if not current.active or current.retired:
        return "retired"
    saved = snapshot_for_profile(profile)
    if saved is None:
        return "invalid"
    accepted_hash = str(profile.get("accepted_frequency_version_hash") or "")
    current_hash = current.version_hash or current.content_hash or ""
    if accepted_hash and current_hash:
        return "current" if accepted_hash == current_hash else "changed"
    ignored = {"created_utc", "updated_utc", "content_hash", "version_hash", "content_version"}
    payload = lambda resource: {key: value for key, value in asdict(resource).items() if key not in ignored}
    return "current" if payload(saved) == payload(current) else "changed"


def _resource_category(resource: FrequencyResource) -> str:
    if resource.service in ("FRS", "MURS"):
        return resource.service
    receive = resource.receive_hz or resource.center_hz or 0
    if resource.transmit_hz is None:
        return "Other"
    kind = "Repeater" if resource.resource_kind == "repeater" or resource.transmit_hz != receive else "Simplex"
    if resource.service == "GMRS":
        return f"GMRS {kind}"
    if resource.service == "AMATEUR" and 30_000_000 <= receive < 3_000_000_000:
        return f"{'VHF' if receive < 300_000_000 else 'UHF'} {kind}"
    return "Other"


def frequency_to_profile(group: str, resource: FrequencyResource) -> dict[str, str]:
    group = str(group or "").strip()
    if not group:
        raise ValueError("Choose a Local Comms Group first")
    if resource.resource_kind == "band_range" or not resource.active or resource.retired:
        raise ValueError("Choose an active channel frequency")
    snapshot = json.dumps(asdict(resource), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(snapshot.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Frequency reference is too large to assign")
    return {"group": group, "resource": _resource_category(resource), "mode": resource.mode or "Voice",
            "target": frequency_programming_text(resource), "notes": resource.notes or "",
            "frequency_resource_key": resource.frequency_resource_key,
            "accepted_frequency_version_hash": resource.version_hash or resource.content_hash or "",
            "accepted_frequency_snapshot": snapshot}


def merge_catalog_frequencies(profiles: Iterable[Mapping], group: str, resources: Iterable[FrequencyResource], *,
                              replace_existing: bool = False) -> tuple[list[dict[str, str]], int, int]:
    result = [normalize_local_profile(row) for row in profiles if isinstance(row, Mapping)]
    positions = {local_profile_key(row): i for i, row in enumerate(result)}
    count = skipped = 0
    seen = set()
    for index, resource in enumerate(resources):
        if index >= MAX_ASSIGNMENTS:
            raise ValueError("Select no more than 2000 frequencies at a time")
        candidate = frequency_to_profile(group, resource)
        key = local_profile_key(candidate)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        position = positions.get(key)
        if position is not None:
            if not replace_existing:
                skipped += 1
                continue
            original = result[position]
            candidate["group"] = original["group"]
            candidate["notes"] = original["notes"]
            result[position] = candidate
        else:
            if replace_existing:
                skipped += 1
                continue
            positions[key] = len(result)
            result.append(candidate)
        count += 1
    return result, count, skipped


def updated_local_profile(old: Mapping | None, candidate: Mapping) -> dict[str, str]:
    result = normalize_local_profile(candidate)
    original = normalize_local_profile(old or {})
    if original.get("frequency_resource_key") and all(result.get(name) == original.get(name) for name in ("resource", "mode", "target")):
        for name in LINK_FIELDS:
            if name in original:
                result[name] = original[name]
    else:
        for name in LINK_FIELDS:
            result.pop(name, None)
    return result
