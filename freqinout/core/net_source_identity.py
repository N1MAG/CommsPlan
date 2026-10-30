"""Canonical saved NET source identity, usable without a GUI import."""
from typing import Any, Dict
from freqinout.core.schedule_targeting import normalize_schedule_target_fields
from freqinout.core.fldigi_tuning import tuning_preset_json

def _tuning_identity(value):
    try:
        return tuning_preset_json(value)
    except (TypeError, ValueError):
        return "{}"

def net_source_identity(row: Dict[str, Any]) -> tuple:
    """Canonical saved schedule context used to detect reused row IDs."""
    def value(key: str, default: str = "") -> str:
        return str(row.get(key) or default).strip().upper()

    frequency = value("frequency")
    try:
        frequency = format(float(frequency), ".12g")
    except ValueError:
        pass
    recurrence = value("recurrence", "WEEKLY")
    if recurrence == "MONTHLY":
        recurrence = "PERIODIC"
    target = normalize_schedule_target_fields(row)
    def number(key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return (value("net_name"), value("day_utc"), value("start_utc"),
            frequency, value("end_utc"), value("group_name"), value("band"),
            value("mode"), recurrence, value("month_weeks"),
            number("biweekly_offset_weeks"), number("early_checkin"),
            target["target_scope"], target["target_device_profile_id"],
            target["target_operating_profile_id"],
            value("primary_js8call_group"), value("vfo", "A"),
            str(row.get("auto_tune") or "").strip().lower() in {"1", "true", "yes", "on"},
            value("fldigi_mode"), value("fldigi_offset"),
            _tuning_identity(row.get("fldigi_tuning_json", "{}")))
