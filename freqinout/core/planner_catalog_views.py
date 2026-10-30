"""Memory-only views for Planner rendering; explicit mutations remain explicit."""
from __future__ import annotations

import copy
from pathlib import Path

from freqinout.core.planner_catalog_model import validation_key
from freqinout.core.plan_context_service import PlanContextSnapshot
from freqinout.core.schedule_source_inputs import ScheduleSourceError


class PlannerSettingsView:
    def __init__(self, config_dir: Path, values: dict | None = None, *, on_mutation=None):
        self.config_dir = Path(config_dir)
        self.db_path = self.config_dir / "freqinout.db"
        self._data = dict(values or {})
        self._ready = False
        self._writer = None
        self._on_mutation = on_mutation

    def get(self, key, default=None):
        return self._data.get(key, default)

    def all(self):
        return copy.deepcopy(self._data)

    def reload(self):
        # Input reload is requested asynchronously by the owning workspace.
        return None

    def publish(self, values, config_dir):
        self.config_dir = Path(config_dir)
        self.db_path = self.config_dir / "freqinout.db"
        self._data = values
        self._ready = True

    def assert_mutation_root(self):
        if not self._ready:
            raise ScheduleSourceError("Plan Builder inputs are still loading; retry this action when ready")
        from freqinout.core.config_paths import get_config_dir
        if get_config_dir() / "config" != self.config_dir:
            raise ScheduleSourceError("The active configuration profile changed; reopen Plan Builder before saving")

    def set(self, key, value):
        self.assert_mutation_root()
        if self._writer is None:
            from freqinout.core.settings_manager import SettingsManager
            self._writer = SettingsManager(runtime_worker=True)
            if self._writer.db_path != self.db_path:
                self._writer.close()
                self._writer = None
                raise ScheduleSourceError("Configuration profile changed before the explicit settings update")
        self._writer.set(key, value)
        self._data = {**self._data, key: value}
        if self._on_mutation:
            self._on_mutation("settings:kv", key, copy.deepcopy(value))

    def save(self):
        return None

    write = save

    def close(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None


class PlannerStoreView:
    def __init__(self, settings, *, on_mutation=None, request_validation=None):
        self.settings = settings
        self.model = None
        self._on_mutation = on_mutation
        self._request_validation = request_validation
        self._verified_validations = {}
        self._expected_plan = None

    @property
    def db_path(self):
        return self.settings.db_path

    def list_frequency_plans(self):
        return self.model.plans if self.model is not None else ()

    def get_frequency_plan(self, record_id):
        if self.model is None:
            return None
        row = self.model.plan_index.get(int(record_id))
        return dict(row) if row is not None else None

    def list_device_profiles(self):
        return self.model.devices if self.model is not None else ()

    def get_device_profile(self, record_id):
        if self.model is None:
            return None
        row = self.model.device_index.get(int(record_id))
        return dict(row) if row is not None else None

    def list_effective_assigned_plans(self):
        return self.model.assignments if self.model is not None else ()

    def validate_frequency_plan_for_device(self, radio_id, payload):
        key = validation_key(radio_id, payload)
        result = self._verified_validations.get(key)
        if result is None and self.model is not None:
            result = self.model.validations.get(key)
        if result is not None:
            return copy.deepcopy(result)
        if self._request_validation is not None:
            self._request_validation(int(radio_id), payload)
        return {"state": "pending", "rf_guard_validation": "unavailable",
                "messages": ["Checking RF Guard against the selected inputs…"]}

    def save_frequency_plan(self, payload, **kwargs):
        self.assert_action()
        self.settings.assert_mutation_root()
        from freqinout.core.multi_radio_store import MultiRadioStore
        clean = {key: value for key, value in payload.items() if not key.startswith("_catalog_")}
        if self._expected_plan is not None and int(clean.get("id") or 0) == int(self._expected_plan.get("id") or 0):
            kwargs.setdefault("expected", self._expected_plan)
        result = MultiRadioStore(self.db_path).save_frequency_plan(clean, **kwargs)
        if self._on_mutation:
            self._on_mutation("settings:frequency_plans", int(result["id"]), dict(result))
        return result

    def delete_frequency_plan(self, record_id):
        self.assert_action()
        self.settings.assert_mutation_root()
        from freqinout.core.multi_radio_store import MultiRadioStore
        result = MultiRadioStore(self.db_path).delete_frequency_plan(record_id)
        if self._on_mutation:
            self._on_mutation("settings:frequency_plans", int(record_id), None)
        return result


class PlannerContextView:
    def __init__(self, store):
        self.store = store
        self.selection_service = None
        self._snapshot = PlanContextSnapshot((), None, None, (), "unknown")
        self._tab_radio = None
        self._settings_radio = None

    def publish(self, snapshot, *, tab_radio=None, settings_radio=None):
        self._snapshot = snapshot
        self._tab_radio = tab_radio
        self._settings_radio = settings_radio

    def invalidate(self):
        return None

    def snapshot(self, *, refresh=False):
        return self._snapshot

    def context_for_tab(self, tab_id, *, refresh=False):
        radio_id = self._tab_radio or self._settings_radio or self._snapshot.selected_radio_id or self._snapshot.primary_runtime_radio_id
        return self._snapshot.context_for_radio(radio_id)

    def context_for_radio(self, radio_id, *, refresh=False):
        return self._snapshot.context_for_radio(radio_id)

    def primary_context(self, *, refresh=False):
        return self._snapshot.primary_context
