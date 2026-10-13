"""Exact frequency-variant identity and fail-closed selection, without Qt/I/O."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping
import uuid

from freqinout.core.operating_group_identity import ensure_operating_group_keys, normalize_logical_group_name
from freqinout.core.mode_utils import normalize_operating_group_mode, normalize_band_label

_NAMESPACE = uuid.UUID("30e3d6ef-e849-4e18-9dc1-78760a34fd0b")


def normalize_frequency_hz(value: object) -> int | None:
    """Convert decimal MHz to exact positive integer Hz; reject sub-Hz values."""
    try:
        hz = Decimal(str(value).strip()) * 1_000_000
        if not hz.is_finite() or hz <= 0 or hz != hz.to_integral_value():
            return None
        return int(hz)
    except (InvalidOperation, ValueError, TypeError):
        return None


def canonical_band(value: object) -> str:
    return normalize_band_label(str(value or "").strip().upper().replace(" ", ""))


def format_frequency_mhz(value: object) -> str:
    """Render exact Hz with at least three decimal MHz places."""
    hz = normalize_frequency_hz(value)
    if hz is None:
        return str(value or "").strip()
    fraction = f"{hz % 1_000_000:06d}".rstrip("0").ljust(3, "0")
    return f"{hz // 1_000_000}.{fraction}"


def _canonical_mode(mode: object, band: str) -> str:
    return normalize_operating_group_mode(str(mode or "").strip().upper(), band)


def _row_group_names(row: Mapping) -> set[str]:
    names = [row.get("group") or row.get("group_name") or row.get("operating_group") or row.get("name")]
    aliases = row.get("aliases") or ()
    names.extend((aliases,) if isinstance(aliases, str) else aliases if isinstance(aliases, (tuple, list, set)) else ())
    names.append(row.get("alias"))
    return {normalize_logical_group_name(value) for value in names if value}


def frequency_identity(row: Mapping) -> tuple:
    band = canonical_band(row.get("band"))
    return (row.get("operating_group_key", ""), band,
            _canonical_mode(row.get("mode"), band),
            normalize_frequency_hz(row.get("frequency")))


def new_operating_group_frequency_key() -> str:
    return "operating_group_frequency_" + str(uuid.uuid4())


def ensure_operating_group_frequency_keys(rows: Iterable[Mapping] | None) -> list[dict]:
    result = ensure_operating_group_keys(rows)
    legacy_occurrences: dict[tuple, int] = {}
    for row in result:
        if not str(row.get("operating_group_frequency_key") or "").strip():
            identity = frequency_identity(row)
            # Malformed rows survive normalization and remain unusable for tuning.
            seed = repr(identity if identity[-1] is not None else (*identity, str(row.get("frequency"))))
            ordinal = legacy_occurrences.get(identity, 0)
            legacy_occurrences[identity] = ordinal + 1
            if ordinal:
                seed += f":legacy-duplicate:{ordinal}"
            row["operating_group_frequency_key"] = "operating_group_frequency_" + str(uuid.uuid5(_NAMESPACE, seed))
    return result


@dataclass(frozen=True)
class FrequencyResolution:
    state: str
    row: dict | None
    candidates: tuple[dict, ...] = ()
    reason: str = ""

    @property
    def candidate_keys(self) -> tuple[str, ...]:
        return tuple(str(row.get("operating_group_frequency_key") or "") for row in self.candidates)

    @property
    def resolved(self) -> bool:
        return self.state == "resolved"


def resolve_operating_group_frequency(
    rows: Iterable[Mapping] | None, *, group: object = "", group_key: object = "",
    frequency_key: object = "", band: object = "", mode: object = "", frequency: object = None,
) -> FrequencyResolution:
    normalized = ensure_operating_group_frequency_keys(rows)
    name = normalize_logical_group_name(group)
    key = str(group_key or "").strip()
    variant_key = str(frequency_key or "").strip()
    wanted_band = canonical_band(band)
    wanted_mode = _canonical_mode(mode, wanted_band) if str(mode or "").strip() else ""
    has_frequency = frequency not in (None, "")
    hz = normalize_frequency_hz(frequency) if has_frequency else None
    family = [row for row in normalized if
              (not key or row.get("operating_group_key") == key) and
              (not name or name in _row_group_names(row))]
    if not name and not key and not variant_key:
        return FrequencyResolution("unresolved", None, (), "Select an operating group and frequency.")
    candidates = [row for row in family if
                  (not wanted_band or canonical_band(row.get("band")) == wanted_band) and
                  (not wanted_mode or frequency_identity(row)[2] == wanted_mode)]
    matches = [row for row in candidates if
               (not variant_key or row.get("operating_group_frequency_key") == variant_key) and
               (not has_frequency or hz is not None and normalize_frequency_hz(row.get("frequency")) == hz)]
    frequencies = ", ".join(str(row.get("frequency") or "invalid") for row in candidates)
    reason = f"{name or key} {wanted_band}: select a configured frequency ({frequencies or 'none available'})."
    if len(matches) != 1:
        state = "ambiguous" if len(matches) > 1 else "unresolved"
        return FrequencyResolution(state, None, tuple(candidates), reason)
    selected = matches[0]
    # A duplicate identity/key is a configuration integrity error, even if a
    # caller supplies a key that happens to address one duplicate.
    identity = frequency_identity(selected)
    if not identity[0] or not identity[1] or not identity[2] or identity[-1] is None or sum(frequency_identity(row) == identity for row in normalized) != 1 or sum(
        row.get("operating_group_frequency_key") == selected.get("operating_group_frequency_key") for row in normalized
    ) != 1:
        return FrequencyResolution("unresolved", None, tuple(candidates), "Duplicate or invalid configured frequency; repair Settings before tuning.")
    return FrequencyResolution("resolved", selected, tuple(candidates))


def reattach_schedule_frequency_keys(rows: Iterable[Mapping], stored_rows: Iterable[Mapping]) -> list[dict]:
    """Restore JSON reference identity after reading an unchanged SQLite mirror.

    Colliding mirror references fail closed instead of depending on row order.
    This does not resolve against the current catalog: deleted keys stay deleted.
    """
    def signature(row):
        band = canonical_band(row.get("band"))
        return (normalize_logical_group_name(row.get("group_name") or row.get("group")),
                band, _canonical_mode(row.get("mode"), band),
                normalize_frequency_hz(row.get("frequency")),
                *(str(row.get(k) or "").strip().upper() for k in
                  ("day_utc", "start_utc", "end_utc", "net_name", "target_device_profile_id", "target_operating_profile_id")))
    refs: dict[tuple, set[str]] = {}
    for row in stored_rows if isinstance(stored_rows, (list, tuple)) else ():
        if isinstance(row, Mapping) and row.get("operating_group_frequency_key"):
            refs.setdefault(signature(row), set()).add(str(row["operating_group_frequency_key"]))
    restored = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        keys = refs.get(signature(row), set())
        if not item.get("operating_group_frequency_key") and keys:
            item["operating_group_frequency_key"] = next(iter(keys)) if len(keys) == 1 else "unresolved_mirror_reference"
        restored.append(item)
    return restored


def sop_frequency_reference_signature(row: Mapping) -> dict:
    return {
        "group": str(row.get("group_name") or row.get("group") or "").strip().upper(),
        "band": str(row.get("band") or "").strip().upper(),
        "mode": str(row.get("mode") or "").strip().upper(),
        "frequency_hz": normalize_frequency_hz(row.get("frequency")),
    }


def reattach_sop_frequency_key(row: Mapping, sidecar: Mapping, profile_id: object, *, kind: str = "layers") -> dict:
    item = dict(row)
    profile = sidecar.get(str(profile_id), {}) if isinstance(sidecar, Mapping) else {}
    references = profile.get(kind, {}) if isinstance(profile, Mapping) else {}
    reference = references.get(str(item.get("id") or 0)) if isinstance(references, Mapping) else None
    if isinstance(reference, Mapping):
        key = str(reference.get("operating_group_frequency_key") or "")
        if reference.get("deleted") or key and reference.get("signature") != sop_frequency_reference_signature(item):
            item["operating_group_frequency_key"] = "unresolved_mirror_reference"
        elif key:
            item["operating_group_frequency_key"] = key
    return item
