from __future__ import annotations

import datetime
import json
import os
import time
import sqlite3
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QFrame,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QMessageBox,
    QLineEdit,
    QSizePolicy,
    QScrollArea,
    QMenu,
)

from pathlib import Path

from freqinout.core.settings_manager import SettingsManager
from freqinout.core.planner_radio_projection import build_radio_windows
from freqinout.core.planner_guard_rules import PlannerGuardRules, guard_key
from freqinout.core.planner_input_catalog import PlannerCatalogReader
from freqinout.core.planner_catalog_worker import PlannerCatalogWorker
from freqinout.core.planner_catalog_views import PlannerSettingsView, PlannerStoreView, PlannerContextView
from freqinout.core.product_identity import STORAGE_NAME
from freqinout.core.schedule_configuration_cache import ScheduleRevisionTracker
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
from freqinout.core.operating_group_frequency import format_frequency_mhz, resolve_operating_group_frequency
from freqinout.core.logger import log
from freqinout.core.config_paths import get_config_dir
from freqinout.core.perf_metrics import emit_span
from freqinout.core.plan_context_service import PlanContextService
from freqinout.core.legacy_resource_projection import update_legacy_resource_fields
from freqinout.core.guided_setup import (
    SCHEDULE_DAILY_NO_NETS,
    SCHEDULE_DAILY_PLUS_NETS,
    SCHEDULE_JS8_STANDARD,
    SCHEDULE_SOP_CONDITION,
)
from freqinout.core.schedule_source_sets import (
    LIVE_SOURCE_SET_ID,
    NO_NET_SOURCE_SET_ID,
    NO_NET_SOURCE_SET_LABEL,
    HF_DAILY_SOURCE_CATEGORY,
    HF_DAILY_SOURCE_SETS_KEY,
    HF_NET_SOURCE_CATEGORY,
    HF_NET_SOURCE_SETS_KEY,
    SELECTED_HF_DAILY_SOURCE_SET_KEY,
    SELECTED_HF_NET_SOURCE_SET_KEY,
    selected_source_schedule_dependency_refs,
    selected_source_set_id,
    assigned_plan_rf_guard_impacts_for_source_update,
    save_source_schedule,
    plan_source_usage_summary,
    source_set_row_by_id_for_category,
    source_sets_for_category,
)
from freqinout.core.schedule_source_inputs import ScheduleSourceError, MissingScheduleSourceError, read_plan_schedule_table
from freqinout.core.sqlite_utils import connect_sqlite_readonly
from freqinout.core.schedule_projection import (
    BlendedScheduleProjection,
    ProjectionCell,
    ScheduleSegment,
    build_blended_schedule_projection,
)
from freqinout.core.operational_projection import (
    OperationalCell,
    OperationalDayProjection,
    build_operational_day_projection,
    build_operational_day_projection_from_refs,
)
from freqinout.utils.timezones import get_timezone
from freqinout.gui.planner_choice_model import PlannerChoiceCombo
from freqinout.gui.responsive_geometry import WrappingControlLayout
from freqinout.gui.plan_context_label import PLAN_CONTEXT_FALLBACK_TEXT, PlanContextLabel
from freqinout.gui.theme import (
    resolve_theme,
    button_style,
    label_style,
    font_derived_widget_height,
    apply_text_size_accessibility_guards,
    band_cell_colors,
    qcolor,
    BAND_COLORS_LIGHT,
    BAND_COLORS_DARK,
)

DAY_NAMES = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]
DAY_NAMES_UPPER = [d.upper() for d in DAY_NAMES]
FREQPLANNER_CONTEXT_FALLBACK_TEXT = PLAN_CONTEXT_FALLBACK_TEXT


@dataclass(frozen=True)
class EffectiveWindowCell:
    segment: ScheduleSegment
    hf_segments: Tuple[ScheduleSegment, ...] = ()
    net_segments: Tuple[ScheduleSegment, ...] = ()
    sop_segments: Tuple[ScheduleSegment, ...] = ()

    @property
    def day_utc(self) -> str:
        return self.segment.day_utc

    @property
    def effective_source(self) -> str:
        return self.segment.source

    @property
    def display_label(self) -> str:
        return self.segment.net_name or self.segment.group_name or self.segment.profile_name or self.segment.label


@dataclass(frozen=True)
class _PlanProjectionResult:
    request_id: int
    mode: str
    snapshot: str
    hf_sched: Tuple[Dict[str, Any], ...]
    net_sched: Tuple[Dict[str, Any], ...]
    sop_sched: Tuple[Dict[str, Any], ...]
    policy_rows: Tuple[Dict[str, Any], ...]
    week_sunday: datetime.date
    projection: Optional[object] = None
    selected_plan: Optional[Dict[str, Any]] = None
    selected_plan_refs: Tuple[Dict[str, Any], ...] = ()
    started_at: float = 0.0
    error: str = ""


class _PlanProjectionEmitter(QObject):
    finished = Signal(object)


class FreqPlannerTab(QWidget, PlannerGuardRules):
    """
    Frequency planner view.

    - Rows: hours 00..23 (UTC hour buckets)
    - Columns:
        0: UTC Hour
        1: Local Time (HH:00 AM/PM TZ)
        2-8: Sunday .. Saturday

    Cell contents:
      - If only HF schedule applies at that hour: show the band (or multiple bands as "40M / 80M").
      - If one or more nets apply: show "band|net name" or "band1 / band2|net1 / net2".
      - Uses hf_schedule (or legacy daily_schedule) and net_schedule from config.json.

    Highlighting:
      - Current UTC weekday column cells are highlighted *only if* they have a net in that hour.

    Local time:
      - Uses the timezone stored in Settings ("timezone") via get_timezone(), so it is
        consistent and cross-platform.
    """

    COL_UTC = 0
    COL_LOCAL = 1
    COL_DAY_OFFSET = 2  # Sunday at column 2
    MAX_PROJECTION_TABLE_ROWS = 500
    MAX_RF_GUARD_ROWS = 200

    schedule_saved = Signal()

    def __init__(self, parent=None, *, plan_context_service: Optional[PlanContextService] = None):
        super().__init__(parent)
        self._host_plan_context_service = plan_context_service
        # Construct only memory views on Qt. Even profile-marker/file probing is
        # owned by the serial worker; the hint itself performs no filesystem I/O.
        inherited = getattr(parent, "settings", None)
        hint = getattr(inherited, "config_dir", None)
        bound_root = hint is not None
        if hint is None and plan_context_service is not None:
            store_path = getattr(getattr(plan_context_service, "store", None), "db_path", None)
            if store_path is not None:
                hint = Path(store_path).parent
                bound_root = True
        if hint is None:
            base = os.environ.get("FREQINOUT_CONFIG_DIR")
            if not base:
                base = (str(Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()) / STORAGE_NAME)
                        if os.name == "nt" else str(Path.home() / f".{STORAGE_NAME.lower()}"))
            hint = Path(base) / "config"
        inherited_values = getattr(inherited, "_data", {})
        bootstrap = {key: inherited_values[key] for key in (
            "ui_theme", "ui_text_size", "timezone", "display_time_mode", "band_colors", "perf_metrics_enabled")
            if key in inherited_values}
        self.settings = PlannerSettingsView(hint, bootstrap, on_mutation=self._catalog_mutated)
        self._catalog_store = PlannerStoreView(self.settings, on_mutation=self._catalog_mutated,
                                               request_validation=self._request_catalog_validation)
        self._catalog_store.assert_action = self._assert_fresh_action
        self.plan_context_service = PlannerContextView(self._catalog_store)
        self._catalog_reader = PlannerCatalogReader(hint)
        self._catalog_worker = PlannerCatalogWorker(self._catalog_reader, fixed_root=bound_root)
        self._catalog_model = None
        self._catalog_generation = 0
        self._catalog_pending = False
        self._catalog_latest = None
        self._pending_catalog_focus = None
        self._pending_guided_handoff = None
        self._sop_edit_draft = None
        self._sop_entry_override = None
        self._published_guard_context = None
        self._published_runtime_status = None
        self._probe_retry_after = 0.0
        self._probe_retry_attempt = 0
        self._catalog_validations = {}
        self._catalog_review_required = False
        self._catalog_mutations = {}
        self._operation_feedback = ""
        self._guard_advisory_pending = set()
        self._fresh_action_pending = False
        self._fresh_guard_reviews = {}
        self._fresh_source_impacts = ()
        self._fresh_action_token = None
        self._action_emitter = _PlanProjectionEmitter(self)
        self._action_emitter.finished.connect(self._on_fresh_action_ready)
        self._catalog_emitter = _PlanProjectionEmitter(self)
        self._catalog_emitter.finished.connect(self._on_catalog_ready)
        self._revision_executor = DaemonSerialExecutor(thread_name_prefix="fio-plan-revisions")
        self._revision_tracker = None
        self._revision_future = None
        self._revision_key = None
        self._revision_closed = False
        self._tab_active = True
        self._input_read_error = ""
        self._input_retry_after = 0.0
        self._input_retry_attempt = 0
        self._input_missing_source = False
        self._input_observed_revision = None
        self._input_retry_clock = time.monotonic
        default_mode = (self.settings.get("display_time_mode", "LOCAL") or "LOCAL").upper()
        self._show_local = default_mode != "UTC"
        self._show_band = True
        self._band_colors: Dict[str, str] = {}
        self._visible_bands: List[str] = []
        self._selected_projection_cell: Optional[object] = None
        self._selected_operational_cell: Optional[OperationalCell] = None
        self._inline_edit_segment: Optional[ScheduleSegment] = None
        self._clock_timer: QTimer | None = None
        self._last_snapshot: str = ""
        self._last_rebuild_check_ts: float = 0.0
        self._pending_rebuild: bool = False
        self._projection_executor: ThreadPoolExecutor | None = None
        self._projection_request_id: int = 0
        self._projection_pending: bool = False
        self._projection_future = None
        self._action_projection_pending = False
        self._action_projection_prepared = None
        self._action_projection_emitter = _PlanProjectionEmitter(self)
        self._action_projection_emitter.finished.connect(self._on_action_projection_ready)
        self._projection_latest = None
        self._projection_emitter = _PlanProjectionEmitter(self)
        self._projection_emitter.finished.connect(self._on_projection_ready)
        self._latest_projection_snapshot: str = ""
        self._latest_projection_mode: str = ""
        self._latest_projection: Optional[object] = None
        self._creating_new_frequency_plan: bool = False
        self._frequency_plan_layers_dirty: bool = False
        self._guided_plan_handoff_device_profile_id: int = 0
        self._build_ui()
        self._apply_theme()
        self._request_input_catalog()

    def _draft_identity(self):
        return (self._catalog_generation, self._snapshot((), (), (), ()),
                self.frequency_plan_combo.currentText(), self._inline_draft_state(),
                bool(self._frequency_plan_layers_dirty), self._host_selection_identity(), self._host_runtime_status(),
                int(self._guided_plan_handoff_device_profile_id or 0))

    def _host_selection_identity(self):
        selection = getattr(self._host_plan_context_service, "selection_service", None)
        return ((selection.tab_radio_id("freqplanner"), selection.settings_radio_id())
                if selection is not None else (None, None))

    def _host_runtime_status(self):
        parent = self.parent()
        while parent is not None:
            manager = getattr(parent, "station_runtime_manager", None)
            if manager is not None:
                return getattr(manager, "_runtime_status", None)
            parent = parent.parent()
        return None

    def _inline_draft_state(self):
        return tuple((name, widget.text() if isinstance(widget, QLineEdit) else widget.currentText())
                     for name, widget in vars(self).items()
                     if name.startswith("inline_") and isinstance(widget, (QLineEdit, QComboBox)))

    def _assert_fresh_action(self):
        token = self._fresh_action_token
        if token is None or token != self._draft_identity() or not self._tab_active or self._revision_closed:
            raise ScheduleSourceError("This action no longer matches the verified draft; review inputs and try again")

    def _request_fresh_action(self, action, args=(), kwargs=None, *, guard_payload=None, target_plan_id=None, source_update=None):
        if self._fresh_action_token is not None:
            self._assert_fresh_action()
            if guard_payload is None or guard_key(guard_payload, self._effective_guard_radio_id(),
                                                   self._guided_plan_handoff_device_profile_id) in self._fresh_guard_reviews:
                return False
            self._fresh_action_token = None
        if self._fresh_action_pending:
            return True
        current_context = (self._effective_guard_radio_id(), int(self._guided_plan_handoff_device_profile_id or 0))
        runtime_changed = self._published_runtime_status != self._host_runtime_status()
        if self._published_guard_context is not None and (current_context != self._published_guard_context or runtime_changed) and self._has_authoring_draft():
            self._catalog_review_required = True
            self.review_plan_inputs_btn.show()
            if not self._catalog_pending:
                self._request_input_catalog()
        if self._catalog_review_required:
            self.frequency_plan_action_hint_label.setText("Current inputs differ from this draft. Choose Review changed inputs before applying it.")
            return True
        if self._input_read_error:
            self._show_plan_input_failure()
            return True
        if self._catalog_pending or self._catalog_model is None:
            self.frequency_plan_action_hint_label.setText("Wait for current inputs before changing a plan. Entered work is retained.")
            return True
        identity = self._draft_identity()
        request = self._catalog_request(False)
        self._fresh_action_pending = True
        plan = self._selected_frequency_plan_row()
        self.frequency_plan_action_hint_label.setText("Verifying current inputs before applying this action…")
        future = self._revision_executor.submit(
            self._catalog_worker.verify_action,
            expected_revision=self._catalog_model.catalog.revision,
            plan_id=int(target_plan_id if target_plan_id is not None else (plan or {}).get("id") or 0),
            source_ids=request["source_ids"], validation_requests=request["validation_requests"],
            runtime_status=request["runtime_status"], guard_context=request["guard_context"],
            guard_payloads=(guard_payload,) if guard_payload is not None else request["guard_payloads"],
            source_update=source_update)
        emitter = self._action_emitter
        def completed(done):
            try:
                value, error = done.result(), None
            except Exception as exc:
                value, error = None, exc
            try:
                emitter.finished.emit((identity, action, args, kwargs or {}, value, error))
            except RuntimeError:
                pass
        future.add_done_callback(completed)
        return True

    def _on_fresh_action_ready(self, result):
        identity, action, args, kwargs, value, error = result
        self._fresh_action_pending = False
        if self._revision_closed or not self._tab_active:
            return
        if identity != self._draft_identity():
            self.frequency_plan_action_hint_label.setText("The draft or selected inputs changed during verification. Review them, then choose the action again.")
            return
        if error is not None:
            self._handle_plan_input_failure(error)
            return
        model, expected = value
        self._catalog_store._verified_validations = model.validations
        self._catalog_store._expected_plan = expected
        self._fresh_guard_reviews = model.guard_reviews
        self._fresh_source_impacts = model.source_impacts
        self._fresh_action_token = identity
        try:
            action(*args, **kwargs)
        except Exception as exc:
            self.frequency_plan_action_hint_label.setText(f"Unable to apply action: {exc}")
            log.exception("Plan Builder explicit action failed")
        finally:
            self._fresh_action_token = None
            self._catalog_store._verified_validations.clear()
            self._catalog_store._expected_plan = None
            self._fresh_guard_reviews = {}
            self._fresh_source_impacts = ()

    def _rf_guard_preflight_for_plan(self, plan_payload):
        radio_id = self._effective_guard_radio_id()
        key = guard_key(plan_payload, radio_id, self._guided_plan_handoff_device_profile_id)
        cached = self._fresh_guard_reviews.get(key)
        if cached is None and self._catalog_model is not None:
            cached = self._catalog_model.guard_reviews.get(key)
        if cached is None and key not in self._guard_advisory_pending and not self._fresh_action_pending and self._fresh_action_token is None:
            self._guard_advisory_pending.add(key)
            QTimer.singleShot(0, self._refresh_catalog_advisories)
        return dict(cached) if cached is not None else {
            "state": "pending", "rf_guard_validation": "unavailable",
            "messages": ["RF Guard is awaiting background verification."]}

    def _effective_guard_radio_id(self):
        tab_radio, settings_radio = self._host_selection_identity()
        snapshot = self.plan_context_service.snapshot()
        status = self._host_runtime_status()
        runtime_primary = getattr(status, "primary_radio_id", None)
        return tab_radio or settings_radio or runtime_primary or snapshot.selected_radio_id or snapshot.primary_runtime_radio_id

    def _merge_frequency_variant_validation(self, validation, plan_payload):
        # Exact variant resolution is included in the worker RF result.
        return dict(validation)

    def _has_authoring_draft(self):
        if self._sop_edit_draft is not None or self._frequency_plan_layers_dirty or self._inline_draft_dirty() or self._creating_new_frequency_plan:
            return True
        plan = self._selected_frequency_plan_row()
        text = self.frequency_plan_combo.currentText().strip()
        name = str((plan or {}).get("name") or "").strip()
        return bool(text and text not in {name, f"{name} (draft)"})

    def _review_changed_inputs(self):
        if not self._catalog_review_required or self._catalog_pending or self._catalog_model is None:
            return
        generation = self._catalog_generation
        context_identity = self._host_selection_identity(), self._guided_plan_handoff_device_profile_id
        response = QMessageBox.question(
            self, "Review Changed Inputs",
            "The schedule inputs or radio context changed while entered work was retained.\n\n"
            + self._source_selection_summary()
            + "\n\nReview the current plan windows and selected sources. Keep this draft against the current inputs? "
              "This does not save it; choose Save or Update separately after review.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if (response == QMessageBox.Yes and generation == self._catalog_generation and not self._catalog_pending
                and context_identity == (self._host_selection_identity(), self._guided_plan_handoff_device_profile_id)):
            self._catalog_review_required = False
            self.review_plan_inputs_btn.hide()
            self.frequency_plan_action_hint_label.setText("Current inputs acknowledged. Entered work retained; choose Save or Update when ready.")

    def _ensure_action_projection(self, mode, callback):
        if self._catalog_model is None or self._catalog_pending or self._input_read_error:
            if self._input_read_error:
                self._show_plan_input_failure()
            else:
                self.frequency_plan_action_hint_label.setText("Wait for current Plan Builder inputs before this action.")
            return True
        snapshot = self._snapshot((), (), (), ())
        expected_type = OperationalDayProjection if mode == "operational" else BlendedScheduleProjection
        if (snapshot == self._latest_projection_snapshot and isinstance(self._latest_projection, expected_type)):
            return False
        prepared = self._action_projection_prepared
        if prepared is not None and prepared[:2] == (mode, snapshot):
            return False
        if self._action_projection_pending:
            return True
        self._action_projection_pending = True
        identity = self._draft_identity()
        hf, net, sop, policies = self._load_schedules()
        week = self._week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc))
        future = self._projection_worker().submit(
            self._build_projection_snapshot, 0, mode, snapshot, hf, net, sop, policies,
            self._catalog_model.resources, week, None, (), time.perf_counter(),
            frozenset(self._configured_operating_group_names()))
        self.frequency_plan_action_hint_label.setText("Preparing the current plan for this action…")
        emitter = self._action_projection_emitter
        def completed(done):
            try:
                result, error = done.result(), None
            except Exception as exc:
                result, error = None, exc
            try:
                emitter.finished.emit((identity, callback, mode, snapshot, result, error))
            except RuntimeError:
                pass
        future.add_done_callback(completed)
        return True

    def _on_action_projection_ready(self, value):
        identity, callback, mode, snapshot, result, error = value
        self._action_projection_pending = False
        if self._revision_closed or not self._tab_active:
            return
        if identity != self._draft_identity():
            self.frequency_plan_action_hint_label.setText("The draft or selected inputs changed during preparation; choose the action again after review.")
            return
        if error is not None or result.error:
            self._handle_plan_input_failure(error or ScheduleSourceError(result.error))
            return
        self._action_projection_prepared = (mode, snapshot, result.projection)
        callback()

    def _set_action_feedback(self, text):
        self._operation_feedback = str(text)
        self.frequency_plan_action_hint_label.setText(self._operation_feedback)

    def _catalog_mutated(self, table=None, identity=None, value=None):
        if table is not None:
            self._catalog_mutations[(table, str(identity))] = value
        if not getattr(self, "_revision_closed", False):
            QTimer.singleShot(0, lambda: self._request_input_catalog(force=True))

    def _request_catalog_validation(self, radio_id, payload):
        from freqinout.core.planner_catalog_model import validation_key
        key = validation_key(radio_id, payload)
        if key not in self._catalog_validations:
            if len(self._catalog_validations) >= 64:
                self._catalog_validations.pop(next(iter(self._catalog_validations)))
            self._catalog_validations[key] = (radio_id, dict(payload))
            if self._fresh_action_token is None:
                QTimer.singleShot(0, self._refresh_catalog_advisories)

    def _refresh_catalog_advisories(self):
        if not self._fresh_action_pending and self._tab_active and not self._revision_closed:
            self._request_input_catalog()

    def _catalog_request(self, force):
        host = self._host_plan_context_service
        selection = getattr(host, "selection_service", None)
        tab_radio = selection.tab_radio_id("freqplanner") if selection is not None else None
        settings_radio = selection.settings_radio_id() if selection is not None else None
        runtime_status = self._host_runtime_status()
        return {"generation": self._catalog_generation, "force": bool(force),
                "tab_radio": tab_radio, "settings_radio": settings_radio,
                "runtime_status": runtime_status,
                "source_ids": ({HF_DAILY_SOURCE_SETS_KEY: self._selected_source_set_id(SELECTED_HF_DAILY_SOURCE_SET_KEY),
                                HF_NET_SOURCE_SETS_KEY: self._selected_source_set_id(SELECTED_HF_NET_SOURCE_SET_KEY)}
                               if self._catalog_model is not None else None),
                "validation_requests": tuple(self._catalog_validations.values()),
                "guard_context": (self._effective_guard_radio_id(), int(self._guided_plan_handoff_device_profile_id or 0)),
                "guard_payloads": (self._selected_frequency_plan_row(),) if self._selected_frequency_plan_row() else (),
                "baseline_catalog": self._catalog_model.catalog if self._catalog_model is not None else None,
                "accepted_mutations": tuple((table, identity, value) for (table, identity), value in self._catalog_mutations.items())}

    def _request_input_catalog(self, *, force=False):
        if getattr(self, "_revision_closed", False):
            return
        self._catalog_generation += 1
        self._projection_request_id += 1
        self._catalog_store._verified_validations.clear()
        request = self._catalog_request(force)
        if self._catalog_latest is not None:
            request["force"] |= self._catalog_latest["force"]
        self._catalog_latest = request
        if self._catalog_pending or not getattr(self, "_tab_active", True):
            return
        self._start_catalog_request()

    def _start_catalog_request(self):
        request, self._catalog_latest = self._catalog_latest, None
        if request is None:
            return
        self._catalog_pending = True
        self.table.setEnabled(False)
        self.frequency_plan_action_hint_label.setText(
            "Loading Plan Builder inputs…" if self._catalog_model is None
            else "Updating Plan Builder inputs — previous rows and entered work retained…")
        future = self._revision_executor.submit(
            self._catalog_worker.load, force=request["force"], source_ids=request["source_ids"],
            validation_requests=request["validation_requests"], runtime_status=request["runtime_status"],
            guard_context=request["guard_context"], guard_payloads=request["guard_payloads"],
            baseline_catalog=request["baseline_catalog"], accepted_mutations=request["accepted_mutations"])
        emitter = self._catalog_emitter
        def completed(done):
            try:
                model, error = done.result(), None
            except Exception as exc:
                model, error = None, exc
            try:
                emitter.finished.emit((request, model, error))
            except RuntimeError:
                pass
        future.add_done_callback(completed)

    def _on_catalog_ready(self, result):
        request, model, error = result
        self._catalog_pending = False
        if getattr(self, "_revision_closed", False) or not self._tab_active:
            return
        if self._catalog_latest is not None:
            self._start_catalog_request()
            return
        if request["generation"] != self._catalog_generation:
            return
        if ((request["tab_radio"], request["settings_radio"]) != self._host_selection_identity()
                or request["guard_context"][1] != int(self._guided_plan_handoff_device_profile_id or 0)
                or request["runtime_status"] != self._host_runtime_status()):
            self._request_input_catalog()
            return
        if error is not None:
            self._handle_plan_input_failure(error)
            return
        old_model = self._catalog_model
        name_before = self.frequency_plan_combo.currentText()
        retain_name = self._has_authoring_draft() if old_model is not None else False
        if model.acknowledged_mutation:
            selected_id = int(self.frequency_plan_combo.currentData() or 0)
            old_name = str((old_model.plan_index.get(selected_id, {}) if old_model else {}).get("name") or "")
            new_name = str(model.plan_index.get(selected_id, {}).get("name") or "")
            if name_before in {old_name, new_name, f"{old_name} (draft)", f"{new_name} (draft)"}:
                retain_name = False
        self._catalog_name_retention = (name_before, retain_name)
        incoming_guard_context = (request["guard_context"][0] or model.context.selected_radio_id or model.context.primary_runtime_radio_id,
                                  request["guard_context"][1])
        context_changed = self._published_guard_context is not None and (
            self._published_guard_context != incoming_guard_context or self._published_runtime_status != request["runtime_status"])
        if (old_model is not None and self._has_authoring_draft()
                and (context_changed or (old_model.catalog.revision != model.catalog.revision and not model.acknowledged_mutation))):
            self._catalog_review_required = True
            self.review_plan_inputs_btn.show()
        for table, identity, value in request["accepted_mutations"]:
            if self._catalog_mutations.get((table, identity)) == value:
                self._catalog_mutations.pop((table, identity), None)
        self._published_guard_context = incoming_guard_context
        self._published_runtime_status = request["runtime_status"]
        self._catalog_model = model
        self._catalog_store.model = model
        self._guard_advisory_pending.clear()
        self._catalog_store._verified_validations.clear()
        self.settings.publish(model.catalog.settings, model.catalog.config_dir)
        self.plan_context_service.publish(model.context, tab_radio=request["tab_radio"],
                                          settings_radio=request["settings_radio"])
        self._revision_key = (model.catalog.revision, self._week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc)))
        self._input_retry_after = 0.0
        self._input_missing_source = False
        if old_model is None or any(old_model.catalog.settings.get(key) != model.catalog.settings.get(key)
                                    for key in ("ui_theme", "ui_text_size", "band_colors")):
            self._apply_theme()
        self.rebuild_table()
        if self._pending_guided_handoff is not None:
            profile, choice = self._pending_guided_handoff
            self._pending_guided_handoff = None
            if int(profile.get("id") or 0) not in model.device_index:
                self.frequency_plan_action_hint_label.setText("The radio selected for plan setup is unavailable. Reopen the radio configuration to review it.")
            else:
                self.begin_guided_radio_plan_handoff(profile, schedule_choice=choice)
        elif self._pending_catalog_focus is not None:
            plan_id, radio_id = self._pending_catalog_focus
            self._pending_catalog_focus = None
            self.focus_frequency_plan(plan_id, device_profile_id=radio_id)

    # ------------- UI ------------- #

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.planner_scroll = QScrollArea(self)
        self.planner_scroll.setObjectName("freqPlannerContentScroll")
        self.planner_scroll.setWidgetResizable(True)
        self.planner_scroll.setFrameShape(QFrame.NoFrame)
        self.planner_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.planner_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.planner_body = QWidget()
        self.planner_scroll.setWidget(self.planner_body)
        self.planner_scroll.viewport().installEventFilter(self)
        self.planner_scroll.verticalScrollBar().rangeChanged.connect(self._queue_geometry_settle)
        self.setProperty("fioOwnsContentScroll", True)
        outer.addWidget(self.planner_scroll)
        layout = QVBoxLayout(self.planner_body)

        header = QHBoxLayout()
        self.plan_title_label = QLabel("<h3>Plan Builder</h3>")
        header.addWidget(self.plan_title_label)
        self.plan_assignment_header_host = QWidget()
        self.plan_assignment_header_host.setObjectName("freqPlannerPlanAssignmentHeader")
        self.plan_assignment_header_host.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        header.addStretch()
        header.addWidget(self.plan_assignment_header_host)
        self.utc_label = QLabel()
        self.local_label = QLabel()
        self.utc_label.setVisible(False)
        self.local_label.setVisible(False)
        self.time_toggle_btn = QPushButton("Times: Local" if self._show_local else "Times: UTC")
        theme = resolve_theme(self.settings)
        self.time_toggle_btn.setStyleSheet(button_style("primary", theme))
        self.time_toggle_btn.clicked.connect(self._toggle_time_view)
        header.addWidget(self.utc_label)
        header.addWidget(self.local_label)
        layout.addLayout(header)

        self.plan_context_label = PlanContextLabel(
            "freqplanner",
            service=self.plan_context_service,
            fallback_text=FREQPLANNER_CONTEXT_FALLBACK_TEXT,
        )
        self.plan_context_label.setToolTip(
            "Build where-to-be, when-to-be-there, and what-to-do plans from HF Daily, HF Nets, and SOP layers."
        )
        self.plan_context_label.setVisible(False)
        layout.addWidget(self.plan_context_label)
        self.plan_context_label.refresh_context(refresh=True)

        plan_workspace = QVBoxLayout()
        plan_workspace.setSpacing(8)
        plan_select_row = QGridLayout()
        plan_select_row.setSpacing(8)
        self.plan_select_layout = plan_select_row
        self.plan_select_label = QLabel("Plan:")
        plan_select_row.addWidget(self.plan_select_label, 0, 0)
        self.plan_mode_label = QLabel("New")
        self.plan_mode_label.setObjectName("freqPlannerPlanMode")
        self.plan_mode_label.setToolTip("Shows whether Save Plan will create a new plan or update the selected plan.")
        self.frequency_plan_combo = PlannerChoiceCombo()
        self.frequency_plan_combo.setObjectName("freqPlannerFrequencyPlanCombo")
        self.frequency_plan_combo.setEditable(True)
        self.frequency_plan_combo.setInsertPolicy(QComboBox.NoInsert)
        self.frequency_plan_combo.setMinimumWidth(240)
        if self.frequency_plan_combo.lineEdit() is not None:
            self.frequency_plan_combo.lineEdit().setPlaceholderText("Name or select a Frequency Plan")
        self.frequency_plan_combo.currentIndexChanged.connect(self._on_frequency_plan_selected)
        plan_select_row.addWidget(self.frequency_plan_combo, 0, 1)
        plan_select_row.addWidget(self.plan_mode_label, 0, 2)
        plan_actions_row = QVBoxLayout()
        plan_actions_row.setSpacing(8)
        plan_actions_row.setContentsMargins(0, 0, 0, 0)
        self.plan_actions_layout = plan_actions_row
        self.new_plan_btn = QPushButton("New Plan")
        self.save_plan_btn = QPushButton("Save Plan")
        self.save_sop_plan_btn = QPushButton("Save SOP Plan")
        self.rename_plan_btn = QPushButton("Rename Plan")
        self.delete_plan_btn = QPushButton("Delete Plan")
        self.assign_plan_btn = QPushButton("Assign in Configuration")
        self.new_plan_btn.clicked.connect(self._on_new_plan_clicked)
        self.new_plan_btn.setToolTip("Start a new Frequency Plan from the selected Daily, Net, and SOP layers.")
        self.save_plan_btn.clicked.connect(self._on_save_plan_clicked)
        self.save_plan_btn.setToolTip("Save or update the visible HF Daily + HF Nets + SOP projection as a named Frequency Plan.")
        self.save_sop_plan_btn.clicked.connect(self._on_save_sop_plan_clicked)
        self.save_sop_plan_btn.setToolTip("Review side-by-side SOP, HF Daily, HF Net, and Net Resource lanes and save them as an SOP Schedule Plan.")
        self.rename_plan_btn.clicked.connect(self._on_rename_plan_clicked)
        self.rename_plan_btn.setEnabled(False)
        self.rename_plan_btn.setToolTip("Rename the selected Frequency Plan without changing its schedule windows.")
        self.delete_plan_btn.clicked.connect(self._on_delete_plan_clicked)
        self.delete_plan_btn.setToolTip("Delete the selected saved Frequency Plan when it is not assigned to a radio.")
        self.assign_plan_btn.clicked.connect(self._on_assign_plan_clicked)
        self.assign_plan_btn.setEnabled(False)
        self.assign_plan_btn.setToolTip(
            "Select or save a Frequency Plan, then use Configuration > Radios > Schedule Assignment to assign it with RF Guard."
        )
        assignment_header_layout = QHBoxLayout(self.plan_assignment_header_host)
        assignment_header_layout.setContentsMargins(0, 0, 0, 0)
        assignment_header_layout.addWidget(self.assign_plan_btn)
        self.plan_more_btn = QPushButton()
        self.plan_more_btn.setObjectName("freqPlannerPlanMore")
        self.plan_more_btn.setText("More…")
        self.plan_more_btn.setToolTip("Rename or delete the selected Frequency Plan.")
        self.plan_more_menu = QMenu(self.plan_more_btn)
        self.plan_rename_action = QAction("Rename Plan…", self.plan_more_menu)
        self.plan_delete_action = QAction("Delete Plan…", self.plan_more_menu)
        self.plan_more_menu.addAction(self.plan_rename_action)
        self.plan_more_menu.addSeparator()
        self.plan_more_menu.addAction(self.plan_delete_action)
        self.plan_rename_action.triggered.connect(lambda _checked=False: self._on_plan_menu_rename())
        self.plan_delete_action.triggered.connect(lambda _checked=False: self.delete_plan_btn.click())
        self.plan_more_menu.aboutToShow.connect(self._sync_plan_more_actions)
        self.plan_more_btn.setMenu(self.plan_more_menu)
        self.plan_action_controls = [
            self.new_plan_btn,
            self.save_plan_btn,
            self.plan_more_btn,
        ]
        self.rename_plan_btn.setVisible(False)
        self.delete_plan_btn.setVisible(False)
        for action in self.plan_action_controls:
            action.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        initial_actions_row = QHBoxLayout()
        initial_actions_row.setSpacing(8)
        initial_actions_row.setContentsMargins(0, 0, 0, 0)
        for action in self.plan_action_controls:
            initial_actions_row.addWidget(action)
        initial_actions_row.addStretch(1)
        plan_actions_row.addLayout(initial_actions_row)
        self.plan_actions_host = QWidget()
        self.plan_actions_host.setObjectName("freqPlannerPlanActions")
        self.plan_actions_host.setLayout(plan_actions_row)
        for compatibility_action in (self.rename_plan_btn, self.delete_plan_btn):
            compatibility_action.setParent(self.plan_actions_host)
            compatibility_action.hide()
        self.plan_select_controls = [
            self.plan_select_label,
            self.frequency_plan_combo,
            self.plan_mode_label,
        ]
        plan_workspace.addLayout(plan_select_row)
        self.plan_workspace_layout = plan_workspace
        plan_workspace.addWidget(self.plan_actions_host)
        self.plan_assignment_label = QLabel("Radio assignment: No saved plan selected")
        self.plan_assignment_label.setObjectName("freqPlannerRadioAssignment")
        self.plan_assignment_label.setTextFormat(Qt.PlainText)
        self.plan_assignment_label.setWordWrap(True)
        self.plan_assignment_label.setToolTip(
            "Configuration assignment selects the schedule this radio follows after current validation. "
            "Manual QSY changes the radio frequency, not the saved plan."
        )
        self.plan_assignment_label.setVisible(False)
        plan_workspace.addWidget(self.plan_assignment_label)

        self.build_sop_layer_btn = QPushButton("Build SOP Layer")
        self.review_rf_guard_btn = QPushButton("Review RF Guard")
        self.resolve_rf_guard_btn = QPushButton("Resolve RF Guard")
        self.build_sop_layer_btn.clicked.connect(self._on_build_sop_layer_clicked)
        self.build_sop_layer_btn.setToolTip("Open SOP Builder to create or update what-to-do condition layers for this Frequency Plan.")
        self.review_rf_guard_btn.clicked.connect(self._on_review_rf_guard_clicked)
        self.review_rf_guard_btn.setToolTip("Run RF Guard against the visible plan before saving or assigning.")
        self.resolve_rf_guard_btn.clicked.connect(self._on_resolve_rf_guard_clicked)
        self.resolve_rf_guard_btn.setEnabled(False)
        self.resolve_rf_guard_btn.setToolTip("Review RF Guard issues first, then open the radio assignment area to resolve them.")
        layout.addLayout(plan_workspace)

        source_workspace = QGridLayout()
        source_workspace.setSpacing(8)
        self.source_workspace_layout = source_workspace
        self.source_daily_label = QLabel("HF Daily:")
        self.source_net_label = QLabel("HF Nets:")
        self.source_sop_label = QLabel("SOP:")
        self.hf_daily_source_combo = PlannerChoiceCombo()
        self.hf_daily_source_combo.setObjectName("freqPlannerHfDailySourceCombo")
        self.hf_daily_source_combo.setToolTip("Select the active HF Daily schedule or a named schedule saved from the HF Daily tab.")
        self.hf_daily_source_combo.currentIndexChanged.connect(self._on_source_set_selected)
        self.hf_net_source_combo = PlannerChoiceCombo()
        self.hf_net_source_combo.setObjectName("freqPlannerHfNetSourceCombo")
        self.hf_net_source_combo.setToolTip("Select the active HF Net schedule or a named schedule saved from the HF Nets tab.")
        self.hf_net_source_combo.currentIndexChanged.connect(self._on_source_set_selected)
        self.sop_plan_source_combo = PlannerChoiceCombo()
        self.sop_plan_source_combo.setObjectName("freqPlannerSopPlanSourceCombo")
        self.sop_plan_source_combo.setToolTip("Select active SOP Builder layers or a saved SOP Schedule Plan to include as the what-to-do layer.")
        self.sop_plan_source_combo.currentIndexChanged.connect(self._on_source_set_selected)
        self.source_field_hosts = []
        for name, label, combo in (
            ("Daily", self.source_daily_label, self.hf_daily_source_combo),
            ("Net", self.source_net_label, self.hf_net_source_combo),
            ("SOP", self.source_sop_label, self.sop_plan_source_combo),
        ):
            field_host = QWidget()
            field_host.setObjectName(f"freqPlannerSourceField{name}")
            field_layout = QHBoxLayout(field_host)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(4)
            label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            field_layout.addWidget(label)
            field_layout.addWidget(combo, 1)
            self.source_field_hosts.append(field_host)
        self.source_actions_host = QWidget()
        self.source_actions_host.setObjectName("freqPlannerSourceActions")
        source_actions_layout = QHBoxLayout(self.source_actions_host)
        source_actions_layout.setContentsMargins(0, 0, 0, 0)
        source_actions_layout.setSpacing(8)
        self.save_sop_plan_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.build_sop_layer_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        source_actions_layout.addWidget(self.save_sop_plan_btn)
        source_actions_layout.addWidget(self.build_sop_layer_btn)
        source_actions_layout.addStretch(1)
        for column, field_host in enumerate(self.source_field_hosts):
            source_workspace.addWidget(field_host, 0, column)
        source_workspace.addWidget(self.source_actions_host, 0, len(self.source_field_hosts))
        self.source_controls = [
            self.source_daily_label,
            self.hf_daily_source_combo,
            self.source_net_label,
            self.hf_net_source_combo,
            self.source_sop_label,
            self.sop_plan_source_combo,
            self.save_sop_plan_btn,
            self.build_sop_layer_btn,
        ]
        layout.addLayout(source_workspace)

        self.plan_ingredients_frame = QFrame()
        self.plan_ingredients_frame.setObjectName("freqPlannerPlanIngredients")
        self.plan_ingredients_frame.setFrameShape(QFrame.StyledPanel)
        ingredients_layout = QHBoxLayout(self.plan_ingredients_frame)
        ingredients_layout.setContentsMargins(8, 4, 8, 4)
        ingredients_layout.setSpacing(8)
        self.plan_ingredient_plan = QLabel("")
        self.plan_ingredient_daily = QLabel("")
        self.plan_ingredient_nets = QLabel("")
        self.plan_ingredient_sop = QLabel("")
        self.plan_ingredient_assignments = QLabel("")
        for chip in (
            self.plan_ingredient_plan,
            self.plan_ingredient_daily,
            self.plan_ingredient_nets,
            self.plan_ingredient_sop,
            self.plan_ingredient_assignments,
        ):
            chip.setWordWrap(False)
            chip.setMinimumHeight(font_derived_widget_height(chip, vertical_padding=8, floor=0))
            chip.setMinimumWidth(230)
            chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            chip.setStyleSheet(
                f"QLabel {{ border: 1px solid {theme['border']}; border-radius: 6px; "
                f"padding: 4px 8px; background: {theme['surface_alt']}; }}"
            )
            ingredients_layout.addWidget(chip)
        ingredients_layout.addStretch(1)
        self.plan_ingredients_scroll = QScrollArea()
        self.plan_ingredients_scroll.setObjectName("freqPlannerPlanIngredientsScroll")
        self.plan_ingredients_scroll.setWidgetResizable(True)
        self.plan_ingredients_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.plan_ingredients_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.plan_ingredients_scroll.setFrameShape(QFrame.NoFrame)
        self.plan_ingredients_scroll.setWidget(self.plan_ingredients_frame)
        self.plan_ingredients_scroll.setVisible(False)
        layout.addWidget(self.plan_ingredients_scroll)

        self.plan_layers_label = QLabel("")
        self.plan_layers_label.setObjectName("freqPlannerPlanLayers")
        self.plan_layers_label.setWordWrap(False)
        self.plan_layers_label.setMaximumHeight(
            font_derived_widget_height(self.plan_layers_label, vertical_padding=8, floor=0)
        )
        self.plan_layers_label.setToolTip(
            "Shows the selected Daily baseline, Net overlay, SOP condition layer, and review state for this Frequency Plan."
        )
        self.plan_layers_label.setVisible(False)

        self.plan_review_controls_frame = QFrame()
        self.plan_review_controls_frame.setObjectName("freqPlannerReviewControls")
        self.plan_review_controls_frame.setFrameShape(QFrame.NoFrame)
        review_controls_layout = QVBoxLayout(self.plan_review_controls_frame)
        review_controls_layout.setContentsMargins(0, 0, 0, 0)
        review_controls_layout.setSpacing(2)
        view_workspace = QHBoxLayout()
        view_workspace.setSpacing(6)
        view_workspace.addWidget(QLabel("Table View:"))
        self.planner_view_combo = QComboBox()
        self.planner_view_combo.setObjectName("freqPlannerViewCombo")
        self.planner_view_combo.addItem("Effective Windows", "effective")
        self.planner_view_combo.addItem("Pattern Summary", "patterns")
        self.planner_view_combo.addItem("Radio Windows", "radio")
        self.planner_view_combo.addItem("Week Grid", "blended")
        self.planner_view_combo.addItem("SOP Lanes", "operational")
        self.planner_view_combo.currentIndexChanged.connect(self._on_planner_view_changed)
        view_workspace.addWidget(self.planner_view_combo)
        view_workspace.addWidget(self.time_toggle_btn)
        self.band_toggle_btn = QPushButton("Band/Freq: Band")
        self.band_toggle_btn.setStyleSheet(button_style("info", theme))
        self.band_toggle_btn.clicked.connect(self._toggle_band_view)
        view_workspace.addWidget(self.band_toggle_btn)
        self.operational_day_label = QLabel("Day:")
        self.operational_day_label.setVisible(False)
        view_workspace.addWidget(self.operational_day_label)
        self.operational_day_combo = QComboBox()
        self.operational_day_combo.setObjectName("freqPlannerOperationalDayCombo")
        for day in DAY_NAMES:
            self.operational_day_combo.addItem(day, day)
        self.operational_day_combo.currentIndexChanged.connect(self._on_planner_view_changed)
        self.operational_day_combo.setEnabled(False)
        self.operational_day_combo.setVisible(False)
        view_workspace.addWidget(self.operational_day_combo)
        self.radio_window_radio_label = QLabel("Radio:")
        self.radio_window_radio_label.setVisible(False)
        view_workspace.addWidget(self.radio_window_radio_label)
        self.radio_window_radio_combo = QComboBox()
        self.radio_window_radio_combo.setObjectName("freqPlannerRadioWindowRadioCombo")
        self.radio_window_radio_combo.setToolTip("Filter Radio Windows to one assigned radio.")
        self.radio_window_radio_combo.currentIndexChanged.connect(self._on_radio_window_radio_changed)
        self.radio_window_radio_combo.setVisible(False)
        view_workspace.addWidget(self.radio_window_radio_combo)
        self.band_legend = QWidget()
        self.band_legend_layout = QHBoxLayout(self.band_legend)
        self.band_legend_layout.setContentsMargins(0, 0, 0, 0)
        self.band_legend_layout.setSpacing(6)
        view_workspace.addWidget(self.band_legend)
        view_workspace.addStretch(1)
        view_workspace.addWidget(self.review_rf_guard_btn)
        view_workspace.addWidget(self.resolve_rf_guard_btn)
        self.plan_review_toolbar = QWidget()
        self.plan_review_toolbar.setObjectName("freqPlannerReviewToolbar")
        self.plan_review_toolbar.setLayout(view_workspace)
        self.plan_review_toolbar_scroll = QScrollArea()
        self.plan_review_toolbar_scroll.setObjectName("freqPlannerReviewToolbarScroll")
        self.plan_review_toolbar_scroll.setWidgetResizable(True)
        self.plan_review_toolbar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.plan_review_toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.plan_review_toolbar_scroll.setFrameShape(QFrame.NoFrame)
        self.plan_review_toolbar_scroll.setWidget(self.plan_review_toolbar)
        review_controls_layout.addWidget(self.plan_review_toolbar_scroll)

        self.frequency_plan_summary_label = QLabel("")
        self.frequency_plan_summary_label.setObjectName("freqPlannerFrequencyPlanSummary")
        self.frequency_plan_summary_label.setWordWrap(True)
        self.frequency_plan_summary_label.setVisible(False)
        review_controls_layout.addWidget(self.frequency_plan_summary_label)
        self.frequency_plan_action_hint_label = QLabel(
            "Build a named plan by selecting HF Daily, HF Nets, and optional SOP layers. Use New Plan when you want a separate plan instead of updating the selected one."
        )
        self.frequency_plan_action_hint_label.setObjectName("freqPlannerFrequencyPlanActionHint")
        self.frequency_plan_action_hint_label.setWordWrap(False)
        self.frequency_plan_action_hint_label.setMaximumHeight(
            font_derived_widget_height(self.frequency_plan_action_hint_label, vertical_padding=8, floor=0)
        )
        input_status_row = QVBoxLayout()
        input_status_row.setSpacing(4)
        input_status_row.addWidget(self.frequency_plan_action_hint_label)
        input_recovery_host = QWidget()
        input_recovery_buttons = WrappingControlLayout(input_recovery_host, alignment=Qt.AlignLeft)
        input_status_row.addWidget(input_recovery_host)
        self.retry_plan_inputs_btn = QPushButton("Retry inputs")
        self.retry_plan_inputs_btn.clicked.connect(self._retry_plan_inputs)
        self.retry_plan_inputs_btn.hide()
        input_recovery_buttons.addWidget(self.retry_plan_inputs_btn)
        self.review_plan_inputs_btn = QPushButton("Review changed inputs")
        self.review_plan_inputs_btn.clicked.connect(self._review_changed_inputs)
        self.review_plan_inputs_btn.hide()
        input_recovery_buttons.addWidget(self.review_plan_inputs_btn)
        self.review_sop_draft_btn = QPushButton("Review retained SOP edit")
        self.review_sop_draft_btn.clicked.connect(self._resume_sop_edit_draft)
        self.review_sop_draft_btn.hide()
        input_recovery_buttons.addWidget(self.review_sop_draft_btn)
        review_controls_layout.addLayout(input_status_row)
        layout.addWidget(self.plan_review_controls_frame)
        self.rf_guard_review_card = QFrame()
        self.rf_guard_review_card.setObjectName("freqPlannerRfGuardReviewCard")
        self.rf_guard_review_card.setFrameShape(QFrame.StyledPanel)
        rf_guard_review_layout = QVBoxLayout(self.rf_guard_review_card)
        rf_guard_review_layout.setContentsMargins(10, 6, 10, 6)
        rf_guard_review_layout.setSpacing(6)
        rf_guard_review_header = QHBoxLayout()
        self.rf_guard_review_title_label = QLabel("RF Guard Review")
        self.rf_guard_review_title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.rf_guard_review_summary_label = QLabel("Review conflicts before assigning or updating this plan.")
        self.rf_guard_review_summary_label.setWordWrap(True)
        rf_guard_review_header.addWidget(self.rf_guard_review_title_label)
        rf_guard_review_header.addWidget(self.rf_guard_review_summary_label, 1)
        rf_guard_review_layout.addLayout(rf_guard_review_header)
        self.rf_guard_review_table = QTableWidget(0, 4)
        self.rf_guard_review_table.setObjectName("freqPlannerRfGuardReviewTable")
        self.rf_guard_review_table.setHorizontalHeaderLabels(["Level", "Issue", "Impact", "Next"])
        self.rf_guard_review_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.rf_guard_review_table.setSelectionMode(QTableWidget.SingleSelection)
        self.rf_guard_review_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.rf_guard_review_table.itemSelectionChanged.connect(self._on_rf_guard_review_selection_changed)
        self.rf_guard_review_table.itemDoubleClicked.connect(lambda _item: self._on_resolve_rf_guard_clicked())
        rf_header = self.rf_guard_review_table.horizontalHeader()
        rf_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        rf_header.setSectionResizeMode(1, QHeaderView.Stretch)
        rf_header.setSectionResizeMode(2, QHeaderView.Stretch)
        rf_header.setSectionResizeMode(3, QHeaderView.Stretch)
        rf_guard_review_layout.addWidget(self.rf_guard_review_table)
        self.rf_guard_review_card.setVisible(False)
        layout.addWidget(self.rf_guard_review_card)
        self.cell_inspector_label = QLabel("Select a schedule cell to review the blended HF Daily, HF Nets, and SOP sources.")
        self.cell_inspector_label.setObjectName("freqPlannerCellInspector")
        self.cell_inspector_label.setWordWrap(True)
        self.cell_inspector_label.setVisible(False)
        self.selected_window_card = QFrame()
        self.selected_window_card.setObjectName("freqPlannerSelectedWindowCard")
        self.selected_window_card.setFrameShape(QFrame.StyledPanel)
        selected_layout = QHBoxLayout(self.selected_window_card)
        selected_layout.setContentsMargins(10, 6, 10, 6)
        selected_layout.setSpacing(12)
        self.selected_window_title_label = QLabel("Select a window")
        self.selected_window_title_label.setObjectName("freqPlannerSelectedWindowTitle")
        self.selected_window_title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.selected_window_detail_label = QLabel("Click a row to review and edit its Daily, Net, or SOP source.")
        self.selected_window_detail_label.setObjectName("freqPlannerSelectedWindowDetail")
        self.selected_window_detail_label.setWordWrap(True)
        selected_layout.addWidget(self.selected_window_title_label, 0)
        selected_layout.addWidget(self.selected_window_detail_label, 1)
        self.inline_editor_card = QFrame()
        self.inline_editor_card.setObjectName("freqPlannerInlineEditorCard")
        self.inline_editor_card.setFrameShape(QFrame.StyledPanel)
        # Details are progressive disclosure. Hide the editor before the first
        # asynchronous projection completes so compact startup never flashes a
        # large empty form over the timeline.
        self.inline_editor_card.setVisible(False)
        inline_layout = QVBoxLayout(self.inline_editor_card)
        inline_layout.setContentsMargins(10, 6, 10, 6)
        inline_layout.setSpacing(6)
        inline_header = QHBoxLayout()
        self.inline_editor_title_label = QLabel("Selected Window Editor")
        self.inline_editor_title_label.setStyleSheet(label_style("text", theme, weight=700))
        self.inline_editor_scope_label = QLabel("Select an Effective Windows row to edit.")
        self.inline_editor_scope_label.setWordWrap(True)
        inline_header.addWidget(self.inline_editor_title_label)
        inline_header.addWidget(self.inline_editor_scope_label, 1)
        inline_layout.addLayout(inline_header)
        self.inline_editor_impact_label = QLabel("Select a window to see what will change.")
        self.inline_editor_impact_label.setObjectName("freqPlannerInlineEditorImpact")
        self.inline_editor_impact_label.setWordWrap(True)
        inline_layout.addWidget(self.inline_editor_impact_label)
        inline_identity_row = QGridLayout()
        inline_identity_row.setSpacing(8)
        self.inline_identity_row = inline_identity_row
        self.inline_group_edit = QLineEdit()
        self.inline_group_edit.setPlaceholderText("Group")
        self.inline_band_edit = QLineEdit()
        self.inline_band_edit.setPlaceholderText("Band")
        self.inline_frequency_edit = QLineEdit()
        self.inline_frequency_edit.setPlaceholderText("Freq")
        self.inline_start_edit = QLineEdit()
        self.inline_start_edit.setPlaceholderText("Start UTC")
        self.inline_end_edit = QLineEdit()
        self.inline_end_edit.setPlaceholderText("End UTC")
        self.inline_mode_edit = QLineEdit()
        self.inline_mode_edit.setPlaceholderText("Mode")
        inline_identity_widgets = []
        for label_text, widget in (
            ("Group", self.inline_group_edit),
            ("Band", self.inline_band_edit),
            ("Freq", self.inline_frequency_edit),
        ):
            label = QLabel(label_text)
            inline_identity_widgets.append((label, widget))
            col = len(inline_identity_widgets) - 1
            inline_identity_row.addWidget(label, 0, col * 2)
            inline_identity_row.addWidget(widget, 0, col * 2 + 1)
        self.inline_identity_widgets = inline_identity_widgets
        inline_layout.addLayout(inline_identity_row)
        inline_timing_row = QGridLayout()
        inline_timing_row.setSpacing(8)
        self.inline_timing_row = inline_timing_row
        inline_timing_widgets = []
        for label_text, widget in (
            ("Start", self.inline_start_edit),
            ("End", self.inline_end_edit),
            ("Mode", self.inline_mode_edit),
        ):
            label = QLabel(label_text)
            inline_timing_widgets.append((label, widget))
            col = len(inline_timing_widgets) - 1
            inline_timing_row.addWidget(label, 0, col * 2)
            inline_timing_row.addWidget(widget, 0, col * 2 + 1)
        self.inline_timing_widgets = inline_timing_widgets
        self.inline_update_plan_btn = QPushButton("Update Plan Only")
        self.inline_update_hf_daily_btn = QPushButton("Update Source")
        self.inline_update_plan_btn.clicked.connect(self._on_inline_update_plan_clicked)
        self.inline_update_hf_daily_btn.clicked.connect(self._on_inline_update_hf_daily_clicked)
        inline_timing_row.addWidget(self.inline_update_plan_btn, 0, 6)
        inline_timing_row.addWidget(self.inline_update_hf_daily_btn, 0, 7)
        inline_layout.addLayout(inline_timing_row)
        inspector_actions = QGridLayout()
        self.inspector_actions_layout = inspector_actions
        self.edit_hf_daily_btn = QPushButton("Edit HF Daily")
        self.edit_hf_net_btn = QPushButton("Edit HF Net")
        self.open_sop_builder_btn = QPushButton("Open SOP Builder")
        self.edit_sop_plan_entry_btn = QPushButton("Edit Plan Entry")
        self.edit_hf_daily_btn.clicked.connect(self._on_edit_hf_daily_clicked)
        self.edit_hf_net_btn.clicked.connect(self._on_edit_hf_net_clicked)
        self.open_sop_builder_btn.clicked.connect(self._on_open_sop_builder_clicked)
        self.edit_sop_plan_entry_btn.clicked.connect(self._on_edit_sop_plan_entry_clicked)
        for column, btn in enumerate((self.edit_hf_daily_btn, self.edit_hf_net_btn, self.open_sop_builder_btn, self.edit_sop_plan_entry_btn)):
            btn.setEnabled(False)
            inspector_actions.addWidget(btn, 0, column)
        inspector_actions.setColumnStretch(4, 1)
        self._refresh_plan_workspace_header()

        self.table = QTableWidget()
        self.table.setRowCount(24)
        self.table.setColumnCount(9)  # UTC, Local, Sun..Sat
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.cellClicked.connect(self._on_schedule_cell_clicked)

        # Set headers with local TZ name in Local column
        tz_name, tz_abbr = self._current_timezone_label()
        self.table.setHorizontalHeaderLabels(
            [
                "UTC Hour",
                f"Local Time ({tz_abbr})",
                "Sunday",
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
            ]
        )

        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(self.COL_UTC, QHeaderView.Stretch)
        hv.setSectionResizeMode(self.COL_LOCAL, QHeaderView.Stretch)
        for col in range(self.COL_DAY_OFFSET, 9):
            hv.setSectionResizeMode(col, QHeaderView.Stretch)
        hv.setHighlightSections(False)

        layout.addWidget(self.table, stretch=1)
        layout.addWidget(self.cell_inspector_label)
        layout.addWidget(self.selected_window_card)
        layout.addWidget(self.inline_editor_card)
        layout.addLayout(inspector_actions)

        self._setup_clock_timer()
        self._load_band_colors()
        self._refresh_source_set_controls()
        self._render_band_legend()
        self.frequency_plan_action_hint_label.setWordWrap(True)
        self._update_responsive_height_bounds()
        self.rf_guard_review_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._apply_responsive_layout(force=True)

    @staticmethod
    def _reflow_grid(layout: QGridLayout, placements: List[Tuple[QWidget, int, int, int, int]]) -> None:
        """Move existing controls within a grid without duplicating widgets."""
        for widget, _row, _column, _rowspan, _colspan in placements:
            layout.removeWidget(widget)
        for widget, row, column, rowspan, colspan in placements:
            layout.addWidget(widget, row, column, rowspan, colspan)

    def _update_responsive_height_bounds(self) -> None:
        """Bound secondary surfaces without clipping Large Text."""
        line = max(18, int(self.fontMetrics().lineSpacing()))
        self.frequency_plan_action_hint_label.setMaximumHeight(max(48, line * 3 + 8))
        self.selected_window_card.setMaximumHeight(max(76, line * 3 + 18))
        self.rf_guard_review_table.setMaximumHeight(max(190, line * 7 + 36))
        self._update_toolbar_height_bounds()
        self._update_inline_editor_height_bounds()

    def _update_inline_editor_height_bounds(self) -> None:
        card = getattr(self, "inline_editor_card", None)
        if card is None:
            return
        if card.isHidden():
            card.setMinimumHeight(0)
            return
        layout = card.layout()
        self.inline_identity_row.invalidate()
        self.inline_timing_row.invalidate()
        layout.invalidate()
        margins = self.planner_body.layout().contentsMargins()
        width = max(1, self.planner_scroll.viewport().width() - margins.left() - margins.right() - 2 * card.frameWidth())
        height = max(layout.minimumSize().height(), layout.totalHeightForWidth(width)) + 2 * card.frameWidth()
        if card.minimumHeight() != height:
            card.setMinimumHeight(height)

    def _update_toolbar_height_bounds(self) -> None:
        """Reserve text/control height plus the horizontal scrollbar gutter."""
        for name in ("plan_ingredients_scroll", "plan_review_toolbar_scroll"):
            scroll = getattr(self, name, None)
            if scroll is None:
                continue
            content = scroll.widget()
            content.layout().activate()
            height = max(content.minimumSizeHint().height(), content.sizeHint().height())
            height += scroll.horizontalScrollBar().sizeHint().height() + 2 * scroll.frameWidth()
            if scroll.minimumHeight() != height or scroll.maximumHeight() != height:
                scroll.setFixedHeight(height)

    def _update_content_height_floor(self) -> None:
        if getattr(self, "_content_height_refresh_running", False):
            return
        self._content_height_refresh_running = True
        try:
            layout = self.planner_body.layout()
            layout.invalidate()
            # Wrapped review/editor text can need more than minimumSize reports.
            # Reserve that height so the footer cannot overlap the row editor.
            height = max(layout.minimumSize().height(), layout.totalHeightForWidth(self.planner_scroll.viewport().width()))
            if self.planner_body.minimumHeight() != height:
                self.planner_body.setMinimumHeight(height)
        finally:
            self._content_height_refresh_running = False

    def _refresh_font_layout(self) -> None:
        self._font_layout_refresh_pending = False
        if (not hasattr(self, "inline_mode_edit") or getattr(self, "_revision_closed", False)
                or getattr(self, "_font_layout_refresh_running", False)):
            return
        # Stylesheet-polished controls can retain the old inherited font after
        # QApplication changes it. Keep this workspace on the selected app size.
        app = QApplication.instance()
        if app is not None:
            self._font_layout_refresh_running = True
            try:
                app_font = app.font() if getattr(self, "_application_font_refresh", False) else self.font()
                self._application_font_refresh = False
                self.setFont(app_font)
                for control in self.findChildren(QWidget):
                    if isinstance(control, (QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget, QHeaderView)):
                        font = control.font()
                        if app_font.pointSizeF() > 0:
                            font.setPointSizeF(app_font.pointSizeF())
                        else:
                            font.setPixelSize(app_font.pixelSize())
                        if font != control.font():
                            control.setFont(font)
            finally:
                self._font_layout_refresh_running = False
        apply_text_size_accessibility_guards(self, include_widths=False)
        for chip in (self.plan_ingredient_plan, self.plan_ingredient_daily, self.plan_ingredient_nets,
                     self.plan_ingredient_sop, self.plan_ingredient_assignments):
            chip.setMinimumHeight(font_derived_widget_height(chip, vertical_padding=8, floor=0))
        for field, example in ((self.inline_group_edit, "Operating Group"), (self.inline_band_edit, "160M"),
                               (self.inline_frequency_edit, "000.000000"), (self.inline_start_edit, "00:00"),
                               (self.inline_end_edit, "00:00"), (self.inline_mode_edit, "DIGI")):
            field.setMinimumHeight(font_derived_widget_height(field))
            margins = field.textMargins()
            width = field.fontMetrics().horizontalAdvance(example) + 2 * field.fontMetrics().horizontalAdvance("M")
            field.setMinimumWidth(width + margins.left() + margins.right())
        self._apply_responsive_layout(force=True)
        if hasattr(self, "table"):
            row_height = font_derived_widget_height(self.table, include_size_hints=False)
            self.table.setMinimumHeight(self.table.horizontalHeader().sizeHint().height() + row_height * 2
                                        + self.table.horizontalScrollBar().sizeHint().height() + 2 * self.table.frameWidth())
        self._update_content_height_floor()
        self._queue_geometry_settle()

    def _queue_geometry_settle(self, *_args) -> None:
        if getattr(self, "_geometry_settle_pending", False):
            return
        self._geometry_settle_pending = True
        QTimer.singleShot(0, self._settle_editor_geometry)

    def _settle_editor_geometry(self) -> None:
        self._geometry_settle_pending = False
        if getattr(self, "_revision_closed", False):
            return
        self._apply_responsive_layout(force=True)
        self._update_inline_editor_height_bounds()
        self._update_content_height_floor()

    def _apply_responsive_layout(self, *, force: bool = False) -> None:
        """Keep the Plan Builder usable at compact sizes without losing actions."""
        if not hasattr(self, "plan_select_layout") or not hasattr(self, "source_workspace_layout"):
            return
        width = max(0, int(self.planner_scroll.viewport().width()))
        height = max(0, int(self.height()))
        # Existing wide rows must fit their actual font, including OS text size.
        plan_width = sum(widget.sizeHint().width() for widget in (*self.plan_select_controls, *self.plan_action_controls)
                         if not widget.isHidden())
        # Source selectors have their own capped, font-derived field budget;
        # long option text must not force the whole workspace into compact mode.
        wide_floor = max(1100, plan_width + 80)
        mode = "compact" if width < wide_floor or height < 700 else "wide"
        body_margins = self.planner_body.layout().contentsMargins()
        content_width = max(1, width - body_margins.left() - body_margins.right())
        if (hasattr(self, "inline_editor_card") and self.inline_editor_card.isVisible()
                and max(self.inline_editor_card.minimumSizeHint().width(),
                        self.inline_editor_card.sizeHint().width()) > content_width):
            mode = "compact"
        last_width = int(getattr(self, "_responsive_layout_width", -1))
        if (not force and mode == getattr(self, "_responsive_layout_mode", "")
                and abs(width - last_width) < 2):
            return
        self._responsive_layout_mode = mode
        self._responsive_layout_width = width

        if mode == "compact":
            plan_placements = [
                (self.plan_select_label, 0, 0, 1, 1),
                (self.frequency_plan_combo, 0, 1, 1, 3),
                (self.plan_mode_label, 0, 4, 1, 1),
            ]
            for column in range(5):
                self.plan_select_layout.setColumnStretch(column, 0)
            self.plan_select_layout.setColumnStretch(1, 1)
            for combo in (self.frequency_plan_combo, self.hf_daily_source_combo, self.hf_net_source_combo,
                          self.sop_plan_source_combo):
                combo.setMinimumWidth(0)
                combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.plan_select_layout.setVerticalSpacing(4)
            self.plan_actions_layout.setSpacing(4)
            self.source_workspace_layout.setVerticalSpacing(4)
        else:
            plan_placements = [
                (self.plan_select_label, 0, 0, 1, 1),
                (self.frequency_plan_combo, 0, 1, 1, 1),
                (self.plan_mode_label, 0, 2, 1, 1),
            ]
            self.frequency_plan_combo.setMinimumWidth(240)
            for combo in (self.frequency_plan_combo, self.hf_daily_source_combo, self.hf_net_source_combo,
                          self.sop_plan_source_combo):
                combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.plan_select_layout.setVerticalSpacing(8)
            self.plan_actions_layout.setSpacing(8)
            self.source_workspace_layout.setVerticalSpacing(8)

        # The plan toolbar uses width/content fit; a short viewport must not
        # force a second toolbar row when the controls fit horizontally.
        toolbar_mode = "wide" if width >= max(700, plan_width + 80) else "compact"
        if toolbar_mode == "wide":
            plan_placements = [
                (self.plan_select_label, 0, 0, 1, 1),
                (self.frequency_plan_combo, 0, 1, 1, 1),
                (self.plan_mode_label, 0, 2, 1, 1),
            ]
            for column in range(5):
                self.plan_select_layout.setColumnStretch(column, 0)
            self.plan_select_layout.setColumnStretch(1, 1)
            self.frequency_plan_combo.setMinimumWidth(240)
            self.frequency_plan_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.plan_select_layout.setVerticalSpacing(8)
            self.plan_actions_layout.setSpacing(8)

        # Keep the actions beside the selector whenever their actual width fits.
        self.plan_workspace_layout.removeWidget(self.plan_actions_host)
        self.plan_select_layout.removeWidget(self.plan_actions_host)
        if toolbar_mode == "wide":
            self.plan_select_layout.addWidget(self.plan_actions_host, 0, 3, 1, 1)
        else:
            self.plan_workspace_layout.insertWidget(1, self.plan_actions_host)
        self._reflow_grid(self.plan_select_layout, plan_placements)
        action_margins = self.plan_actions_layout.contentsMargins()
        action_width = max(1, width - body_margins.left() - body_margins.right()
                           - action_margins.left() - action_margins.right())
        action_placements = self._plan_action_placements(action_width, toolbar_mode)
        self._reflow_plan_action_rows(action_placements)
        source_placements = self._source_layout_placements(width)
        self._reflow_grid(self.source_workspace_layout, source_placements)

        # Inline editing is secondary detail; stack its fields only when a user opens it.
        def _inline_placements(compact: bool):
            if compact:
                identity = []
                for row, (label, widget) in enumerate(self.inline_identity_widgets):
                    identity.extend(((label, row, 0, 1, 1), (widget, row, 1, 1, 3)))
                timing = []
                for row, (label, widget) in enumerate(self.inline_timing_widgets):
                    timing.extend(((label, row, 0, 1, 1), (widget, row, 1, 1, 3)))
                timing.extend(
                    ((self.inline_update_plan_btn, 3, 0, 1, 4),
                     (self.inline_update_hf_daily_btn, 4, 0, 1, 4))
                )
                return identity, timing
            identity = []
            for column, (label, widget) in enumerate(self.inline_identity_widgets):
                identity.extend(((label, 0, column * 2, 1, 1), (widget, 0, column * 2 + 1, 1, 1)))
            timing = []
            for column, (label, widget) in enumerate(self.inline_timing_widgets):
                timing.extend(((label, 0, column * 2, 1, 1), (widget, 0, column * 2 + 1, 1, 1)))
            timing.extend(
                ((self.inline_update_plan_btn, 0, 6, 1, 1),
                 (self.inline_update_hf_daily_btn, 0, 7, 1, 1))
            )
            return identity, timing

        inline_identity, inline_timing = _inline_placements(mode == "compact")
        if mode == "compact":
            for _label, widget in (*self.inline_identity_widgets, *self.inline_timing_widgets):
                widget.setMinimumWidth(0)
                widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            for widget in (self.inline_update_plan_btn, self.inline_update_hf_daily_btn):
                widget.setMinimumWidth(0)
                widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            for column in range(8):
                self.inline_identity_row.setColumnMinimumWidth(column, 0)
                self.inline_identity_row.setColumnStretch(column, 0)
                self.inline_timing_row.setColumnMinimumWidth(column, 0)
                self.inline_timing_row.setColumnStretch(column, 0)
        self._reflow_grid(self.inline_identity_row, inline_identity)
        self._reflow_grid(self.inline_timing_row, inline_timing)

        # Reflowing from a compact editor to a wide editor can make the
        # intrinsic wide row wider than the current viewport. Re-evaluate once
        # after the actual placements, then keep the compact arrangement.
        if (mode == "wide" and self.inline_editor_card.isVisible()
                and max(self.inline_editor_card.minimumSizeHint().width(),
                        self.inline_editor_card.sizeHint().width()) > content_width):
            mode = "compact"
            self._responsive_layout_mode = mode
            for _label, widget in (*self.inline_identity_widgets, *self.inline_timing_widgets):
                widget.setMinimumWidth(0)
                widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            for widget in (self.inline_update_plan_btn, self.inline_update_hf_daily_btn):
                widget.setMinimumWidth(0)
                widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            for column in range(8):
                self.inline_identity_row.setColumnMinimumWidth(column, 0)
                self.inline_identity_row.setColumnStretch(column, 0)
                self.inline_timing_row.setColumnMinimumWidth(column, 0)
                self.inline_timing_row.setColumnStretch(column, 0)
            inline_identity, inline_timing = _inline_placements(True)
            self._reflow_grid(self.inline_identity_row, inline_identity)
            self._reflow_grid(self.inline_timing_row, inline_timing)
        buttons = (self.edit_hf_daily_btn, self.edit_hf_net_btn, self.open_sop_builder_btn, self.edit_sop_plan_entry_btn)
        columns = 2 if mode == "compact" else 4
        self.inspector_actions_layout.setColumnStretch(4, 0 if mode == "compact" else 1)
        self._reflow_grid(self.inspector_actions_layout,
                          [(button, index // columns, index % columns, 1, 1) for index, button in enumerate(buttons)])
        self._fit_assignment_button_text()
        self._update_responsive_height_bounds()
        if mode == "compact":
            self.frequency_plan_action_hint_label.setMaximumHeight(
                max(self.frequency_plan_action_hint_label.maximumHeight(), self.fontMetrics().lineSpacing() * 3 + 16)
            )

    def _plan_action_placements(self, width: int, mode: str) -> List[Tuple[QWidget, int, int, int, int]]:
        """Pack plan actions at natural widths, wrapping only when needed."""
        controls = [button for button in self.plan_action_controls if not button.isHidden()]
        if not controls:
            return []
        if mode == "wide":
            return [(button, 0, column, 1, 1) for column, button in enumerate(controls)]
        spacing = max(0, self.plan_actions_layout.spacing())
        placements: List[Tuple[QWidget, int, int, int, int]] = []
        row = 0
        column = 0
        used = 0
        for button in controls:
            natural = max(button.sizeHint().width(), button.minimumSizeHint().width())
            required = natural if column == 0 else spacing + natural
            if column and used + required > width:
                row += 1
                column = 0
                used = 0
                required = natural
            placements.append((button, row, column, 1, 1))
            used += required
            column += 1
        return placements

    def _source_field_budget(self, label: QLabel, combo: QComboBox) -> int:
        """Reserve a readable, font-derived width without letting one source name own the row."""
        metrics = combo.fontMetrics()
        padding = 2 * max(1, metrics.horizontalAdvance("MM"))
        semantic_floor = metrics.horizontalAdvance("Active Daily Schedule")
        current_width = metrics.horizontalAdvance(str(combo.currentText() or "")[:32])
        return label.sizeHint().width() + 4 + max(semantic_floor, current_width) + padding

    def _source_layout_placements(self, viewport_width: int) -> List[Tuple[QWidget, int, int, int, int]]:
        """Place source fields by content width; source actions wrap as one natural-width group."""
        hosts = list(self.source_field_hosts)
        spacing = max(4, self.source_workspace_layout.horizontalSpacing())
        margins = self.source_workspace_layout.contentsMargins()
        body_margins = self.planner_body.layout().contentsMargins()
        available = max(1, int(viewport_width) - body_margins.left() - body_margins.right()
                        - margins.left() - margins.right())
        budgets = [
            self._source_field_budget(label, combo)
            for label, combo in (
                (self.source_daily_label, self.hf_daily_source_combo),
                (self.source_net_label, self.hf_net_source_combo),
                (self.source_sop_label, self.sop_plan_source_combo),
            )
        ]
        for host, budget, (label, combo) in zip(
            hosts,
            budgets,
            (
                (self.source_daily_label, self.hf_daily_source_combo),
                (self.source_net_label, self.hf_net_source_combo),
                (self.source_sop_label, self.sop_plan_source_combo),
            ),
        ):
            host.setMinimumWidth(budget)
            combo.setMinimumWidth(max(0, budget - label.sizeHint().width() - 4))
        max_budget = max(budgets)
        columns = max(1, min(3, (available + spacing) // max(1, max_budget + spacing)))
        if columns == 1:
            budgets = [min(budget, available) for budget in budgets]
            for host, budget, (label, combo) in zip(
                hosts,
                budgets,
                (
                    (self.source_daily_label, self.hf_daily_source_combo),
                    (self.source_net_label, self.hf_net_source_combo),
                    (self.source_sop_label, self.sop_plan_source_combo),
                ),
            ):
                host.setMinimumWidth(budget)
                combo.setMinimumWidth(max(0, budget - label.sizeHint().width() - 4))
        actions_visible = any(not button.isHidden() for button in (self.save_sop_plan_btn, self.build_sop_layer_btn))
        self.source_actions_host.setVisible(actions_visible)
        action_width = self.source_actions_host.sizeHint().width() if actions_visible else 0
        field_width = sum(budgets[:columns]) + spacing * (columns - 1)
        actions_inline = bool(actions_visible and columns == 3
                              and field_width + spacing + action_width <= available)
        field_rows = (len(hosts) + columns - 1) // columns
        placements: List[Tuple[QWidget, int, int, int, int]] = []
        for index, host in enumerate(hosts):
            placements.append((host, index // columns, index % columns, 1, 1))
        if actions_visible:
            if actions_inline:
                placements.append((self.source_actions_host, 0, columns, 1, 1))
            elif (columns == 2
                  and max(budgets[0::2]) + spacing
                  + max(action_width, *budgets[1::2]) <= available):
                placements.append((self.source_actions_host, field_rows - 1, 1, 1, 1))
            else:
                placements.append((self.source_actions_host, field_rows, 0, 1, columns))
        for column in range(8):
            self.source_workspace_layout.setColumnStretch(column, 0)
        for column in range(columns):
            self.source_workspace_layout.setColumnStretch(column, 1)
        for host in (*hosts, self.source_actions_host):
            self.source_workspace_layout.removeWidget(host)
        return placements

    def _reflow_plan_action_rows(self, placements: List[Tuple[QWidget, int, int, int, int]]) -> None:
        """Rebuild action rows independently so wrapped rows do not share grid columns."""
        signature = (self.plan_actions_layout.spacing(),
                     tuple((id(widget), row, column) for widget, row, column, _row_span, _column_span in placements))
        if signature == getattr(self, "_plan_action_layout_signature", None):
            return
        self._plan_action_layout_signature = signature
        layout = self.plan_actions_layout
        while layout.count():
            item = layout.takeAt(0)
            row_layout = item.layout()
            if row_layout is not None:
                while row_layout.count():
                    row_layout.takeAt(0)
                row_layout.deleteLater()
        rows: Dict[int, List[Tuple[int, QWidget]]] = {}
        for widget, row, column, _row_span, _column_span in placements:
            rows.setdefault(row, []).append((column, widget))
        for row in sorted(rows):
            row_layout = QHBoxLayout()
            row_layout.setSpacing(max(0, layout.spacing()))
            row_layout.setContentsMargins(0, 0, 0, 0)
            for _column, widget in sorted(rows[row], key=lambda item: item[0]):
                row_layout.addWidget(widget)
            row_layout.addStretch(1)
            layout.addLayout(row_layout)

    def _on_planner_view_changed(self) -> None:
        self._operation_feedback = ""
        day_visible = self._planner_view_mode() == "operational"
        if hasattr(self, "operational_day_label"):
            self.operational_day_label.setVisible(day_visible)
        if hasattr(self, "operational_day_combo"):
            self.operational_day_combo.setEnabled(day_visible)
            self.operational_day_combo.setVisible(day_visible)
        radio_visible = self._planner_view_mode() == "radio"
        if hasattr(self, "radio_window_radio_label"):
            self.radio_window_radio_label.setVisible(radio_visible)
        if hasattr(self, "radio_window_radio_combo"):
            self.radio_window_radio_combo.setVisible(radio_visible)
            if radio_visible:
                self._refresh_radio_window_radio_combo(self._selected_frequency_plan_row())
        self.rebuild_table()

    def _on_radio_window_radio_changed(self, *_args: Any) -> None:
        self._operation_feedback = ""
        if getattr(self, "_radio_window_radio_combo_loading", False):
            return
        if self._planner_view_mode() == "radio":
            self.rebuild_table()

    # ------------- helpers ------------- #

    def _current_timezone(self) -> tuple[str, datetime.tzinfo]:
        """
        Returns (tz_name, tzinfo) using the Settings timezone and the
        shared get_timezone() helper so it works on all platforms.
        """
        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        return tz_name, tz

    def _current_timezone_label(self) -> tuple[str, str]:
        """
        Returns (tz_name, tz_abbr) for labeling the Local column header.
        Uses tzname() when available, otherwise a short fallback (ET/CT/MT/PT/UTC).
        """
        tz_name, tz = self._current_timezone()
        now = datetime.datetime.now(tz)
        abbr = now.tzname() or self._ui_tz_abbr(tz_name, tz_name)
        if abbr and len(abbr) > 5:
            abbr = self._ui_tz_abbr(tz_name, abbr)
        return tz_name, abbr

    def _planner_view_mode(self) -> str:
        if not hasattr(self, "planner_view_combo"):
            return "effective"
        mode = str(self.planner_view_combo.currentData() or "").strip().lower()
        return mode if mode in {"effective", "patterns", "radio", "blended", "operational"} else "effective"

    def _selected_operational_day(self) -> str:
        if not hasattr(self, "operational_day_combo"):
            return DAY_NAMES[0]
        day = str(self.operational_day_combo.currentData() or self.operational_day_combo.currentText() or "").strip()
        return day if day in DAY_NAMES else DAY_NAMES[0]

    def _prompt_for_name(self, title: str, label: str, default_name: str) -> Tuple[str, bool]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        prompt = QLabel(label)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        name_edit = QLineEdit(str(default_name or "").strip())
        name_edit.setObjectName("freqPlannerNameEdit")
        name_edit.selectAll()
        layout.addWidget(name_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        name_edit.setFocus(Qt.OtherFocusReason)
        if dialog.exec() != QDialog.Accepted:
            return "", False
        return name_edit.text().strip(), True

    def _source_sets(self, key):
        model = self._catalog_model
        return model.sources.get(key, ()) if model is not None else ()


    def _selected_source_set_id(self, settings_key: str) -> str:
        # Saved-plan inspection has a detached selection; do not confuse it
        # with the operator's persisted editor defaults.
        inspected = getattr(self, "_inspected_source_ids", {})
        return inspected.get(settings_key, selected_source_set_id(self.settings, settings_key))

    def _visible_source_dependency_refs(self) -> List[str]:
        return selected_source_schedule_dependency_refs({
            key: self._selected_source_set_id(key)
            for key in (SELECTED_HF_DAILY_SOURCE_SET_KEY, SELECTED_HF_NET_SOURCE_SET_KEY)
        })

    def _active_source_label(self, sets_key: str) -> str:
        return "Active Daily Schedule" if sets_key == HF_DAILY_SOURCE_SETS_KEY else "Active Net Schedule"

    def _refresh_source_combo(self, combo, sets_key, selected_key):
        selected = self._selected_source_set_id(selected_key)
        model = self._catalog_model
        choices = model.choices.get(sets_key, ()) if model else ((self._active_source_label(sets_key), LIVE_SOURCE_SET_ID),)
        indices = model.choice_indices.get(sets_key, {}) if model else {LIVE_SOURCE_SET_ID: 0}
        combo.blockSignals(True)
        combo.publish_choices(choices, indices)
        if selected not in {LIVE_SOURCE_SET_ID, NO_NET_SOURCE_SET_ID} and combo.findData(selected) < 0:
            label = "Missing HF Daily source" if sets_key == HF_DAILY_SOURCE_SETS_KEY else "Missing HF Net source"
            combo.addItem(f"{label}: {selected}", selected)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)


    def _selected_sop_plan_source_id(self) -> str:
        inspected = getattr(self, "_inspected_source_ids", {})
        if "freqplanner_selected_sop_schedule_plan_id" in inspected:
            return inspected["freqplanner_selected_sop_schedule_plan_id"]
        return str(self.settings.get("freqplanner_selected_sop_schedule_plan_id", LIVE_SOURCE_SET_ID) or LIVE_SOURCE_SET_ID).strip() or LIVE_SOURCE_SET_ID

    def _set_selected_sop_plan_source_id(self, plan_id: Any) -> None:
        clean = str(plan_id or LIVE_SOURCE_SET_ID).strip() or LIVE_SOURCE_SET_ID
        self.settings.set("freqplanner_selected_sop_schedule_plan_id", clean)

    def _sop_schedule_plan_rows(self) -> List[Dict[str, Any]]:
        try:
            rows = self.plan_context_service.store.list_frequency_plans()
        except Exception as exc:
            raise ScheduleSourceError(f"Cannot read SOP plan catalog: {exc}") from exc
        out: List[Dict[str, Any]] = []
        for row in rows:
            if str(row.get("category") or "").strip().lower() == "sop_schedule":
                out.append(dict(row))
        return out

    def _sop_schedule_plan_row_by_id(self, plan_id: Any) -> Optional[Dict[str, Any]]:
        try:
            target = int(plan_id or 0)
        except Exception:
            target = 0
        if target <= 0:
            return None
        try:
            plan = self.plan_context_service.store.get_frequency_plan(target)
        except Exception as exc:
            raise ScheduleSourceError(f"Cannot read selected SOP plan: {exc}") from exc
        if not isinstance(plan, dict):
            return None
        return plan if str(plan.get("category") or "").strip().lower() == "sop_schedule" else None

    def _refresh_sop_plan_source_combo(self):
        if not hasattr(self, "sop_plan_source_combo"):
            return
        selected = self._selected_sop_plan_source_id()
        model = self._catalog_model
        combo = self.sop_plan_source_combo
        combo.blockSignals(True)
        combo.publish_choices(model.choices["sop"] if model else (("Active SOP Builder Layers", LIVE_SOURCE_SET_ID),),
                              model.choice_indices["sop"] if model else {LIVE_SOURCE_SET_ID: 0})
        if selected != LIVE_SOURCE_SET_ID and combo.findData(selected) < 0:
            combo.addItem(f"Missing SOP source: {selected}", selected)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)


    def _refresh_source_set_controls(self) -> None:
        if not hasattr(self, "hf_daily_source_combo"):
            return
        self._refresh_source_combo(self.hf_daily_source_combo, HF_DAILY_SOURCE_SETS_KEY, SELECTED_HF_DAILY_SOURCE_SET_KEY)
        self._refresh_source_combo(self.hf_net_source_combo, HF_NET_SOURCE_SETS_KEY, SELECTED_HF_NET_SOURCE_SET_KEY)
        self._refresh_sop_plan_source_combo()
        self._refresh_plan_layer_summary()
        self._update_plan_action_styles()

    def _set_frequency_plan_layers_dirty(self, dirty: bool) -> None:
        self._frequency_plan_layers_dirty = bool(dirty)
        self._update_plan_action_styles()

    def _selected_frequency_plan_category(self, plan: Optional[Mapping[str, Any]] = None) -> str:
        if plan is None:
            plan = self._selected_frequency_plan_row()
        return str((plan or {}).get("category") or "normal").strip().lower() or "normal"

    def _save_plan_button_text(self, plan: Optional[Mapping[str, Any]] = None) -> str:
        if bool(getattr(self, "_creating_new_frequency_plan", False)) or not isinstance(plan, Mapping):
            return "Create Plan"
        if self._selected_frequency_plan_category(plan) == "sop_schedule":
            return "Create Frequency Plan"
        return "Update Plan" if getattr(self, "_frequency_plan_layers_dirty", False) else "Save Plan"

    def _plan_source_dependency_refs(self, plan: Optional[Mapping[str, Any]] = None) -> List[str]:
        """Return explicit source dependencies; legacy live markers are not dependencies."""
        if plan is None:
            plan = self._selected_frequency_plan_row()
        refs = self._source_refs_from_plan_row(plan) if isinstance(plan, Mapping) else []
        prefixes = (
            f"{HF_DAILY_SOURCE_CATEGORY}:",
            f"{HF_NET_SOURCE_CATEGORY}:",
            "sop_schedule_plan:",
            "sop_schedule_layer:",
        )
        return [ref for ref in refs if any(ref.startswith(prefix) for prefix in prefixes)]

    def _missing_source_dependencies(self) -> List[str]:
        missing: List[str] = []
        daily_id = self._selected_source_set_id(SELECTED_HF_DAILY_SOURCE_SET_KEY)
        if daily_id != LIVE_SOURCE_SET_ID and self._source_set_row_by_id(HF_DAILY_SOURCE_SETS_KEY, daily_id) is None:
            missing.append(f"HF Daily source {daily_id}")
        net_id = self._selected_source_set_id(SELECTED_HF_NET_SOURCE_SET_KEY)
        if net_id not in {LIVE_SOURCE_SET_ID, NO_NET_SOURCE_SET_ID} and self._source_set_row_by_id(HF_NET_SOURCE_SETS_KEY, net_id) is None:
            missing.append(f"HF Net source {net_id}")
        sop_id = self._selected_sop_plan_source_id()
        if sop_id != LIVE_SOURCE_SET_ID and self._sop_schedule_plan_row_by_id(sop_id) is None:
            missing.append(f"SOP source {sop_id}")
        return missing

    def _source_dependency_block_text(self) -> str:
        missing = self._missing_source_dependencies()
        if not missing:
            return ""
        return (
            "Missing declared schedule dependency: " + ", ".join(missing) + ". "
            "Choose an explicit valid source before saving or editing this plan."
        )

    def _source_backed_normal_plan(self, plan: Optional[Mapping[str, Any]] = None) -> bool:
        if plan is None:
            plan = self._selected_frequency_plan_row()
        if not isinstance(plan, Mapping):
            return False
        category = str(plan.get("category") or "normal").strip().lower()
        return category == "normal" and bool(self._plan_source_dependency_refs(plan))

    def _plan_mode_text(self, plan: Optional[Mapping[str, Any]] = None) -> str:
        if bool(getattr(self, "_creating_new_frequency_plan", False)) or not isinstance(plan, Mapping):
            return "New"
        if self._selected_frequency_plan_category(plan) == "sop_schedule":
            return "SOP Plan"
        return "Modified" if getattr(self, "_frequency_plan_layers_dirty", False) else "Saved"

    def _sync_plan_more_actions(self) -> None:
        if not hasattr(self, "plan_rename_action"):
            return
        self.plan_rename_action.setEnabled(bool(self.rename_plan_btn.isEnabled()))
        self.plan_delete_action.setEnabled(bool(self.delete_plan_btn.isEnabled()))

    def _on_plan_menu_rename(self) -> None:
        """Prompt for a name, then reuse the guarded legacy rename action."""
        if not self.rename_plan_btn.isEnabled():
            return
        plan = self._selected_frequency_plan_row()
        if not plan:
            return
        selected_id = int(plan.get("id") or 0)
        current = str(self._current_frequency_plan_name() or plan.get("name") or "").strip()
        name, accepted = QInputDialog.getText(
            self,
            "Rename Frequency Plan",
            "Plan name:",
            QLineEdit.Normal,
            current,
        )
        if not accepted:
            return
        current_plan = self._selected_frequency_plan_row()
        try:
            current_combo_id = int(self.frequency_plan_combo.currentData() or 0)
        except Exception:
            current_combo_id = 0
        if (not self.rename_plan_btn.isEnabled()
                or not current_plan
                or int(current_plan.get("id") or 0) != selected_id
                or current_combo_id != selected_id):
            self.frequency_plan_action_hint_label.setText(
                "Selected plan changed; choose it again before renaming."
            )
            return
        name = str(name).strip()
        if not name:
            return
        self.frequency_plan_combo.setEditText(name)
        self.rename_plan_btn.click()

    def _fit_assignment_button_text(self) -> None:
        if not hasattr(self, "assign_plan_btn"):
            return
        full_text = self.assign_plan_btn.property("fioAssignmentFullText")
        if not isinstance(full_text, str) or not full_text:
            return
        available = 0
        if hasattr(self, "planner_scroll") and hasattr(self, "plan_assignment_header_host"):
            viewport_width = int(self.planner_scroll.viewport().width())
            header_layout = self.planner_body.layout().itemAt(0).layout() if self.planner_body.layout().count() else None
            if viewport_width > 0 and header_layout is not None:
                fixed_width = 0
                visible_count = 0
                for index in range(header_layout.count()):
                    item = header_layout.itemAt(index)
                    widget = item.widget()
                    if widget is None or widget is self.plan_assignment_header_host or widget.isHidden():
                        continue
                    fixed_width += max(widget.sizeHint().width(), widget.minimumSizeHint().width())
                    visible_count += 1
                margins = header_layout.contentsMargins()
                spacing = max(0, header_layout.spacing())
                body_margins = self.planner_body.layout().contentsMargins()
                available = (viewport_width - body_margins.left() - body_margins.right()
                             - margins.left() - margins.right() - fixed_width)
                available -= spacing * visible_count
        if available <= 0:
            available = self.assign_plan_btn.fontMetrics().horizontalAdvance(full_text)
        button_budget = max(1, available)
        text_padding = max(self.assign_plan_btn.fontMetrics().horizontalAdvance("MM"),
                           self.assign_plan_btn.sizeHint().width()
                           - self.assign_plan_btn.fontMetrics().horizontalAdvance(
                               self.assign_plan_btn.text().replace("&&", "&")))
        available = max(1, button_budget - text_padding)
        self.assign_plan_btn.setMinimumWidth(0)
        self.assign_plan_btn.setMaximumWidth(button_budget)
        visible = full_text
        if self.assign_plan_btn.fontMetrics().horizontalAdvance(full_text) > available:
            suffix = " · Change…"
            suffix_width = self.assign_plan_btn.fontMetrics().horizontalAdvance(suffix)
            if full_text.endswith(suffix) and suffix_width + self.assign_plan_btn.fontMetrics().horizontalAdvance("…") <= available:
                prefix_budget = max(1, available - suffix_width)
                prefix = full_text[:-len(suffix)]
                visible = self.assign_plan_btn.fontMetrics().elidedText(prefix, Qt.ElideRight, prefix_budget) + suffix
            else:
                visible = self.assign_plan_btn.fontMetrics().elidedText(full_text, Qt.ElideRight, available)
        visible = visible.replace("&", "&&")
        if self.assign_plan_btn.text() != visible:
            self.assign_plan_btn.setText(visible)
        self.assign_plan_btn.setAccessibleName(full_text)

    def _update_plan_action_styles(self, theme: Optional[Dict[str, str]] = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        plan = self._selected_frequency_plan_row()
        if hasattr(self, "save_plan_btn"):
            role = "eligible_warning" if getattr(self, "_frequency_plan_layers_dirty", False) else "primary"
            self.save_plan_btn.setText(self._save_plan_button_text(plan))
            missing_text = self._source_dependency_block_text()
            self.save_plan_btn.setEnabled(not bool(missing_text or getattr(self, "_input_read_error", "")))
            self.save_plan_btn.setStyleSheet(button_style(role if not missing_text else "muted", theme))
            self.save_plan_btn.setToolTip(
                missing_text
                if missing_text
                else "Layer selections changed. The highlighted save action updates the selected plan with the visible Daily, Net, and SOP layers."
                if getattr(self, "_frequency_plan_layers_dirty", False)
                else "Save or update the visible HF Daily + HF Nets + SOP projection as a named Frequency Plan."
            )
        if hasattr(self, "plan_mode_label"):
            mode_text = self._plan_mode_text(plan)
            self.plan_mode_label.setText(mode_text)
            self.plan_mode_label.setStyleSheet(button_style("eligible_warning" if mode_text == "Modified" else "muted", theme))
        self._update_assign_plan_action_state(theme)

    def _update_assign_plan_action_state(
        self,
        theme: Optional[Dict[str, str]] = None,
        plan: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not hasattr(self, "assign_plan_btn"):
            return
        if theme is None:
            theme = resolve_theme(self.settings)
        if plan is None:
            plan = self._selected_frequency_plan_row()
        if not isinstance(plan, Mapping):
            if hasattr(self, "plan_assignment_label"):
                self.plan_assignment_label.setText("Radio assignment: No saved plan selected")
            self.assign_plan_btn.setText("Assign in Configuration")
            self.assign_plan_btn.setAccessibleName("Assign in Configuration")
            self.assign_plan_btn.setProperty("fioAssignmentFullText", "Assign in Configuration")
            self.assign_plan_btn.setEnabled(False)
            self.assign_plan_btn.setStyleSheet(button_style("muted", theme))
            self.assign_plan_btn.setToolTip(
                "Select or save a Frequency Plan, then use Configuration > Radios > Schedule Assignment to assign it with RF Guard."
            )
            return
        assigned_ids = self._assigned_radio_ids_for_plan(plan)
        if hasattr(self, "plan_assignment_label"):
            names = ", ".join(self._radio_label_for_id(ident) for ident in assigned_ids)
            self.plan_assignment_label.setText(f"Assigned to: {names}" if names else "Radio assignment: Not assigned")
        plan_name = str(plan.get("name") or "Frequency Plan").strip()
        self.assign_plan_btn.setEnabled(True)
        if assigned_ids:
            labels = ", ".join(self._radio_label_for_id(radio_id) for radio_id in assigned_ids)
            full_assignment = f"Assigned: {labels} · Change…"
            self.assign_plan_btn.setText(full_assignment)
            self.assign_plan_btn.setProperty("fioAssignmentFullText", full_assignment)
            self.assign_plan_btn.setAccessibleName(full_assignment)
            self.assign_plan_btn.setStyleSheet(button_style("muted", theme))
            self.assign_plan_btn.setToolTip(
                f"{full_assignment}\n'{plan_name}' is assigned to {labels}. Open Configuration to review or change assignment."
            )
            self._fit_assignment_button_text()
        else:
            self.assign_plan_btn.setText("Assign with RF Guard")
            self.assign_plan_btn.setAccessibleName("Assign with RF Guard")
            self.assign_plan_btn.setProperty("fioAssignmentFullText", "Assign with RF Guard")
            self.assign_plan_btn.setStyleSheet(button_style("eligible_warning", theme))
            self.assign_plan_btn.setToolTip(
                f"'{plan_name}' is not assigned to a radio yet. Open Configuration > Radios > Schedule Assignment to assign it with RF Guard."
            )

    def _on_source_set_selected(self, *_args: Any) -> None:
        self._operation_feedback = ""
        # Combo population happens before the table exists; construction-time
        # index signals must not persist inspected plan sources as operator defaults.
        if not hasattr(self, "table"):
            return
        self._inspected_source_ids = {}
        if hasattr(self, "hf_daily_source_combo"):
            self.settings.set(SELECTED_HF_DAILY_SOURCE_SET_KEY, str(self.hf_daily_source_combo.currentData() or LIVE_SOURCE_SET_ID))
        if hasattr(self, "hf_net_source_combo"):
            self.settings.set(SELECTED_HF_NET_SOURCE_SET_KEY, str(self.hf_net_source_combo.currentData() or LIVE_SOURCE_SET_ID))
        if hasattr(self, "sop_plan_source_combo"):
            self._set_selected_sop_plan_source_id(self.sop_plan_source_combo.currentData())
        self._set_frequency_plan_layers_dirty(True)
        self._input_retry_after = 0.0
        self._input_retry_attempt = 0
        self._input_missing_source = False
        self.rebuild_table()
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        plan_name = self._current_frequency_plan_name() or "this plan"
        action = self._save_plan_button_text(self._selected_frequency_plan_row())
        self._set_action_feedback(
            f"Layer selection changed for '{plan_name}'. {action} saves the visible Daily, Nets, and SOP layers; "
            "choose New Plan first to save a separate plan."
        )
        self._queue_geometry_settle()

    def _source_set_row_by_id(self, sets_key, set_id):
        model = self._catalog_model
        row = model.source_index.get((sets_key, str(set_id))) if model is not None else None
        if row is not None and row.get("_catalog_error"):
            raise ScheduleSourceError(row["_catalog_error"])
        return row


    def _source_selection_summary(self) -> str:
        hf_id = self._selected_source_set_id(SELECTED_HF_DAILY_SOURCE_SET_KEY)
        net_id = self._selected_source_set_id(SELECTED_HF_NET_SOURCE_SET_KEY)
        sop_id = self._selected_sop_plan_source_id()
        hf_row = self._source_set_row_by_id(HF_DAILY_SOURCE_SETS_KEY, hf_id)
        net_row = self._source_set_row_by_id(HF_NET_SOURCE_SETS_KEY, net_id)
        sop_row = self._sop_schedule_plan_row_by_id(sop_id)
        hf_label = str((hf_row or {}).get("name") or (f"Missing source: {hf_id}" if hf_id != LIVE_SOURCE_SET_ID else self._active_source_label(HF_DAILY_SOURCE_SETS_KEY)))
        net_label = (
            NO_NET_SOURCE_SET_LABEL
            if str(net_id or "").strip() == NO_NET_SOURCE_SET_ID
            else str((net_row or {}).get("name") or (f"Missing source: {net_id}" if net_id != LIVE_SOURCE_SET_ID else self._active_source_label(HF_NET_SOURCE_SETS_KEY)))
        )
        sop_label = str((sop_row or {}).get("name") or (f"Missing source: {sop_id}" if sop_id != LIVE_SOURCE_SET_ID else "Active SOP Builder layers"))
        return f"HF Daily: {hf_label}; HF Nets: {net_label}; SOP: {sop_label}"

    def _selected_source_layer_label(self, sets_key: str, selected_key: str) -> str:
        set_id = self._selected_source_set_id(selected_key)
        if sets_key == HF_NET_SOURCE_SETS_KEY and str(set_id or "").strip() == NO_NET_SOURCE_SET_ID:
            return NO_NET_SOURCE_SET_LABEL
        row = self._source_set_row_by_id(sets_key, set_id)
        return str((row or {}).get("name") or (f"Missing source: {set_id}" if set_id != LIVE_SOURCE_SET_ID else self._active_source_label(sets_key)))

    def _selected_sop_layer_label(self) -> str:
        row = self._sop_schedule_plan_row_by_id(self._selected_sop_plan_source_id())
        plan_id = self._selected_sop_plan_source_id()
        return str((row or {}).get("name") or (f"Missing source: {plan_id}" if plan_id != LIVE_SOURCE_SET_ID else "Active SOP Builder layers"))

    def _selected_sop_plan_dependency_refs(self) -> List[str]:
        plan_id = self._selected_sop_plan_source_id()
        plan = self._sop_schedule_plan_row_by_id(plan_id)
        if not plan:
            return []
        try:
            clean_id = int(plan.get("id") or plan_id or 0)
        except Exception:
            clean_id = 0
        return [f"sop_schedule_plan:{clean_id}"] if clean_id > 0 else []

    @staticmethod
    def _layer_count_text(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"

    def _plan_layer_summary_text(
        self,
        hf_sched: Optional[List[Dict]] = None,
        net_sched: Optional[List[Dict]] = None,
        sop_sched: Optional[List[Dict]] = None,
        effective_count: Optional[int] = None,
        effective_label: str = "Windows",
    ) -> str:
        view_label = "Effective Windows"
        if hasattr(self, "planner_view_combo"):
            view_label = str(self.planner_view_combo.currentText() or view_label).strip() or view_label
        parts = [
            f"Daily: {self._selected_source_layer_label(HF_DAILY_SOURCE_SETS_KEY, SELECTED_HF_DAILY_SOURCE_SET_KEY)}",
            f"Nets: {self._selected_source_layer_label(HF_NET_SOURCE_SETS_KEY, SELECTED_HF_NET_SOURCE_SET_KEY)}",
            f"SOP: {self._selected_sop_layer_label()}" if any(isinstance(row, dict) for row in (sop_sched or [])) else "SOP: Not included",
            f"View: {view_label}",
        ]
        if effective_count is not None:
            parts.append(f"Windows: {effective_count}")
        return " | ".join(parts)

    def _radio_lane_summary_for_payload(self, plan_payload: Mapping[str, Any]) -> str:
        if not isinstance(plan_payload, Mapping):
            return ""
        ids = self._radio_lane_ids_for_plan(dict(plan_payload))
        if not ids:
            return ""
        labels: List[str] = []
        for radio_id in ids[:4]:
            name = ""
            try:
                profile = self.plan_context_service.store.get_device_profile(int(radio_id))
                name = str((profile or {}).get("name") or "").strip()
            except Exception:
                name = ""
            labels.append(name or f"Radio {int(radio_id)}")
        suffix = f", +{len(ids) - 4} more" if len(ids) > 4 else ""
        return f"{len(ids)} radio lane{'s' if len(ids) != 1 else ''}: {', '.join(labels)}{suffix}"

    def _refresh_plan_layer_summary(
        self,
        hf_sched: Optional[List[Dict]] = None,
        net_sched: Optional[List[Dict]] = None,
        sop_sched: Optional[List[Dict]] = None,
        effective_count: Optional[int] = None,
        effective_label: str = "Windows",
        plan_payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not hasattr(self, "plan_layers_label"):
            return
        text = self._plan_layer_summary_text(
            hf_sched=hf_sched,
            net_sched=net_sched,
            sop_sched=sop_sched,
            effective_count=effective_count,
            effective_label=effective_label,
        )
        lane_summary = self._radio_lane_summary_for_payload(plan_payload or {})
        if lane_summary:
            text = f"{text} | Radios: {lane_summary}"
        self.plan_layers_label.setText(text)
        if hasattr(self, "plan_ingredients_scroll"):
            self.plan_ingredients_scroll.setToolTip(text)
        if hasattr(self, "plan_ingredients_frame"):
            self.plan_ingredients_frame.setToolTip(text)
        self._refresh_plan_ingredients(plan_payload=plan_payload)

    def _source_combo_label(self, combo: Optional[QComboBox], fallback: str) -> str:
        if combo is None:
            return fallback
        text = str(combo.currentText() or "").strip()
        return text or fallback

    def _selected_source_combo_id(self, combo: Optional[QComboBox]) -> str:
        if combo is None:
            return LIVE_SOURCE_SET_ID
        value = str(combo.currentData() or "").strip()
        return value or LIVE_SOURCE_SET_ID

    def _source_usage_text(self, *, category, set_id, live_label):
        key = HF_DAILY_SOURCE_SETS_KEY if category == HF_DAILY_SOURCE_CATEGORY else HF_NET_SOURCE_SETS_KEY
        model = self._catalog_model
        usage = model.usage.get((key, set_id), {}) if model is not None else {}
        return str(usage.get("text") or "Usage: checking…")


    @staticmethod
    def _set_ingredient_text(label: QLabel, title: str, value: str, detail: str = "") -> None:
        detail_text = f" | {detail}" if detail else ""
        label.setText(f"<b>{title}:</b> {value}{detail_text}")
        label.setToolTip((f"{title}: {value}\n{detail}" if detail else f"{title}: {value}"))

    def _refresh_plan_ingredients(self, *, plan_payload: Optional[Mapping[str, Any]] = None) -> None:
        if not hasattr(self, "plan_ingredient_plan"):
            return
        plan = plan_payload if isinstance(plan_payload, Mapping) else self._selected_frequency_plan_row()
        plan_name = str((plan or {}).get("name") or self._current_frequency_plan_name() or "New Frequency Plan").strip()
        category = str((plan or {}).get("category") or "normal").strip().lower()
        plan_kind = "SOP Schedule Plan" if category == "sop_schedule" else "Frequency Plan"
        daily_label = self._source_combo_label(getattr(self, "hf_daily_source_combo", None), "Active Daily Schedule")
        daily_id = self._selected_source_combo_id(getattr(self, "hf_daily_source_combo", None))
        net_label = self._source_combo_label(getattr(self, "hf_net_source_combo", None), "Active Net Schedule")
        net_id = self._selected_source_combo_id(getattr(self, "hf_net_source_combo", None))
        sop_label = self._source_combo_label(getattr(self, "sop_plan_source_combo", None), "Active SOP Builder Layers")
        assigned_radios = self._assigned_radio_ids_for_plan(plan or {})
        assigned_text = ", ".join(self._radio_label_for_id(ident) for ident in assigned_radios) or "Not assigned"
        daily_usage = self._source_usage_text(
            category=HF_DAILY_SOURCE_CATEGORY,
            set_id=daily_id,
            live_label="hf_daily",
        )
        net_usage = self._source_usage_text(
            category=HF_NET_SOURCE_CATEGORY,
            set_id=net_id,
            live_label="hf_nets",
        )
        self._set_ingredient_text(
            self.plan_ingredient_plan,
            "Plan",
            plan_name,
            f"{plan_kind} | linked by default",
        )
        self._set_ingredient_text(
            self.plan_ingredient_daily,
            "Daily",
            daily_label,
            daily_usage,
        )
        self._set_ingredient_text(
            self.plan_ingredient_nets,
            "Nets",
            net_label,
            net_usage,
        )
        self._set_ingredient_text(self.plan_ingredient_sop, "SOP", sop_label, "What-to-do layer")
        self._set_ingredient_text(self.plan_ingredient_assignments, "Assigned", assigned_text, "Manage assignment in Configuration > Radios")
        if hasattr(self, "frequency_plan_combo"):
            self.frequency_plan_combo.setToolTip(
                f"Plan: {plan_name}\n{plan_kind}; Daily: {daily_label}; Nets: {net_label}; SOP: {sop_label}; Assigned: {assigned_text}"
            )
        if hasattr(self, "hf_daily_source_combo"):
            self.hf_daily_source_combo.setToolTip(f"HF Daily: {daily_label}\n{daily_usage}")
        if hasattr(self, "hf_net_source_combo"):
            self.hf_net_source_combo.setToolTip(f"HF Nets: {net_label}\n{net_usage}")
        if hasattr(self, "sop_plan_source_combo"):
            self.sop_plan_source_combo.setToolTip(f"SOP: {sop_label}\nWhat-to-do layer")

    @staticmethod
    def _normalize_condition_levels(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if not raw or raw == "ALL":
            return "ALL"
        vals: List[int] = []
        for token in raw.replace(";", ",").replace("|", ",").split(","):
            token = token.strip()
            if not token:
                continue
            if token == "ALL":
                return "ALL"
            try:
                lvl = int(token)
            except Exception:
                continue
            if 1 <= lvl <= 5:
                vals.append(lvl)
        if not vals:
            return "ALL"
        return ",".join(str(v) for v in sorted(set(vals)))

    def _condition_level_map(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        rows = self.settings.get("operating_groups", []) or []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            group = str(row.get("group", "") or "").strip().upper()
            if not group:
                continue
            if not bool(row.get("use_condition_levels", False)):
                continue
            try:
                level = int(row.get("condition_level", 0) or 0)
            except Exception:
                level = 0
            if not (1 <= level <= 5):
                continue
            prev = out.get(group)
            if prev is None or level < prev:
                out[group] = level
        return out

    def _configured_operating_group_names(self) -> Set[str]:
        groups: Set[str] = set()
        rows = self.settings.get("operating_groups", []) or []
        if not isinstance(rows, list):
            return groups
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("group", "name", "group_name"):
                value = str(row.get(key) or "").strip().upper()
                if value:
                    groups.add(value)
                    break
        return groups

    def _row_group_name(self, row: Mapping[str, Any]) -> str:
        return str(row.get("group_name") or row.get("group") or row.get("primary_js8call_group") or "").strip().upper()

    def _filter_rows_to_configured_groups(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        configured = self._configured_operating_group_names()
        if not configured:
            return []
        return [dict(row) for row in rows if self._row_group_name(row) in configured]

    def _group_is_configured(self, group_name: Any) -> bool:
        group = str(group_name or "").strip().upper()
        return bool(group and group in self._configured_operating_group_names())

    def _configured_group_help_text(self) -> str:
        groups = sorted(self._configured_operating_group_names())
        if not groups:
            return "No Operating Groups are configured in Configuration."
        return "Configured Operating Groups: " + ", ".join(groups)

    @staticmethod
    def _json_ref_count(value: Any) -> int:
        if isinstance(value, (list, tuple, set)):
            return len([item for item in value if str(item or "").strip()])
        if value in (None, ""):
            return 0
        try:
            parsed = json.loads(str(value))
        except Exception:
            parsed = [part.strip() for part in str(value).split(",") if part.strip()]
        if not isinstance(parsed, list):
            return 0
        return len([item for item in parsed if str(item or "").strip()])

    def _frequency_plan_summary_text(self, profile: Optional[Dict[str, Any]]) -> str:
        if not isinstance(profile, dict):
            return "No plan selected. Choose a saved plan or select New Plan."
        category = str(profile.get("category", "normal") or "normal").strip().lower()
        if category == "sop_schedule":
            return "SOP Schedule Plan selected. Review the condition layers and save only after RF Guard review."
        name = str(profile.get("name") or "Frequency Plan").strip()
        source_count = len(profile["_catalog_source_refs"]) if "_catalog_source_refs" in profile else self._json_ref_count(profile.get("source_refs_json"))
        schedule_count = len(profile["_catalog_schedule_refs"]) if "_catalog_schedule_refs" in profile else self._json_ref_count(profile.get("schedule_refs_json"))
        if not schedule_count:
            return f"'{name}' has no saved windows yet. Select Daily and Net layers, then Save Plan."
        if source_count:
            return f"'{name}' is loaded. Change Daily, Nets, or SOP layers, then Save Plan to update it."
        return f"'{name}' is loaded from saved windows. Review the windows, then assign it to a radio when ready."

    def _refresh_plan_workspace_header(self) -> None:
        if not hasattr(self, "frequency_plan_combo"):
            return
        selected_id = self.frequency_plan_combo.currentData()
        typed_name = self._current_frequency_plan_name()
        context = self.plan_context_service.context_for_tab("freqplanner", refresh=True)
        context_plan_id = 0
        if context is not None and context.frequency_plan_id:
            try:
                context_plan_id = int(str(context.frequency_plan_id).split("_")[-1])
            except Exception:
                context_plan_id = 0
        creating_new = bool(getattr(self, "_creating_new_frequency_plan", False))
        preferred_id = (int(getattr(self, "_pending_saved_plan_id", 0) or 0)
                        or (0 if creating_new else int(selected_id or 0) or context_plan_id))
        if self._catalog_model is not None and preferred_id in self._catalog_model.plan_index:
            self._pending_saved_plan_id = 0
        self.frequency_plan_combo.blockSignals(True)
        model = self._catalog_model
        self.frequency_plan_combo.publish_choices(model.choices["plans"] if model else (),
                                                  model.choice_indices["plans"] if model else {})
        if preferred_id:
            idx = self.frequency_plan_combo.findData(preferred_id)
            if idx >= 0:
                self.frequency_plan_combo.setCurrentIndex(idx)
            elif self._catalog_review_required:
                self.frequency_plan_combo.addItem(f"Missing plan: {typed_name}", preferred_id)
                self.frequency_plan_combo.setCurrentIndex(self.frequency_plan_combo.count() - 1)
        elif creating_new:
            self.frequency_plan_combo.setCurrentIndex(-1)
            self.frequency_plan_combo.setEditText(typed_name if typed_name and typed_name != "Frequency Plan" else "")
        self._editing_frequency_plan_id = int(self.frequency_plan_combo.currentData() or 0)
        self.frequency_plan_combo.blockSignals(False)
        selected_plan = self._selected_frequency_plan_row()
        if (
            not getattr(self, "_frequency_plan_layers_dirty", False)
            and str((selected_plan or {}).get("category") or "").strip().lower() != "sop_schedule"
        ):
            self._apply_frequency_plan_source_refs(selected_plan, persist=False)
        self._update_frequency_plan_summary()
        self._refresh_plan_ingredients(plan_payload=selected_plan)
        self._update_assign_plan_action_state(plan=selected_plan)
        self._refresh_radio_window_radio_combo(selected_plan)
        self._refresh_assigned_plan_rf_guard_review(selected_plan)
        if hasattr(self, "rename_plan_btn"):
            self.rename_plan_btn.setEnabled(self._selected_frequency_plan_row() is not None)
        if hasattr(self, "delete_plan_btn"):
            self.delete_plan_btn.setEnabled(self._selected_frequency_plan_row() is not None)
        self._update_sop_plan_action_state()
        self._update_plan_action_styles()

    def _selected_frequency_plan_row(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "frequency_plan_combo"):
            return None
        try:
            selected_id = int(self.frequency_plan_combo.currentData() or 0)
        except Exception:
            selected_id = 0
        if selected_id <= 0:
            try:
                selected_id = int(getattr(self, "_editing_frequency_plan_id", 0) or 0)
            except Exception:
                selected_id = 0
        if selected_id <= 0:
            return None
        try:
            return self.plan_context_service.store.get_frequency_plan(selected_id)
        except Exception as exc:
            self._handle_plan_input_failure(ScheduleSourceError(f"Cannot read selected Frequency Plan: {exc}"))
            return None

    def _current_frequency_plan_name(self) -> str:
        if not hasattr(self, "frequency_plan_combo"):
            return ""
        return str(self.frequency_plan_combo.currentText() or "").strip()

    def _set_plan_manager_new_plan_state(self) -> None:
        if not hasattr(self, "frequency_plan_combo"):
            return
        self._creating_new_frequency_plan = True
        self._set_frequency_plan_layers_dirty(False)
        self.frequency_plan_combo.blockSignals(True)
        self.frequency_plan_combo.setCurrentIndex(-1)
        self.frequency_plan_combo.setEditText("")
        self.frequency_plan_combo.blockSignals(False)
        self._editing_frequency_plan_id = 0
        self._update_frequency_plan_summary()
        self._update_assign_plan_action_state(plan=None)
        self._update_plan_action_styles()
        if hasattr(self, "rename_plan_btn"):
            self.rename_plan_btn.setEnabled(False)
        if hasattr(self, "delete_plan_btn"):
            self.delete_plan_btn.setEnabled(False)
        if hasattr(self, "selected_window_title_label"):
            self.selected_window_title_label.setText("New Frequency Plan")
        if hasattr(self, "selected_window_detail_label"):
            self.selected_window_detail_label.setText("Choose Daily and Net schedules, add SOP layers if needed, then enter a plan name and Save Plan.")
        if hasattr(self, "frequency_plan_action_hint_label"):
            self.frequency_plan_action_hint_label.setText(
                "Creating a new plan. Select Daily and Net schedules, enter a plan name, then Save Plan."
            )
        self._set_rf_guard_review_card({})

    def _on_new_plan_clicked(self) -> None:
        self._pending_saved_plan_id = 0
        self._operation_feedback = ""
        self._set_plan_manager_new_plan_state()
        self.rebuild_table()
        self._set_plan_manager_new_plan_state()

    def begin_guided_radio_plan_handoff(
        self,
        device_profile: Optional[Mapping[str, Any]] = None,
        *,
        schedule_choice: str = "",
    ) -> None:
        """
        Prime Plan Manager after guided radio setup saves a radio without a plan.

        The Add Radio wizard owns radio/app configuration only. Plan creation and
        RF Guard assignment stay here, so this method gives the user a clear next
        step without creating a second schedule-assignment path.
        """
        if self._catalog_model is None or self._catalog_pending:
            self._pending_guided_handoff = (dict(device_profile or {}), str(schedule_choice))
            self.frequency_plan_action_hint_label.setText("Loading Plan Builder inputs for this radio…")
            if not self._catalog_pending:
                self._request_input_catalog()
            return
        radio_name = str((device_profile or {}).get("name") or "this radio").strip() or "this radio"
        built_plans = self._catalog_model.choices["plans"]
        self._refresh_source_set_controls()
        self._refresh_plan_workspace_header()
        self._set_plan_manager_new_plan_state()
        schedule_choice = str(schedule_choice or "").strip()
        if schedule_choice in {SCHEDULE_JS8_STANDARD, SCHEDULE_DAILY_NO_NETS} and hasattr(self, "hf_net_source_combo"):
            self._set_source_combo_to_id(
                self.hf_net_source_combo,
                SELECTED_HF_NET_SOURCE_SET_KEY,
                NO_NET_SOURCE_SET_ID,
            )
        try:
            self._guided_plan_handoff_device_profile_id = int((device_profile or {}).get("id", 0) or 0)
        except Exception:
            self._guided_plan_handoff_device_profile_id = 0
        if hasattr(self, "frequency_plan_combo") and self.frequency_plan_combo.lineEdit() is not None:
            self.frequency_plan_combo.lineEdit().setPlaceholderText(f"Name the Frequency Plan for {radio_name}")
        path_message = ""
        path_detail = "Select the schedule layers this radio should follow."
        if schedule_choice == SCHEDULE_JS8_STANDARD:
            path_message = (
                "Start with a JS8Call-standard daily plan. Choose the JS8Call frequency set, name the plan, "
                "then Save Plan and Assign with RF Guard."
            )
            path_detail = "Build a no-net JS8Call operating plan for this radio, then assign it after RF Guard review."
        elif schedule_choice == SCHEDULE_DAILY_NO_NETS:
            path_message = (
                "Build a Daily with No Nets plan. Choose or create the HF Daily schedule, keep Nets set to No Nets, "
                "name the plan, then Save Plan and Assign with RF Guard."
            )
            path_detail = "Use this when the radio follows a daily frequency pattern without net overlays."
        elif schedule_choice == SCHEDULE_DAILY_PLUS_NETS:
            path_message = (
                "Build a Daily + Nets plan. Choose the HF Daily baseline and the HF Net schedule to layer over it, "
                "review the effective windows, then Save Plan and Assign with RF Guard."
            )
            path_detail = "Layer net windows over the daily baseline so the user can see where to be and when."
        elif schedule_choice == SCHEDULE_SOP_CONDITION:
            path_message = (
                "Build an SOP condition plan. Choose Daily and Net layers, add the SOP condition layer, "
                "review RF Guard, then Save Plan and Assign with RF Guard."
            )
            path_detail = "Use this when the plan also defines what to do when condition levels are active."

        if path_message:
            message = f"{radio_name} was saved. {path_message}"
        elif built_plans:
            message = (
                f"{radio_name} was saved without a Frequency Plan. Choose an existing plan above, "
                "or keep New Plan selected, choose Daily/Nets/SOP layers, name the plan, then Save Plan and Assign with RF Guard."
            )
        else:
            message = (
                f"{radio_name} was saved. Build its first Frequency Plan here: choose an HF Daily schedule, "
                "choose No Nets or a Net schedule, add an SOP layer if needed, name the plan, then Save Plan and Assign with RF Guard."
            )
        if hasattr(self, "frequency_plan_action_hint_label"):
            self.frequency_plan_action_hint_label.setText(message)
        if hasattr(self, "selected_window_title_label"):
            self.selected_window_title_label.setText(f"Build Plan for {radio_name}")
        if hasattr(self, "selected_window_detail_label"):
            self.selected_window_detail_label.setText(
                f"{path_detail} No radio changes happen until the saved plan is assigned with RF Guard."
            )
        try:
            self.planner_view_combo.setCurrentIndex(max(0, self.planner_view_combo.findData("effective")))
        except Exception:
            pass
        self.rebuild_table()
        if hasattr(self, "frequency_plan_action_hint_label"):
            self.frequency_plan_action_hint_label.setText(message)
        self._published_guard_context = (self._effective_guard_radio_id(), int(self._guided_plan_handoff_device_profile_id or 0))

    def _has_sop_schedule_rows(self) -> bool:
        # Header/action presentation consumes the last selected schedule load;
        # it must not reread all Daily/Net/SOP tables on every activation.
        return bool(getattr(self, "_sop_schedule_rows_present", False))

    def _update_sop_plan_action_state(self) -> None:
        if not hasattr(self, "save_sop_plan_btn"):
            return
        selected_plan = self._selected_frequency_plan_row()
        selected_plan_is_sop = self._selected_frequency_plan_category(selected_plan) == "sop_schedule"
        selected_sop_layer_is_saved = self._selected_sop_plan_source_id() != LIVE_SOURCE_SET_ID
        has_rows = self._has_sop_schedule_rows()
        visible = bool(has_rows and (selected_plan_is_sop or selected_sop_layer_is_saved))
        missing_text = self._source_dependency_block_text()
        enabled = visible and not missing_text and not getattr(self, "_input_read_error", "")
        self.save_sop_plan_btn.setVisible(visible)
        self.save_sop_plan_btn.setEnabled(enabled)
        self.save_sop_plan_btn.setText("Update SOP Plan" if selected_plan_is_sop else "Save SOP Plan")
        self.save_sop_plan_btn.setToolTip(
            "Save the selected Daily + Nets + SOP layers as an SOP Schedule Plan."
            if enabled
            else missing_text
            if missing_text
            else "Select a saved SOP layer in the SOP selector, or select an SOP Schedule Plan, before saving an SOP plan."
        )
        if hasattr(self, "source_actions_host"):
            self._queue_geometry_settle()

    def _update_frequency_plan_summary(self) -> None:
        if not hasattr(self, "frequency_plan_summary_label"):
            return
        self.frequency_plan_summary_label.setText(self._frequency_plan_summary_text(self._selected_frequency_plan_row()))

    def _source_refs_from_plan_row(self, plan: Mapping[str, Any]) -> List[str]:
        if "source_refs" not in plan and "_catalog_source_refs" in plan:
            return plan["_catalog_source_refs"]
        raw = plan.get("source_refs_json", plan.get("source_refs", "[]"))
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = []
        else:
            parsed = raw
        refs: List[str] = []
        if isinstance(parsed, list):
            for item in parsed:
                text = str(item or "").strip()
                if text:
                    refs.append(text)
        return refs

    @staticmethod
    def _source_set_id_from_refs(source_refs: List[str], category: str) -> str:
        prefix = f"{str(category or '').strip().lower()}:"
        for ref in source_refs:
            if ref.startswith(prefix):
                return ref[len(prefix) :].strip()
        return ""

    @staticmethod
    def _sop_plan_id_from_refs(source_refs: List[str]) -> str:
        prefix = "sop_schedule_plan:"
        for ref in source_refs:
            if ref.startswith(prefix):
                return ref[len(prefix) :].strip()
        return ""

    def _set_source_combo_to_id(
        self,
        combo: QComboBox,
        selected_key: str,
        set_id: str,
        *,
        persist: bool = True,
    ) -> bool:
        target = str(set_id or LIVE_SOURCE_SET_ID).strip() or LIVE_SOURCE_SET_ID
        idx = combo.findData(target)
        if idx < 0:
            if target not in {LIVE_SOURCE_SET_ID, NO_NET_SOURCE_SET_ID}:
                if combo is getattr(self, "hf_daily_source_combo", None):
                    label = "Missing HF Daily source"
                elif combo is getattr(self, "hf_net_source_combo", None):
                    label = "Missing HF Net source"
                else:
                    label = "Missing SOP source"
                combo.addItem(f"{label}: {target}", target)
                idx = combo.findData(target)
        if idx < 0:
            return False
        combo.blockSignals(True)
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)
        inspected = dict(getattr(self, "_inspected_source_ids", {}))
        if persist:
            self.settings.set(selected_key, target)
            inspected.pop(selected_key, None)
        else:
            inspected[selected_key] = target
        self._inspected_source_ids = inspected
        return True

    def _apply_frequency_plan_source_refs(
        self,
        plan: Optional[Mapping[str, Any]],
        *,
        persist: bool = True,
    ) -> bool:
        if not plan or not hasattr(self, "hf_daily_source_combo"):
            return False
        source_refs = self._source_refs_from_plan_row(plan)
        if not source_refs:
            if getattr(self, "_inspected_source_ids", {}):
                self._inspected_source_ids = {}
                self._refresh_source_set_controls()
            return False
        changed = False
        daily_set_id = self._source_set_id_from_refs(source_refs, HF_DAILY_SOURCE_CATEGORY) or LIVE_SOURCE_SET_ID
        net_set_id = self._source_set_id_from_refs(source_refs, HF_NET_SOURCE_CATEGORY) or LIVE_SOURCE_SET_ID
        sop_plan_id = self._sop_plan_id_from_refs(source_refs) or LIVE_SOURCE_SET_ID
        changed = (
            self._set_source_combo_to_id(
                self.hf_daily_source_combo,
                SELECTED_HF_DAILY_SOURCE_SET_KEY,
                daily_set_id,
                persist=persist,
            )
            or changed
        )
        changed = (
            self._set_source_combo_to_id(
                self.hf_net_source_combo,
                SELECTED_HF_NET_SOURCE_SET_KEY,
                net_set_id,
                persist=persist,
            )
            or changed
        )
        if hasattr(self, "sop_plan_source_combo"):
            idx = self.sop_plan_source_combo.findData(sop_plan_id)
            if idx < 0 and sop_plan_id != LIVE_SOURCE_SET_ID:
                self.sop_plan_source_combo.addItem(f"Missing SOP source: {sop_plan_id}", sop_plan_id)
                idx = self.sop_plan_source_combo.findData(sop_plan_id)
            if idx >= 0:
                self.sop_plan_source_combo.blockSignals(True)
                self.sop_plan_source_combo.setCurrentIndex(idx)
                self.sop_plan_source_combo.blockSignals(False)
                inspected = dict(getattr(self, "_inspected_source_ids", {}))
                if persist:
                    self._set_selected_sop_plan_source_id(sop_plan_id)
                    inspected.pop("freqplanner_selected_sop_schedule_plan_id", None)
                else:
                    inspected["freqplanner_selected_sop_schedule_plan_id"] = sop_plan_id
                self._inspected_source_ids = inspected
                changed = True
        return changed

    def focus_frequency_plan(self, plan_id: int, *, device_profile_id: int) -> bool:
        """Inspect a card's assigned plan without changing command authority."""
        if self._catalog_model is None or self._catalog_pending:
            self._pending_catalog_focus = (int(plan_id), int(device_profile_id))
            self.frequency_plan_action_hint_label.setText("Loading the assigned Frequency Plan for review…")
            if not self._catalog_pending:
                self._request_input_catalog()
            return True
        index = self.frequency_plan_combo.findData(int(plan_id)) if plan_id > 0 else -1
        if index < 0:
            self.frequency_plan_action_hint_label.setText(
                "This radio's assigned Frequency Plan is unavailable. Select a plan to review."
                if plan_id > 0 else "This radio has no assigned Frequency Plan. Select a plan to review."
            )
            return False
        previous_block = self.frequency_plan_combo.blockSignals(True)
        try:
            self.frequency_plan_combo.setCurrentIndex(index)
        finally:
            self.frequency_plan_combo.blockSignals(previous_block)
        self._refresh_radio_window_radio_combo(self._selected_frequency_plan_row())
        radio_index = self.radio_window_radio_combo.findData(int(device_profile_id))
        if radio_index >= 0:
            previous_block = self.radio_window_radio_combo.blockSignals(True)
            try:
                self.radio_window_radio_combo.setCurrentIndex(radio_index)
            finally:
                self.radio_window_radio_combo.blockSignals(previous_block)
        self._on_frequency_plan_selected(inspect_only=True)
        return True

    def _on_frequency_plan_selected(self, *_args: Any, inspect_only: bool = False) -> None:
        self._pending_saved_plan_id = 0
        self._operation_feedback = ""
        if self.frequency_plan_combo.currentIndex() < 0 and self._current_frequency_plan_name():
            self._creating_new_frequency_plan = True
            self._editing_frequency_plan_id = 0
            self._set_frequency_plan_layers_dirty(False)
            self._update_assign_plan_action_state(plan=None)
            if hasattr(self, "rename_plan_btn"):
                self.rename_plan_btn.setEnabled(False)
            if hasattr(self, "delete_plan_btn"):
                self.delete_plan_btn.setEnabled(False)
            self._update_frequency_plan_summary()
            return
        self._creating_new_frequency_plan = False
        self._set_frequency_plan_layers_dirty(False)
        try:
            self._editing_frequency_plan_id = int(self.frequency_plan_combo.currentData() or 0)
        except Exception:
            self._editing_frequency_plan_id = 0
        plan = self._selected_frequency_plan_row()
        loaded_sources = (self._apply_frequency_plan_source_refs(plan, persist=False) if inspect_only
                          else self._apply_frequency_plan_source_refs(plan))
        assignment_hint = "" if inspect_only else self._sync_command_radio_for_selected_plan(plan)
        self._update_frequency_plan_summary()
        self._update_assign_plan_action_state(plan=plan)
        if hasattr(self, "rename_plan_btn"):
            self.rename_plan_btn.setEnabled(self._selected_frequency_plan_row() is not None)
        if hasattr(self, "delete_plan_btn"):
            self.delete_plan_btn.setEnabled(self._selected_frequency_plan_row() is not None)
        self._update_sop_plan_action_state()
        self._update_plan_action_styles()
        if loaded_sources:
            layer_hint = (
                f"Loaded saved layers for '{str((plan or {}).get('name') or 'Frequency Plan')}'. "
                "Review Effective Windows before updating."
            )
            if assignment_hint:
                layer_hint = f"{layer_hint} {assignment_hint}"
            self.frequency_plan_action_hint_label.setText(layer_hint)
        elif assignment_hint:
            self.frequency_plan_action_hint_label.setText(assignment_hint)
        if loaded_sources or self._planner_view_mode() == "operational":
            self._input_retry_after = 0.0
            self._input_retry_attempt = 0
            self._input_missing_source = False
            self.rebuild_table()
        self._refresh_assigned_plan_rf_guard_review(plan)

    def _sync_command_radio_for_selected_plan(self, plan: Optional[Mapping[str, Any]]) -> str:
        if not isinstance(plan, Mapping):
            return ""
        plan_name = str(plan.get("name") or "Frequency Plan").strip()
        assigned_ids = self._assigned_radio_ids_for_plan(plan)
        if not assigned_ids:
            self._update_assign_plan_action_state(plan=plan)
            return f"'{plan_name}' is not assigned to a radio yet. Use Assign in Configuration before relying on this plan operationally."
        self._update_assign_plan_action_state(plan=plan)
        if len(assigned_ids) > 1:
            labels = ", ".join(self._radio_label_for_id(radio_id) for radio_id in assigned_ids[:3])
            if len(assigned_ids) > 3:
                labels += f", +{len(assigned_ids) - 3}"
            return f"'{plan_name}' is assigned to {len(assigned_ids)} radios: {labels}. Use Radio Windows for RF Guard review."
        radio_id = int(assigned_ids[0])
        label = self._radio_label_for_id(radio_id)
        activated = False
        try:
            win = self.window()
            activate = getattr(win, "_activate_station_command_radio", None)
            if callable(activate):
                activated = bool(activate(radio_id))
        except Exception as exc:
            log.debug("FreqPlanner: failed activating command radio for selected plan: %s", exc)
        if activated:
            return f"'{plan_name}' is assigned to {label}; command bar switched to that radio."
        return f"'{plan_name}' is assigned to {label}."

    def _on_assign_plan_clicked(self) -> None:
        plan = self._selected_frequency_plan_row()
        if not plan:
            self.frequency_plan_action_hint_label.setText(
                "Select or save a Frequency Plan before assigning it in Configuration."
            )
            return
        try:
            plan_id = int(plan.get("id") or 0)
        except Exception:
            plan_id = 0
        if self._open_schedule_assignment_settings(
            plan_name=str(plan.get("name") or "Frequency Plan"),
            purpose="assign",
            plan_id=plan_id,
            device_profile_id=int(getattr(self, "_guided_plan_handoff_device_profile_id", 0) or 0),
        ):
            return
        self.frequency_plan_action_hint_label.setText(
            f"Assign '{str(plan.get('name') or 'Frequency Plan')}' from Configuration > Radios > Schedule Assignment. "
            "Choose the radio and save with RF Guard before the schedule changes."
        )

    def _on_rename_plan_clicked(self) -> None:
        if self._request_fresh_action(self._on_rename_plan_clicked):
            return
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        plan = self._selected_frequency_plan_row()
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        if not plan:
            self.frequency_plan_action_hint_label.setText("Select a saved Frequency Plan before renaming.")
            return
        plan_id = int(plan.get("id", 0) or 0)
        old_name = str(plan.get("name") or f"Frequency Plan #{plan_id}").strip()
        new_name = self._current_frequency_plan_name()
        if not new_name:
            self.frequency_plan_action_hint_label.setText("Enter the new Frequency Plan name, then choose Rename Plan.")
            return
        if new_name == old_name:
            self.frequency_plan_action_hint_label.setText(f"'{old_name}' is already using that name.")
            return
        payload = dict(plan)
        payload["id"] = plan_id
        payload["name"] = new_name
        try:
            saved = self.plan_context_service.store.save_frequency_plan(payload)
        except Exception as exc:
            log.exception("FreqPlanner: failed renaming Frequency Plan.")
            self.frequency_plan_action_hint_label.setText(f"Unable to rename '{old_name}': {exc}")
            return
        self._creating_new_frequency_plan = False
        self._set_frequency_plan_layers_dirty(False)
        self.plan_context_service.invalidate()
        saved_id = int(saved.get("id", 0) or plan_id)
        self._pending_saved_plan_id = saved_id
        idx = self.frequency_plan_combo.findData(saved_id)
        if idx >= 0:
            self.frequency_plan_combo.setCurrentIndex(idx)
        self._set_action_feedback(
            f"Renamed Frequency Plan '{old_name}' to '{str(saved.get('name') or new_name)}'. Schedule windows were not changed."
        )

    def _on_delete_plan_clicked(self) -> None:
        if self._request_fresh_action(self._on_delete_plan_clicked):
            return
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        plan = self._selected_frequency_plan_row()
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        if not plan:
            self.frequency_plan_action_hint_label.setText("Select a saved Frequency Plan before deleting.")
            return
        plan_id = int(plan.get("id", 0) or 0)
        name = str(plan.get("name") or f"Frequency Plan #{plan_id}")
        response = QMessageBox.question(
            self,
            "Delete Frequency Plan",
            f"Delete '{name}'? Assigned plans cannot be deleted until the radio assignment is changed.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if response != QMessageBox.Yes:
            return
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        try:
            self.plan_context_service.store.delete_frequency_plan(plan_id)
        except Exception as exc:
            log.exception("FreqPlanner: failed deleting Frequency Plan.")
            self.frequency_plan_action_hint_label.setText(f"Unable to delete '{name}': {exc}")
            return
        self.plan_context_service.invalidate()
        if hasattr(self, "frequency_plan_combo"):
            self.frequency_plan_combo.setEditText("")
        self._editing_frequency_plan_id = 0
        self._refresh_plan_workspace_header()
        self._set_action_feedback(f"Deleted Frequency Plan '{name}'.")

    def _build_blended_projection(self):
        snapshot = self._snapshot((), (), (), ())
        if snapshot == self._latest_projection_snapshot and isinstance(self._latest_projection, BlendedScheduleProjection):
            return self._latest_projection
        prepared = self._action_projection_prepared
        if prepared is not None and prepared[:2] == ("shared", snapshot):
            return prepared[2]
        raise ScheduleSourceError("This action is awaiting background plan preparation")


    def _build_operational_projection(self):
        snapshot = self._snapshot((), (), (), ())
        if snapshot == self._latest_projection_snapshot and isinstance(self._latest_projection, OperationalDayProjection):
            return self._latest_projection
        prepared = self._action_projection_prepared
        if prepared is not None and prepared[:2] == ("operational", snapshot):
            return prepared[2]
        raise ScheduleSourceError("This action is awaiting background plan preparation")


    def _selected_sop_schedule_plan_row(self) -> Optional[Dict[str, Any]]:
        plan = self._selected_frequency_plan_row()
        if not isinstance(plan, dict):
            return None
        category = str(plan.get("category") or "").strip().lower()
        return plan if category == "sop_schedule" else None


    def _refresh_radio_window_radio_combo(self, plan: Optional[Mapping[str, Any]]) -> None:
        if not hasattr(self, "radio_window_radio_combo"):
            return
        try:
            current = int(self.radio_window_radio_combo.currentData() or 0)
        except Exception:
            current = 0
        radio_ids = list(self._assigned_radio_ids_for_plan(plan or {}))
        if isinstance(plan, Mapping) and "_catalog_lane_ids" in plan:
            radio_ids.extend(plan["_catalog_lane_ids"])
        elif isinstance(plan, Mapping):
            for ref in self._schedule_refs_from_plan_row(plan):
                radio_id = self._radio_id_for_schedule_ref(ref)
                if radio_id > 0:
                    radio_ids.append(radio_id)
        radio_ids = sorted(set(int(radio_id) for radio_id in radio_ids if int(radio_id) > 0))
        self._radio_window_radio_combo_loading = True
        previous_block = self.radio_window_radio_combo.blockSignals(True)
        try:
            self.radio_window_radio_combo.clear()
            self.radio_window_radio_combo.addItem("All assigned radios", 0)
            for radio_id in radio_ids:
                self.radio_window_radio_combo.addItem(self._radio_label_for_id(int(radio_id)), int(radio_id))
            if current > 0:
                idx = self.radio_window_radio_combo.findData(current)
                if idx >= 0:
                    self.radio_window_radio_combo.setCurrentIndex(idx)
        finally:
            self.radio_window_radio_combo.blockSignals(previous_block)
            self._radio_window_radio_combo_loading = False

    def _selected_radio_window_radio_id(self) -> int:
        if not hasattr(self, "radio_window_radio_combo"):
            return 0
        try:
            return int(self.radio_window_radio_combo.currentData() or 0)
        except Exception:
            return 0

    def _radio_window_refs_for_plan(self, plan: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(plan, Mapping):
            return []
        refs = self._schedule_refs_from_plan_row(plan)
        direct_refs = [dict(ref) for ref in refs if self._radio_id_for_schedule_ref(ref) > 0]
        if direct_refs:
            return direct_refs
        assigned_ids = self._assigned_radio_ids_for_plan(plan)
        if not assigned_ids:
            return []
        radio_refs: List[Dict[str, Any]] = []
        for radio_id in assigned_ids:
            for ref in refs:
                lane_ref = dict(ref)
                lane_ref["target_device_profile_id"] = int(radio_id)
                lane_ref["lane_key"] = f"radio:{int(radio_id)}"
                lane_ref.setdefault("lane_label", self._radio_label_for_id(int(radio_id)))
                radio_refs.append(lane_ref)
        return radio_refs

    def _radio_window_overlap_labels(self, refs: List[Dict[str, Any]]) -> Dict[int, str]:
        labels: Dict[int, List[str]] = {}
        for left_index, left_ref in enumerate(refs):
            left_radio_id = self._radio_id_for_schedule_ref(left_ref)
            left_band = str(left_ref.get("band") or "").strip().upper()
            if left_radio_id <= 0:
                continue
            for right_index, right_ref in enumerate(refs):
                if right_index == left_index:
                    continue
                right_radio_id = self._radio_id_for_schedule_ref(right_ref)
                if right_radio_id <= 0 or right_radio_id == left_radio_id:
                    continue
                if not self._single_schedule_ref_overlap(left_ref, right_ref):
                    continue
                right_band = str(right_ref.get("band") or "").strip().upper()
                right_label = self._radio_label_for_id(right_radio_id)
                if left_band and right_band and left_band == right_band:
                    labels.setdefault(left_index, []).append(f"overlaps {right_label} on {left_band}")
                else:
                    labels.setdefault(left_index, []).append(f"overlaps {right_label}")
        return {index: "; ".join(dict.fromkeys(values)) for index, values in labels.items()}

    def _radio_window_group_label(self, ref: Mapping[str, Any], week_sunday: datetime.date) -> str:
        day_label, time_label = self._schedule_ref_day_and_time(ref, week_sunday)
        return f"{day_label} {time_label}".strip()

    def _radio_window_summary_text(
        self,
        plan_name: str,
        refs: List[Dict[str, Any]],
        overlap_labels: Mapping[int, str],
        week_sunday: datetime.date,
    ) -> str:
        radio_ids = sorted({self._radio_id_for_schedule_ref(ref) for ref in refs if self._radio_id_for_schedule_ref(ref) > 0})
        window_labels = {self._radio_window_group_label(ref, week_sunday) for ref in refs}
        overlap_windows = {
            self._radio_window_group_label(refs[index], week_sunday)
            for index, label in overlap_labels.items()
            if str(label or "").strip() and 0 <= int(index) < len(refs)
        }
        plan_label = str(plan_name or "selected plan").strip()
        parts = [
            f"Radio Windows: {len(refs)} assigned window{'s' if len(refs) != 1 else ''} from '{plan_label}'",
            f"{len(radio_ids)} radio{'s' if len(radio_ids) != 1 else ''}",
            f"{len(window_labels)} time window{'s' if len(window_labels) != 1 else ''}",
        ]
        if overlap_windows:
            parts.append(f"{len(overlap_windows)} overlap window{'s' if len(overlap_windows) != 1 else ''} to review")
        else:
            parts.append("no same-time radio overlaps detected")
        return " | ".join(parts) + ". Use Review RF Guard before assignment changes."

    def _build_selected_sop_plan_projection(self, week_sunday: datetime.date) -> Optional[OperationalDayProjection]:
        plan = self._selected_sop_schedule_plan_row()
        if not plan:
            return None
        refs = self._filter_rows_to_configured_groups(self._schedule_refs_from_plan_row(plan))
        return build_operational_day_projection_from_refs(refs, week_start_utc=week_sunday)


    def _default_save_plan_name(self, projection: BlendedScheduleProjection) -> str:
        source_bits = []
        counts = projection.source_counts
        if counts.get("HF", 0):
            source_bits.append("HF Daily")
        if counts.get("NET", 0):
            source_bits.append("HF Nets")
        if counts.get("SOP", 0):
            source_bits.append("SOP")
        source_text = " + ".join(source_bits) if source_bits else "Schedule"
        return f"{source_text} Plan {datetime.datetime.now().strftime('%Y-%m-%d')}"

    def _projection_review_text(self, projection: BlendedScheduleProjection) -> str:
        counts = projection.source_counts
        effective_count = len(projection.effective_segments)
        lines = [
            "Visible blended schedule projection:",
            self._source_selection_summary(),
            f"HF Daily rows considered: {counts.get('HF', 0)}",
            f"HF Net rows considered: {counts.get('NET', 0)}",
            f"SOP rows considered: {counts.get('SOP', 0)}",
            f"Effective windows to save: {effective_count}",
        ]
        preview = projection.effective_segments[:8]
        if preview:
            lines.append("")
            lines.append("First saved windows:")
            for segment in preview:
                label = segment.net_name or segment.group_name or segment.profile_name or segment.band or segment.source
                band_freq = f"{segment.band} {segment.frequency}".strip()
                lines.append(
                    f"- {segment.day_utc} {segment.start_utc}-{segment.end_utc} "
                    f"{segment.source} {label} {band_freq}".strip()
                )
            if len(projection.effective_segments) > len(preview):
                lines.append(f"- +{len(projection.effective_segments) - len(preview)} more")
        return "\n".join(lines)

    def _operational_projection_review_text(self, projection: OperationalDayProjection) -> str:
        counts = projection.source_counts
        refs = projection.schedule_refs()
        lines = [
            "Visible SOP Schedule Plan projection:",
            self._source_selection_summary(),
            f"Operational lanes: {len(projection.lanes)}",
            f"HF Daily entries: {counts.get('HF', 0)}",
            f"HF Net entries: {counts.get('NET', 0)}",
            f"Net Resource entries: {counts.get('NET_RESOURCE', 0)}",
            f"SOP entries: {counts.get('SOP', 0)}",
            f"Saved operational entries: {len(refs)}",
        ]
        lanes = projection.lanes[:8]
        if lanes:
            lines.append("")
            lines.append("Lanes:")
            for lane in lanes:
                lines.append(f"- {lane.lane_label}: {len(lane.entries)} entr{'y' if len(lane.entries) == 1 else 'ies'}")
            if len(projection.lanes) > len(lanes):
                lines.append(f"- +{len(projection.lanes) - len(lanes)} more lane(s)")
        preview = refs[:8]
        if preview:
            lines.append("")
            lines.append("First saved entries:")
            for row in preview:
                label = str(row.get("action_label") or row.get("net_name") or row.get("profile_name") or row.get("group_name") or row.get("source") or "").strip()
                band_freq = f"{str(row.get('band') or '').strip()} {str(row.get('frequency') or '').strip()}".strip()
                lines.append(
                    f"- {row.get('lane_label')} | {row.get('day_utc')} {row.get('start_utc')}-{row.get('end_utc')} "
                    f"{row.get('source')} {label} {band_freq}".strip()
                )
            if len(refs) > len(preview):
                lines.append(f"- +{len(refs) - len(preview)} more")
        return "\n".join(lines)

    def _current_plan_payload_from_projection(self) -> Tuple[Optional[Dict[str, Any]], int, str]:
        selected_plan = self._selected_frequency_plan_row()
        existing_id = int(selected_plan.get("id", 0) or 0) if selected_plan else 0
        name = self._current_frequency_plan_name()
        if self._planner_view_mode() == "operational":
            projection = self._build_operational_projection()
            refs = projection.schedule_refs()
            if not refs:
                return None, 0, "No HF Daily, HF Nets, Net Resources, or SOP entries are available to review."
            if not name:
                name = f"SOP Schedule Plan {datetime.datetime.now().strftime('%Y-%m-%d')}"
            payload = self._sop_plan_payload_from_projection(
                projection,
                name,
                description=f"Saved from FreqPlanner operational SOP Schedule Plan projection. {self._source_selection_summary()}",
            )
            if existing_id > 0:
                payload["id"] = existing_id
            return payload, len(refs), "SOP Schedule Plan"

        projection = self._build_blended_projection()
        if not projection.effective_segments:
            return None, 0, "No effective HF Daily, HF Nets, or SOP windows are available to review."
        if not name:
            name = self._default_save_plan_name(projection)
        schedule_refs = projection.schedule_refs()
        source_refs = (
            projection.source_refs()
            + self._visible_source_dependency_refs()
            + self._selected_sop_plan_dependency_refs()
        )
        payload = {
            "name": name,
            "status": "saved",
            "category": "normal",
            "description": "Saved from FreqPlanner blended HF Daily + HF Nets + SOP projection.",
            "source_refs": source_refs,
            "schedule_refs": schedule_refs,
            "frequency_refs": projection.frequency_refs(),
            "group_refs": projection.group_refs(),
            "notes": (
                f"FreqPlanner blended projection saved {datetime.datetime.now(datetime.timezone.utc).isoformat()}; "
                f"{self._source_selection_summary()}"
            ),
        }
        if existing_id > 0:
            payload["id"] = existing_id
        return payload, len(schedule_refs), "Frequency Plan"

    def _rf_guard_validation_summary_text(
        self,
        validation: Mapping[str, Any],
        *,
        schedule_count: int,
        plan_kind: str,
        plan_payload: Optional[Mapping[str, Any]] = None,
    ) -> str:
        state = str(validation.get("state") or "").strip().lower()
        rf_state = str(validation.get("rf_guard_validation") or "").strip().lower()
        warnings = validation.get("warnings", ()) or ()
        blocked = validation.get("blocked", ()) or ()
        messages = [str(item).strip() for item in (validation.get("messages", ()) or ())[:3] if str(item or "").strip()]
        lines: List[str] = []
        if state in {"pending", "unavailable"} or rf_state == "unavailable":
            lines.append("RF Guard verification is incomplete. Wait for current inputs, then review again.")
        elif state == "blocked":
            lines.append(f"RF Guard blocked this {plan_kind}. Resolve the blocked item(s), then review again.")
        elif state == "warning":
            lines.append(f"RF Guard found warning(s) for this {plan_kind}. Review the checklist before assignment or save.")
        elif state in {"off", "not_enforced"} or rf_state == "not_enforced":
            lines.append(
                "RF Guard could not run against a selected radio here. Assignment checks are still required in Configuration."
            )
        else:
            lines.append(f"RF Guard passed for {schedule_count} effective window(s).")
        lane_summary = self._radio_lane_summary_for_payload(plan_payload or {})
        if lane_summary:
            lines.append(f"Radio lanes reviewed: {lane_summary}.")
        if blocked:
            lines.append("")
            lines.append("Resolution Checklist - Blocked:")
            lines.extend(self._rf_guard_resolution_lines(blocked, limit=5))
            if len(blocked) > 5:
                lines.append(f"- +{len(blocked) - 5} more blocked item(s). Resolve the visible items first, then review again.")
        if warnings:
            lines.append("")
            lines.append("Resolution Checklist - Warnings:")
            lines.extend(self._rf_guard_resolution_lines(warnings, limit=5))
            if len(warnings) > 5:
                lines.append(f"- +{len(warnings) - 5} more warning(s). Resolve or accept the visible items first, then review again.")
        if not blocked and not warnings and messages:
            lines.extend(f"- {item}" for item in messages[:3])
        return "\n".join(lines)

    def _rf_guard_resolution_lines(self, items: List[str], *, limit: int) -> List[str]:
        lines: List[str] = []
        for index, item in enumerate(items[:limit], start=1):
            impact = self._rf_guard_issue_impact(item)
            next_step = self._rf_guard_resolution_hint(item)
            lines.append(f"{index}. Issue: {item}")
            if impact:
                lines.append(f"   Impact: {impact}")
            if next_step:
                lines.append(f"   Next: {next_step}")
        return lines

    def _rf_guard_issue_impact(self, message: str) -> str:
        lower = str(message or "").lower()
        if "antenna support does not include" in lower:
            return "The selected radio may not be safe or useful on the planned band."
        if "prevent band overlap" in lower or "would both be assigned on" in lower:
            return "Two transmit-capable radios may operate in the same protected band/window."
        if "advanced guard" in lower or ("within" in lower and "hz" in lower):
            return "Two planned frequencies may be too close for the configured RF Guard spacing."
        if "observer" in lower or "receive-only" in lower:
            return "A receive-only radio is being asked to use a transmit-capable plan."
        return "This plan may not be assignable exactly as configured."

    def _rf_guard_resolution_hint(self, message: str) -> str:
        text = str(message or "")
        lower = text.lower()
        if "antenna support does not include" in lower:
            return "Open Configuration > Radios and adjust the radio antenna bands or choose a plan layer on a supported band."
        if "prevent band overlap" in lower or "would both be assigned on" in lower:
            return "Open Configuration > Radios and separate the assignments, change one plan window, or adjust the RF Guard group/mode."
        if "advanced guard" in lower or ("within" in lower and "hz" in lower):
            return "Open Configuration > Radios and review the Advanced RF Guard spacing or move one schedule window/frequency."
        if "observer" in lower or "receive-only" in lower:
            return "Open Configuration > Radios and assign a receive-only plan or change the radio role."
        return "Review the affected plan window and radio assignment before saving."

    def _set_rf_guard_resolution_available(self, enabled: bool) -> None:
        if not hasattr(self, "resolve_rf_guard_btn"):
            return
        self.resolve_rf_guard_btn.setEnabled(bool(enabled))
        self.resolve_rf_guard_btn.setToolTip(
            "Open Configuration > Radios > Schedule Assignment to resolve the RF Guard issue(s)."
            if enabled
            else "Review RF Guard issues first, then open the radio assignment area to resolve them."
        )

    def _rf_guard_issue_rows(self, validation: Mapping[str, Any]) -> List[Tuple[str, str, str, str]]:
        rows: List[Tuple[str, str, str, str]] = []
        for level, key in (("Blocked", "blocked"), ("Warning", "warnings")):
            for item in (validation.get(key, ()) or ())[:self.MAX_RF_GUARD_ROWS - len(rows)]:
                issue = str(item or "").strip()
                if not issue:
                    continue
                rows.append(
                    (
                        level,
                        issue,
                        self._rf_guard_issue_impact(issue),
                        self._rf_guard_resolution_hint(issue),
                    )
                )
        return rows

    def _set_rf_guard_review_card(self, validation: Mapping[str, Any]) -> None:
        if not hasattr(self, "rf_guard_review_card"):
            return
        all_rows = self._rf_guard_issue_rows(validation) if isinstance(validation, Mapping) else []
        rows = all_rows[: self.MAX_RF_GUARD_ROWS]
        if not rows:
            self.rf_guard_review_table.setRowCount(0)
            self.rf_guard_review_card.setVisible(False)
            return
        state = str(validation.get("state") or "").strip().lower()
        blocked_count = len(validation.get("blocked", ()) or ())
        warning_count = len(validation.get("warnings", ()) or ())
        total_rows = blocked_count + warning_count
        bits: List[str] = []
        if blocked_count:
            bits.append(f"{blocked_count} blocked")
        if warning_count:
            bits.append(f"{warning_count} warning{'s' if warning_count != 1 else ''}")
        summary = ", ".join(bits) if bits else "Review needed"
        if total_rows > len(rows):
            summary += f"; showing first {len(rows)} of {total_rows}"
        if state == "blocked":
            summary = f"{summary}. Resolve blocked items before assignment or save."
        elif state == "warning":
            summary = f"{summary}. Review warnings before assignment or save."
        self.rf_guard_review_summary_label.setText(summary)
        self.rf_guard_review_table.setRowCount(len(rows))
        theme = resolve_theme(self.settings)
        for row_index, (level, issue, impact, next_action) in enumerate(rows):
            for col, value in enumerate((level, issue, impact, next_action)):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setToolTip(value)
                if level == "Blocked":
                    item.setBackground(qcolor(theme.get("danger_bg", "#F8D7DA")))
                    item.setForeground(qcolor(theme.get("danger_fg", theme.get("text", "#1f2328"))))
                else:
                    item.setBackground(qcolor(theme.get("warning_bg", "#FFF3CD")))
                    item.setForeground(qcolor(theme.get("warning_fg", theme.get("text", "#1f2328"))))
                self.rf_guard_review_table.setItem(row_index, col, item)
        self.rf_guard_review_table.resizeRowsToContents()
        self.rf_guard_review_card.setVisible(True)

    def _on_rf_guard_review_selection_changed(self) -> None:
        table = getattr(self, "rf_guard_review_table", None)
        if table is None or not hasattr(self, "frequency_plan_action_hint_label"):
            return
        selected = table.selectionModel().selectedRows() if table.selectionModel() is not None else []
        if not selected:
            return
        row = selected[0].row()
        level_item = table.item(row, 0)
        issue_item = table.item(row, 1)
        impact_item = table.item(row, 2)
        next_item = table.item(row, 3)
        level = level_item.text() if level_item is not None else "RF Guard"
        issue = issue_item.text() if issue_item is not None else ""
        impact = impact_item.text() if impact_item is not None else ""
        next_step = next_item.text() if next_item is not None else "Open Assign Schedule to resolve this item."
        self.frequency_plan_action_hint_label.setText(
            f"{level}: {issue} Impact: {impact} Next: {next_step} Double-click the issue or use Resolve RF Guard."
        )

    def _refresh_assigned_plan_rf_guard_review(self, plan: Optional[Mapping[str, Any]]) -> bool:
        if not isinstance(plan, Mapping) or getattr(self, "_frequency_plan_layers_dirty", False):
            self._set_rf_guard_review_card({})
            return False
        if not self._assigned_radio_ids_for_plan(plan):
            self._set_rf_guard_review_card({})
            return False
        try:
            validation = self._rf_guard_preflight_for_plan(dict(plan))
        except Exception as exc:
            log.debug("FreqPlanner: assigned-plan RF Guard refresh skipped: %s", exc)
            return False
        validation = self._merge_frequency_variant_validation(validation, dict(plan))
        state = str(validation.get("state") or "").strip().lower()
        if state not in {"blocked", "warning"}:
            self._set_rf_guard_review_card({})
            self._set_rf_guard_resolution_available(False)
            return False
        self.frequency_plan_action_hint_label.setText(
            self._rf_guard_validation_summary_text(
                validation,
                schedule_count=len(self._schedule_ref_mappings(dict(plan))),
                plan_kind="Frequency Plan",
                plan_payload=plan,
            )
        )
        self._set_rf_guard_review_card(validation)
        self._set_rf_guard_resolution_available(True)
        return True


    def _open_schedule_assignment_settings(
        self,
        *,
        plan_name: str = "",
        purpose: str = "resolve",
        plan_id: int = 0,
        device_profile_id: int = 0,
    ) -> bool:
        win = self.window()
        if hasattr(win, "open_settings_section"):
            try:
                win.open_settings_section("schedule_assignments", settings_nav_context="radios")
                label = str(plan_name or "the selected plan").strip()
                assigned_ids: List[int] = []
                if plan_id > 0:
                    assigned_ids = self._assigned_radio_ids_for_plan({"id": int(plan_id)})
                try:
                    target_radio_id = int(device_profile_id or 0)
                except Exception:
                    target_radio_id = 0
                if target_radio_id <= 0:
                    target_radio_id = int(assigned_ids[0]) if assigned_ids else 0
                settings_tab = getattr(win, "settings_tab", None)
                opener = getattr(settings_tab, "open_schedule_assignment_editor", None)
                if callable(opener):
                    QTimer.singleShot(
                        0,
                        lambda pid=int(plan_id or 0), rid=int(target_radio_id or 0): opener(
                            plan_id=pid,
                            device_profile_id=rid,
                        ),
                    )
                if str(purpose or "").strip().lower() == "assign":
                    self.frequency_plan_action_hint_label.setText(
                        f"Opened Configuration > Radios > Schedule Assignment for '{label}'. Choose the radio and save with RF Guard."
                    )
                else:
                    self.frequency_plan_action_hint_label.setText(
                        f"Opened Configuration > Radios > Schedule Assignment to resolve RF Guard issues for {label}."
                    )
                return True
            except Exception as exc:
                log.debug("FreqPlanner: failed opening Settings schedule assignment: %s", exc)
        return False

    def _on_resolve_rf_guard_clicked(self) -> None:
        plan = self._selected_frequency_plan_row()
        plan_name = str((plan or {}).get("name") or self._current_frequency_plan_name() or "the visible plan").strip()
        try:
            plan_id = int((plan or {}).get("id") or 0)
        except Exception:
            plan_id = 0
        if self._open_schedule_assignment_settings(plan_name=plan_name, plan_id=plan_id):
            return
        self.frequency_plan_action_hint_label.setText(
            "Open Configuration > Radios > Schedule Assignment to resolve the RF Guard issue(s)."
        )

    def _on_review_rf_guard_clicked(self) -> None:
        if self._ensure_action_projection("operational" if self._planner_view_mode() == "operational" else "shared", self._on_review_rf_guard_clicked):
            return
        try:
            plan_payload, schedule_count, plan_kind = self._current_plan_payload_from_projection()
        except Exception as exc:
            log.exception("FreqPlanner: failed building RF Guard review payload.")
            self.frequency_plan_action_hint_label.setText(f"Unable to build RF Guard review: {exc}")
            return
        if not plan_payload:
            self.frequency_plan_action_hint_label.setText(plan_kind)
            self._set_rf_guard_resolution_available(False)
            self._set_rf_guard_review_card({})
            return
        self._review_plan_payload_with_guard(plan_payload, schedule_count, plan_kind)

    def _review_plan_payload_with_guard(self, plan_payload, schedule_count, plan_kind):
        if "_catalog_action_nonce" not in plan_payload:
            import uuid
            plan_payload["_catalog_action_nonce"] = str(uuid.uuid4())
        if self._request_fresh_action(self._review_plan_payload_with_guard,
                                      (plan_payload, schedule_count, plan_kind),
                                      guard_payload=plan_payload, target_plan_id=int(plan_payload.get("id") or 0)):
            return
        try:
            validation = self._rf_guard_preflight_for_plan(plan_payload)
        except ValueError as exc:
            validation = {
                "state": "blocked",
                "rf_guard_validation": "enforced",
                "messages": [str(exc)],
                "blocked": [str(exc)],
            }
        except Exception as exc:
            log.exception("FreqPlanner: RF Guard review failed.")
            validation = {
                "state": "warning",
                "messages": [f"RF Guard review could not complete: {exc}"],
                "warnings": [f"RF Guard review could not complete: {exc}"],
            }
        validation = self._merge_frequency_variant_validation(validation, plan_payload)
        self.frequency_plan_action_hint_label.setText(
            self._rf_guard_validation_summary_text(
                validation,
                schedule_count=schedule_count,
                plan_kind=plan_kind,
                plan_payload=plan_payload,
            )
        )
        state = str(validation.get("state") or "").strip().lower()
        self._set_rf_guard_review_card(validation)
        self._set_rf_guard_resolution_available(state in {"blocked", "warning"})


    def _save_plan_payload_with_guard(
        self,
        plan_payload: Dict[str, Any],
        *,
        schedule_count: int,
        success_kind: str = "Frequency Plan",
        after_save=None,
    ) -> Optional[Dict[str, Any]]:
        if "_catalog_action_nonce" not in plan_payload:
            import uuid
            plan_payload["_catalog_action_nonce"] = str(uuid.uuid4())
        # Populate advisory validation requests before the fresh worker check.
        # Pending validation is never permission to commit.
        if self._fresh_action_token is None:
            self._rf_guard_preflight_for_plan(plan_payload)
        if self._request_fresh_action(self._save_plan_payload_with_guard, (dict(plan_payload),),
                                      {"schedule_count": schedule_count, "success_kind": success_kind, "after_save": after_save},
                                      guard_payload=dict(plan_payload), target_plan_id=int(plan_payload.get("id") or 0)):
            return None
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        missing_text = self._source_dependency_block_text()
        if missing_text:
            self.frequency_plan_action_hint_label.setText(missing_text)
            return None
        try:
            validation = self._rf_guard_preflight_for_plan(plan_payload)
        except ValueError as exc:
            self.frequency_plan_action_hint_label.setText(f"RF Guard blocked this plan before save: {exc}")
            return None
        except Exception as exc:
            log.exception("FreqPlanner: RF Guard preflight failed.")
            validation = {
                "state": "warning",
                "messages": [f"RF Guard preflight could not complete: {exc}"],
            }
        if str(validation.get("state") or "").lower() in {"pending", "unavailable"} or validation.get("rf_guard_validation") == "unavailable":
            self.frequency_plan_action_hint_label.setText("RF Guard verification is incomplete. Review current inputs before saving.")
            return None
        validation = self._merge_frequency_variant_validation(validation, plan_payload)
        state = str(validation.get("state") or "").strip().lower()
        messages = [str(item) for item in validation.get("messages", []) if str(item or "").strip()]
        if state == "blocked":
            self.frequency_plan_action_hint_label.setText(
                "RF Guard blocked this Frequency Plan before save. " + (messages[0] if messages else "")
            )
            return None
        if state == "warning":
            response = QMessageBox.question(
                self,
                "RF Guard Warning",
                "RF Guard found warnings for the selected radio context.\n\n"
                + "\n".join(messages[:5])
                + "\n\nSave the Frequency Plan anyway?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if response != QMessageBox.Save:
                return None
        if state in {"off", "not_enforced"} or str(validation.get("rf_guard_validation") or "").strip().lower() == "not_enforced":
            response = QMessageBox.question(
                self,
                "RF Guard Preflight Skipped",
                "No radio context is selected, so FreqPlanner could not run assignment-specific RF Safety Guard checks.\n\n"
                "Save this Frequency Plan anyway? RF Safety Guard will run when the plan is assigned to a radio.",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if response != QMessageBox.Save:
                return None
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return None
        try:
            saved = self.plan_context_service.store.save_frequency_plan(plan_payload)
        except Exception as exc:
            log.exception("FreqPlanner: failed saving Frequency Plan.")
            self.frequency_plan_action_hint_label.setText(f"Unable to save Frequency Plan: {exc}")
            return None
        self._creating_new_frequency_plan = False
        self._set_frequency_plan_layers_dirty(False)
        self.plan_context_service.invalidate()
        saved_id = int(saved.get("id", 0) or 0)
        self._pending_saved_plan_id = saved_id
        if saved_id:
            idx = self.frequency_plan_combo.findData(saved_id)
            if idx >= 0:
                self.frequency_plan_combo.setCurrentIndex(idx)
        if state in {"", "ok"}:
            guard_text = "RF Guard preflight passed."
        elif state in {"off", "not_enforced"} or str(validation.get("rf_guard_validation") or "").strip().lower() == "not_enforced":
            guard_text = "RF Guard preflight skipped; assignment checks still required."
        else:
            guard_text = "RF Guard warning accepted."
        self._set_action_feedback(
            f"Saved {success_kind} '{str(saved.get('name') or plan_payload.get('name') or 'Plan')}' "
            f"with {schedule_count} effective window(s). {guard_text}"
        )
        if after_save is not None:
            after_save(saved)
        return saved

    def _on_save_plan_clicked(self) -> None:
        if self._ensure_action_projection("shared", self._on_save_plan_clicked):
            return
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        try:
            projection = self._build_blended_projection()
        except Exception as exc:
            log.exception("FreqPlanner: failed building blended projection.")
            self.frequency_plan_action_hint_label.setText(f"Unable to build the blended schedule projection: {exc}")
            return
        if not projection.effective_segments:
            self.frequency_plan_action_hint_label.setText(
                "No effective HF Daily, HF Nets, or SOP windows are available to save."
            )
            return
        selected_plan = self._selected_frequency_plan_row()
        if getattr(self, "_input_read_error", ""):
            return
        selected_category = str((selected_plan or {}).get("category") or "").strip().lower()
        existing_id = int(selected_plan.get("id", 0) or 0) if selected_plan and selected_category != "sop_schedule" else 0
        name = self._current_frequency_plan_name()
        if selected_plan and selected_category == "sop_schedule" and name == str(selected_plan.get("name") or "").strip():
            name = self._default_save_plan_name(projection)
            if hasattr(self, "frequency_plan_combo"):
                self.frequency_plan_combo.setEditText(name)
        if not name:
            name = self._default_save_plan_name(projection)
            if hasattr(self, "frequency_plan_combo"):
                self.frequency_plan_combo.setEditText(name)
        if not name:
            self.frequency_plan_action_hint_label.setText("Enter a clear Frequency Plan name before saving.")
            return
        schedule_refs = projection.schedule_refs()
        source_refs = (
            projection.source_refs()
            + self._visible_source_dependency_refs()
            + self._selected_sop_plan_dependency_refs()
        )
        plan_payload: Dict[str, Any] = {
            "name": name,
            "status": "saved",
            "category": "normal",
            "description": "Saved from FreqPlanner blended HF Daily + HF Nets + SOP projection.",
            "source_refs": source_refs,
            "schedule_refs": schedule_refs,
            "frequency_refs": projection.frequency_refs(),
            "group_refs": projection.group_refs(),
            "notes": (
                f"FreqPlanner blended projection saved {datetime.datetime.now(datetime.timezone.utc).isoformat()}; "
                f"{self._source_selection_summary()}"
            ),
        }
        if existing_id > 0:
            plan_payload["id"] = existing_id
        self._save_plan_payload_with_guard(
            plan_payload,
            schedule_count=len(schedule_refs),
            success_kind="Frequency Plan",
        )

    def _on_save_sop_plan_clicked(self) -> None:
        if self._ensure_action_projection("operational", self._on_save_sop_plan_clicked):
            return
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        try:
            projection = self._build_operational_projection()
        except Exception as exc:
            log.exception("FreqPlanner: failed building SOP Schedule Plan projection.")
            self.frequency_plan_action_hint_label.setText(f"Unable to build the SOP Schedule Plan projection: {exc}")
            return
        refs = projection.schedule_refs()
        if not refs:
            self.frequency_plan_action_hint_label.setText(
                "No HF Daily, HF Nets, Net Resources, or SOP entries are available to save as an SOP Schedule Plan."
            )
            return
        selected_plan = self._selected_frequency_plan_row()
        if getattr(self, "_input_read_error", ""):
            return
        selected_category = str((selected_plan or {}).get("category") or "").strip().lower()
        existing_id = int(selected_plan.get("id", 0) or 0) if selected_plan and selected_category == "sop_schedule" else 0
        name = self._current_frequency_plan_name()
        if selected_plan and selected_category != "sop_schedule" and name == str(selected_plan.get("name") or "").strip():
            name = f"SOP Schedule Plan {datetime.datetime.now().strftime('%Y-%m-%d')}"
            if hasattr(self, "frequency_plan_combo"):
                self.frequency_plan_combo.setEditText(name)
        if not name:
            name = f"SOP Schedule Plan {datetime.datetime.now().strftime('%Y-%m-%d')}"
            if hasattr(self, "frequency_plan_combo"):
                self.frequency_plan_combo.setEditText(name)
        if not name:
            self.frequency_plan_action_hint_label.setText("Enter a clear SOP Schedule Plan name before saving.")
            return
        plan_payload = projection.to_frequency_plan_payload(
            name,
            description=f"Saved from FreqPlanner operational SOP Schedule Plan projection. {self._source_selection_summary()}",
        )
        if existing_id > 0:
            plan_payload["id"] = existing_id
        self._save_plan_payload_with_guard(
            plan_payload,
            schedule_count=len(refs),
            success_kind="SOP Schedule Plan",
        )

    @classmethod
    def _condition_level_match(cls, condition_levels: str, group_level: Optional[int]) -> bool:
        normalized = cls._normalize_condition_levels(condition_levels)
        if normalized == "ALL":
            return True
        if group_level is None:
            return True
        allowed: Set[int] = set()
        for token in normalized.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                lvl = int(token)
            except Exception:
                continue
            if 1 <= lvl <= 5:
                allowed.add(lvl)
        if not allowed:
            return True
        return int(group_level) in allowed

    def _load_live_schedules(self):
        if self._catalog_model is None:
            raise ScheduleSourceError("Plan Builder inputs are still loading")
        return self._catalog_model.live


    def _load_schedules(self) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict[str, Any]]]:
        selected_hf = self._selected_source_set_id(SELECTED_HF_DAILY_SOURCE_SET_KEY)
        selected_net = self._selected_source_set_id(SELECTED_HF_NET_SOURCE_SET_KEY)
        hf_set = self._source_set_row_by_id(HF_DAILY_SOURCE_SETS_KEY, selected_hf)
        net_set = self._source_set_row_by_id(HF_NET_SOURCE_SETS_KEY, selected_net)
        if selected_hf != LIVE_SOURCE_SET_ID and hf_set is None:
            raise MissingScheduleSourceError(f"Declared schedule source is unavailable: {HF_DAILY_SOURCE_CATEGORY}:{selected_hf}")
        if selected_net not in {LIVE_SOURCE_SET_ID, NO_NET_SOURCE_SET_ID} and net_set is None:
            raise MissingScheduleSourceError(f"Declared schedule source is unavailable: {HF_NET_SOURCE_CATEGORY}:{selected_net}")
        selected_sop = self._selected_sop_plan_source_id()
        sop_plan = self._sop_schedule_plan_row_by_id(selected_sop)
        if selected_sop != LIVE_SOURCE_SET_ID and sop_plan is None:
            raise MissingScheduleSourceError(f"Declared SOP source is unavailable: sop_schedule_plan:{selected_sop}")
        hf, net, sop, policies = self._load_live_schedules()
        if hf_set is not None:
            hf = hf_set.get("rows", ())
        if str(selected_net or "").strip() == NO_NET_SOURCE_SET_ID:
            net = []
        if net_set is not None:
            net = net_set.get("rows", ())
        if sop_plan is not None:
            if sop_plan.get("_catalog_error"):
                raise ScheduleSourceError(sop_plan["_catalog_error"])
            sop = sop_plan.get("_catalog_sop_refs", ())
        self._sop_schedule_rows_present = bool(sop)
        return hf, net, sop, policies

    @staticmethod
    def _policy_overlap(
        a_start: datetime.datetime,
        a_end: datetime.datetime,
        b_start: datetime.datetime,
        b_end: datetime.datetime,
    ) -> bool:
        return a_start < b_end and b_start < a_end

    @staticmethod
    def _parse_iso_utc(value: str) -> Optional[datetime.datetime]:
        txt = str(value or "").strip()
        if not txt:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(txt.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone(datetime.timezone.utc)
        except Exception:
            return None

    def _load_net_sop_policies_from_db(self):
        return self._catalog_model.live[3] if self._catalog_model is not None else None


    def _effective_net_sop_policy_for_window(
        self,
        *,
        cell_start_utc: datetime.datetime,
        cell_end_utc: datetime.datetime,
        net_slices: List[Tuple[int, int, str, str]],
        sop_slices: List[Tuple[int, int, str, str]],
        policy_rows: List[Dict[str, Any]],
    ) -> str:
        saw_sop = False
        for n_start, n_end, _n_label, n_sig in net_slices:
            if n_end <= n_start:
                continue
            for s_start, s_end, _s_label, s_sig in sop_slices:
                if s_end <= s_start:
                    continue
                overlap_start_min = max(n_start, s_start)
                overlap_end_min = min(n_end, s_end)
                if overlap_end_min <= overlap_start_min:
                    continue
                overlap_start_utc = cell_start_utc + datetime.timedelta(minutes=overlap_start_min)
                overlap_end_utc = cell_start_utc + datetime.timedelta(minutes=overlap_end_min)
                for row in policy_rows:
                    if str(row.get("net_row_signature") or "") != str(n_sig or ""):
                        continue
                    if str(row.get("sop_row_signature") or "") != str(s_sig or ""):
                        continue
                    policy = str(row.get("policy") or "").strip().upper()
                    start_dt = row.get("start_utc")
                    end_dt = row.get("end_utc")
                    if not isinstance(start_dt, datetime.datetime) or not isinstance(end_dt, datetime.datetime):
                        continue
                    if not self._policy_overlap(overlap_start_utc, overlap_end_utc, start_dt, end_dt):
                        continue
                    if policy == "NET_PRIORITY":
                        return "NET_PRIORITY"
                    if policy == "SOP_PRIORITY":
                        saw_sop = True
        return "SOP_PRIORITY" if saw_sop else ""

    @staticmethod
    def _normalize_day_for_signature(day: str) -> str:
        raw = str(day or "").strip()
        if not raw:
            return "ALL"
        up = raw.upper()
        if up in {"ALL", "DAILY"}:
            return "ALL"
        for opt in DAY_NAMES:
            if up.startswith(opt[:3].upper()):
                return opt
        return raw

    @staticmethod
    def _normalize_recurrence_for_signature(recurrence: str) -> str:
        raw = str(recurrence or "Weekly").strip().upper()
        if raw == "MONTHLY":
            raw = "PERIODIC"
        if raw in {"DAILY", "PERIODIC", "BI-WEEKLY", "WEEKLY"}:
            return "Bi-Weekly" if raw == "BI-WEEKLY" else raw.title()
        return "Weekly"

    @staticmethod
    def _normalize_frequency_for_signature(value: Any) -> str:
        txt = str(value or "").strip()
        if not txt:
            return ""
        return format_frequency_mhz(txt)

    def _normalize_month_weeks_for_signature(self, value: Any) -> str:
        weeks = self._parse_month_weeks(str(value or ""))
        return ",".join(str(v) for v in weeks)

    def _net_row_signature(self, row: Dict[str, Any]) -> str:
        day = self._normalize_day_for_signature(str(row.get("day_utc") or "ALL"))
        recurrence = self._normalize_recurrence_for_signature(str(row.get("recurrence") or "Weekly"))
        biweekly = int(row.get("biweekly_offset_weeks") or 0)
        weeks = self._normalize_month_weeks_for_signature(row.get("month_weeks"))
        group = str(row.get("group_name") or "").strip().upper()
        band = str(row.get("band") or "").strip().upper()
        freq = self._normalize_frequency_for_signature(row.get("frequency"))
        start = str(row.get("start_utc") or "").strip()
        end = str(row.get("end_utc") or "").strip()
        net_name = str(row.get("net_name") or row.get("name") or "").strip().upper()
        return (
            f"NET|{group}|{band}|{freq}|{day}|{recurrence}|{biweekly}|"
            f"{weeks}|{start}|{end}|{net_name}"
        )

    def _sop_row_signature(self, row: Dict[str, Any]) -> str:
        day = self._normalize_day_for_signature(str(row.get("day_utc") or "ALL"))
        recurrence = self._normalize_recurrence_for_signature(str(row.get("recurrence") or "Weekly"))
        biweekly = int(row.get("biweekly_offset_weeks") or 0)
        weeks = self._normalize_month_weeks_for_signature(row.get("month_weeks"))
        group = str(row.get("group_name") or "").strip().upper()
        band = str(row.get("band") or "").strip().upper()
        freq = self._normalize_frequency_for_signature(row.get("frequency"))
        start = str(row.get("start_utc") or "").strip()
        end = str(row.get("end_utc") or "").strip()
        profile_id = int(row.get("sop_profile_id") or 0)
        layer_id = int(row.get("sop_layer_id") or row.get("id") or 0)
        return (
            f"SOP|{profile_id}|{layer_id}|{group}|{band}|{freq}|{day}|"
            f"{recurrence}|{biweekly}|{weeks}|{start}|{end}"
        )

    def _load_hf_from_db(self):
        return self._catalog_model.live[0] if self._catalog_model is not None else None


    def _load_net_from_db(self):
        return self._catalog_model.live[1] if self._catalog_model is not None else None


    def _load_net_resources_from_db(self):
        return self._catalog_model.resources if self._catalog_model is not None else ()


    def _load_sop_layer_from_db(self):
        return self._catalog_model.live[2] if self._catalog_model is not None else None


    def _parse_hhmm(self, s: str) -> int | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            h, m = s.split(":")
            h = int(h)
            m = int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except Exception:
            return None
        return None

    def _week_start_sunday_utc(self, now_utc: datetime.datetime) -> datetime.date:
        delta = (now_utc.weekday() + 1) % 7  # Sunday=0
        return (now_utc - datetime.timedelta(days=delta)).date()

    def _month_week_index(self, date_val: datetime.date) -> int:
        return 1 + ((date_val.day - 1) // 7)

    def _parse_month_weeks(self, txt: str) -> List[int]:
        weeks: List[int] = []
        for token in (txt or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                val = int(token)
            except Exception:
                continue
            if 1 <= val <= 5:
                weeks.append(val)
        return sorted(set(weeks))

    def _net_row_applies_this_week(
        self, row: Dict, targets: List[str], week_sunday: datetime.date
    ) -> bool:
        recurrence = (row.get("recurrence") or "Weekly").strip()
        if recurrence not in ("Periodic", "Monthly"):
            return True
        weeks = self._parse_month_weeks(row.get("month_weeks", ""))
        if not weeks:
            weeks = [1]
        for idx, day_name in enumerate(DAY_NAMES):
            if day_name not in targets:
                continue
            date_val = week_sunday + datetime.timedelta(days=idx)
            if self._month_week_index(date_val) in weeks:
                return True
        return False

    def _hour_overlaps(self, start_min: int, end_min: int, hour: int) -> bool:
        """
        Returns True if the [start_min, end_min] interval overlaps any minute in this hour bucket.
        """
        hour_start = hour * 60
        hour_end = hour * 60 + 59
        return not (end_min < hour_start or start_min > hour_end)

    def _next_day(self, day_name_upper: str) -> str:
        try:
            idx = DAY_NAMES_UPPER.index(day_name_upper)
            return DAY_NAMES[(idx + 1) % 7]
        except Exception:
            return DAY_NAMES[0]

    def _expand_hours_for_day(self, day_val: str, start_min: int, end_min: int, *, early: int = 0) -> List[tuple[str, int]]:
        """
        Expand a schedule row into (day_name, hour) tuples, handling ALL and overnight spans.
        Times are in minutes from 00:00 UTC. early applies only to net rows (already adjusted).
        """
        targets: List[str] = []
        day_txt = (day_val or "ALL").strip().upper()
        if day_txt == "ALL" or day_txt not in DAY_NAMES_UPPER:
            targets = DAY_NAMES[:]  # all days in Title case
        else:
            # Title-case version from canonical list
            targets = [DAY_NAMES[DAY_NAMES_UPPER.index(day_txt)]]

        hours: List[tuple[str, int]] = []
        smin = start_min
        emin = end_min
        overnight = smin > emin

        for day_name in targets:
            day_upper = day_name.upper()
            if not overnight:
                for h in range(24):
                    if self._hour_overlaps(smin, emin, h):
                        hours.append((day_name, h))
            else:
                # Segment 1: from start to 23:59 on current day
                for h in range(24):
                    if self._hour_overlaps(smin, 23 * 60 + 59, h):
                        hours.append((day_name, h))
                # Segment 2: from 00:00 to end on next day
                next_day = self._next_day(day_upper)
                for h in range(24):
                    if self._hour_overlaps(0, emin, h):
                        hours.append((next_day, h))

        return hours

    def _net_window_for_day(
        self, row: Dict, day_name: str, now_utc: datetime.datetime
    ) -> Optional[tuple[datetime.datetime, datetime.datetime]]:
        """
        Given a net row and target day name, compute start/end UTC datetimes for that day.
        Returns None if times are invalid; caller filters by time window.
        """
        start_m = self._parse_hhmm(row.get("start_utc", ""))
        end_m = self._parse_hhmm(row.get("end_utc", ""))
        if start_m is None or end_m is None:
            return None
        overnight = start_m > end_m
        if not overnight:
            if end_m % 60 == 0:
                end_m = min(end_m + 60, 24 * 60)

        # Map day_name to offset from current UTC day (DAY_NAMES starts with Sunday=0)
        try:
            day_idx = DAY_NAMES.index(day_name)
        except ValueError:
            return None
        now_idx = now_utc.weekday()  # Monday=0
        now_day_sun0 = (now_idx + 1) % 7  # convert to Sunday=0..Saturday=6
        offset = (day_idx - now_day_sun0) % 7

        base_date = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=offset)
        start_dt = base_date + datetime.timedelta(minutes=start_m)
        end_dt = base_date + datetime.timedelta(minutes=end_m)
        if overnight:
            end_dt += datetime.timedelta(days=1)
        return start_dt, end_dt

    def _start_of_week_local(self, tz: datetime.tzinfo) -> datetime.datetime:
        """
        Returns a datetime for Sunday 00:00 of the current local week.
        """
        now_local = datetime.datetime.now(tz)
        # weekday: Monday=0, Sunday=6; want Sunday as start -> offset (weekday+1) % 7
        days_to_sunday = (now_local.weekday() + 1) % 7
        start_date = (now_local - datetime.timedelta(days=days_to_sunday)).date()
        return datetime.datetime.combine(start_date, datetime.time(0, 0)).replace(tzinfo=tz)

    def _projection_cells_by_utc_key(self, projection: BlendedScheduleProjection) -> Dict[Tuple[str, int], ProjectionCell]:
        return {(cell.day_utc, int(cell.hour_utc)): cell for cell in projection.cells}

    def _projection_cell_text(self, cell: Optional[ProjectionCell]) -> str:
        if cell is None or not cell.effective_source:
            return ""
        if cell.effective_source == "HF" and not self._show_band:
            return cell.frequency or cell.band or cell.display_label
        return cell.display_label or cell.band or cell.frequency

    def _projection_cell_tooltip(self, cell: Optional[ProjectionCell]) -> str:
        if cell is None:
            return ""
        lines = [
            f"{cell.day_utc} {cell.start_utc.strftime('%H:%M')}-{cell.end_utc.strftime('%H:%M')} UTC",
            f"Effective source: {cell.effective_source or 'None'}",
        ]
        if cell.band or cell.frequency:
            lines.append(f"Band/Frequency: {cell.band or '--'} {cell.frequency or ''}".strip())
        lines.append(
            f"Sources: HF {len(cell.hf_segments)}, Net {len(cell.net_segments)}, SOP {len(cell.sop_segments)}"
        )
        return "\n".join(lines)

    def _projection_window_tooltip(self, cell: Optional[EffectiveWindowCell]) -> str:
        if cell is None:
            return ""
        segment = cell.segment
        lines = [
            self._effective_window_time_label(segment),
            f"Effective layer: {self._effective_window_source_label(segment.source)}",
        ]
        label = self._effective_window_label(segment)
        if label:
            lines.append(f"Plan target: {label}")
        if segment.band or segment.frequency:
            lines.append(f"Band/Frequency: {segment.band or '--'} {segment.frequency or ''}".strip())
        lines.append(f"Sources: Daily {len(cell.hf_segments)}, Net {len(cell.net_segments)}, SOP {len(cell.sop_segments)}")
        return "\n".join(lines)

    def _operational_cell_text(self, cell: Optional[OperationalCell]) -> str:
        if cell is None or not cell.entries:
            return ""
        label = cell.display_label
        return f"! {label}" if cell.has_contention and label else label

    @staticmethod
    def _effective_window_source_label(source: str) -> str:
        key = str(source or "").strip().upper()
        if key == "HF":
            return "Daily"
        if key == "NET":
            return "Net"
        if key == "SOP":
            return "SOP"
        return key or "Plan"

    def _effective_window_time_label(self, segment: ScheduleSegment) -> str:
        if not self._show_local:
            return f"{segment.day_utc} {segment.start_utc}-{segment.end_utc} UTC"
        tz_name, tz = self._current_timezone()
        try:
            day_index = DAY_NAMES.index(segment.day_utc)
        except ValueError:
            day_index = 0
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        week_sunday = self._week_start_sunday_utc(now_utc)
        base_date = week_sunday + datetime.timedelta(days=day_index)
        base_utc = datetime.datetime.combine(base_date, datetime.time(0), tzinfo=datetime.timezone.utc)
        start_utc = base_utc + datetime.timedelta(minutes=segment.start_minute)
        end_offset = segment.end_minute if segment.end_minute > segment.start_minute else segment.end_minute + 1440
        end_utc = base_utc + datetime.timedelta(minutes=end_offset)
        start_local = start_utc.astimezone(tz)
        end_local = end_utc.astimezone(tz)
        abbr = self._ui_tz_abbr(tz_name, start_local.tzname() or tz_name)
        day_label = start_local.strftime("%A")
        if start_local.date() != end_local.date():
            return f"{day_label} {start_local.strftime('%H:%M')}-{end_local.strftime('%a %H:%M')} {abbr}"
        return f"{day_label} {start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')} {abbr}"

    def _effective_window_day_and_time(self, segment: ScheduleSegment) -> Tuple[str, str]:
        label = self._effective_window_time_label(segment)
        parts = label.split(" ", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return segment.day_utc, f"{segment.start_utc}-{segment.end_utc}"

    def _schedule_ref_day_and_time(self, ref: Mapping[str, Any], week_sunday: datetime.date) -> Tuple[str, str]:
        day_utc = str(ref.get("day_utc") or "Sunday").strip()
        if day_utc.upper() == "ALL":
            day_utc = "Sunday"
        if day_utc not in DAY_NAMES:
            try:
                day_utc = DAY_NAMES[DAY_NAMES_UPPER.index(day_utc.upper())]
            except Exception:
                day_utc = "Sunday"
        start_minute = self._parse_guard_hhmm(str(ref.get("start_utc") or ref.get("start") or ""))
        end_minute = self._parse_guard_hhmm(str(ref.get("end_utc") or ref.get("end") or ""))
        if start_minute is None or end_minute is None:
            return day_utc, "--"
        if not self._show_local:
            return day_utc, f"{start_minute // 60:02d}:{start_minute % 60:02d}-{end_minute // 60:02d}:{end_minute % 60:02d} UTC"
        tz_name, tz = self._current_timezone()
        day_index = DAY_NAMES.index(day_utc)
        base_date = week_sunday + datetime.timedelta(days=day_index)
        base_utc = datetime.datetime.combine(base_date, datetime.time(0), tzinfo=datetime.timezone.utc)
        start_utc = base_utc + datetime.timedelta(minutes=start_minute)
        end_offset = end_minute if end_minute > start_minute else end_minute + 1440
        end_utc = base_utc + datetime.timedelta(minutes=end_offset)
        start_local = start_utc.astimezone(tz)
        end_local = end_utc.astimezone(tz)
        abbr = self._ui_tz_abbr(tz_name, start_local.tzname() or tz_name)
        day_label = start_local.strftime("%A")
        if start_local.date() != end_local.date():
            return day_label, f"{start_local.strftime('%H:%M')}-{end_local.strftime('%a %H:%M')} {abbr}"
        return day_label, f"{start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')} {abbr}"

    def _radio_label_for_id(self, radio_id: int) -> str:
        try:
            profile = self.plan_context_service.store.get_device_profile(int(radio_id))
        except Exception:
            profile = None
        name = str((profile or {}).get("name") or "").strip()
        return name or f"Radio {int(radio_id)}"

    def _radio_guard_context_label(self, radio_id: int) -> str:
        try:
            profile = self.plan_context_service.store.get_device_profile(int(radio_id))
        except Exception:
            profile = None
        if not isinstance(profile, Mapping):
            return "Radio assignment"
        role_raw = str(profile.get("device_class") or "").strip().lower()
        role = "RX-only" if role_raw in {"observer", "rx_only", "receive_only", "receiver"} else "TX/RX"
        supported_bands = self._json_text_list(profile.get("antenna_supported_bands_json") or profile.get("antenna_supported_bands"))
        band_text = ", ".join(supported_bands[:4]) if supported_bands else "bands not limited"
        overlap_group = str(profile.get("band_overlap_guard_group") or "").strip()
        overlap_mode = str(profile.get("band_overlap_guard_mode") or "").strip().title()
        if overlap_group:
            return f"{role}; {band_text}; overlap {overlap_group.title()} {overlap_mode or 'Review'}"
        return f"{role}; {band_text}"

    def _schedule_ref_display_label(self, ref: Mapping[str, Any]) -> str:
        for key in ("action_label", "net_name", "profile_name", "group_name", "band", "frequency", "source"):
            value = str(ref.get(key) or "").strip()
            if value:
                return value
        return "Plan"

    @staticmethod
    def _json_text_list(value: Any) -> List[str]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                parsed = [part.strip() for part in value.split(",")]
        else:
            parsed = value
        if not isinstance(parsed, list):
            return []
        return [str(item or "").strip().upper() for item in parsed if str(item or "").strip()]

    def _effective_window_label(self, segment: ScheduleSegment) -> str:
        if segment.source == "NET":
            return segment.net_name or segment.group_name or "Net"
        if segment.source == "SOP":
            return segment.profile_name or segment.group_name or "SOP"
        return segment.group_name or segment.band or segment.frequency or "Daily"

    def _effective_window_notes(self, segment: ScheduleSegment, source_cell: EffectiveWindowCell) -> str:
        parts: List[str] = []
        if segment.source == "HF" and not source_cell.net_segments and not source_cell.sop_segments:
            parts.append("Daily baseline")
        if segment.source == "NET":
            parts.append("Net overrides daily window")
        if segment.source == "SOP":
            cond = str(segment.raw.get("condition_levels") or "").strip()
            if cond and cond.upper() != "ALL":
                parts.append(f"Condition {cond}")
            else:
                parts.append("SOP action layer")
        if segment.raw.get("implicit_continuation") or segment.raw.get("_resolution_implicit_continuation"):
            parts.append("Schedule gap: keep current tuning until next scheduled change")
        if source_cell.net_segments and segment.source != "NET":
            parts.append(f"{len(source_cell.net_segments)} net layer(s)")
        if source_cell.sop_segments and segment.source != "SOP":
            parts.append(f"{len(source_cell.sop_segments)} SOP layer(s)")
        return "; ".join(parts)

    def _effective_window_cell_for_segment(self, segment: ScheduleSegment, projection: BlendedScheduleProjection) -> EffectiveWindowCell:
        hf = tuple(
            candidate
            for candidate in projection.source_segments
            if candidate.source == "HF"
            and candidate.day_utc == segment.day_utc
            and candidate.start_minute < segment.end_minute
            and candidate.end_minute > segment.start_minute
        )
        net = tuple(
            candidate
            for candidate in projection.source_segments
            if candidate.source == "NET" and candidate.day_utc == segment.day_utc and candidate.start_minute < segment.end_minute and candidate.end_minute > segment.start_minute
        )
        sop = tuple(
            candidate
            for candidate in projection.source_segments
            if candidate.source == "SOP" and candidate.day_utc == segment.day_utc and candidate.start_minute < segment.end_minute and candidate.end_minute > segment.start_minute
        )
        return EffectiveWindowCell(
            segment=segment,
            hf_segments=hf or ((segment,) if segment.source == "HF" else ()),
            net_segments=net or ((segment,) if segment.source == "NET" else ()),
            sop_segments=sop or ((segment,) if segment.source == "SOP" else ()),
        )

    def _compact_pattern_days(self, days: Set[str]) -> str:
        ordered = [day for day in DAY_NAMES if day in days]
        extras = sorted(day for day in days if day not in DAY_NAMES)
        if len(ordered) == len(DAY_NAMES) and not extras:
            return "Daily"
        return ", ".join([day[:3] for day in ordered] + extras)

    def _operational_cell_tooltip(self, cell: Optional[OperationalCell]) -> str:
        if cell is None:
            return ""
        lines = [
            f"{cell.day_utc} {cell.start_utc.strftime('%H:%M')}-{cell.end_utc.strftime('%H:%M')} UTC",
            f"Entries: {len(cell.entries)}",
        ]
        for entry in cell.entries[:5]:
            band_freq = f"{entry.band} {entry.frequency}".strip()
            label = entry.action_label or entry.net_name or entry.group_name or entry.profile_name or entry.source
            lines.append(f"- {entry.source} {entry.start_utc}-{entry.end_utc} {label} {band_freq}".strip())
        if len(cell.entries) > 5:
            lines.append(f"- +{len(cell.entries) - 5} more")
        return "\n".join(lines)

    def _set_operational_inspector(self, cell: Optional[OperationalCell]) -> None:
        if self._inline_draft_dirty():
            self.inline_update_plan_btn.setEnabled(False)
            self.inline_update_hf_daily_btn.setEnabled(False)
            self.frequency_plan_action_hint_label.setText("Entered window edits retained. Select a current row to review or discard them before updating.")
            return
        self._selected_projection_cell = None
        self._selected_operational_cell = cell
        self._inline_edit_segment = None
        self._update_inspector_action_buttons(None)
        self._populate_inline_editor(None)
        if hasattr(self, "edit_sop_plan_entry_btn"):
            can_edit = bool(cell is not None and cell.entries and self._selected_sop_schedule_plan_row())
            self.edit_sop_plan_entry_btn.setEnabled(can_edit)
            self.edit_sop_plan_entry_btn.setToolTip(
                "Edit the selected saved SOP Schedule Plan entry only."
                if can_edit
                else "Select a saved SOP Schedule Plan entry in SOP Lanes view to edit it locally."
            )
        if not hasattr(self, "cell_inspector_label"):
            return
        if cell is None:
            self.cell_inspector_label.setText("Select an SOP lane cell to review where to be, when to be there, and what to do.")
            self._set_selected_window_card(
                "Select an SOP lane",
                "Click an SOP lane cell to review the active group, action, frequency, and condition layer.",
            )
            return
        primary = cell.entries[0] if cell.entries else None
        if primary is not None:
            primary_label = primary.action_label or primary.net_name or primary.group_name or primary.profile_name or primary.source
            band_freq = f"{primary.band} {primary.frequency}".strip()
            title = f"{primary_label} | {band_freq}".strip(" |")
        else:
            title = f"{cell.lane_key} | No scheduled entry"
        self._set_selected_window_card(
            title,
            " | ".join(
                bit
                for bit in (
                    f"{cell.day_utc} {cell.start_utc.strftime('%H:%M')}-{cell.end_utc.strftime('%H:%M')} UTC",
                    f"Lane: {cell.lane_key}",
                    "Contention: yes" if cell.has_contention else "",
                    f"{len(cell.entries)} entr{'y' if len(cell.entries) == 1 else 'ies'}",
                )
                if bit
            ),
        )
        lines = [
            f"{cell.day_utc} {cell.start_utc.strftime('%H:%M')}-{cell.end_utc.strftime('%H:%M')} UTC",
            f"Lane: {cell.lane_key}",
            "Contention: yes" if cell.has_contention else "Contention: no",
        ]
        if not cell.entries:
            lines.append("No scheduled operational entry.")
        for entry in cell.entries[:6]:
            band_freq = f"{entry.band} {entry.frequency}".strip()
            label = entry.action_label or entry.net_name or entry.group_name or entry.profile_name or entry.source
            lines.append(
                f"{entry.source}: {entry.start_utc}-{entry.end_utc} {label} {band_freq}".strip()
            )
        if len(cell.entries) > 6:
            lines.append(f"+{len(cell.entries) - 6} more")
        self.cell_inspector_label.setText("\n".join(lines))

    def _choose_operational_entry_for_edit(self) -> Optional[Any]:
        cell = self._selected_operational_cell
        if cell is None or not cell.entries:
            return None
        if len(cell.entries) == 1:
            return cell.entries[0]
        labels = []
        for idx, entry in enumerate(cell.entries):
            labels.append(
                f"{idx + 1}. {entry.source} {entry.start_utc}-{entry.end_utc} "
                f"{entry.action_label or entry.net_name or entry.group_name or entry.profile_name or entry.source}"
            )
        selected, ok = QInputDialog.getItem(
            self,
            "Choose SOP Plan Entry",
            "Multiple entries are active in this cell. Choose the plan-local entry to edit:",
            labels,
            0,
            False,
        )
        if not ok:
            return None
        try:
            return cell.entries[labels.index(str(selected))]
        except ValueError:
            return None

    def _edit_plan_entry_dialog(self, entry: Any) -> Optional[Dict[str, Any]]:
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit SOP Schedule Plan Entry")
        layout = QVBoxLayout(dialog)
        note = QLabel("These changes update the selected SOP Schedule Plan. Resource-backed entries can optionally update the master Net Resource after the plan save passes RF Guard.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        day_combo = QComboBox()
        for day in DAY_NAMES:
            day_combo.addItem(day, day)
        day_idx = day_combo.findData(str(entry.day_utc or ""))
        if day_idx >= 0:
            day_combo.setCurrentIndex(day_idx)
        start_edit = QLineEdit(str(entry.start_utc or ""))
        end_edit = QLineEdit(str(entry.end_utc or ""))
        band_edit = QLineEdit(str(entry.band or ""))
        frequency_edit = QLineEdit(str(entry.frequency or ""))
        mode_edit = QLineEdit(str(entry.mode or ""))
        group_edit = QLineEdit(str(entry.group_name or ""))
        net_edit = QLineEdit(str(entry.net_name or ""))
        action_edit = QLineEdit(str(entry.action_label or ""))
        lane_label_edit = QLineEdit(str(entry.lane_label or ""))
        for label, widget in (
            ("Day", day_combo),
            ("Start UTC", start_edit),
            ("End UTC", end_edit),
            ("Band", band_edit),
            ("Frequency", frequency_edit),
            ("Mode", mode_edit),
            ("Group", group_edit),
            ("Net name", net_edit),
            ("Action", action_edit),
            ("Lane label", lane_label_edit),
        ):
            form.addRow(label, widget)
        draft = self._sop_edit_draft
        current_plan = self._selected_sop_schedule_plan_row()
        current_refs = self._schedule_refs_from_plan_row(current_plan) if current_plan is not None else ()
        current_index = int((getattr(entry, "raw", None) or {}).get("plan_ref_index", -1))
        same_draft_row = (draft is not None and int((current_plan or {}).get("id") or 0) == draft["plan_id"]
                          and 0 <= current_index < len(current_refs) and dict(current_refs[current_index]) == draft["original_ref"])
        if same_draft_row:
            retained = draft["updates"]
            index = day_combo.findData(retained.get("day_utc"))
            if index >= 0:
                day_combo.setCurrentIndex(index)
            for key, field in (("start_utc", start_edit), ("end_utc", end_edit), ("band", band_edit),
                               ("frequency", frequency_edit), ("mode", mode_edit), ("group_name", group_edit),
                               ("net_name", net_edit), ("action_label", action_edit), ("lane_label", lane_label_edit)):
                field.setText(str(retained.get(key, field.text())))
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        validation_label = QLabel()
        validation_label.setWordWrap(True)
        validation_label.setStyleSheet(label_style("warning", resolve_theme(self.settings)))
        layout.addWidget(validation_label)
        def accept_valid_fields():
            if self._parse_guard_hhmm(start_edit.text()) is None or self._parse_guard_hhmm(end_edit.text()) is None:
                validation_label.setText("Enter valid UTC times as HH:MM. Your fields remain available here.")
                return
            if not band_edit.text().strip() and not frequency_edit.text().strip():
                validation_label.setText("Enter at least a band or frequency. Your fields remain available here.")
                return
            if not self._group_is_configured(group_edit.text()):
                validation_label.setText("Choose a configured Operating Group. " + self._configured_group_help_text())
                return
            dialog.accept()
        buttons.accepted.connect(accept_valid_fields)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        updates = {
            "day_utc": str(day_combo.currentData() or day_combo.currentText() or "").strip(),
            "start_utc": start_edit.text().strip(),
            "end_utc": end_edit.text().strip(),
            "band": band_edit.text().strip().upper(),
            "frequency": frequency_edit.text().strip(),
            "mode": mode_edit.text().strip(),
            "group_name": group_edit.text().strip().upper(),
            "net_name": net_edit.text().strip(),
            "action_label": action_edit.text().strip(),
            "lane_label": lane_label_edit.text().strip(),
        }
        if self._parse_guard_hhmm(updates["start_utc"]) is None or self._parse_guard_hhmm(updates["end_utc"]) is None:
            self.frequency_plan_action_hint_label.setText("Enter valid UTC times as HH:MM before saving the plan-local edit.")
            return None
        if not updates["band"] and not updates["frequency"]:
            self.frequency_plan_action_hint_label.setText("Enter at least a band or frequency before saving the plan-local edit.")
            return None
        if not self._group_is_configured(updates["group_name"]):
            self.frequency_plan_action_hint_label.setText(
                f"Choose a configured Operating Group before saving the plan-local edit. {self._configured_group_help_text()}"
            )
            return None
        return updates

    def _updated_sop_plan_payload_for_entry(self, plan: Dict[str, Any], entry: Any, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._group_is_configured(updates.get("group_name")):
            self.frequency_plan_action_hint_label.setText(
                f"Choose a configured Operating Group before saving the plan-local edit. {self._configured_group_help_text()}"
            )
            return None
        refs = [dict(row) for row in self._schedule_refs_from_plan_row(plan)]
        try:
            index = int((entry.raw or {}).get("plan_ref_index"))
        except Exception:
            index = -1
        if index < 0 or index >= len(refs):
            return None
        updated_ref = dict(refs[index])
        updated_ref.update(updates)
        updated_ref.setdefault("source", entry.source)
        if entry.radio_id and not updated_ref.get("radio_id"):
            updated_ref["radio_id"] = entry.radio_id
        self._normalize_plan_local_lane_identity(updated_ref)
        refs[index] = updated_ref
        projection = build_operational_day_projection_from_refs(refs)
        payload = dict(plan)
        payload.update(
            {
                "id": int(plan.get("id") or 0),
                "name": str(plan.get("name") or "SOP Schedule Plan"),
                "category": "sop_schedule",
                "source_refs": projection.source_refs(),
                "schedule_refs": refs,
                "frequency_refs": projection.frequency_refs(),
                "group_refs": projection.group_refs(),
                "notes": str(plan.get("notes") or ""),
            }
        )
        return payload

    def _resource_id_for_operational_entry(self, entry: Any) -> int:
        raw = getattr(entry, "raw", {}) or {}
        return self._coerce_positive_int(raw.get("resource_id") or raw.get("_resource_id"))

    def _resource_update_payload_from_plan_ref(self, ref: Mapping[str, Any], updates: Mapping[str, Any]) -> Dict[str, Any]:
        row = dict(ref)
        row.update(dict(updates))
        group = str(row.get("group_name") or "").strip().upper()
        return {
            "day_utc": str(row.get("day_utc") or "ALL").strip() or "ALL",
            "recurrence": str(row.get("recurrence") or "Weekly").strip() or "Weekly",
            "biweekly_offset_weeks": self._coerce_positive_int(row.get("biweekly_offset_weeks")),
            "month_weeks": str(row.get("month_weeks") or "").strip(),
            "group_name": group,
            "band": str(row.get("band") or "").strip().upper(),
            "mode": str(row.get("mode") or "").strip(),
            "frequency": str(row.get("frequency") or row.get("freq") or "").strip(),
            "start_utc": str(row.get("start_utc") or "").strip(),
            "end_utc": str(row.get("end_utc") or "").strip(),
            "early_checkin": self._coerce_positive_int(row.get("early_checkin")),
            "primary_js8call_group": str(row.get("primary_js8call_group") or group).strip().upper(),
            "comment": str(row.get("comment") or "").strip(),
            "net_name": str(row.get("net_name") or "").strip(),
            "fldigi_mode": str(row.get("fldigi_mode") or "").strip(),
            "fldigi_offset": str(row.get("fldigi_offset") or "").strip(),
        }

    def _update_master_net_resource_from_plan_ref(
        self,
        resource_id: int,
        ref: Mapping[str, Any],
        updates: Mapping[str, Any],
    ) -> bool:
        rid = int(resource_id or 0)
        if rid <= 0:
            return False
        db_path = get_config_dir() / "config" / "freqinout_nets.db"
        if not db_path.exists():
            raise FileNotFoundError(f"Net Resources database not found: {db_path}")
        payload = self._resource_update_payload_from_plan_ref(ref, updates)
        with sqlite3.connect(db_path) as conn:
            changed = update_legacy_resource_fields(
                conn,
                rid,
                {
                    "source_type": "manual",
                    "source_ref": "updated_from_sop_schedule_plan",
                    **payload,
                },
            )
            if not changed:
                raise KeyError(f"Unknown Net Resource id: {rid}")
            conn.commit()
        return True

    def _preflight_master_net_resource_update(self, resource_id):
        if self._catalog_model is None or not any(int(row.get("id") or 0) == int(resource_id)
                                                  for row in self._catalog_model.resources):
            raise ScheduleSourceError(f"Net Resource #{resource_id} is unavailable")


    def _prompt_master_resource_update(self, resource_id: int) -> Optional[bool]:
        if int(resource_id or 0) <= 0:
            return False
        response = QMessageBox.question(
            self,
            "Update Master Net Resource?",
            f"This entry is linked to Net Resource #{int(resource_id)}.\n\n"
            "Save the SOP Schedule Plan edit only, or also update the master Net Resource so future schedules use the change?\n\n"
            "Choose Yes to update both. Choose No to update this plan only.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.No,
        )
        if response == QMessageBox.Cancel:
            return None
        return response == QMessageBox.Yes

    def _normalize_plan_local_lane_identity(self, ref: Dict[str, Any]) -> None:
        radio_id = self._coerce_positive_int(
            ref.get("radio_id") or ref.get("device_profile_id") or ref.get("target_device_profile_id")
        )
        if radio_id > 0:
            ref["lane_key"] = f"radio:{radio_id}"
            ref.setdefault("lane_label", f"Radio {radio_id}")
            return
        source = str(ref.get("source") or "").strip().upper()
        if source == "SOP":
            sop_id = self._coerce_positive_int(ref.get("sop_profile_id") or ref.get("profile_id"))
            if sop_id > 0:
                ref["lane_key"] = f"sop:{sop_id}"
                ref.setdefault("lane_label", str(ref.get("profile_name") or f"SOP {sop_id}"))
                return
        group = str(ref.get("group_name") or "").strip().upper()
        if group:
            ref["lane_key"] = f"group:{group}"
            ref["lane_label"] = group
            return
        ref["lane_key"] = "station"
        if not str(ref.get("lane_label") or "").strip():
            ref["lane_label"] = "Station"

    def _sop_workspace_draft_identity(self):
        return (int(self.frequency_plan_combo.currentData() or 0), self.frequency_plan_combo.currentText(),
                self.hf_daily_source_combo.currentData(), self.hf_net_source_combo.currentData(),
                self.sop_plan_source_combo.currentData(), bool(self._frequency_plan_layers_dirty),
                self._inline_draft_state() if self._inline_draft_dirty() else None)

    def _resume_sop_edit_draft(self):
        draft = self._sop_edit_draft
        if draft is None:
            return
        if self._catalog_pending or self._input_read_error or self._catalog_review_required:
            self.frequency_plan_action_hint_label.setText("Review current inputs before reopening the retained SOP edit. Its entered fields are retained.")
            return
        current_plan = self._selected_frequency_plan_row()
        current_text = self.frequency_plan_combo.currentText().strip()
        current_name = str((current_plan or {}).get("name") or "").strip()
        other_name = bool(current_text and current_text not in {current_name, f"{current_name} (draft)"})
        other_plan = int((current_plan or {}).get("id") or 0) != draft["plan_id"]
        workspace_changed = draft.get("workspace_state") != self._sop_workspace_draft_identity()
        if workspace_changed and (self._frequency_plan_layers_dirty or self._inline_draft_dirty() or other_name
                or (other_plan and self._creating_new_frequency_plan and bool(current_text))):
            self.frequency_plan_action_hint_label.setText(
                "A separate plan or source draft is open. Save it or explicitly discard it before returning to the retained SOP edit. Both drafts are retained.")
            return
        plan = self._catalog_store.get_frequency_plan(draft["plan_id"])
        refs = self._schedule_refs_from_plan_row(plan) if plan is not None else ()
        matches = [index for index, row in enumerate(refs) if dict(row) == draft["original_ref"]]
        if len(matches) != 1:
            fields = "\n".join(f"{key}: {value}" for key, value in draft["updates"].items())
            response = QMessageBox.question(
                self, "Retained SOP Edit",
                "The original plan row is unavailable or changed. The edit has not been applied or retargeted.\n\n"
                + fields + "\n\nDiscard these retained fields? Choose Cancel to keep them for review.",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if response == QMessageBox.Discard:
                self._sop_edit_draft = None
                self.review_sop_draft_btn.hide()
            return
        if other_plan:
            self.focus_frequency_plan(draft["plan_id"], device_profile_id=0)
        entry = draft["entry"]
        self._sop_entry_override = replace(entry, raw={**(entry.raw or {}), "plan_ref_index": matches[0]})
        self._on_edit_sop_plan_entry_clicked()

    def _on_edit_sop_plan_entry_clicked(self) -> None:
        edit_identity = self._draft_identity()
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        plan = self._selected_sop_schedule_plan_row()
        if not plan:
            self.frequency_plan_action_hint_label.setText("Select a saved SOP Schedule Plan before editing plan-local entries.")
            return
        entry, self._sop_entry_override = self._sop_entry_override, None
        if entry is None:
            entry = self._choose_operational_entry_for_edit()
        if entry is None:
            self.frequency_plan_action_hint_label.setText("Select an SOP Lanes cell with a plan entry to edit.")
            return
        draft = self._sop_edit_draft
        refs_before = self._schedule_refs_from_plan_row(plan)
        index_before = int((entry.raw or {}).get("plan_ref_index", -1))
        if draft is not None and (int(plan["id"]) != draft["plan_id"] or not 0 <= index_before < len(refs_before)
                                  or dict(refs_before[index_before]) != draft["original_ref"]):
            response = QMessageBox.question(self, "Retained SOP Edit",
                "A different SOP row still has retained edits. Discard those fields and edit this row?",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if response != QMessageBox.Discard:
                return
            self._sop_edit_draft = None
            self.review_sop_draft_btn.hide()
        updates = self._edit_plan_entry_dialog(entry)
        if updates is None:
            return
        original_ref = dict(refs_before[index_before]) if 0 <= index_before < len(refs_before) else None
        self._sop_edit_draft = {"plan_id": int(plan["id"]), "plan_name": str(plan.get("name") or ""),
                                "original_ref": original_ref, "updates": dict(updates), "entry": entry,
                                "workspace_state": self._sop_workspace_draft_identity()}
        self.review_sop_draft_btn.show()
        payload = self._updated_sop_plan_payload_for_entry(plan, entry, updates)
        if payload is None:
            self.frequency_plan_action_hint_label.setText("Unable to validate the selected SOP plan row. Entered fields are retained for review.")
            return
        resource_id = self._resource_id_for_operational_entry(entry)
        update_master_resource = self._prompt_master_resource_update(resource_id) if resource_id > 0 else False
        if update_master_resource is None:
            return
        if update_master_resource:
            try:
                self._preflight_master_net_resource_update(resource_id)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Master Net Resource Unavailable",
                    f"Net Resource #{resource_id} cannot be updated, so this combined save was not started.\n\n{exc}",
                )
                self.frequency_plan_action_hint_label.setText(
                    f"Could not update Net Resource #{resource_id}; no SOP Schedule Plan edit was saved."
                )
                return
        if update_master_resource:
            confirm_text = (
                f"Save this change to the selected SOP Schedule Plan and update Net Resource #{resource_id}?\n\n"
                "This will change the saved plan after RF Guard preflight and then update the master Net Resource record."
            )
        else:
            confirm_text = (
                "Save this change to the selected SOP Schedule Plan only?\n\n"
                "HF Daily, HF Nets, SOP Builder, and master Net Resources will not be changed."
            )
        response = QMessageBox.question(
            self,
            "Save Plan-Local Edit",
            confirm_text,
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if response != QMessageBox.Save:
            return
        if edit_identity != self._draft_identity():
            self.frequency_plan_action_hint_label.setText("Inputs or the draft changed while the editor was open. Review them before saving.")
            return
        def after_save(saved):
            self._sop_edit_draft = None
            self.review_sop_draft_btn.hide()
            followup_message = ""
            if update_master_resource:
                try:
                    index = int((entry.raw or {}).get("plan_ref_index"))
                    updated_ref = (payload.get("schedule_refs") or [])[index]
                    self._update_master_net_resource_from_plan_ref(resource_id, updated_ref, updates)
                    followup_message = (
                        f"Saved SOP Schedule Plan '{str(saved.get('name') or plan.get('name') or 'Plan')}' "
                        f"and updated Net Resource #{resource_id}."
                    )
                except Exception as exc:
                    log.exception("FreqPlanner: failed updating master Net Resource from SOP Schedule Plan edit.")
                    QMessageBox.warning(
                        self,
                        "Master Net Resource Update Failed",
                        f"The SOP Schedule Plan was saved, but Net Resource #{resource_id} could not be updated.\n\n{exc}",
                    )
                    followup_message = (
                        f"Saved the SOP Schedule Plan, but could not update Net Resource #{resource_id}: {exc}"
                    )
            self.rebuild_table()
            if followup_message:
                self._set_action_feedback(followup_message)

        self._save_plan_payload_with_guard(
            payload,
            schedule_count=len(payload.get("schedule_refs") or []),
            success_kind="SOP Schedule Plan", after_save=after_save,
        )

    def _format_segment_for_inspector(self, segment: ScheduleSegment) -> str:
        label = segment.net_name or segment.group_name or segment.profile_name or segment.band or segment.source
        band_freq = f"{segment.band} {segment.frequency}".strip()
        return f"{segment.source} {segment.start_utc}-{segment.end_utc} {label} {band_freq}".strip()

    @staticmethod
    def _segments_for_source(cell: Optional[object], source: str) -> Tuple[ScheduleSegment, ...]:
        if not isinstance(cell, (ProjectionCell, EffectiveWindowCell)):
            return ()
        key = str(source or "").strip().upper()
        if key == "HF":
            return cell.hf_segments
        if key == "NET":
            return cell.net_segments
        if key == "SOP":
            return cell.sop_segments
        return ()

    @staticmethod
    def _source_button_state(
        segments: Tuple[ScheduleSegment, ...],
        open_text: str,
        none_text: str,
        multiple_text: str,
    ) -> Tuple[bool, str]:
        if len(segments) == 1:
            return True, open_text
        if len(segments) > 1:
            return True, multiple_text
        return False, none_text

    def _update_inspector_action_buttons(self, cell: Optional[object]) -> None:
        hf_segments = self._segments_for_source(cell, "HF")
        net_segments = self._segments_for_source(cell, "NET")
        sop_segments = self._segments_for_source(cell, "SOP")
        missing_text = self._source_dependency_block_text()
        if hasattr(self, "edit_hf_daily_btn"):
            enabled, tooltip = self._source_button_state(
                hf_segments,
                "Open HF Daily and focus the underlying source row.",
                "No HF Daily source row for this cell.",
                "Multiple HF Daily source rows match this cell; open HF Daily and choose the row to edit.",
            )
            self.edit_hf_daily_btn.setEnabled(enabled and not missing_text)
            self.edit_hf_daily_btn.setToolTip(tooltip)
            self.edit_hf_daily_btn.setStyleSheet(button_style("primary" if enabled else "muted", resolve_theme(self.settings)))
        if hasattr(self, "edit_hf_net_btn"):
            enabled, tooltip = self._source_button_state(
                net_segments,
                "Open HF Nets and focus the underlying source row.",
                "No HF Net source row for this cell.",
                "Multiple HF Net source rows match this cell; open HF Nets and choose the row to edit.",
            )
            self.edit_hf_net_btn.setEnabled(enabled and not missing_text)
            self.edit_hf_net_btn.setToolTip(tooltip)
            self.edit_hf_net_btn.setStyleSheet(button_style("primary" if enabled else "muted", resolve_theme(self.settings)))
        if hasattr(self, "open_sop_builder_btn"):
            enabled, tooltip = self._source_button_state(
                sop_segments,
                "Open SOP Builder and select the underlying SOP profile.",
                "No SOP source row for this cell.",
                "Multiple SOP source rows match this cell; open SOP Builder and choose the source to edit.",
            )
            self.open_sop_builder_btn.setEnabled(enabled and not missing_text)
            self.open_sop_builder_btn.setToolTip(tooltip)
            self.open_sop_builder_btn.setStyleSheet(button_style("primary" if enabled else "muted", resolve_theme(self.settings)))

    def _set_projection_inspector(self, cell: Optional[object]) -> None:
        if self._inline_draft_dirty():
            self.inline_update_plan_btn.setEnabled(False)
            self.inline_update_hf_daily_btn.setEnabled(False)
            self.frequency_plan_action_hint_label.setText("Entered window edits retained. Select a current row to review or discard them before updating.")
            return
        self._selected_projection_cell = cell
        self._selected_operational_cell = None
        self._inline_edit_segment = cell.segment if isinstance(cell, EffectiveWindowCell) else None
        self._update_inspector_action_buttons(cell)
        self._populate_inline_editor(self._inline_edit_segment)
        if hasattr(self, "edit_sop_plan_entry_btn"):
            self.edit_sop_plan_entry_btn.setEnabled(False)
            self.edit_sop_plan_entry_btn.setToolTip("Use SOP Lanes to edit a saved SOP Schedule Plan entry.")
        if not hasattr(self, "cell_inspector_label"):
            return
        if cell is None:
            self.cell_inspector_label.setText("Select a schedule cell to review the blended HF Daily, HF Nets, and SOP sources.")
            self._set_selected_window_card(
                "Select a window",
                "Click a row to review its Daily baseline, Net override, and SOP source. Use the edit buttons to open the exact source row.",
            )
            return
        if isinstance(cell, EffectiveWindowCell):
            segment = cell.segment
            title = f"{self._effective_window_label(segment)} | {segment.band or '--'}"
            if segment.frequency:
                title = f"{title} {segment.frequency}"
            detail_bits = [
                self._effective_window_time_label(segment),
                f"Layer: {self._effective_window_source_label(segment.source)}",
                f"Mode: {segment.mode or '--'}",
                self._effective_window_notes(segment, cell) or "Effective operating window",
            ]
            self._set_selected_window_card(title, " | ".join(bit for bit in detail_bits if bit))
            lines = [
                self._effective_window_time_label(segment),
                f"Effective: {self._effective_window_source_label(segment.source)} {self._effective_window_label(segment)}".strip(),
                f"Band/Frequency: {segment.band or '--'} {segment.frequency or ''}".strip(),
                f"What it means: {self._effective_window_notes(segment, cell) or 'Effective operating window'}",
            ]
            for title, segments in (
                ("HF Daily", cell.hf_segments),
                ("HF Nets", cell.net_segments),
                ("SOP", cell.sop_segments),
            ):
                if not segments:
                    lines.append(f"{title}: none")
                    continue
                detail = "; ".join(self._format_segment_for_inspector(item) for item in segments[:3])
                if len(segments) > 3:
                    detail += f"; +{len(segments) - 3} more"
                lines.append(f"{title}: {detail}")
            self.cell_inspector_label.setText("\n".join(lines))
            return
        lines = [
            f"{cell.day_utc} {cell.start_utc.strftime('%H:%M')}-{cell.end_utc.strftime('%H:%M')} UTC",
            f"Effective: {cell.effective_source or 'None'} {cell.display_label}".strip(),
        ]
        self._set_selected_window_card(
            f"{cell.display_label or 'Selected window'}",
            " | ".join(
                bit
                for bit in (
                    f"{cell.day_utc} {cell.start_utc.strftime('%H:%M')}-{cell.end_utc.strftime('%H:%M')} UTC",
                    f"Layer: {cell.effective_source or 'None'}",
                )
                if bit
            ),
        )
        for title, segments in (
            ("HF Daily", cell.hf_segments),
            ("HF Nets", cell.net_segments),
            ("SOP", cell.sop_segments),
        ):
            if not segments:
                lines.append(f"{title}: none")
                continue
            detail = "; ".join(self._format_segment_for_inspector(segment) for segment in segments[:3])
            if len(segments) > 3:
                detail += f"; +{len(segments) - 3} more"
            lines.append(f"{title}: {detail}")
        self.cell_inspector_label.setText("\n".join(lines))

    def _set_selected_window_card(self, title: str, detail: str) -> None:
        if hasattr(self, "selected_window_title_label"):
            self.selected_window_title_label.setText(str(title or "Select a window"))
        if hasattr(self, "selected_window_detail_label"):
            self.selected_window_detail_label.setText(str(detail or "Click a row to review or edit its source."))
        self._queue_geometry_settle()

    def _inline_draft_dirty(self) -> bool:
        return any(getattr(self, name, None) is not None and getattr(self, name).isModified()
                   for name in ("inline_group_edit", "inline_band_edit", "inline_frequency_edit",
                                "inline_start_edit", "inline_end_edit", "inline_mode_edit"))

    def _populate_inline_editor(self, segment: Optional[ScheduleSegment]) -> None:
        if not hasattr(self, "inline_editor_card"):
            return
        if any(getattr(self, name).isModified() for name in ("inline_group_edit", "inline_band_edit", "inline_frequency_edit", "inline_start_edit", "inline_end_edit", "inline_mode_edit")):
            self.inline_update_plan_btn.setEnabled(False)
            self.inline_update_hf_daily_btn.setEnabled(False)
            return
        widgets = (
            getattr(self, "inline_group_edit", None),
            getattr(self, "inline_band_edit", None),
            getattr(self, "inline_frequency_edit", None),
            getattr(self, "inline_start_edit", None),
            getattr(self, "inline_end_edit", None),
            getattr(self, "inline_mode_edit", None),
        )
        if segment is None:
            values = ["", "", "", "", "", ""]
        else:
            values = [
                segment.group_name,
                segment.band,
                segment.frequency,
                segment.start_utc,
                segment.end_utc,
                segment.mode,
            ]
        missing_text = self._source_dependency_block_text()
        for widget, value in zip(widgets, values):
            if widget is not None:
                widget.setText(str(value or ""))
                widget.setEnabled(segment is not None and not missing_text)
        self.inline_editor_card.setVisible(segment is not None)
        plan_ready = bool(segment is not None and self._selected_frequency_plan_row())
        source_backed = self._source_backed_normal_plan(self._selected_frequency_plan_row())
        source_target = self._single_source_update_target()
        source_ready = source_target is not None
        if hasattr(self, "inline_update_plan_btn"):
            self.inline_update_plan_btn.setEnabled(plan_ready and not source_backed and not missing_text and not getattr(self, "_input_read_error", ""))
            self.inline_update_plan_btn.setToolTip(
                "Update Plan Only is unavailable for a source-backed Frequency Plan; edit its named source instead."
                if source_backed
                else missing_text
                if missing_text
                else "Update this saved Frequency Plan only. Source schedules are not changed."
                if plan_ready
                else "Select a saved Frequency Plan and Effective Windows row before updating the plan."
            )
        if hasattr(self, "inline_update_hf_daily_btn"):
            source_label = source_target["source_label"] if source_target else "source"
            self.inline_update_hf_daily_btn.setText(f"Update {source_label} Source" if source_target else "Update Source")
            self.inline_update_hf_daily_btn.setEnabled(source_ready and not missing_text and not getattr(self, "_input_read_error", ""))
            self.inline_update_hf_daily_btn.setToolTip(
                f"Update the saved {source_label} source row used by this window after RF Guard impact review."
                if source_ready and not missing_text
                else missing_text
                if missing_text
                else "Available when the selected window maps to exactly one saved HF Daily or HF Net source row."
            )
        if hasattr(self, "inline_editor_scope_label"):
            if segment is None:
                text = "Select an Effective Windows row to edit."
            elif source_target:
                text = (
                    f"Update the linked {source_target['source_label']} source row."
                    if source_backed
                    else f"Edit this plan window, or update the linked {source_target['source_label']} source row."
                )
            elif source_backed:
                text = "This plan is source-backed; edit its named source to change this window."
            else:
                text = "Edit this saved plan window. Open the source tab for master schedule changes."
            self.inline_editor_scope_label.setText(text)
        if hasattr(self, "inline_editor_impact_label"):
            if segment is None:
                text = "Select a window to see what will change."
            elif source_target:
                if self._selected_frequency_plan_row():
                    text = (
                        "This plan is source-backed; Update Plan Only is unavailable. Edit the named source instead."
                        if source_backed else
                        f"Plan Only changes this saved Frequency Plan. Update {source_target['source_label']} Source "
                        "changes the named schedule used by this and any assigned plans; RF Guard reviews the impact before saving."
                    )
                else:
                    text = (
                        f"Update {source_target['source_label']} Source changes the named schedule used by assigned plans; "
                        "RF Guard reviews the impact before saving. Select or save a Frequency Plan to make plan-local edits."
                    )
            elif source_backed:
                text = "This plan is source-backed; Update Plan Only is unavailable. Edit the named source instead."
            elif not self._selected_frequency_plan_row():
                text = (
                    "Save or select a Frequency Plan before making plan-local edits. "
                    "Source updates are available only for named HF Daily or HF Net schedules."
                )
            else:
                text = (
                    "Plan Only changes this saved Frequency Plan. Source update is unavailable because this window "
                    "does not map to one saved HF Daily or HF Net source row."
                )
            self.inline_editor_impact_label.setText(text)
        self._refresh_font_layout()

    def _inline_editor_updates(self) -> Optional[Dict[str, Any]]:
        if self._parse_guard_hhmm(self.inline_start_edit.text()) is None or self._parse_guard_hhmm(self.inline_end_edit.text()) is None:
            self.frequency_plan_action_hint_label.setText("Enter valid UTC times as HH:MM before updating this window.")
            return None
        group = self.inline_group_edit.text().strip().upper()
        if group and not self._group_is_configured(group):
            self.frequency_plan_action_hint_label.setText(
                f"Choose a configured Operating Group before updating this window. {self._configured_group_help_text()}"
            )
            return None
        band = self.inline_band_edit.text().strip().upper()
        frequency = self.inline_frequency_edit.text().strip()
        if not band and not frequency:
            self.frequency_plan_action_hint_label.setText("Enter at least a band or frequency before updating this window.")
            return None
        return {
            "group_name": group,
            "band": band,
            "frequency": frequency,
            "start_utc": self.inline_start_edit.text().strip(),
            "end_utc": self.inline_end_edit.text().strip(),
            "mode": self.inline_mode_edit.text().strip(),
        }

    @staticmethod
    def _same_source_key(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        left_key = str(left.get("source_key") or "").strip()
        right_key = str(right.get("source_key") or "").strip()
        if left_key and right_key:
            return left_key == right_key
        left_row = str(left.get("source_row_id") or "").strip()
        right_row = str(right.get("source_row_id") or "").strip()
        return bool(left_row and right_row and left_row == right_row)

    def _updated_plan_payload_for_segment(self, plan: Mapping[str, Any], segment: ScheduleSegment, updates: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        refs = self._schedule_refs_from_plan_row(plan)
        target = segment.to_schedule_ref()
        match_index = -1
        for idx, ref in enumerate(refs):
            if self._same_source_key(ref, target):
                match_index = idx
                break
        if match_index < 0:
            for idx, ref in enumerate(refs):
                if (
                    str(ref.get("source") or "").strip().upper() == segment.source
                    and str(ref.get("day_utc") or "").strip().upper() == segment.day_utc.upper()
                    and str(ref.get("start_utc") or "").strip() == segment.start_utc
                    and str(ref.get("end_utc") or "").strip() == segment.end_utc
                    and str(ref.get("band") or "").strip().upper() == segment.band.upper()
                    and str(ref.get("frequency") or "").strip() == segment.frequency
                ):
                    match_index = idx
                    break
        if match_index < 0:
            return None
        refs[match_index].update({k: v for k, v in updates.items() if v not in (None, "")})
        payload = dict(plan)
        payload["id"] = int(plan.get("id") or 0)
        payload["name"] = str(plan.get("name") or self._current_frequency_plan_name() or "Frequency Plan")
        payload["schedule_refs"] = refs
        payload["frequency_refs"] = self._frequency_refs_for_schedule_refs(refs)
        payload["group_refs"] = list(
            dict.fromkeys(str(ref.get("group_name") or "").strip().upper() for ref in refs if str(ref.get("group_name") or "").strip())
        )
        return payload

    def _on_inline_update_plan_clicked(self) -> None:
        edit_identity = self._draft_identity()
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        plan = self._selected_frequency_plan_row()
        segment = self._inline_edit_segment
        if not plan or segment is None:
            self.frequency_plan_action_hint_label.setText("Select a saved Frequency Plan window before updating the plan.")
            return
        if self._source_backed_normal_plan(plan):
            self.frequency_plan_action_hint_label.setText(
                "Update Plan Only is unavailable for a source-backed Frequency Plan. "
                "Edit the named HF Daily, HF Net, or SOP source; plan-local updates are unavailable."
            )
            return
        missing_text = self._source_dependency_block_text()
        if missing_text:
            self.frequency_plan_action_hint_label.setText(missing_text)
            return
        updates = self._inline_editor_updates()
        if updates is None:
            return
        payload = self._updated_plan_payload_for_segment(plan, segment, updates)
        if payload is None:
            self.frequency_plan_action_hint_label.setText("Could not match this visible window to the saved plan row. Open the source schedule to edit it.")
            return
        response = QMessageBox.question(
            self,
            "Update Plan Only",
            "Update this Frequency Plan only?\n\nHF Daily, HF Nets, and SOP source schedules will not be changed.",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if response != QMessageBox.Save:
            return
        if edit_identity != self._draft_identity():
            self.frequency_plan_action_hint_label.setText("Inputs or the draft changed while the editor was open. Review them before saving.")
            return
        def after_save(saved):
            self.rebuild_table()
            self._set_action_feedback(
                f"Updated plan-only window in '{str(saved.get('name') or payload.get('name') or 'Frequency Plan')}'."
            )

        self._save_plan_payload_with_guard(
            payload,
            schedule_count=len(payload.get("schedule_refs") or []),
            success_kind="Frequency Plan", after_save=after_save,
        )

    def _single_hf_daily_source_segment(self) -> Optional[ScheduleSegment]:
        cell = self._selected_projection_cell
        if not isinstance(cell, EffectiveWindowCell):
            return None
        segments = tuple(seg for seg in cell.hf_segments if seg.source == "HF")
        if len(segments) != 1:
            return None
        selected_id = self._selected_source_set_id(SELECTED_HF_DAILY_SOURCE_SET_KEY)
        if not selected_id or selected_id == LIVE_SOURCE_SET_ID:
            return None
        return segments[0]

    def _single_hf_net_source_segment(self) -> Optional[ScheduleSegment]:
        cell = self._selected_projection_cell
        if not isinstance(cell, EffectiveWindowCell):
            return None
        if self._inline_edit_segment is not None and self._inline_edit_segment.source != "NET":
            return None
        segments = tuple(seg for seg in cell.net_segments if seg.source == "NET")
        if len(segments) != 1:
            return None
        selected_id = self._selected_source_set_id(SELECTED_HF_NET_SOURCE_SET_KEY)
        if not selected_id or selected_id == LIVE_SOURCE_SET_ID:
            return None
        return segments[0]

    def _single_source_update_target(self) -> Optional[Dict[str, Any]]:
        segment = self._inline_edit_segment
        if segment is None:
            return None
        if segment.source == "HF":
            source_segment = self._single_hf_daily_source_segment()
            if source_segment is None:
                return None
            return {
                "segment": source_segment,
                "sets_key": HF_DAILY_SOURCE_SETS_KEY,
                "selected_key": SELECTED_HF_DAILY_SOURCE_SET_KEY,
                "category": HF_DAILY_SOURCE_CATEGORY,
                "source_label": "HF Daily",
            }
        if segment.source == "NET":
            source_segment = self._single_hf_net_source_segment()
            if source_segment is None:
                return None
            return {
                "segment": source_segment,
                "sets_key": HF_NET_SOURCE_SETS_KEY,
                "selected_key": SELECTED_HF_NET_SOURCE_SET_KEY,
                "category": HF_NET_SOURCE_CATEGORY,
                "source_label": "HF Net",
            }
        return None

    def _updated_source_rows_for_segment(self, sets_key: str, category: str, set_id: str, segment: ScheduleSegment, updates: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
        row = self._source_set_row_by_id(sets_key, set_id)
        if not row:
            return None
        target = segment.to_schedule_ref()
        target_source_key = str(target.get("source_key") or "").strip()
        target_source_row = str(target.get("source_row_id") or "").strip()
        out: List[Dict[str, Any]] = []
        matched = False
        for item in row.get("rows", []) or []:
            if not isinstance(item, dict):
                continue
            new_item = dict(item)
            item_key = str(new_item.get("source_key") or "").strip()
            item_row = str(new_item.get("source_row_id") or "").strip()
            item_matches = False
            if (
                (item_key and target_source_key and item_key == target_source_key)
                or (item_row and target_source_row and item_row == target_source_row)
            ):
                item_matches = True
            elif (
                str(new_item.get("day_utc") or new_item.get("day") or "").strip().upper() == segment.day_utc.upper()
                and str(new_item.get("start_utc") or new_item.get("start") or "").strip() == segment.raw.get("start_utc", segment.start_utc)
                and str(new_item.get("end_utc") or new_item.get("end") or "").strip() == segment.raw.get("end_utc", segment.end_utc)
                and str(new_item.get("band") or "").strip().upper() == segment.band.upper()
                and str(new_item.get("frequency") or new_item.get("freq") or "").strip() == segment.frequency
                and str(new_item.get("group_name") or new_item.get("group") or "").strip().upper() == segment.group_name.upper()
            ):
                item_matches = True
            if item_matches:
                new_item.update({k: v for k, v in updates.items() if v not in (None, "")})
                matched = True
            out.append(new_item)
        return out if matched else None

    def _confirm_inline_source_update(
        self,
        category: str,
        set_id: str,
        rows: List[Dict[str, Any]],
        name: str,
        source_label: str,
    ) -> bool:
        try:
            self._assert_fresh_action()
            impacts = self._fresh_source_impacts
        except Exception as exc:
            log.exception("FreqPlanner: RF Guard impact scan failed for inline source update.")
            response = QMessageBox.question(
                self,
                "RF Guard Check Unavailable",
                f"RF Guard could not check assigned plans before updating this {source_label} source.\n\n"
                f"{exc}\n\nUpdate the {source_label} source anyway?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            return response == QMessageBox.Save
        if impacts:
            blocked = any(str((impact.get("validation") or {}).get("state") or "").lower() == "blocked" for impact in impacts)
            lines = []
            for impact in impacts[:6]:
                plan = impact.get("plan", {})
                device = impact.get("device", {})
                validation = impact.get("validation", {})
                messages = [str(item) for item in validation.get("messages", []) if str(item or "").strip()]
                lines.append(
                    f"- {str(device.get('name') or 'Radio')}: {str(plan.get('name') or 'Frequency Plan')} - "
                    f"{messages[0] if messages else 'RF Guard reported a conflict.'}"
                )
            body = f"Updating '{name}' affects assigned plans:\n\n" + "\n".join(lines)
            if blocked:
                QMessageBox.warning(self, "RF Guard Blocked Update", body + "\n\nFix the conflict before updating this source.")
                return False
            response = QMessageBox.question(
                self,
                "RF Guard Warning",
                body + f"\n\nUpdate this {source_label} source anyway?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            return response == QMessageBox.Save
        response = QMessageBox.question(
            self,
            f"Update {source_label} Source",
            f"Update the saved {source_label} source '{name}'?\n\nAny plan using this schedule may change.",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return response == QMessageBox.Save

    def _on_inline_update_hf_daily_clicked(self) -> None:
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        missing_text = self._source_dependency_block_text()
        if missing_text:
            self.frequency_plan_action_hint_label.setText(missing_text)
            return
        target = self._single_source_update_target()
        if target is None:
            self.frequency_plan_action_hint_label.setText("Select a window backed by exactly one saved HF Daily or HF Net source row before updating the source.")
            return
        updates = self._inline_editor_updates()
        if updates is None:
            return
        segment = target["segment"]
        sets_key = str(target["sets_key"])
        selected_key = str(target["selected_key"])
        category = str(target["category"])
        source_label = str(target["source_label"])
        set_id = self._selected_source_set_id(selected_key)
        source_row = self._source_set_row_by_id(sets_key, set_id)
        if not source_row:
            self.frequency_plan_action_hint_label.setText(f"Could not find the selected saved {source_label} schedule.")
            return
        rows = self._updated_source_rows_for_segment(sets_key, category, set_id, segment, updates)
        if rows is None:
            self.frequency_plan_action_hint_label.setText(f"Could not match this window to a single {source_label} source row. Open the source tab to edit it.")
            return
        name = str(source_row.get("name") or f"{source_label} schedule")
        self._commit_inline_source_update(category, set_id, rows, name, source_label, selected_key)

    def _commit_inline_source_update(self, category, set_id, rows, name, source_label, selected_key):
        if self._request_fresh_action(
                self._commit_inline_source_update, (category, set_id, rows, name, source_label, selected_key),
                source_update=(category, set_id, rows)):
            return
        if not self._confirm_inline_source_update(category, set_id, rows, name, source_label):
            return
        if getattr(self, "_input_read_error", ""):
            self._show_plan_input_failure()
            return
        existing_id = int(str(set_id).split(":", 1)[1]) if str(set_id).startswith("plan:") else None
        self._assert_fresh_action()
        saved = save_source_schedule(
            self.settings,
            category,
            selected_key,
            name,
            rows,
            existing_plan_id=existing_id,
        )
        # The scheduler reprojects assigned plans on its existing worker.
        self.schedule_saved.emit()
        self.plan_context_service.invalidate()
        self.rebuild_table()
        self._set_action_feedback(
            f"Updated {source_label} source '{str(saved.get('name') or name)}'. Assigned Frequency Plans refresh automatically."
        )

    def _row_user_role_cell(self, row: int, expected_type: type) -> Optional[object]:
        for col in range(max(0, self.table.columnCount())):
            item = self.table.item(row, col)
            value = item.data(Qt.UserRole) if item is not None else None
            if isinstance(value, expected_type):
                return value
        return None

    def _on_schedule_cell_clicked(self, row: int, col: int) -> None:
        if self._inline_draft_dirty():
            choice = QMessageBox.question(self, "Retain window edits",
                "Window edits are still entered. Discard them and inspect the selected current row?",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if choice != QMessageBox.Discard:
                return
            for name in ("inline_group_edit", "inline_band_edit", "inline_frequency_edit",
                         "inline_start_edit", "inline_end_edit", "inline_mode_edit"):
                getattr(self, name).setModified(False)
        self.table.selectRow(row)
        if self._planner_view_mode() in {"effective", "patterns"}:
            cell = self._row_user_role_cell(row, EffectiveWindowCell)
            self._set_projection_inspector(cell if isinstance(cell, EffectiveWindowCell) else None)
            return
        if col < self.COL_DAY_OFFSET:
            if self._planner_view_mode() == "operational":
                self._set_operational_inspector(None)
            else:
                self._set_projection_inspector(None)
            return
        item = self.table.item(row, col)
        cell = item.data(Qt.UserRole) if item is not None else None
        if cell is None and self._planner_view_mode() == "operational":
            cell = self._row_user_role_cell(row, OperationalCell)
        if isinstance(cell, OperationalCell):
            self._set_operational_inspector(cell)
            return
        self._set_projection_inspector(cell if isinstance(cell, ProjectionCell) else None)

    def _selected_source_segment(self, source: str) -> Optional[ScheduleSegment]:
        cell = self._selected_projection_cell
        segments = self._segments_for_source(cell, source)
        return segments[0] if len(segments) == 1 else None

    def _segment_choice_label(self, segment: ScheduleSegment, index: int) -> str:
        identity = str(segment.raw.get("source_key") or "").strip()
        source_row_id = segment.raw.get("source_row_id")
        resource_id = segment.raw.get("resource_id")
        if source_row_id not in (None, "", 0):
            identity = f"row {source_row_id}"
        elif resource_id not in (None, "", 0):
            identity = f"resource {resource_id}"
        elif not identity:
            identity = f"candidate {index + 1}"
        return f"{index + 1}. {self._format_segment_for_inspector(segment)} [{identity}]"

    def _choose_source_segment(self, source: str, title: str) -> Optional[ScheduleSegment]:
        cell = self._selected_projection_cell
        segments = self._segments_for_source(cell, source)
        if len(segments) == 1:
            return segments[0]
        if len(segments) <= 0:
            return None
        labels = [self._segment_choice_label(segment, idx) for idx, segment in enumerate(segments)]
        selected, ok = QInputDialog.getItem(
            self,
            f"Choose {title} Source",
            "Multiple source rows match this cell. Choose the row to review:",
            labels,
            0,
            False,
        )
        if not ok:
            return None
        try:
            return segments[labels.index(str(selected))]
        except ValueError:
            return None

    def _navigate_to_tab(self, tab_label: str) -> None:
        target = str(tab_label or "").strip().upper()
        if not target:
            return
        win = self.window()
        try:
            if hasattr(win, "_screens") and hasattr(win, "_set_screen"):
                for idx, (label, _widget) in enumerate(win._screens):
                    if str(label or "").strip().upper() == target:
                        win._set_screen(idx)
                        return
        except Exception as exc:
            log.debug("FreqPlanner: failed navigating to %s: %s", tab_label, exc)

    def _load_selected_source_schedule_in_tab(
        self,
        tab: Any,
        *,
        selected_key: str,
        sets_key: str,
        category: str,
        live_loader_name: str,
    ) -> Optional[bool]:
        if tab is None:
            return False
        selected_id = self._selected_source_set_id(selected_key)
        if hasattr(tab, "_refresh_freqplanner_source_combo"):
            try:
                tab._refresh_freqplanner_source_combo()
            except Exception:
                pass
        combo = getattr(tab, "schedule_source_combo", None)
        if combo is not None:
            try:
                idx = combo.findData(selected_id)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            except Exception:
                pass
        if hasattr(tab, "_confirm_discard_unsaved_source_load"):
            try:
                if not tab._confirm_discard_unsaved_source_load():
                    return None
            except Exception:
                pass
        if selected_id == LIVE_SOURCE_SET_ID:
            loader = getattr(tab, live_loader_name, None)
            if callable(loader):
                try:
                    loader()
                    return True
                except Exception as exc:
                    log.debug("FreqPlanner: failed loading live source schedule: %s", exc)
                    return False
            return True
        row = source_set_row_by_id_for_category(self.settings, sets_key, category, selected_id)
        if row is None:
            return False
        loader = getattr(tab, "_load_source_rows_into_table", None)
        if not callable(loader):
            return False
        try:
            loader([dict(item) for item in row.get("rows", []) if isinstance(item, dict)])
            return True
        except Exception as exc:
            log.debug("FreqPlanner: failed loading selected source schedule %s: %s", selected_id, exc)
            return False

    @staticmethod
    def _normalize_freq_text(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return format_frequency_mhz(text)

    @staticmethod
    def _widget_text(widget: Any) -> str:
        if widget is None:
            return ""
        if hasattr(widget, "currentText"):
            try:
                return str(widget.currentText() or "").strip()
            except Exception:
                return ""
        if isinstance(widget, QLineEdit) or hasattr(widget, "text"):
            try:
                return str(widget.text() or "").strip()
            except Exception:
                return ""
        return ""

    def _focus_tab_row_for_segment(self, tab: Any, segment: ScheduleSegment, *, source: str) -> bool:
        table = getattr(tab, "table", None)
        if table is None:
            return False
        col_day = getattr(tab, "COL_DAY", None)
        col_group = getattr(tab, "COL_GROUP", None)
        col_band = getattr(tab, "COL_BAND", None)
        col_freq = getattr(tab, "COL_FREQ", None)
        col_start = getattr(tab, "COL_START", None)
        col_end = getattr(tab, "COL_END", None)
        col_net = getattr(tab, "COL_NETNAME", None)
        target_day = str(segment.day_utc or "").strip().upper()
        target_group = str(segment.group_name or "").strip().upper()
        target_band = str(segment.band or "").strip().upper()
        target_freq = self._normalize_freq_text(segment.frequency)
        target_start = str(segment.start_utc or "").strip()
        target_end = str(segment.end_utc or "").strip()
        target_net = str(segment.net_name or "").strip().upper()
        for row in range(table.rowCount()):
            def combo_value(col: Optional[int]) -> str:
                if col is None:
                    return ""
                if hasattr(tab, "_get_combo_value"):
                    try:
                        return str(tab._get_combo_value(row, col, "") or "").strip()
                    except Exception:
                        pass
                return self._widget_text(table.cellWidget(row, col))

            def item_value(col: Optional[int]) -> str:
                if col is None:
                    return ""
                item = table.item(row, col)
                return str(item.text() if item is not None else "").strip()

            day_val = combo_value(col_day).upper()
            group_val = combo_value(col_group).upper()
            band_val = combo_value(col_band).upper()
            freq_val = self._normalize_freq_text(item_value(col_freq))
            start_val = item_value(col_start)
            end_val = item_value(col_end)
            net_val = self._widget_text(table.cellWidget(row, col_net)).upper() if col_net is not None else ""
            if target_day and day_val and day_val != target_day:
                continue
            if target_group and group_val and group_val != target_group:
                continue
            if target_band and band_val and band_val != target_band:
                continue
            if target_freq and freq_val and freq_val != target_freq:
                continue
            if target_start and start_val and start_val != target_start:
                continue
            if target_end and end_val and end_val != target_end:
                continue
            if source.upper() == "NET" and target_net and net_val and net_val != target_net:
                continue
            table.selectRow(row)
            focus_col = col_net if source.upper() == "NET" and col_net is not None else col_freq
            if focus_col is not None and table.item(row, focus_col) is not None:
                table.scrollToItem(table.item(row, focus_col))
            table.setFocus(Qt.TabFocusReason)
            return True
        return False

    def _on_edit_hf_daily_clicked(self) -> None:
        segment = self._choose_source_segment("HF", "HF Daily")
        if segment is None:
            self.frequency_plan_action_hint_label.setText("Select a cell with an HF Daily source row first.")
            return
        self._navigate_to_tab("HF Schedule")
        tab = getattr(self.window(), "hf_schedule_tab", None)
        loaded = self._load_selected_source_schedule_in_tab(
            tab,
            selected_key=SELECTED_HF_DAILY_SOURCE_SET_KEY,
            sets_key=HF_DAILY_SOURCE_SETS_KEY,
            category=HF_DAILY_SOURCE_CATEGORY,
            live_loader_name="_load_schedule",
        )
        if loaded is None:
            self.frequency_plan_action_hint_label.setText(
                "HF Daily load was cancelled; the selected source row was not opened."
            )
            return
        focused = False
        if tab is not None and hasattr(tab, "focus_source_segment"):
            try:
                focused = bool(tab.focus_source_segment(segment))
            except Exception:
                focused = False
        if not focused and tab is not None and hasattr(tab, "_focus_daily_row"):
            try:
                tab._focus_daily_row(segment.group_name, segment.band, segment.frequency)
                focused = True
            except Exception:
                focused = False
        if not focused and tab is not None:
            focused = self._focus_tab_row_for_segment(tab, segment, source="HF")
        self.frequency_plan_action_hint_label.setText(
            (
                "Opened the selected HF Daily schedule and source row for review."
                if focused and loaded
                else (
                    "Opened HF Daily source row for review."
                    if focused
                    else "Opened HF Daily. Review the matching source row before editing."
                )
            )
        )

    def _on_edit_hf_net_clicked(self) -> None:
        segment = self._choose_source_segment("NET", "HF Net")
        if segment is None:
            self.frequency_plan_action_hint_label.setText("Select a cell with an HF Net source row first.")
            return
        self._navigate_to_tab("Net Schedule")
        tab = getattr(self.window(), "net_tab", None)
        loaded = self._load_selected_source_schedule_in_tab(
            tab,
            selected_key=SELECTED_HF_NET_SOURCE_SET_KEY,
            sets_key=HF_NET_SOURCE_SETS_KEY,
            category=HF_NET_SOURCE_CATEGORY,
            live_loader_name="_load",
        )
        if loaded is None:
            self.frequency_plan_action_hint_label.setText(
                "HF Net load was cancelled; the selected source row was not opened."
            )
            return
        focused = False
        if tab is not None and hasattr(tab, "focus_source_segment"):
            try:
                focused = bool(tab.focus_source_segment(segment))
            except Exception:
                focused = False
        if not focused:
            focused = self._focus_tab_row_for_segment(tab, segment, source="NET") if tab is not None else False
        self.frequency_plan_action_hint_label.setText(
            (
                "Opened the selected HF Net schedule and source row for review."
                if focused and loaded
                else (
                    "Opened HF Net source row for review."
                    if focused
                    else "Opened HF Nets. Review the matching source row before editing."
                )
            )
        )

    def _on_build_sop_layer_clicked(self) -> None:
        self._navigate_to_tab("SOP")
        self.frequency_plan_action_hint_label.setText(
            "Opened SOP Builder. Create or update condition-based what-to-do layers, then return to Plan Builder to review Effective Windows and RF Guard."
        )

    def _on_open_sop_builder_clicked(self) -> None:
        segment = self._choose_source_segment("SOP", "SOP")
        if segment is None:
            self.frequency_plan_action_hint_label.setText("Select a cell with an SOP source row first.")
            return
        self._navigate_to_tab("SOP")
        profile_id = int(segment.raw.get("sop_profile_id") or 0)
        tab = getattr(self.window(), "sop_tab", None)
        selected = False
        focused = False
        if tab is not None and hasattr(tab, "focus_source_segment"):
            try:
                focused = bool(tab.focus_source_segment(segment))
                selected = focused
            except Exception as exc:
                log.debug("FreqPlanner: failed focusing SOP layer source: %s", exc)
                focused = False
        if not focused and tab is not None and profile_id > 0:
            try:
                if hasattr(tab, "select_profile"):
                    selected = bool(tab.select_profile(profile_id))
                elif hasattr(tab, "profile_combo"):
                    for idx in range(tab.profile_combo.count()):
                        if int(tab.profile_combo.itemData(idx) or 0) == profile_id:
                            tab.profile_combo.setCurrentIndex(idx)
                            selected = True
                            break
            except Exception as exc:
                log.debug("FreqPlanner: failed selecting SOP profile %s: %s", profile_id, exc)
        self.frequency_plan_action_hint_label.setText(
            "Opened SOP Builder source row for review."
            if focused
            else (
                "Opened SOP Builder source profile for review."
                if selected
                else "Opened SOP Builder. Review the matching SOP source before editing."
            )
        )

    def mark_schedule_dirty(self) -> None:
        self._pending_rebuild = True

    def on_schedule_sources_changed(self):
        self.mark_schedule_dirty()
        self._request_input_catalog(force=True)


    def on_tab_activated(self):
        self._tab_active = True
        self._request_input_catalog()


    def _projection_worker(self) -> ThreadPoolExecutor:
        if self._projection_executor is None:
            self._projection_executor = DaemonSerialExecutor(thread_name_prefix="fio-plan-projection")
        return self._projection_executor

    def _shutdown_projection_worker(self) -> None:
        if not self._revision_closed:
            self._revision_closed = True
            self._revision_executor.submit(self._close_revision_tracker)
            self._revision_executor.shutdown(wait=False, cancel_futures=False)
        executor = self._projection_executor
        self._projection_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def closeEvent(self, event) -> None:  # pragma: no cover - Qt lifecycle glue
        self._catalog_generation += 1
        self._catalog_latest = None
        self.settings.close()
        self._shutdown_projection_worker()
        super().closeEvent(event)

    @staticmethod
    def _build_projection_snapshot(
        request_id: int,
        mode: str,
        snapshot: str,
        hf_sched: Tuple[Dict[str, Any], ...],
        net_sched: Tuple[Dict[str, Any], ...],
        sop_sched: Tuple[Dict[str, Any], ...],
        policy_rows: Tuple[Dict[str, Any], ...],
        net_resources: Tuple[Dict[str, Any], ...],
        week_sunday: datetime.date,
        selected_plan: Optional[Dict[str, Any]],
        selected_plan_refs: Tuple[Dict[str, Any], ...],
        started_at: float,
        configured_groups: frozenset = frozenset(),
        assignments: tuple = (), devices: tuple = (), selected_radio: int = 0,
    ) -> _PlanProjectionResult:
        try:
            if mode == "operational":
                def filtered(rows):
                    return tuple(dict(row) for row in rows
                                 if str(row.get("group_name") or row.get("group") or row.get("primary_js8call_group") or "").strip().upper() in configured_groups)
                selected_plan_refs = filtered(selected_plan_refs)
                hf_sched, net_sched, sop_sched = filtered(hf_sched), filtered(net_sched), filtered(sop_sched)
                net_resources = filtered(net_resources)
            if mode == "radio":
                projection = build_radio_windows(selected_plan, assignments, devices, selected_radio, FreqPlannerTab.MAX_PROJECTION_TABLE_ROWS)
            elif mode == "operational":
                if selected_plan_refs:
                    projection = build_operational_day_projection_from_refs(
                        [dict(row) for row in selected_plan_refs],
                        week_start_utc=week_sunday,
                    )
                else:
                    projection = build_operational_day_projection(
                        [dict(row) for row in hf_sched],
                        [dict(row) for row in net_sched],
                        [dict(row) for row in sop_sched],
                        [dict(row) for row in net_resources],
                        [dict(row) for row in policy_rows],
                        week_start_utc=week_sunday,
                    )
            else:
                projection = build_blended_schedule_projection(
                    [dict(row) for row in hf_sched],
                    [dict(row) for row in net_sched],
                    [dict(row) for row in sop_sched],
                    [dict(row) for row in policy_rows],
                    week_start_utc=week_sunday,
                )
            return _PlanProjectionResult(
                request_id=request_id,
                mode=mode,
                snapshot=snapshot,
                hf_sched=hf_sched,
                net_sched=net_sched,
                sop_sched=sop_sched,
                policy_rows=policy_rows,
                week_sunday=week_sunday,
                projection=projection,
                selected_plan=dict(selected_plan) if isinstance(selected_plan, dict) else None,
                selected_plan_refs=selected_plan_refs,
                started_at=started_at,
            )
        except Exception as exc:
            return _PlanProjectionResult(
                request_id=request_id,
                mode=mode,
                snapshot=snapshot,
                hf_sched=hf_sched,
                net_sched=net_sched,
                sop_sched=sop_sched,
                policy_rows=policy_rows,
                week_sunday=week_sunday,
                selected_plan=dict(selected_plan) if isinstance(selected_plan, dict) else None,
                selected_plan_refs=selected_plan_refs,
                started_at=started_at,
                error=str(exc),
            )

    def _start_projection_worker(
        self,
        *,
        mode: str,
        snapshot: str,
        hf_sched: List[Dict[str, Any]],
        net_sched: List[Dict[str, Any]],
        sop_sched: List[Dict[str, Any]],
        policy_rows: List[Dict[str, Any]],
        week_sunday: datetime.date,
        started_at: float,
    ) -> None:
        self._projection_request_id += 1
        request_id = int(self._projection_request_id)
        self._projection_pending = True
        selected_plan = (self._selected_frequency_plan_row() if mode == "radio"
                         else self._selected_sop_schedule_plan_row() if mode == "operational" else None)
        selected_plan_refs: Tuple[Dict[str, Any], ...] = ()
        net_resources: Tuple[Dict[str, Any], ...] = ()
        worker_hf, worker_net, worker_sop, worker_policy = hf_sched, net_sched, sop_sched, policy_rows
        if mode == "operational":
            if selected_plan:
                selected_plan_refs = self._schedule_refs_from_plan_row(selected_plan)
            else:
                net_resources = self._load_net_resources_from_db() or ()
        # Only one CPU task and one latest intent are retained. Source tuples
        # belong to a detached publication; copying/filtering runs on the worker.
        self._projection_latest = (
            request_id, mode, snapshot, worker_hf, worker_net, worker_sop,
            worker_policy, net_resources, week_sunday, selected_plan,
            selected_plan_refs, started_at, frozenset(self._configured_operating_group_names()),
            self._catalog_model.assignments, self._catalog_model.devices, self._selected_radio_window_radio_id())
        if self._projection_future is None:
            self._submit_latest_projection()

    def _submit_latest_projection(self):
        arguments, self._projection_latest = self._projection_latest, None
        if arguments is None:
            return
        request_id, mode, snapshot = arguments[:3]
        started_at = arguments[11]
        self._projection_future = self._projection_worker().submit(self._build_projection_snapshot, *arguments)
        self._projection_future.add_done_callback(
            lambda done: self._on_projection_future_done(request_id, mode, snapshot, started_at, done))

    def _on_projection_future_done(
        self,
        request_id: int,
        mode: str,
        snapshot: str,
        started_at: float,
        future: Future,
    ) -> None:
        try:
            result = future.result()
        except Exception as exc:
            result = _PlanProjectionResult(
                request_id=request_id,
                mode=mode,
                snapshot=snapshot,
                hf_sched=(),
                net_sched=(),
                sop_sched=(),
                policy_rows=(),
                week_sunday=self._week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc)),
                started_at=started_at,
                error=str(exc),
            )
        try:
            self._projection_emitter.finished.emit(result)
        except RuntimeError:
            pass  # The owning workspace was destroyed after worker completion.

    def _capture_table_display(self) -> tuple:
        """Retain the last complete display until all render-time reads succeed."""
        table = self.table
        rows, columns = table.rowCount(), table.columnCount()
        headers = [table.horizontalHeaderItem(c).clone() if table.horizontalHeaderItem(c) else None for c in range(columns)]
        cells = [(r, c, table.item(r, c).clone()) for r in range(rows) for c in range(columns) if table.item(r, c)]
        spans = []
        covered = set()
        for row in range(rows):
            for column in range(columns):
                if (row, column) in covered:
                    continue
                height, width = table.rowSpan(row, column), table.columnSpan(row, column)
                if height > 1 or width > 1:
                    spans.append((row, column, height, width))
                    covered.update((r, c) for r in range(row, row + height) for c in range(column, column + width))
        selection = [(index.row(), index.column()) for index in table.selectedIndexes()]
        return (rows, columns, headers, cells, spans, table.verticalScrollBar().value(),
                table.horizontalScrollBar().value(), table.currentRow(), table.currentColumn(), selection)

    def _restore_table_display(self, display: tuple) -> None:
        rows, columns, headers, cells, spans, vertical, horizontal, current_row, current_column, selection = display
        table = self.table
        blocked = table.blockSignals(True)
        table.setSortingEnabled(False)
        table.clearContents()
        table.clearSpans()
        table.setRowCount(rows)
        table.setColumnCount(columns)
        for column, header in enumerate(headers):
            if header is not None:
                table.setHorizontalHeaderItem(column, header)
        for row, column, item in cells:
            table.setItem(row, column, item)
        for row, column, row_span, column_span in spans:
            table.setSpan(row, column, row_span, column_span)
        table.setCurrentCell(current_row, current_column)
        table.clearSelection()
        for row, column in selection:
            table.selectionModel().select(table.model().index(row, column), QItemSelectionModel.Select)
        table.blockSignals(blocked)
        table.verticalScrollBar().setValue(vertical)
        table.horizontalScrollBar().setValue(horizontal)
        table.setEnabled(False)

    def _restore_equivalent_table_position(self, display: tuple) -> None:
        _rows, columns, _headers, cells, _spans, vertical, horizontal, current_row, current_column, _selection = display
        if current_row >= 0 and columns == self.table.columnCount():
            previous = {column: item for row, column, item in cells if row == current_row}
            has_identity = any(item.data(Qt.UserRole) is not None for item in previous.values())
            for row in range(self.table.rowCount()) if has_identity else ():
                matches = all(
                    (self.table.item(row, column).text() if self.table.item(row, column) else "")
                    == (previous[column].text() if column in previous else "")
                    and (self.table.item(row, column).data(Qt.UserRole) if self.table.item(row, column) else None)
                    == (previous[column].data(Qt.UserRole) if column in previous else None)
                    for column in range(columns))
                if matches:
                    blocked = self.table.blockSignals(True)
                    self.table.setCurrentCell(row, current_column)
                    self.table.selectRow(row)
                    self.table.blockSignals(blocked)
                    if not self._inline_draft_dirty():
                        try:
                            self._on_schedule_cell_clicked(row, current_column)
                        except Exception as exc:
                            self._handle_plan_input_failure(exc)
                    break
        self.table.verticalScrollBar().setValue(vertical)
        self.table.horizontalScrollBar().setValue(horizontal)

    def _on_projection_ready(self, result: object) -> None:
        self._projection_future = None
        if getattr(self, "_revision_closed", False) or not getattr(self, "_tab_active", True):
            self._projection_latest = None
            return
        if self._projection_latest is not None:
            self._submit_latest_projection()
        if not isinstance(result, _PlanProjectionResult):
            return
        if result.request_id != int(getattr(self, "_projection_request_id", 0) or 0):
            return
        try:
            current_snapshot = self._snapshot((), (), (), ())
        except Exception as exc:
            self._handle_plan_input_failure(exc)
            log.debug("FreqPlanner: projection freshness check failed: %s", exc)
            return
        if result.snapshot != current_snapshot:
            self._handle_plan_input_failure(ScheduleSourceError("Plan Builder inputs changed during projection"))
            return
        self._projection_pending = False
        if result.error:
            self._handle_plan_input_failure(ScheduleSourceError(result.error))
            log.warning("FreqPlanner projection worker failed: %s", result.error)
            return
        failure_generation = getattr(self, "_input_failure_generation", 0)
        self._last_snapshot = result.snapshot
        self._latest_projection_snapshot = result.snapshot
        self._latest_projection_mode = result.mode
        self._latest_projection = result.projection
        hf_sched, net_sched, sop_sched, policy_rows = result.hf_sched, result.net_sched, result.sop_sched, result.policy_rows
        theme = resolve_theme(self.settings)
        retained_display = self._capture_table_display()
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setSortingEnabled(False)
            self.table.clearContents()
            self.table.clearSpans()
            if result.mode == "radio":
                self._rebuild_radio_windows_table(hf_sched, net_sched, sop_sched, result.week_sunday, theme,
                                                  prepared=result.projection)
            elif result.mode == "operational":
                self._rebuild_operational_lane_table(
                    hf_sched,
                    net_sched,
                    sop_sched,
                    policy_rows,
                    result.week_sunday,
                    theme,
                    projection=result.projection if isinstance(result.projection, OperationalDayProjection) else None,
                    selected_plan=result.selected_plan,
                )
            elif result.mode == "effective":
                self._rebuild_effective_windows_table(
                    hf_sched,
                    net_sched,
                    sop_sched,
                    policy_rows,
                    result.week_sunday,
                    theme,
                    projection=result.projection if isinstance(result.projection, BlendedScheduleProjection) else None,
                )
            elif result.mode == "patterns":
                self._rebuild_pattern_summary_table(
                    hf_sched,
                    net_sched,
                    sop_sched,
                    policy_rows,
                    result.week_sunday,
                    theme,
                    projection=result.projection if isinstance(result.projection, BlendedScheduleProjection) else None,
                )
            else:
                self._rebuild_shared_week_table(
                    hf_sched,
                    net_sched,
                    sop_sched,
                    policy_rows,
                    result.week_sunday,
                    theme,
                    projection=result.projection if isinstance(result.projection, BlendedScheduleProjection) else None,
                )
        except Exception as exc:
            self._restore_table_display(retained_display)
            self._handle_plan_input_failure(exc)
            return
        finally:
            self.table.setUpdatesEnabled(True)
        self._restore_equivalent_table_position(retained_display)
        if failure_generation != getattr(self, "_input_failure_generation", 0):
            self._restore_table_display(retained_display)
            return
        if not self._clear_plan_input_failure():
            self._restore_table_display(retained_display)
            return
        self.table.setEnabled(True)
        if self._operation_feedback and not self._catalog_review_required:
            self.frequency_plan_action_hint_label.setText(self._operation_feedback)
        self._update_clock_labels()
        emit_span(
            "freqplanner.rebuild_table",
            (time.perf_counter() - float(result.started_at or time.perf_counter())) * 1000.0,
            settings=self.settings,
            meta={"show_local": bool(self._show_local), "show_band": bool(self._show_band), "projection": result.mode, "worker": True},
            min_ms=5.0,
        )
        log.info("FreqPlanner table rebuilt from %s worker projection.", result.mode)

    # ------------- core rebuild ------------- #

    def _rebuild_operational_lane_table(
        self,
        hf_sched: List[Dict],
        net_sched: List[Dict],
        sop_sched: List[Dict],
        policy_rows: List[Dict[str, Any]],
        week_sunday: datetime.date,
        theme: Dict[str, Any],
        *,
        projection: Optional[OperationalDayProjection] = None,
        selected_plan: Optional[Dict[str, Any]] = None,
    ) -> None:
        selected_plan = selected_plan if isinstance(selected_plan, dict) else self._selected_sop_schedule_plan_row()
        projection = projection or (self._build_selected_sop_plan_projection(week_sunday) if selected_plan else None)
        source_text = f"saved plan '{str(selected_plan.get('name') or 'SOP Schedule Plan')}'" if selected_plan else "live projection"
        if projection is None:
            hf_sched = self._filter_rows_to_configured_groups(hf_sched)
            net_sched = self._filter_rows_to_configured_groups(net_sched)
            sop_sched = self._filter_rows_to_configured_groups(sop_sched)
            net_resources = self._filter_rows_to_configured_groups(self._load_net_resources_from_db() or [])
            projection = build_operational_day_projection(
                hf_sched,
                net_sched,
                sop_sched,
                net_resources,
                policy_rows,
                week_start_utc=week_sunday,
            )
        selected_day = self._selected_operational_day()
        lanes = list(projection.lanes)
        column_count = max(self.COL_DAY_OFFSET + len(lanes), self.COL_DAY_OFFSET + 1)
        self.table.setColumnCount(column_count)
        self.table.setRowCount(24)
        tz_name, tz_abbr = self._current_timezone_label()
        headers = ["UTC Hour", f"Local Time ({tz_abbr})"]
        headers.extend(lane.lane_label for lane in lanes)
        if not lanes:
            headers.append("SOP Lanes")
        self.table.setHorizontalHeaderLabels(headers)
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(self.COL_UTC, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(self.COL_LOCAL, QHeaderView.ResizeToContents)
        for col in range(self.COL_DAY_OFFSET, column_count):
            hv.setSectionResizeMode(col, QHeaderView.Stretch)
        tz_name_cfg, tz = self._current_timezone()
        try:
            day_index = DAY_NAMES.index(selected_day)
        except ValueError:
            day_index = 0
        date_value = week_sunday + datetime.timedelta(days=day_index)
        visible_bands: set[str] = set()
        for hour in range(24):
            utc_dt = datetime.datetime.combine(date_value, datetime.time(hour=hour), tzinfo=datetime.timezone.utc)
            local_dt = utc_dt.astimezone(tz)
            utc_item = QTableWidgetItem(f"{hour:02d}:00")
            utc_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(hour, self.COL_UTC, utc_item)
            local_item = QTableWidgetItem(f"{local_dt.hour:02d}:00")
            local_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(hour, self.COL_LOCAL, local_item)
            if not lanes:
                item = QTableWidgetItem("")
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(hour, self.COL_DAY_OFFSET, item)
                continue
            for lane_index, lane in enumerate(lanes):
                col = self.COL_DAY_OFFSET + lane_index
                cell = projection.cell_for(lane.lane_key, selected_day, hour)
                item = QTableWidgetItem(self._operational_cell_text(cell))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if cell is not None:
                    item.setData(Qt.UserRole, cell)
                    tooltip = self._operational_cell_tooltip(cell)
                    if tooltip:
                        item.setToolTip(tooltip)
                    if cell.has_contention:
                        item.setBackground(qcolor(theme["surface_alt"]))
                    elif cell.entries:
                        primary_band = str(cell.entries[0].band or "").split("/")[0].strip()
                        if primary_band:
                            visible_bands.add(primary_band.lower())
                            colors = self._band_cell_colors(primary_band, theme)
                            if colors:
                                item.setBackground(qcolor(colors["bg"]))
                                item.setForeground(qcolor(colors["fg"]))
                self.table.setItem(hour, col, item)
        self._visible_bands = sorted(visible_bands)
        self._render_band_legend()
        self._set_operational_inspector(None)
        self._refresh_plan_layer_summary(
            hf_sched=hf_sched,
            net_sched=net_sched,
            sop_sched=sop_sched,
            effective_count=len(projection.lanes),
            effective_label="Lanes",
            plan_payload=selected_plan,
        )
        self.frequency_plan_action_hint_label.setText(
            f"SOP Lanes view from {source_text}: {selected_day}. Review what each radio or group should do during the selected day."
        )

    def _rebuild_pattern_summary_table(
        self,
        hf_sched: List[Dict],
        net_sched: List[Dict],
        sop_sched: List[Dict],
        policy_rows: List[Dict[str, Any]],
        week_sunday: datetime.date,
        theme: Dict[str, Any],
        *,
        projection: Optional[BlendedScheduleProjection] = None,
    ) -> None:
        projection = projection or build_blended_schedule_projection(
            hf_sched,
            net_sched,
            sop_sched,
            policy_rows,
            week_start_utc=week_sunday,
        )
        time_scope = "Local" if self._show_local else "UTC"
        focus_header = "Band" if self._show_band else "Freq"
        headers = ["Group / Net / SOP", focus_header, "Pattern", f"Time ({time_scope})", "Mode", "Layer", "What It Means"]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.Stretch)
        hv.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(6, QHeaderView.Stretch)
        if not projection.effective_segments:
            self.table.setRowCount(1)
            item = QTableWidgetItem("No patterns available")
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, len(headers))
            self._visible_bands = []
            self._render_band_legend()
            self._set_projection_inspector(None)
            self._refresh_plan_layer_summary(
                hf_sched=hf_sched,
                net_sched=net_sched,
                sop_sched=sop_sched,
                effective_count=0,
                effective_label="Patterns",
            )
            self.frequency_plan_action_hint_label.setText("No Daily, Net, or SOP patterns are available.")
            return
        grouped: Dict[Tuple[str, str, str, str, str, str, str], Dict[str, Any]] = {}
        visible_bands: set[str] = set()
        for segment in projection.effective_segments:
            source_cell = self._effective_window_cell_for_segment(segment, projection)
            day_label, time_label = self._effective_window_day_and_time(segment)
            label = self._effective_window_label(segment)
            band_or_freq = segment.band if self._show_band else segment.frequency
            layer = self._effective_window_source_label(segment.source)
            notes = self._effective_window_notes(segment, source_cell)
            key = (
                label,
                band_or_freq,
                time_label,
                segment.mode,
                layer,
                notes,
                segment.source,
            )
            bucket = grouped.setdefault(
                key,
                {
                    "label": label,
                    "band_or_freq": band_or_freq,
                    "time": time_label,
                    "mode": segment.mode,
                    "layer": layer,
                    "notes": notes,
                    "source": segment.source,
                    "days": set(),
                    "cell": source_cell,
                    "band": segment.band,
                },
            )
            bucket["days"].add(day_label)
            if segment.band:
                visible_bands.update(part.strip().lower() for part in str(segment.band).split("/") if part.strip())
        rows = sorted(
            grouped.values(),
            key=lambda item: (
                {"Daily": 0, "Net": 1, "SOP": 2}.get(str(item["layer"]), 9),
                str(item["label"]),
                str(item["time"]),
                str(item["band_or_freq"]),
            ),
        )
        total_rows = len(rows)
        rows = rows[: self.MAX_PROJECTION_TABLE_ROWS]
        self.table.setRowCount(len(rows))
        for row, item_data in enumerate(rows):
            pattern = self._compact_pattern_days(item_data["days"])
            values = [
                item_data["label"],
                item_data["band_or_freq"],
                pattern,
                item_data["time"],
                item_data["mode"],
                item_data["layer"],
                item_data["notes"],
            ]
            source_cell = item_data["cell"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setData(Qt.UserRole, source_cell)
                item.setToolTip(self._projection_window_tooltip(source_cell))
                if item_data["source"] == "HF" and item_data["band"]:
                    primary_band = str(item_data["band"]).split("/")[0].strip()
                    colors = self._band_cell_colors(primary_band, theme)
                    if colors:
                        item.setBackground(qcolor(colors["bg"]))
                        item.setForeground(qcolor(colors["fg"]))
                elif item_data["source"] in {"NET", "SOP"}:
                    item.setBackground(qcolor(theme["surface_alt"]))
                self.table.setItem(row, col, item)
        self._visible_bands = sorted(visible_bands)
        self._render_band_legend()
        self._set_projection_inspector(None)
        self._refresh_plan_layer_summary(
            hf_sched=hf_sched,
            net_sched=net_sched,
            sop_sched=sop_sched,
            effective_count=total_rows,
            effective_label="Patterns",
        )
        bounded_note = f" Showing first {len(rows)} of {total_rows}." if total_rows > len(rows) else ""
        self.frequency_plan_action_hint_label.setText(
            "Pattern Summary groups matching windows so daily baselines and nets are easy to scan. "
            f"Select a pattern to edit one representative window.{bounded_note}"
        )
        self.table.setSortingEnabled(True)

    def _rebuild_effective_windows_table(
        self,
        hf_sched: List[Dict],
        net_sched: List[Dict],
        sop_sched: List[Dict],
        policy_rows: List[Dict[str, Any]],
        week_sunday: datetime.date,
        theme: Dict[str, Any],
        *,
        projection: Optional[BlendedScheduleProjection] = None,
    ) -> None:
        projection = projection or build_blended_schedule_projection(
            hf_sched,
            net_sched,
            sop_sched,
            policy_rows,
            week_start_utc=week_sunday,
        )
        time_scope = "Local" if self._show_local else "UTC"
        focus_header = "Band" if self._show_band else "Freq"
        headers = [f"Day ({time_scope})", "Group / Net / SOP", focus_header, f"Time ({time_scope})", "Mode", "Layer", "What It Means"]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(1, QHeaderView.Stretch)
        hv.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(6, QHeaderView.Stretch)
        all_segments = projection.effective_segments
        visible_segments = all_segments[: self.MAX_PROJECTION_TABLE_ROWS]
        self.table.setRowCount(max(1, len(visible_segments)))
        visible_bands: set[str] = set()
        if not projection.effective_segments:
            item = QTableWidgetItem("No effective schedule windows")
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, len(headers))
            self._visible_bands = []
            self._render_band_legend()
            self._set_projection_inspector(None)
            self._refresh_plan_layer_summary(
                hf_sched=hf_sched,
                net_sched=net_sched,
                sop_sched=sop_sched,
                effective_count=0,
            )
            self.frequency_plan_action_hint_label.setText("No effective HF Daily, HF Nets, or SOP windows are available.")
            return
        for row, segment in enumerate(visible_segments):
            source_cell = self._effective_window_cell_for_segment(segment, projection)
            day_label, time_label = self._effective_window_day_and_time(segment)
            band_or_freq = segment.band if self._show_band else segment.frequency
            values = [
                day_label,
                self._effective_window_label(segment),
                band_or_freq,
                time_label,
                segment.mode,
                self._effective_window_source_label(segment.source),
                self._effective_window_notes(segment, source_cell),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setData(Qt.UserRole, source_cell)
                if segment.band:
                    visible_bands.update(part.strip().lower() for part in str(segment.band).split("/") if part.strip())
                if col in {1, 6}:
                    item.setToolTip(self._projection_window_tooltip(source_cell))
                if segment.source == "HF" and segment.band:
                    primary_band = segment.band.split("/")[0].strip()
                    colors = self._band_cell_colors(primary_band, theme)
                    if colors:
                        item.setBackground(qcolor(colors["bg"]))
                        item.setForeground(qcolor(colors["fg"]))
                elif segment.source in {"NET", "SOP"}:
                    item.setBackground(qcolor(theme["surface_alt"]))
                self.table.setItem(row, col, item)
        self._visible_bands = sorted(visible_bands)
        self._render_band_legend()
        self._set_projection_inspector(None)
        self._refresh_plan_layer_summary(
            hf_sched=hf_sched,
            net_sched=net_sched,
            sop_sched=sop_sched,
            effective_count=len(all_segments),
        )
        bounded_note = (
            f" Showing first {len(visible_segments)} of {len(all_segments)}."
            if len(all_segments) > len(visible_segments)
            else ""
        )
        self.frequency_plan_action_hint_label.setText(
            "Sort by day, time, layer, group, band, or purpose. "
            f"Select a window to review or edit its source.{bounded_note}"
        )
        self.table.setSortingEnabled(True)

    def _rebuild_radio_windows_table(
        self,
        hf_sched: List[Dict],
        net_sched: List[Dict],
        sop_sched: List[Dict],
        week_sunday: datetime.date,
        theme: Dict[str, Any],
        *, prepared=None,
    ) -> None:
        selected_plan = self._selected_frequency_plan_row()
        if prepared is None:
            raise ScheduleSourceError("Radio windows are awaiting background preparation")
        radio_refs = prepared["refs"]
        time_scope = "Local" if self._show_local else "UTC"
        headers = [
            "Window",
            f"Day ({time_scope})",
            f"Time ({time_scope})",
            "Radio",
            "Layer",
            "Group / Net / SOP",
            "Band",
            "Freq",
            "Mode",
            "Guard Context",
        ]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(8, QHeaderView.Stretch)
        visible_bands: set[str] = set()
        selected_radio_id = self._selected_radio_window_radio_id()
        total_radio_refs = prepared["total"]
        if not radio_refs:
            self.table.setRowCount(1)
            if selected_radio_id > 0:
                message = f"No windows are assigned to {self._radio_label_for_id(selected_radio_id)} for the selected plan."
            else:
                message = "Select a saved Frequency Plan or SOP Plan with radio assignments to review Radio Windows."
            item = QTableWidgetItem(message)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, len(headers))
            self._visible_bands = []
            self._render_band_legend()
            self._set_projection_inspector(None)
            self._refresh_plan_layer_summary(
                hf_sched=hf_sched,
                net_sched=net_sched,
                sop_sched=sop_sched,
                effective_count=0,
                effective_label="Radio windows",
                plan_payload=selected_plan,
            )
            self.frequency_plan_action_hint_label.setText(
                "Radio Windows shows assigned plan windows by radio. Assign this plan in Configuration to review cross-radio windows here."
            )
            return

        sorted_refs = radio_refs
        self.table.setRowCount(max(1, len(sorted_refs)))
        overlap_labels = prepared["overlap"]
        for row, ref in enumerate(sorted_refs):
            radio_id = self._radio_id_for_schedule_ref(ref)
            day_label, time_label = self._schedule_ref_day_and_time(ref, week_sunday)
            source = str(ref.get("source") or "").strip().upper()
            band = str(ref.get("band") or "").strip().upper()
            guard_context = self._radio_guard_context_label(radio_id)
            if overlap_labels.get(row):
                guard_context = f"{guard_context}; {overlap_labels[row]}"
            values = [
                self._radio_window_group_label(ref, week_sunday),
                day_label,
                time_label,
                self._radio_label_for_id(radio_id),
                self._effective_window_source_label(source),
                self._schedule_ref_display_label(ref),
                band,
                str(ref.get("frequency") or ref.get("freq") or "").strip(),
                str(ref.get("mode") or "").strip(),
                guard_context,
            ]
            tooltip = " | ".join(value for value in values if value)
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setData(Qt.UserRole, dict(ref))
                item.setToolTip(tooltip)
                if band:
                    visible_bands.update(part.strip().lower() for part in band.split("/") if part.strip())
                if overlap_labels.get(row):
                    item.setBackground(qcolor(theme.get("warning_bg", "#FFF3CD")))
                    item.setForeground(qcolor(theme.get("warning_fg", theme.get("text", "#1f2328"))))
                elif source in {"NET", "SOP"}:
                    item.setBackground(qcolor(theme["surface_alt"]))
                elif band:
                    colors = self._band_cell_colors(band.split("/")[0].strip(), theme)
                    if colors:
                        item.setBackground(qcolor(colors["bg"]))
                        item.setForeground(qcolor(colors["fg"]))
                self.table.setItem(row, col, item)
        self._visible_bands = sorted(visible_bands)
        self._render_band_legend()
        self._set_projection_inspector(None)
        self._refresh_plan_layer_summary(
            hf_sched=hf_sched,
            net_sched=net_sched,
            sop_sched=sop_sched,
            effective_count=total_radio_refs,
            effective_label="Radio windows",
            plan_payload=selected_plan,
        )
        plan_name = str((selected_plan or {}).get("name") or "selected plan").strip()
        summary = self._radio_window_summary_text(plan_name, sorted_refs, overlap_labels, week_sunday)
        if selected_radio_id > 0:
            summary = f"{self._radio_label_for_id(selected_radio_id)}: {summary}"
        if total_radio_refs > len(sorted_refs):
            summary += f" Showing first {len(sorted_refs)} of {total_radio_refs}."
        self.frequency_plan_action_hint_label.setText(summary)

    def _rebuild_shared_week_table(
        self,
        hf_sched: List[Dict],
        net_sched: List[Dict],
        sop_sched: List[Dict],
        policy_rows: List[Dict[str, Any]],
        week_sunday: datetime.date,
        theme: Dict[str, Any],
        *,
        projection: Optional[BlendedScheduleProjection] = None,
    ) -> None:
        tz_name, tz_abbr = self._current_timezone_label()
        headers = (
            [
                "UTC Hour",
                f"Local Time ({tz_abbr})",
                "Sunday",
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
            ]
            if not self._show_local
            else [
                f"Local Hour ({tz_abbr})",
                "UTC Time",
                "Sunday",
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
            ]
        )
        self.table.setColumnCount(9)
        self.table.setRowCount(24)
        self.table.setHorizontalHeaderLabels(headers)
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(self.COL_UTC, QHeaderView.Stretch)
        hv.setSectionResizeMode(self.COL_LOCAL, QHeaderView.Stretch)
        for col in range(self.COL_DAY_OFFSET, 9):
            hv.setSectionResizeMode(col, QHeaderView.Stretch)
        projection = projection or build_blended_schedule_projection(
            hf_sched,
            net_sched,
            sop_sched,
            policy_rows,
            week_start_utc=week_sunday,
        )
        self._refresh_plan_layer_summary(
            hf_sched=hf_sched,
            net_sched=net_sched,
            sop_sched=sop_sched,
            effective_count=len(projection.effective_segments),
        )
        projection_cells = self._projection_cells_by_utc_key(projection)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        tz_name_cfg, tz = self._current_timezone()
        now_local = datetime.datetime.now(tz)
        now_plus_24 = now_utc + datetime.timedelta(hours=24)
        today_utc = now_utc.replace(minute=0, second=0, microsecond=0)
        week_start_local = self._start_of_week_local(tz)
        visible_bands: set[str] = {
            band.strip().lower()
            for segment in projection.effective_segments
            for band in str(segment.band or "").split("/")
            if band.strip()
        }

        for hour in range(24):
            if not self._show_local:
                utc_item = QTableWidgetItem(f"{hour:02d}:00")
                utc_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if hour == now_utc.hour:
                    utc_item.setBackground(qcolor(theme["surface_alt"]))
                self.table.setItem(hour, self.COL_UTC, utc_item)

                utc_dt = datetime.datetime(
                    year=today_utc.year,
                    month=today_utc.month,
                    day=today_utc.day,
                    hour=hour,
                    minute=0,
                    second=0,
                    tzinfo=datetime.timezone.utc,
                )
                local_dt = utc_dt.astimezone(tz)
                local_item = QTableWidgetItem(f"{local_dt.hour:02d}:00")
                local_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if local_dt.hour == now_local.hour:
                    local_item.setBackground(qcolor(theme["surface_alt"]))
                self.table.setItem(hour, self.COL_LOCAL, local_item)
            else:
                local_dt = week_start_local + datetime.timedelta(hours=hour)
                utc_dt = local_dt.astimezone(datetime.timezone.utc)

                local_item = QTableWidgetItem(f"{local_dt.hour:02d}:00")
                local_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if local_dt.hour == now_local.hour:
                    local_item.setBackground(qcolor(theme["surface_alt"]))
                self.table.setItem(hour, self.COL_UTC, local_item)

                utc_item = QTableWidgetItem(f"{utc_dt.hour:02d}:00")
                utc_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if utc_dt.hour == now_utc.hour:
                    utc_item.setBackground(qcolor(theme["surface_alt"]))
                self.table.setItem(hour, self.COL_LOCAL, utc_item)

            for col in range(self.COL_DAY_OFFSET, 9):
                if not self._show_local:
                    lookup_day = DAY_NAMES[col - self.COL_DAY_OFFSET]
                    lookup_hour = hour
                else:
                    cell_local_dt = week_start_local + datetime.timedelta(days=(col - self.COL_DAY_OFFSET), hours=hour)
                    cell_dt_utc = cell_local_dt.astimezone(datetime.timezone.utc)
                    lookup_day = cell_dt_utc.strftime("%A")
                    lookup_hour = cell_dt_utc.hour

                cell = projection_cells.get((lookup_day, lookup_hour))
                cell_text = self._projection_cell_text(cell)
                item = QTableWidgetItem(cell_text)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if cell is not None:
                    item.setData(Qt.UserRole, cell)
                    tooltip = self._projection_cell_tooltip(cell)
                    if tooltip:
                        item.setToolTip(tooltip)
                if cell and cell.effective_source == "HF" and cell.band:
                    primary_band = cell.band.split("/")[0].strip()
                    colors = self._band_cell_colors(primary_band, theme)
                    if colors:
                        item.setBackground(qcolor(colors["bg"]))
                        item.setForeground(qcolor(colors["fg"]))
                if cell and cell.net_segments and (
                    cell.start_utc <= now_utc <= cell.end_utc or now_utc <= cell.start_utc <= now_plus_24
                ):
                    item.setBackground(qcolor(theme["surface_alt"]))
                self.table.setItem(hour, col, item)

        self._visible_bands = sorted(visible_bands)
        self._render_band_legend()

    def _input_retry_ready(self) -> bool:
        return (not getattr(self, "_input_missing_source", False)
                and getattr(self, "_input_retry_clock", time.monotonic)() >= getattr(self, "_input_retry_after", 0.0))

    def _show_plan_input_failure(self) -> None:
        self.table.setEnabled(False)
        for name in ("save_plan_btn", "save_sop_plan_btn", "inline_update_plan_btn",
                     "inline_update_hf_daily_btn", "edit_hf_daily_btn", "edit_hf_net_btn", "edit_sop_plan_entry_btn"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(False)
        detail = str(getattr(self, "_input_read_error", ""))
        self.frequency_plan_action_hint_label.setText(
            "Plan Builder inputs unavailable; previous rows retained; entered work retained. "
            "Unable to verify current inputs. Repair the source or choose Retry inputs. "
            + ("Missing source. " if getattr(self, "_input_missing_source", False) else "")
            + (detail[:140] + "…" if len(detail) > 140 else detail))
        self.frequency_plan_action_hint_label.setToolTip(detail)
        button = getattr(self, "retry_plan_inputs_btn", None)
        if button is not None:
            button.show()

    def _handle_plan_input_failure(self, exc: Exception) -> None:
        self._projection_latest = None
        self._projection_request_id = getattr(self, "_projection_request_id", 0) + 1
        self._projection_pending = False
        self._revision_key = None
        self._input_failure_generation = getattr(self, "_input_failure_generation", 0) + 1
        now = getattr(self, "_input_retry_clock", time.monotonic)()
        repeated = bool(getattr(self, "_input_read_error", "")) and now < getattr(self, "_input_retry_after", 0.0)
        self._input_read_error = str(exc) or type(exc).__name__
        self._input_missing_source = isinstance(exc, MissingScheduleSourceError)
        if not repeated:
            self._input_retry_attempt = getattr(self, "_input_retry_attempt", 0) + 1
            delay = (2.0, 4.0, 8.0, 30.0)[min(self._input_retry_attempt - 1, 3)]
            self._input_retry_after = now + delay
        self._show_plan_input_failure()
        log.warning("Plan Builder inputs unavailable: %s", exc)

    def _clear_plan_input_failure(self) -> bool:
        # Re-evaluate dependent actions before acknowledging recovery. Keep the
        # failed-attempt count until every lookup has succeeded.
        generation = getattr(self, "_input_failure_generation", 0)
        self._input_read_error = ""
        try:
            self._update_plan_action_styles()
            self._update_sop_plan_action_state()
        except Exception as exc:
            self._handle_plan_input_failure(exc)
            return False
        if generation != getattr(self, "_input_failure_generation", 0):
            self._show_plan_input_failure()
            return False
        self._input_missing_source = False
        self._input_retry_attempt = 0
        self._input_retry_after = 0.0
        if self._catalog_review_required:
            self.review_plan_inputs_btn.show()
            self.frequency_plan_action_hint_label.setText("Inputs changed; entered work retained. Choose Review changed inputs before saving.")
        button = getattr(self, "retry_plan_inputs_btn", None)
        if button is not None:
            button.hide()
        return True

    def _retry_plan_inputs(self) -> None:
        if getattr(self, "_revision_closed", False) or not getattr(self, "_tab_active", True):
            return
        self._input_retry_after = 0.0
        self._input_missing_source = False
        self._input_retry_attempt = 0
        self._request_input_catalog(force=True)

    def rebuild_table(self):
        """
        Recompute the table based on current hf_schedule and net_schedule in config.
        """
        if getattr(self, "_revision_closed", False) or not getattr(self, "_tab_active", True):
            return
        if getattr(self, "_input_read_error", "") and not self._input_retry_ready():
            return
        perf_start = time.perf_counter()
        failure_generation = getattr(self, "_input_failure_generation", 0)
        try:
            if self._catalog_model is None:
                if not self._catalog_pending:
                    self._request_input_catalog()
                return
            hf_sched, net_sched, sop_sched, policy_rows = self._load_schedules()
        except MissingScheduleSourceError as exc:
            # Metadata loaded coherently: name the vanished dependency in its
            # selector while retaining and disabling the previous projection.
            try:
                self._refresh_source_set_controls()
            except Exception:
                log.debug("Cannot refresh missing source labels", exc_info=True)
            self._handle_plan_input_failure(exc)
            return
        except Exception as exc:
            self._handle_plan_input_failure(exc)
            return
        try:
            # Preserve an entered plan name; catalogue refresh is not consent to
            # replace an authoring draft or its chosen source layers.
            draft_name, retain_name = getattr(self, "_catalog_name_retention", (
                self.frequency_plan_combo.currentText(), self._has_authoring_draft()))
            self.__dict__.pop("_catalog_name_retention", None)
            self._refresh_source_set_controls()
            self.plan_context_label.refresh_context(refresh=True)
            self._refresh_plan_workspace_header()
            if failure_generation != getattr(self, "_input_failure_generation", 0):
                return
            if draft_name is not None and retain_name:
                self.frequency_plan_combo.blockSignals(True)
                self.frequency_plan_combo.setEditText(draft_name)
                self.frequency_plan_combo.blockSignals(False)
            self._update_sop_plan_action_state()
            theme = resolve_theme(self.settings)
            snapshot = self._snapshot(hf_sched, net_sched, sop_sched, policy_rows)
        except Exception as exc:
            self._handle_plan_input_failure(exc)
            return
        if failure_generation != getattr(self, "_input_failure_generation", 0):
            return
        self._last_snapshot = snapshot
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        week_sunday = self._week_start_sunday_utc(now_utc)
        mode = self._planner_view_mode()
        if mode not in {"radio", "operational", "effective", "patterns"}:
            mode = "shared"
        self.table.setEnabled(False)
        for name in ("edit_hf_daily_btn", "edit_hf_net_btn", "inline_update_plan_btn", "inline_update_hf_daily_btn", "edit_sop_plan_entry_btn"):
            getattr(self, name).setEnabled(False)
        self.frequency_plan_action_hint_label.setText(
            "Updating Plan Builder view — previous rows shown until ready."
            if self.table.rowCount() else "Building Plan Builder view..."
        )
        try:
            self._start_projection_worker(
                mode=mode,
                snapshot=snapshot,
                hf_sched=hf_sched,
                net_sched=net_sched,
                sop_sched=sop_sched,
                policy_rows=policy_rows,
                week_sunday=week_sunday,
                started_at=perf_start,
            )
        except Exception as exc:
            self._handle_plan_input_failure(exc)
        return

    def _snapshot(self, hf_sched, net_sched, sop_sched, policy_rows):
        model = self._catalog_model
        state = {
            "catalog": (model.catalog.revision, model.catalog.publication) if model else None,
            "week": self._week_start_sunday_utc(datetime.datetime.now(datetime.timezone.utc)).isoformat(),
            "timezone": self.settings.get("timezone", "UTC"),
            "view": self._planner_view_mode(), "day": self._selected_operational_day(),
            "local": self._show_local, "bands": self._show_band,
            "plan": self.frequency_plan_combo.currentData(),
            "daily": self.hf_daily_source_combo.currentData(),
            "net": self.hf_net_source_combo.currentData(),
            "sop": self.sop_plan_source_combo.currentData(),
            "radio": self.radio_window_radio_combo.currentData(),
        }
        return json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))


    def _read_configuration_revision(self, config_dir):
        return self._catalog_worker.probe()


    def _close_revision_tracker(self):
        self._catalog_worker.close()


    def _poll_configuration_revision(self):
        if self._revision_closed or not getattr(self, "_tab_active", True) or self._revision_future is None:
            return
        future = self._revision_future
        if not future.done():
            QTimer.singleShot(50, self._poll_configuration_revision)
            return
        self._revision_future = None
        try:
            revision = future.result()
        except Exception as exc:
            self._probe_retry_attempt += 1
            delay = (2.0, 4.0, 8.0, 30.0)[min(self._probe_retry_attempt - 1, 3)]
            self._probe_retry_after = self._input_retry_clock() + delay
            self._handle_plan_input_failure(ScheduleSourceError(f"Cannot verify current Plan Builder input revision: {exc}"))
            return
        self._probe_retry_after = 0.0
        self._probe_retry_attempt = 0
        observed = getattr(self, "_input_observed_revision", None)
        if observed != revision:
            self._input_observed_revision = revision
            self._input_retry_after = 0.0
            self._input_retry_attempt = 0
            self._input_missing_source = False
        if revision != self._revision_key and (not getattr(self, "_input_read_error", "") or self._input_retry_ready()):
            self._revision_key = revision
            # Retain the established authoring/validation path. Only recurring
            # unchanged checks bypass its full source reads and projection.
            self._request_input_catalog()

    def _maybe_rebuild_if_changed(self):
        if self._revision_closed or not getattr(self, "_tab_active", True) or self._projection_pending or self._catalog_pending:
            return
        current_context = (self._effective_guard_radio_id(), int(self._guided_plan_handoff_device_profile_id or 0))
        if self._published_guard_context is not None and (current_context != self._published_guard_context
                or self._published_runtime_status != self._host_runtime_status()):
            self._request_input_catalog()
            return
        if self._input_retry_clock() < self._probe_retry_after:
            return
        if self._revision_future is not None:
            self._poll_configuration_revision()
            return
        self._revision_future = self._revision_executor.submit(
            self._read_configuration_revision, self.settings.config_dir)
        QTimer.singleShot(0, self._poll_configuration_revision)

    def _ui_tz_abbr(self, tz_name: str, fallback: str) -> str:
        mapping = {
            "UTC": "UTC",
            "America/New_York": "ET",
            "America/Chicago": "CT",
            "America/Denver": "MT",
            "America/Los_Angeles": "PT",
            "Mountain Standard Time": "MST",
            "Central Standard Time": "CST",
            "Eastern Standard Time": "EST",
            "Pacific Standard Time": "PST",
        }
        return mapping.get(tz_name, fallback)

    def _update_clock_labels(self):
        """
        UTC from system clock; local time derived via Settings timezone + get_timezone(),
        with a UI label like ET / CT / MT / PT / UTC.
        """
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        utc_day = now_utc.strftime("%a")
        self.utc_label.setText(now_utc.strftime(f"<b>UTC ({utc_day}):</b> %y%m%d %H:%M:%S Z"))

        tz_name = self.settings.get("timezone", "UTC") or "UTC"
        tz = get_timezone(tz_name)
        now_local = now_utc.astimezone(tz)
        fallback = now_local.tzname() or tz_name
        abbr = self._ui_tz_abbr(tz_name, fallback)

        local_day = now_local.strftime("%a")
        self.local_label.setText(
            now_local.strftime(f"<b>Local ({local_day}):</b> %y%m%d %H:%M:%S {abbr}")
        )
        self.time_toggle_btn.setText("Times: Local" if self._show_local else "Times: UTC")
        self._update_toggle_button_styles()

    def _setup_clock_timer(self):
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._on_clock_tick)
        self._clock_timer.start(1000)

    def _on_clock_tick(self):
        self._update_clock_labels()
        now_ts = time.time()
        if now_ts - self._last_rebuild_check_ts >= 2.0:
            self._last_rebuild_check_ts = now_ts
            self._maybe_rebuild_if_changed()

    def set_tab_active(self, active: bool) -> None:
        self._tab_active = bool(active)
        if not active:
            self._catalog_generation += 1
            self._catalog_latest = None
            self._pending_catalog_focus = None
            self._pending_guided_handoff = None
            self._projection_latest = None
            self._projection_request_id += 1
            self._projection_pending = False
            # Retain ownership of an in-flight serial probe across navigation.
            # Activation polls it instead of queuing another blocked read.
        if active:
            if self._clock_timer is None:
                self._setup_clock_timer()
            elif not self._clock_timer.isActive():
                self._clock_timer.start(1000)
            self._update_clock_labels()
            self._maybe_rebuild_if_changed()
            return
        if self._clock_timer and self._clock_timer.isActive():
            self._clock_timer.stop()

    def _toggle_time_view(self):
        self._show_local = not self._show_local
        self._update_toggle_button_styles()
        self.rebuild_table()
        self._update_toggle_button_styles()

    def _toggle_band_view(self):
        self._show_band = not self._show_band
        self.band_toggle_btn.setText("Band/Freq: Band" if self._show_band else "Band/Freq: Freq")
        self._update_toggle_button_styles()
        self.rebuild_table()
        self._update_toggle_button_styles()

    def _load_band_colors(self) -> None:
        raw = self.settings.get("band_colors", {}) or {}
        self._band_colors = {}
        for k, v in raw.items():
            if not k or not v:
                continue
            self._band_colors[str(k).lower().strip()] = str(v).strip()

    def _default_band_colors(self) -> Dict[str, str]:
        theme = resolve_theme(self.settings)
        is_dark = theme.get("bg") == "#0F1216"
        palette = BAND_COLORS_DARK if is_dark else BAND_COLORS_LIGHT
        return {k.lower(): v for k, v in palette.items()}

    def _band_cell_colors(self, band: str, theme: Dict[str, str]) -> Dict[str, str] | None:
        band_key = (band or "").strip().lower()
        if not band_key:
            return None
        base = self._band_colors.get(band_key)
        if not base:
            return band_cell_colors(band_key, theme)
        alpha = 0.18 if theme.get("bg") == "#0F1216" else 0.28
        bg = self._blend_hex(base, theme.get("surface", "#F0F2F4"), alpha)
        fg = self._pick_text_color(bg, theme.get("text", "#1C1F21"), "#111111")
        return {"bg": bg, "fg": fg, "border": base}

    def _render_band_legend(self) -> None:
        while self.band_legend_layout.count():
            item = self.band_legend_layout.takeAt(0)
            if item.widget():
                if item.widget() is self.band_toggle_btn:
                    item.widget().setParent(None)
                else:
                    item.widget().deleteLater()

        theme = resolve_theme(self.settings)
        self._update_toggle_button_styles(theme=theme)

        if not self._visible_bands:
            empty = QLabel("Band colors: none")
            empty.setStyleSheet(f"color: {theme['text_muted']};")
            self.band_legend_layout.addWidget(empty)
            self.band_legend_layout.addStretch()
            return

        label = QLabel("Band colors:")
        label.setStyleSheet(f"color: {theme['text_muted']};")
        self.band_legend_layout.addWidget(label)

        for band in self._visible_bands:
            btn = QPushButton(band.upper())
            btn.setCursor(Qt.PointingHandCursor)
            btn.setProperty("band_key", band)
            btn.clicked.connect(self._on_band_color_clicked)
            btn.setStyleSheet(self._band_chip_style(band, theme))
            self.band_legend_layout.addWidget(btn)

        self.band_legend_layout.addStretch()

    def _band_chip_style(self, band: str, theme: Dict[str, str]) -> str:
        band_key = (band or "").strip().lower()
        base = self._band_colors.get(band_key) or self._default_band_colors().get(band_key)
        if not base:
            base = theme.get("surface_alt", "#DDE1E6")
        fg = self._pick_text_color(base, theme.get("text", "#1C1F21"), "#111111")
        return (
            "QPushButton {"
            f" background-color: {base}; color: {fg}; border: 1px solid {theme['border']};"
            " border-radius: 10px; padding: 2px 10px; font-weight: 600;"
            " }"
            " QPushButton:hover { opacity: 0.9; }"
        )

    def _on_band_color_clicked(self) -> None:
        btn = self.sender()
        if not isinstance(btn, QPushButton):
            return
        band_key = (btn.property("band_key") or "").strip().lower()
        if not band_key:
            return
        current = self._band_colors.get(band_key) or self._default_band_colors().get(band_key, "#CCCCCC")
        selected = self._pick_band_color(band_key, current)
        if not selected:
            return
        self._band_colors[band_key] = selected
        self.settings.set("band_colors", dict(self._band_colors))
        self.rebuild_table()

    def _reset_band_colors(self) -> None:
        self._band_colors = {}
        self.settings.set("band_colors", {})
        self.rebuild_table()

    def _pick_band_color(self, band_key: str, current: str) -> str | None:
        dialog = QColorDialog(qcolor(current), self)
        dialog.setOption(QColorDialog.DontUseNativeDialog, True)
        dialog.setWindowTitle(f"Select {band_key.upper()} Color")
        reset_btn = QPushButton("Reset Default")
        reset_btn.setAutoDefault(False)
        reset_btn.setDefault(False)
        layout = dialog.layout()
        if layout is not None:
            if hasattr(layout, "rowCount"):
                row = layout.rowCount()
                layout.addWidget(reset_btn, row, 0, 1, layout.columnCount())
            else:
                layout.addWidget(reset_btn)

        def reset_and_accept():
            default = self._default_band_colors().get(band_key, current)
            dialog.setCurrentColor(qcolor(default))
            dialog.done(QDialog.Accepted)

        reset_btn.clicked.connect(reset_and_accept)
        if dialog.exec() != QDialog.Accepted:
            return None
        color = dialog.currentColor()
        if not color.isValid():
            return None
        return color.name().upper()

    def _hex_to_rgb(self, value: str) -> tuple[int, int, int]:
        value = (value or "").lstrip("#")
        if len(value) != 6:
            return 0, 0, 0
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    def _rgb_to_hex(self, r: int, g: int, b: int) -> str:
        return f"#{r:02X}{g:02X}{b:02X}"

    def _blend_hex(self, fg: str, bg: str, alpha: float) -> str:
        fr, fg_c, fb = self._hex_to_rgb(fg)
        br, bg_c, bb = self._hex_to_rgb(bg)
        r = int(fr * alpha + br * (1 - alpha))
        g = int(fg_c * alpha + bg_c * (1 - alpha))
        b = int(fb * alpha + bb * (1 - alpha))
        return self._rgb_to_hex(r, g, b)

    def _luminance(self, value: str) -> float:
        r, g, b = self._hex_to_rgb(value)
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0

    def _pick_text_color(self, bg_hex: str, light: str, dark: str) -> str:
        return dark if self._luminance(bg_hex) > 0.6 else light

    def _apply_theme(self):
        theme = resolve_theme(self.settings)
        self._refresh_plan_builder_theme_styles(theme)
        self._update_toggle_button_styles(theme=theme)
        self._update_plan_action_styles(theme=theme)
        self._render_band_legend()
        self._refresh_font_layout()

    def _refresh_plan_builder_theme_styles(self, theme: Dict[str, str]) -> None:
        """Refresh Plan Builder cards and ingredient chips from the shared palette."""
        for name in ("plan_ingredients_frame", "selected_window_card", "inline_editor_card", "rf_guard_review_card"):
            frame = getattr(self, name, None)
            if frame is not None:
                frame.setStyleSheet(
                    f"QFrame#{frame.objectName()} {{ background: {theme['surface']}; "
                    f"border: 1px solid {theme['border']}; border-radius: 6px; }}"
                )
        for chip in (
            getattr(self, "plan_ingredient_plan", None),
            getattr(self, "plan_ingredient_daily", None),
            getattr(self, "plan_ingredient_nets", None),
            getattr(self, "plan_ingredient_sop", None),
            getattr(self, "plan_ingredient_assignments", None),
        ):
            if chip is not None:
                chip.setStyleSheet(
                    f"QLabel {{ color: {theme['text']}; border: 1px solid {theme['border']}; "
                    f"border-radius: 6px; padding: 4px 8px; background: {theme['surface_alt']}; }}"
                )
        for name in ("selected_window_title_label", "inline_editor_title_label", "rf_guard_review_title_label"):
            label = getattr(self, name, None)
            if label is not None:
                label.setStyleSheet(label_style("text", theme, weight=700))
        layers = getattr(self, "plan_layers_label", None)
        if layers is not None:
            layers.setStyleSheet(label_style("muted", theme))

    def _update_toggle_button_styles(self, theme: Dict[str, str] | None = None) -> None:
        if theme is None:
            theme = resolve_theme(self.settings)
        # Local + Band are defaults; highlight when user selects an alternate view.
        # Use explicit info styling so the active alternate state is clearly visible.
        time_role = "info" if not self._show_local else "muted"
        band_role = "info" if not self._show_band else "muted"
        self.time_toggle_btn.setStyleSheet(button_style(time_role, theme))
        if hasattr(self, "band_toggle_btn"):
            self.band_toggle_btn.setText("Band/Freq: Band" if self._show_band else "Band/Freq: Freq")
            self.band_toggle_btn.setStyleSheet(button_style(band_role, theme))

    def on_settings_saved(self):
        self._request_input_catalog(force=True)


    def on_condition_levels_changed(self):
        self._request_input_catalog(force=True)


    def apply_theme(self):
        self._apply_theme()

    # ------------- Qt events ------------- #

    def eventFilter(self, watched, event) -> bool:
        if (hasattr(self, "planner_scroll") and watched is self.planner_scroll.viewport()
                and event.type() == QEvent.Resize):
            self._queue_geometry_settle()
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.ApplicationFontChange:
            self._application_font_refresh = True
        if (event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange)
                and hasattr(self, "inline_mode_edit")
                and not getattr(self, "_font_layout_refresh_running", False)
                and not getattr(self, "_font_layout_refresh_pending", False)):
            self._font_layout_refresh_pending = True
            QTimer.singleShot(0, self._refresh_font_layout)

    def resizeEvent(self, event) -> None:  # pragma: no cover - geometry is covered by UI tests
        super().resizeEvent(event)
        self._apply_responsive_layout()
        self._queue_geometry_settle()
        self._update_toolbar_height_bounds()
        self._update_inline_editor_height_bounds()
        self._update_content_height_floor()

    def hideEvent(self, event):
        self.set_tab_active(False)
        super().hideEvent(event)

    def showEvent(self, event):
        """
        Rebuild the planner whenever the tab becomes visible, so changes from
        HF Schedule or Net Schedule are reflected immediately.
        """
        super().showEvent(event)
        self._tab_active = True
        try:
            if not self._catalog_pending:
                self._request_input_catalog()
        except Exception as e:
            log.error("Failed to rebuild FreqPlanner on showEvent: %s", e)
