"""Worker-owned named schedule review and atomic source/plan save."""
from contextlib import closing
import json

from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.schedule_source_sets import (
    assigned_plan_rf_guard_impacts_for_source_update,
    reproject_frequency_plans_for_source_update,
    resolve_source_schedule_save_target,
    save_source_schedule,
)
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.source_save_revision import (
    require_locked_source_save_revision,
    require_source_save_revision,
    source_save_revision,
)


def _source_save_intent(category, name, target, rows):
    return json.dumps((category, name, (target or {}).get("id"), rows), sort_keys=True)


def prepare_named_schedule_save(config_dir, category, name, selected_id, rows):
    revision = source_save_revision(config_dir)
    with closing(SettingsManager(runtime_worker=True)) as settings:
        target = resolve_source_schedule_save_target(settings, category, name, selected_id=selected_id)
        impacts = assigned_plan_rf_guard_impacts_for_source_update(settings, category, target["id"], rows) if target else []
    require_source_save_revision(config_dir, revision)
    return (revision, _source_save_intent(category, name, target, rows)), target, impacts


def commit_named_schedule_save(config_dir, category, selected_key, name, rows, review):
    proof, target, impacts = review
    revision, intent = proof
    if _source_save_intent(category, name, target, rows) != intent:
        raise ValueError("The source changed after review. Review the current edits before saving.")
    if any(str(item.get("validation", {}).get("state", "")).lower() == "blocked" for item in impacts):
        raise ValueError("RF Guard blocked this update. Resolve the conflict before saving.")
    require_source_save_revision(config_dir, revision)
    with closing(SettingsManager(runtime_worker=True)) as settings:
        store = MultiRadioStore(settings.db_path)
        with store.configuration_save_transaction(prepared_schema=True) as transaction:
            require_locked_source_save_revision(config_dir, revision)
            selected = target or {}
            saved = save_source_schedule(
                settings, category, selected_key, name, rows,
                existing_plan_id=selected.get("db_id"), existing_source_id=selected.get("id"), store=store,
            )
            updated = reproject_frequency_plans_for_source_update(settings, category, saved["id"], rows, store=store)
            require_locked_source_save_revision(config_dir, revision)
            transaction.complete()
    return saved, updated, ""
