"""Pure, radio-keyed operator choices; no endpoint or persistence operations."""
from collections.abc import Mapping

SETTINGS_KEY = "radio_operator_controls"
POLICY_MODES = ("On Schedule Change", "Prompt")
POLICY_INTERVALS = ("Hourly", "Every 5 minutes", "Every 10 minutes", "Every 15 minutes", "Every 30 minutes")
MODE_KEYS = ("freq_enforcement_mode", "fldigi_enforcement_mode", "js8_enforcement_mode")
INTERVAL_KEYS = ("freq_prompt_interval", "fldigi_prompt_interval", "js8_prompt_interval")


def radio_choices(settings: Mapping, radio_id: int) -> dict:
    if int(radio_id or 0) <= 0:
        return {}
    rows = settings.get(SETTINGS_KEY, {})
    row = rows.get(str(radio_id), {}) if isinstance(rows, Mapping) else {}
    choices = dict(row) if isinstance(row, Mapping) else {}
    choices.setdefault("sop_tuning_enabled", bool(settings.get("sop_schedule_layer_enabled", True)))
    return choices


def validated_timer_policy(policy: object) -> dict[str, str]:
    if not isinstance(policy, Mapping):
        return {}
    return {key: str(policy[key]) for keys, allowed in
            ((MODE_KEYS, POLICY_MODES), (INTERVAL_KEYS, POLICY_INTERVALS))
            for key in keys if policy.get(key) in allowed}


def filter_lane_sources(lane: Mapping, choices: Mapping) -> dict:
    """Detach the lane so disabling one source cannot mutate a peer or base plan."""
    out = dict(lane)
    if choices.get("routine_schedule_enabled") is False:
        out.update(hf_rows=[], net_rows=[])
    if choices.get("sop_tuning_enabled") is False:
        out.update(sop_rows=[], policy_rows=[])
    return out
