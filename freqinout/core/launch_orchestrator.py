from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from freqinout.core.logger import log
from freqinout.core.custom_rigctld import parse_custom_rigctld
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.software_status_service import SoftwareStatusService
from freqinout.core.dependency_status_service import (
    LEGACY_PRIMARY_DEPENDENCY_SCOPE,
    get_dependency_status_service,
)
from freqinout.core.config_varac_managed import parse_varac_ini_bytes
from freqinout.core.launch_bundle_store import LaunchBundleStore, normalize_launch_items
from freqinout.core.js8_storage import resolve_js8_storage, variant_family_from_version
from freqinout.core.guided_launch_recipes import managed_instance_window_title
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.managed_directory_contract import (
    managed_directories_from_component,
    materialize_managed_directories,
)
from freqinout.core.process_window_title import set_process_window_title
from freqinout.core.station_launch_planner import LaunchPlan, StationLaunchPlanner
from freqinout.core.varac_launch_recipe import legacy_varac_structured_launch
from freqinout.core.hardware_lifecycle import (
    existing_hardware_lifecycle_service,
    shared_hardware_lifecycle_service,
)
from freqinout.core.hardware_projection import (
    apply_flrig_fltk_serial_projection,
    build_hardware_launch_projection,
)


LAUNCH_APP_ORDER: List[str] = [
    "FLRig",
    "FLDigi",
    "FLAmp",
    "FLMsg",
    "VarAC",
    "JS8Call",
    "JS8Spotter",
    "CommStat",
]

# Canonical companion components that belong in the radio bundle but are not
# independent Launch Control rows.  They must survive catalog reconciliation;
# their owning family controls whether they are launched and displayed.
INTERNAL_LAUNCH_COMPONENT_NAMES = frozenset({"VARA", "SDR++"})

JS8_DEPENDENT_APPS = {"JS8Spotter", "CommStat"}
DEFAULT_VARAC_SETTLE_DELAY_SEC = 12.0
DEFAULT_JS8CALL_DEPENDENT_DELAY_SEC = 4.0
DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC = 90
LEGACY_LAUNCH_READINESS_TIMEOUT_SEC = 30
DEFAULT_PROCESS_LAUNCH_STABILITY_SEC = 3.0
LAUNCH_READINESS_INITIAL_POLL_MS = 2000
LAUNCH_READINESS_RELAXED_POLL_MS = 5000
LAUNCH_READINESS_RELAX_AFTER_SEC = 30.0
LAUNCH_PROCESS_PREFLIGHT_TIMEOUT_SEC = 15.0
LAUNCH_ENDPOINT_PREFLIGHT_TIMEOUT_SEC = 15.0
LAUNCH_ENDPOINT_PREFLIGHT_POLL_MS = 250
LAUNCH_RADIO_CONTROL_READY_TIMEOUT_SEC = 30.0
LAUNCH_RADIO_CONTROL_PROBE_INTERVAL_SEC = 1.0
LAUNCH_SCHEDULE_HANDOFF_TIMEOUT_SEC = 30.0
LAUNCH_PROCESS_REAPER_INTERVAL_MS = 1000
HARDWARE_SNAPSHOT_MAX_AGE_SEC = 315.0


LAUNCH_APP_META: Dict[str, Dict[str, Any]] = {
    "FLRig": {
        "path_key": "path_flrig",
        "legacy_autostart_key": "autostart_flrig",
        "fallback_cmds": ["flrig", "FLRig"],
        "folder_candidates": ["flrig.exe", "FLRig.exe", "flrig", "FLRig"],
    },
    "FLDigi": {
        "path_key": "path_fldigi",
        "legacy_autostart_key": "autostart_fldigi",
        "fallback_cmds": ["fldigi", "FLDigi"],
        "folder_candidates": ["fldigi.exe", "FLDigi.exe", "fldigi", "FLDigi"],
    },
    "FLAmp": {
        "path_key": "path_flamp",
        "legacy_autostart_key": "autostart_flamp",
        "fallback_cmds": ["flamp", "FLAmp"],
        "folder_candidates": ["flamp.exe", "FLAmp.exe", "flamp", "FLAmp"],
    },
    "FLMsg": {
        "path_key": "path_flmsg",
        "legacy_autostart_key": "autostart_flmsg",
        "fallback_cmds": ["flmsg", "FLMsg"],
        "folder_candidates": ["flmsg.exe", "FLMsg.exe", "flmsg", "FLMsg"],
    },
    "VarAC": {
        "path_key": "varac_path",
        "launch_cmd_key": "varac_launch_cmd",
        "legacy_autostart_key": None,
        "fallback_cmds": ["VarAC", "varac"],
        "folder_candidates": ["VarAC.exe", "varac.exe", "VarAC", "varac"],
    },
    "JS8Call": {
        "path_key": "path_js8call",
        "legacy_autostart_key": "autostart_js8call",
        "fallback_cmds": ["js8call", "JS8Call", "JS8Call-improved", "js8call-improved", "js8call-subspace", "subspace"],
        "folder_candidates": [
            "JS8Call.exe",
            "js8call.exe",
            "JS8Call-improved.exe",
            "js8call-improved.exe",
            "js8call-subspace.exe",
            "subspace.exe",
            "JS8Call",
            "js8call",
            "JS8Call-improved",
            "js8call-improved",
            "js8call-subspace",
            "subspace",
        ],
    },
    "JS8Spotter": {
        "path_key": "path_js8spotter",
        "legacy_autostart_key": None,
        "fallback_cmds": ["js8spotter", "JS8Spotter"],
        "folder_candidates": ["js8spotter.exe", "JS8Spotter.exe", "js8spotter.py", "js8spotter", "JS8Spotter"],
    },
    "CommStat": {
        "path_key": "path_commstat",
        "legacy_autostart_key": None,
        "fallback_cmds": ["commstat", "CommStat"],
        "folder_candidates": ["commstat.exe", "CommStat.exe", "commstat.py", "commstat", "CommStat"],
    },
    # Receiver-only setup uses a per-radio path/command override.  This entry
    # intentionally stays out of ``LAUNCH_APP_ORDER`` so conventional radio
    # launch configuration does not gain an unrelated SDR application.
    "SDR++": {
        "fallback_cmds": ["sdrpp", "SDR++"],
        "folder_candidates": ["sdrpp.exe", "SDR++.exe", "sdrpp", "SDR++"],
    },
}


class LaunchOrchestrator(QObject):
    sequence_started = Signal(object)
    sequence_progress = Signal(object)
    sequence_finished = Signal(object)

    @staticmethod
    def read_status_recipe_items(db_path, settings_data, radio_id, items, hardware_lifecycle=None):
        """Worker-only read preview, with no GUI-owned QObject or DB connection.

        Initialize only preview dependencies, not the launch sequencer's timers,
        shared dependency probes or lifecycle subscriptions. The independent
        QObject is created and released on this reader's thread. Existing stores
        open their own read connections; schema assurance belongs to startup.
        """
        class CachedSettings:
            def get(self, key, default=None):
                return settings_data.get(key, default)

            def all(self):
                return dict(settings_data)

        preview = LaunchOrchestrator.__new__(LaunchOrchestrator)
        QObject.__init__(preview)
        preview.settings = CachedSettings()
        preview.multi_radio_store = MultiRadioStore(db_path)
        preview.bundle_store = LaunchBundleStore.__new__(LaunchBundleStore)
        preview.bundle_store.db_path = db_path
        preview.planner = StationLaunchPlanner()
        preview.hardware_lifecycle = hardware_lifecycle
        preview._last_projection_warnings = {}
        plan = preview.preview_radio_recipe_plan(
            radio_id, bundle_override={"launch_enabled": True, "items": items},
        )
        return [instance.as_queue_item() for instance in plan.instances]

    def __init__(
        self,
        settings: SettingsManager,
        parent: QObject | None = None,
        *,
        bundle_store: LaunchBundleStore | None = None,
        multi_radio_store: MultiRadioStore | None = None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.status = SoftwareStatusService(settings)
        self.dependency_status = get_dependency_status_service(settings)
        self.bundle_store = bundle_store or LaunchBundleStore(settings.db_path)
        self.multi_radio_store = multi_radio_store or MultiRadioStore(settings.db_path)
        self.planner = StationLaunchPlanner()
        self.hardware_lifecycle = existing_hardware_lifecycle_service(self.multi_radio_store)
        self._runtime_launch_enabled_override: Optional[bool] = None
        self._runtime_launch_block_reason: str = ""
        self._last_projection_warnings: Dict[int, str] = {}
        self._sequence_projection_warnings: Dict[int, str] = {}
        self._active = False
        self._cancel_requested = False
        self._trigger = ""
        self._queue: List[Any] = []
        self._index = 0
        self._results: List[Dict[str, Any]] = []
        self._current_name: Optional[str] = None
        self._current_item: Any = None
        self._current_cmd: Optional[List[str]] = None
        self._current_process: Any = None
        self._current_launch_description = ""
        self._current_started_monotonic = 0.0
        self._current_phase = ""
        self._hardware_preflight_requested: Dict[str, int] = {}
        self._hardware_preflight_verified: set[str] = set()
        self._endpoint_preflight_verified: set[str] = set()
        self._endpoint_preflight_requested: set[str] = set()
        self._endpoint_preflight_clear: set[str] = set()
        self._sequence_claimed_identities: set[str] = set()
        self._radio_control_last_evidence: Dict[int, Dict[str, Any]] = {}
        self._radio_control_gate_states: Dict[int, str] = {}
        self._radio_control_probe_due_monotonic: Dict[int, float] = {}
        self._sequence_attribution_candidates: tuple[Mapping[str, Any], ...] = ()
        self._sequence_process_records: tuple[Mapping[str, object], ...] = ()
        self._sequence_process_records_ready = False
        self._sequence_preflight_started_wall = 0.0
        self._process_preflight_pending = False
        self._process_preflight_baseline_sequence = 0
        self._process_preflight_reason = ""
        self._process_preflight_generation = 0
        self._wait_timeout_sec = DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(LAUNCH_READINESS_INITIAL_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_current_readiness)
        self._launched_processes: Dict[int, Any] = {}
        self._launched_process_labels: Dict[int, str] = {}
        self._process_reaper_timer = QTimer(self)
        self._process_reaper_timer.setInterval(LAUNCH_PROCESS_REAPER_INTERVAL_MS)
        self._process_reaper_timer.timeout.connect(self._reap_launched_processes)
        try:
            self.dependency_status.snapshot_changed.connect(
                self._on_launch_preflight_snapshot_changed
            )
        except Exception:
            pass

    @staticmethod
    def is_truthy(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return val != 0
        if isinstance(val, str):
            return val.strip().lower() in {"1", "true", "yes", "on"}
        return False

    def set_runtime_launch_enabled(self, enabled: Optional[bool], *, reason: str = "") -> None:
        self._runtime_launch_enabled_override = None if enabled is None else bool(enabled)
        self._runtime_launch_block_reason = str(reason or "").strip()

    def launch_allowed(self) -> bool:
        override = self._runtime_launch_enabled_override
        return True if override is None else bool(override)

    def launch_block_reason(self) -> str:
        return self._runtime_launch_block_reason

    def projection_warning_detail(self, radio_profile_id: Optional[int] = None) -> str:
        warnings = getattr(self, "_last_projection_warnings", {})
        if radio_profile_id is not None:
            return str(warnings.get(int(radio_profile_id), "") or "")
        return "; ".join(str(value) for value in warnings.values() if str(value).strip())

    @staticmethod
    def normalize_custom_tools(raw_items: Any) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        seen: set[str] = set()
        builtin_names = {name.lower() for name in LAUNCH_APP_ORDER}
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            command = str(item.get("command", "")).strip()
            key = name.lower()
            if not name or not command:
                continue
            if key in builtin_names or key in seen:
                continue
            seen.add(key)
            normalized.append({"name": name, "command": command})
        return normalized

    def get_custom_tools(self) -> List[Dict[str, str]]:
        return self.normalize_custom_tools(self.settings.get("custom_tool_items", []))

    @staticmethod
    def format_script_launch_command(path: str) -> str:
        """Return a shell-safe command string for a picker-selected script.

        Custom tools retain their historical command-string storage format, but
        picker-created entries must survive spaces and quoting in paths.  Batch
        files also need ``cmd.exe`` on Windows because Launch Control uses
        ``shell=False`` for every process spawn.
        """
        script = Path(os.path.expanduser(os.path.expandvars(str(path or "").strip())))
        script_text = str(script)
        suffix = script.suffix.casefold()
        if suffix == ".py":
            executable = "python" if platform.system() == "Windows" else "python3"
            parts = [executable, script_text]
        elif suffix == ".bash":
            parts = ["bash", script_text]
        elif suffix == ".sh":
            parts = [script_text] if script.is_file() and os.access(script, os.X_OK) else ["bash", script_text]
        elif suffix == ".ps1":
            if platform.system() == "Windows":
                parts = ["powershell.exe", "-NoProfile", "-File", script_text]
            else:
                parts = ["pwsh", "-NoProfile", "-File", script_text]
        elif platform.system() == "Windows" and suffix in {".bat", ".cmd"}:
            parts = ["cmd.exe", "/d", "/c", f'"{script_text}"']
        else:
            parts = [script_text]
        if platform.system() == "Windows":
            return subprocess.list2cmdline(parts)
        return shlex.join(parts)

    def launch_catalog_order(self, custom_tools: Optional[List[Dict[str, Any]]] = None) -> List[str]:
        names = list(LAUNCH_APP_ORDER)
        for item in self.normalize_custom_tools(custom_tools if custom_tools is not None else self.get_custom_tools()):
            name = str(item.get("name", "")).strip()
            if name and name not in names:
                names.append(name)
        return names

    def _custom_tool_command(self, name: str, custom_tools: Optional[List[Dict[str, Any]]] = None) -> str:
        target = str(name or "").strip()
        if not target:
            return ""
        for item in self.normalize_custom_tools(custom_tools if custom_tools is not None else self.get_custom_tools()):
            if str(item.get("name", "")).strip() == target:
                return str(item.get("command", "")).strip()
        return ""

    def build_default_items(
        self,
        existing: Optional[List[Dict[str, Any]]] = None,
        *,
        custom_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        existing_map: Dict[str, Dict[str, Any]] = {}
        existing_order: List[str] = []
        catalog = self.launch_catalog_order(custom_tools)
        for item in existing or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if name:
                existing_map[name] = item
                existing_order.append(name)
        ordered_names: List[str] = []
        seen: set[str] = set()
        for name in existing_order:
            if (name not in catalog and name not in INTERNAL_LAUNCH_COMPONENT_NAMES) or name in seen:
                continue
            ordered_names.append(name)
            seen.add(name)
        for name in catalog:
            if name in seen:
                continue
            ordered_names.append(name)
            seen.add(name)
        out: List[Dict[str, Any]] = []
        for name in ordered_names:
            prev = existing_map.get(name)
            if prev is not None:
                # Catalog reconciliation is not a launch-recipe migration.  In
                # particular, adding or reordering a custom tool must not erase
                # the instance selector, arguments, working directory,
                # dependencies, or readiness policy that make this row belong
                # to one radio.  Keep the complete radio-owned row verbatim.
                out.append(dict(prev))
                continue
            default_startup = False
            legacy_key = LAUNCH_APP_META.get(name, {}).get("legacy_autostart_key")
            if legacy_key:
                default_startup = self.is_truthy(self.settings.get(str(legacy_key), False))
            normalized_item = {
                "name": name,
                "instance_key": name,
                "enabled": True,
                "startup": bool(default_startup),
                "monitor_health": True,
            }
            custom_command = self._custom_tool_command(name, custom_tools)
            if custom_command:
                # Snapshot the definition into the radio bundle.  Subsequent
                # edits to a station catalog entry cannot silently retarget a
                # different radio's saved custom-tool assignment.
                normalized_item["launch_command_override"] = custom_command
            out.append(normalized_item)
        return out

    def get_launch_items(self) -> List[Dict[str, Any]]:
        raw = self.settings.get("launch_control_items", [])
        if not isinstance(raw, list):
            raw = []
        normalized = self.build_default_items(raw, custom_tools=self.get_custom_tools())
        return normalized

    def _restore_canonical_launch_items(
        self,
        radio_profile_id: int,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Restore recipe fields lost by an older Launch Control catalog edit.

        The canonical GRS-13 identity is the authority for executable identity,
        arguments, working directory, dependencies, and readiness.  Operator
        choices (enabled/startup/monitor) remain owned by the radio launch row.
        This is an in-memory projection: normal Settings Save persists it.
        """

        restored = [dict(item) for item in items if isinstance(item, dict)]
        try:
            generation = self.multi_radio_store.radio_software_identity_generation(
                int(radio_profile_id)
            )
            if generation <= 0:
                return self._apply_managed_cluster_vara_launch_policy(
                    int(radio_profile_id),
                    self._restore_legacy_varac_launch_item(
                        int(radio_profile_id), restored
                    ),
                )
            records = self.multi_radio_store.list_radio_software_identity_records(
                int(radio_profile_id)
            )
        except Exception:
            return self._apply_managed_cluster_vara_launch_policy(
                int(radio_profile_id),
                restored,
            )
        if not records:
            return self._apply_managed_cluster_vara_launch_policy(
                int(radio_profile_id),
                self._restore_legacy_varac_launch_item(
                    int(radio_profile_id), restored
                ),
            )
        if not any(record.family_key == "varac" for record in records):
            restored = self._restore_legacy_varac_launch_item(
                int(radio_profile_id), restored
            )

        consumed: set[int] = set()
        removed: set[int] = set()
        component_names = {
            "flrig": "FLRig",
            "fldigi": "FLDigi",
            "flmsg": "FLMsg",
            "flamp": "FLAmp",
            "js8call": "JS8Call",
            "varac": "VarAC",
            "vara": "VARA",
            "external-js8spotter": "JS8Spotter",
            "commstat": "CommStat",
            "sdrpp": "SDR++",
        }

        for record in records:
            recipe_components: Dict[str, Mapping[str, Any]] = {}
            try:
                manifest = (
                    self.multi_radio_store.get_software_instance_manifest(record.bundle_id)
                    or {}
                )
                evidence = manifest.get("evidence", {})
                evidence = evidence if isinstance(evidence, Mapping) else {}
                recipe = evidence.get("launch_recipe", {})
                recipe = recipe if isinstance(recipe, Mapping) else {}
                for raw in recipe.get("components", ()) or ():
                    if not isinstance(raw, Mapping):
                        continue
                    key = (
                        str(raw.get("component_key", "") or "")
                        .strip()
                        .casefold()
                        .replace("_", "-")
                    )
                    if key:
                        recipe_components[key] = raw
            except Exception:
                recipe_components = {}

            cross_family = record.launch.get("cross_family_dependencies", {})
            cross_family = cross_family if isinstance(cross_family, Mapping) else {}
            for component in record.components:
                component_id = str(component.component_id or "").strip()
                component_key = component_id.casefold().replace("_", "-")
                component_kind = component_key.split(":", 1)[0]
                app_name = component_names.get(component_kind)
                argv = [str(value) for value in component.argv]
                if not app_name or not argv or not argv[0].strip():
                    continue
                recipe_component = (
                    recipe_components.get(component_key)
                    or recipe_components.get(component_kind)
                    or {}
                )
                component_scope = str(
                    recipe_component.get("execution_scope", record.scope or "standard") or "standard"
                ).strip().lower()
                expected_key = (
                    f"fast-light:station-shared:{component_key}"
                    if record.family_key == "fast_light"
                    and component_scope == "station_shared_utility"
                    else f"{record.bundle_id}:{component_key}"
                )
                exact_matches = [
                    index for index, row in enumerate(restored)
                    if index not in consumed
                    and str(row.get("instance_key", row.get("name", "")) or "")
                    .strip()
                    .casefold()
                    == expected_key.casefold()
                ]
                name_matches = [
                    index for index, row in enumerate(restored)
                    if index not in consumed
                    and str(row.get("name", "") or "").strip().casefold() == app_name.casefold()
                ]
                matches = exact_matches or name_matches
                selected_index = matches[0] if matches else len(restored)
                base = dict(restored[selected_index]) if matches else {}
                if matches:
                    consumed.add(selected_index)
                    # Older damaged bundles can contain both a canonical row
                    # and a name-only duplicate.  Collapse only duplicates for
                    # this same built-in component.
                    removed.update(index for index in name_matches if index != selected_index)

                readiness = base.get("readiness_policy", {})
                readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
                recipe_readiness = recipe_component.get("readiness", {})
                if isinstance(recipe_readiness, Mapping):
                    readiness.update(recipe_readiness)
                readiness.update(dict(component.readiness))
                readiness.update(
                    {
                        "launch_arguments": argv[1:],
                        "working_directory": str(component.cwd or ""),
                        "environment": {
                            str(key): str(value) for key, value in component.env.items()
                        },
                    }
                )
                for key in (
                    "effective_command",
                    "effective_command_text",
                    "profile_selector",
                    "configuration_roots",
                    "data_roots",
                    "managed_directories",
                    "endpoints",
                    "evidence",
                    "confidence",
                    "execution_scope",
                    "operator_starts",
                    "structured_launch",
                ):
                    if key in recipe_component:
                        value = recipe_component.get(key)
                        readiness[key] = (
                            dict(value)
                            if isinstance(value, Mapping)
                            else list(value)
                            if isinstance(value, tuple)
                            else value
                        )
                if "executable" in recipe_component or record.family_key == "varac":
                    readiness["executable"] = argv[0]

                if 'operator_recipe_v1' in readiness:
                    readiness['operator_recipe_v1'] = self.is_truthy(readiness['operator_recipe_v1'])
                dependencies = [str(value) for value in component.dependencies if str(value)]
                cross_items = cross_family.get(component.component_id, ())
                if isinstance(cross_items, (tuple, list)):
                    dependencies.extend(
                        str(value.get("family_key", "") or "").strip()
                        for value in cross_items
                        if isinstance(value, Mapping) and str(value.get("family_key", "") or "").strip()
                    )
                launch = component.launch if isinstance(component.launch, Mapping) else {}
                canonical = {
                    **base,
                    "name": app_name,
                    "instance_key": expected_key,
                    "enabled": bool(base.get("enabled", True)),
                    "startup": bool(base.get("startup", launch.get("at_startup", False))),
                    "monitor_health": bool(
                        base.get("monitor_health", launch.get("monitor_health", True))
                    ),
                    "launch_path_override": argv[0],
                    "launch_command_override": "",
                    "dependencies": list(dict.fromkeys(dependencies)),
                    "readiness_policy": readiness,
                    "execution_scope": str(
                        readiness.get("execution_scope", record.scope or "standard")
                        or "standard"
                    ),
                }
                if matches:
                    restored[selected_index] = canonical
                else:
                    restored.append(canonical)
                    consumed.add(selected_index)

        retained = [row for index, row in enumerate(restored) if index not in removed]
        return self._apply_managed_cluster_vara_launch_policy(
            int(radio_profile_id),
            retained,
        )

    def _apply_managed_cluster_vara_launch_policy(
        self,
        radio_profile_id: int,
        items: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Keep managed cluster VARA as parent-owned runtime evidence.

        Older saved recipes independently started VARA before VarAC.  A
        managed cluster INI now requires VarAC to launch its own node-local
        modem, so recovery must suppress only that hidden VARA row and remove
        the obsolete dependency without changing operator choices for any
        visible application.
        """

        rows = [dict(item) for item in items if isinstance(item, Mapping)]
        try:
            profile = self.multi_radio_store.get_device_profile(int(radio_profile_id)) or {}
            if int(profile.get("varac_cluster_member_enabled", 0) or 0) != 1:
                return rows
            node_id = int(profile.get("varac_node_id", 0) or 0)
            node = self.multi_radio_store.get_varac_node(node_id) if node_id > 0 else None
            if not isinstance(node, Mapping):
                return rows
            if str(node.get("native_management_state", "operator") or "operator").strip().casefold() != "managed":
                return rows
            ini_path = Path(str(node.get("ini_path") or "")).expanduser()
            if not ini_path.is_file() or ini_path.is_symlink():
                return rows
            source = parse_varac_ini_bytes(ini_path, ini_path.read_bytes())
            launch_on_connect = ""
            for section_name, section_values in source.values.items():
                if str(section_name).strip().casefold() != "varahf_config":
                    continue
                for field_name, value in section_values.items():
                    if (
                        str(field_name).strip().casefold()
                        == "varahflaunchonmodemconnect"
                    ):
                        launch_on_connect = str(value or "").strip().casefold()
                        break
            if launch_on_connect != "on":
                return rows
        except Exception:
            return rows

        for row in rows:
            name = str(row.get("name", "") or "").strip().casefold()
            if name == "vara":
                readiness = row.get("readiness_policy", {})
                readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
                readiness.update(
                    {
                        "parent_managed": True,
                        "launch_authority": "VarAC",
                    }
                )
                row["startup"] = False
                row["readiness_policy"] = readiness
            elif name == "varac":
                dependencies = row.get("dependencies", ())
                if not isinstance(dependencies, (list, tuple)):
                    dependencies = ()
                row["dependencies"] = [
                    str(value)
                    for value in dependencies
                    if str(value).strip().casefold() != "vara"
                ]
        return rows

    def _restore_legacy_varac_launch_item(
        self,
        radio_profile_id: int,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Recover a legacy VarAC row without parsing its shell command.

        Older radio bundles stored ``wine ... C:\\VarAC\\VarAC.ini`` as one
        command string.  POSIX shell parsing consumes those backslashes.  The
        linked VarAC node already owns the exact executable and INI identity,
        so project it into the existing structured readiness seam while
        retaining the operator's enabled/startup/monitor choices.
        """

        restored = [dict(item) for item in items if isinstance(item, dict)]
        try:
            profile = self.multi_radio_store.get_device_profile(int(radio_profile_id))
            node_id = int((profile or {}).get("varac_node_id") or 0)
            node = self.multi_radio_store.get_varac_node(node_id) if node_id > 0 else None
            recipe = (
                legacy_varac_structured_launch(
                    node,
                    platform_name=platform.system(),
                )
                if isinstance(node, Mapping)
                else None
            )
        except Exception:
            return restored
        if not isinstance(recipe, Mapping):
            return restored

        matches = [
            index
            for index, row in enumerate(restored)
            if str(row.get("name", "") or "").strip().casefold() == "varac"
        ]
        if not matches:
            return restored
        selected = matches[0]
        base = dict(restored[selected])
        readiness = base.get("readiness_policy", {})
        readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
        readiness.update(
            {
                "structured_launch": True,
                "executable": str(recipe.get("executable") or ""),
                "launch_arguments": list(recipe.get("launch_arguments") or ()),
                "working_directory": str(recipe.get("working_directory") or ""),
                "environment": dict(recipe.get("environment") or {}),
                "legacy_identity_recovered": True,
            }
        )
        base.update(
            {
                "launch_path_override": str(recipe.get("executable") or ""),
                "launch_command_override": "",
                "readiness_policy": readiness,
            }
        )
        restored[selected] = base
        return restored

    def get_radio_launch_bundle(self, radio_profile_id: int) -> Dict[str, Any]:
        bundle = self.bundle_store.get_bundle(
            int(radio_profile_id),
            legacy_items=self.settings.get("launch_control_items", []),
        )
        raw_items = bundle.get("items", []) if isinstance(bundle, Mapping) else []
        items = [dict(item) for item in raw_items if isinstance(item, dict)]
        restored = self._restore_canonical_launch_items(int(radio_profile_id), items)
        if restored != items:
            bundle = dict(bundle)
            bundle["items"] = restored
            bundle["canonical_recovery"] = True
        return bundle

    def _restore_canonical_bundle_override(
        self,
        radio_profile_id: int,
        bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Apply canonical recipe recovery to an in-memory Launch Control draft.

        Row-level Start and the unsaved Settings draft intentionally pass a
        bundle override to the planner.  That override must receive the same
        immutable recipe recovery as the saved bundle; otherwise a pre-update
        FLAmp/FLMsg row can lose its radio selector and collapse back to
        executable-only process matching.  Operator-owned checkboxes remain
        untouched by :meth:`_restore_canonical_launch_items`.
        """

        restored_bundle = dict(bundle)
        raw_items = restored_bundle.get("items", [])
        items = [dict(item) for item in raw_items if isinstance(item, Mapping)]
        original_names = {
            str(item.get("name", "") or "").strip().casefold()
            for item in items
            if str(item.get("name", "") or "").strip()
        }
        restored = self._restore_canonical_launch_items(int(radio_profile_id), items)
        # Recovery may append a canonical component that was absent from an
        # older draft.  It is useful for review, but an in-memory row Start or
        # startup preview must never infer that the absent component was
        # selected.  Only rows represented in the operator's draft retain
        # launch eligibility.
        for item in restored:
            name = str(item.get("name", "") or "").strip().casefold()
            if name and name not in original_names:
                item["enabled"] = False
                item["startup"] = False
        restored_bundle["items"] = restored
        if restored != items:
            restored_bundle["canonical_recovery"] = True
        return restored_bundle

    def set_radio_launch_bundle(
        self,
        radio_profile_id: int,
        items: List[Dict[str, Any]],
        launch_enabled: bool,
    ) -> Dict[str, Any]:
        return self.bundle_store.save_bundle(int(radio_profile_id), bool(launch_enabled), items)

    def preview_startup_plan(
        self,
        *,
        scope_radio_id: Optional[int] = None,
        trigger: str = "startup",
        bundle_override: Optional[Mapping[str, Any]] = None,
    ) -> LaunchPlan:
        # An explicit selected-radio start is allowed to prepare an inactive
        # radio's applications without silently activating that radio.  Only
        # unattended station startup is restricted to runtime-active radios.
        manual_selected_radio = (
            str(trigger or "").strip().lower() == "manual"
            and scope_radio_id is not None
        )
        profiles = (
            self.multi_radio_store.list_device_profiles()
            if manual_selected_radio
            else self.multi_radio_store.list_runtime_active_device_profiles()
        )
        if scope_radio_id is not None:
            profiles = [
                profile
                for profile in profiles
                if int(profile.get("id", 0) or 0) == int(scope_radio_id)
            ]
        projection_warnings: Dict[int, str] = {}
        for profile in profiles:
            radio_id = int(profile.get("id", 0) or 0)
            repair_detector = getattr(
                self.multi_radio_store,
                "fast_light_message_component_repair_needed",
                None,
            )
            if (
                radio_id > 0
                and callable(repair_detector)
                and repair_detector(radio_id)
            ):
                try:
                    repair = self.multi_radio_store.prepare_fast_light_message_component_repair(
                        radio_id
                    )
                    self.multi_radio_store.apply_fast_light_message_component_repair(repair)
                    log.info(
                        "Automatically repaired legacy FLMsg/FLAmp launch identity for radio %s.",
                        radio_id,
                    )
                except (KeyError, ValueError) as exc:
                    # Ambiguous or incomplete evidence remains an operator
                    # recovery task in Software Administration.  Launch
                    # planning continues so its existing blocker/warning is
                    # still precise and no unrelated app is suppressed.
                    log.warning(
                        "Automatic FLMsg/FLAmp repair deferred for radio %s: %s",
                        radio_id,
                        exc,
                    )
                except Exception:
                    log.exception(
                        "Automatic FLMsg/FLAmp repair failed for radio %s; existing launch review remains available.",
                        radio_id,
                    )
            projection_issues = (
                self.multi_radio_store.validate_radio_software_identity_projections(radio_id)
                if radio_id > 0
                and self.multi_radio_store.radio_software_identity_generation(radio_id) > 0
                else {}
            )
            if projection_issues:
                detail = "; ".join(
                    f"{family}: {', '.join(issues)}"
                    for family, issues in sorted(projection_issues.items())
                )
                projection_warnings[radio_id] = detail
                log.warning(
                    "Launch proceeding for radio %s with canonical software parity review warning: %s",
                    radio_id,
                    detail,
                )
        self._last_projection_warnings = projection_warnings
        blockers = self.multi_radio_store.varac_native_launch_blockers()
        if blockers:
            blocked_targets = {
                str(target or "").strip()
                for blocker in blockers
                for target in blocker.get("targets", ())
                if str(target or "").strip()
            }
            affected = [
                str(profile.get("name", profile.get("id", "VarAC")) or "VarAC")
                for profile in profiles
                if blocked_targets.intersection(
                    {
                        str(profile.get("varac_ini_path", "") or "").strip(),
                        str(profile.get("varac_vara_ini_path", "") or "").strip(),
                    }
                )
            ]
            if affected:
                raise ValueError(
                    "VarAC launch is blocked pending native configuration recovery for: "
                    + ", ".join(sorted(set(affected), key=str.casefold))
                    + ". Open Station Health before retrying."
                )
        bundles = {
            int(profile["id"]): self.get_radio_launch_bundle(int(profile["id"]))
            for profile in profiles
            if int(profile.get("id", 0) or 0) > 0
        }
        if scope_radio_id is not None and bundle_override is not None:
            bundles[int(scope_radio_id)] = self._restore_canonical_bundle_override(
                int(scope_radio_id),
                bundle_override,
            )
        plan = self.planner.plan_startup(
            profiles,
            bundles,
            scope_radio_id=scope_radio_id,
            trigger=trigger,
        )
        return self._with_effective_launch_preview(plan)

    def preview_manual_plan(
        self,
        radio_profile_id: int,
        *,
        bundle_override: Optional[Mapping[str, Any]] = None,
    ) -> LaunchPlan:
        """Preview the selected-radio plan through the startup planner path.

        Manual station start changes only the requested radio scope; recipe,
        startup inclusion, dependencies, and readiness stay identical.
        """

        return self.preview_startup_plan(
            scope_radio_id=int(radio_profile_id),
            trigger="manual",
            bundle_override=bundle_override,
        )

    def preview_radio_recipe_plan(
        self,
        radio_profile_id: int,
        *,
        bundle_override: Optional[Mapping[str, Any]] = None,
    ) -> LaunchPlan:
        """Preview known recipes and explicit operator-start states for review."""

        profiles = self.multi_radio_store.list_device_profiles()
        bundles = {
            int(profile["id"]): self.get_radio_launch_bundle(int(profile["id"]))
            for profile in profiles
            if int(profile.get("id", 0) or 0) > 0
        }
        if bundle_override is not None:
            bundles[int(radio_profile_id)] = self._restore_canonical_bundle_override(
                int(radio_profile_id),
                bundle_override,
            )
        plan = self.planner.plan_review(
            profiles,
            bundles,
            scope_radio_id=int(radio_profile_id),
        )
        return self._with_effective_launch_preview(plan)

    def _with_effective_launch_preview(self, plan: LaunchPlan) -> LaunchPlan:
        """Resolve executable selection separately from planner-owned launch arguments.

        The resulting queue is safe to render in Launch Control and remains the
        same command shape used at execution time, including ``open --args``
        for macOS application bundles.
        """
        instances = []
        hardware_lifecycle = getattr(self, "hardware_lifecycle", None)
        lifecycle = hardware_lifecycle.latest() if hardware_lifecycle is not None else None
        hardware_store = getattr(self, "multi_radio_store", None)
        for instance in plan.instances:
            queue_item = instance.as_queue_item()
            command, _description = (None, "operator start") if instance.operator_starts else self._resolve_launch_command(queue_item)
            projected = instance
            radio_id = int(instance.radio_ids[0]) if len(instance.radio_ids) == 1 else 0
            bundle = (
                hardware_store.get_radio_hardware_bundle(radio_id)
                if radio_id > 0
                and hardware_store is not None
                and hasattr(hardware_store, "get_radio_hardware_bundle")
                else None
            )
            if bundle:
                readiness = dict(instance.readiness_policy)
                projection_key = str(readiness.get("hardware_projection_key", "") or "").strip()
                if not projection_key:
                    instances.append(replace(projected, effective_command=tuple(command or ())))
                    continue
                if hardware_lifecycle is None and hardware_store is not None:
                    hardware_lifecycle = shared_hardware_lifecycle_service(hardware_store)
                    self.hardware_lifecycle = hardware_lifecycle
                    lifecycle = hardware_lifecycle.latest()
                snapshot = lifecycle.radios.get(radio_id) if lifecycle is not None else None
                snapshot_age = (
                    time.monotonic() - float(lifecycle.captured_monotonic)
                    if lifecycle is not None
                    else float("inf")
                )
                snapshot_current = bool(
                    snapshot is not None
                    and snapshot_age <= HARDWARE_SNAPSHOT_MAX_AGE_SEC
                    and int(snapshot.bundle_generation) == int(bundle.get("generation", 0) or 0)
                )
                if not snapshot_current:
                    if hardware_lifecycle is not None:
                        hardware_lifecycle.request_refresh()
                    readiness["hardware_launch_block"] = (
                        "Radio hardware evidence is refreshing or stale. Wait a moment and retry."
                    )
                else:
                    projection = build_hardware_launch_projection(
                        instance.name,
                        snapshot,
                        projection_key=projection_key,
                        receive_only=(
                            str(instance.execution_scope or "").strip().lower()
                            == "receive_only"
                        ),
                    )
                    if projection.blocked:
                        readiness["hardware_launch_block"] = projection.reason
                    elif projection.environment:
                        merged_environment = dict(instance.environment)
                        merged_environment.update(dict(projection.environment))
                        projected = replace(
                            projected,
                            environment=tuple(sorted(merged_environment.items())),
                        )
                    readiness["hardware_projection_key"] = projection_key
                    # Projection proves only that FIO supplied the reviewed
                    # target. Native post-launch observation is a separate
                    # qualification and must not be implied here.
                    readiness["hardware_projected_roles"] = projection.verified_roles
                projected = replace(projected, readiness_policy=tuple(sorted(readiness.items())))
            instances.append(replace(projected, effective_command=tuple(command or ())))
        return LaunchPlan(trigger=plan.trigger, scope_radio_id=plan.scope_radio_id, instances=tuple(instances))

    def _station_process_attribution_candidates(
        self,
        requested_queue: Sequence[Any],
    ) -> tuple[Mapping[str, Any], ...]:
        """Return all persisted station identities plus the requested draft.

        Launch scope and attribution scope are intentionally different.  A
        row Start for one radio may launch only that row, but an already
        running sibling radio's VarAC/VARA process must be credited against
        the complete station catalog rather than misclassified as unknown.
        """

        candidates: List[Mapping[str, Any]] = []
        try:
            profiles = tuple(self.multi_radio_store.list_device_profiles())
            for profile in profiles:
                radio_id = int(profile.get("id", 0) or 0)
                if radio_id <= 0:
                    continue
                try:
                    # Attribution inventories identities; it does not approve
                    # a multi-radio launch. Plan each radio independently so a
                    # legitimate VarAC cluster-shared database does not invoke
                    # the independent-instance collision gate. Normal startup
                    # planning retains that station-wide validation.
                    review = self.planner.plan_review(
                        (profile,),
                        {radio_id: self.get_radio_launch_bundle(radio_id)},
                        trigger="process-attribution",
                    )
                    candidates.extend(review.queue())
                except Exception as exc:
                    log.warning(
                        "LaunchOrchestrator: process attribution identity is incomplete "
                        "for radio %s: %s",
                        radio_id,
                        exc,
                    )
        except Exception as exc:
            # The requested queue remains usable, but any process that cannot
            # be attributed through it will retain the existing fail-closed
            # duplicate guard.
            log.warning(
                "LaunchOrchestrator: station process attribution catalog is incomplete: %s",
                exc,
            )

        requested = [dict(item) for item in requested_queue if isinstance(item, Mapping)]
        requested_keys = {
            str(item.get("instance_key", "") or "").strip().casefold()
            for item in requested
            if str(item.get("instance_key", "") or "").strip()
        }
        if requested_keys:
            candidates = [
                item
                for item in candidates
                if str(item.get("instance_key", "") or "").strip().casefold()
                not in requested_keys
            ]
        candidates.extend(requested)

        unique: Dict[str, Mapping[str, Any]] = {}
        for index, item in enumerate(candidates):
            identity = str(item.get("instance_identity", "") or "").strip()
            instance_key = str(item.get("instance_key", "") or "").strip()
            key = identity or instance_key or f"candidate:{index}:{self._queue_item_name(item)}"
            unique[key] = item
        return tuple(unique.values())

    def set_launch_items(self, items: List[Dict[str, Any]], launch_all_with_startup: bool) -> None:
        """Compatibility entry point; writes the runtime-primary radio bundle, never legacy KV."""
        profile = self.multi_radio_store.get_runtime_primary_device_profile()
        if not profile:
            raise RuntimeError("Launch Control needs a selected radio before it can be saved.")
        self.set_radio_launch_bundle(
            int(profile["id"]),
            self.build_default_items(items, custom_tools=self.get_custom_tools()),
            bool(launch_all_with_startup),
        )

    def start_startup_sequence(self) -> bool:
        if self._active:
            return False
        if not self.launch_allowed():
            return False
        plan = self.preview_startup_plan(trigger="startup")
        queue = plan.queue()
        if not queue:
            return False
        return self._start_sequence("startup", queue)

    def start_radio_startup_sequence(
        self,
        radio_profile_id: int,
        *,
        bundle_override: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        # ``launch_allowed`` is the primary operating model's unattended
        # startup gate.  This method represents an explicit operator action on
        # one reviewed radio and must remain available independently.
        if self._active:
            log.warning(
                "LaunchOrchestrator: selected-radio launch for radio %s blocked because another sequence is active",
                radio_profile_id,
            )
            return False
        plan = self.preview_manual_plan(
            int(radio_profile_id),
            bundle_override=bundle_override,
        )
        queue = plan.queue()
        if not queue:
            log.warning(
                "LaunchOrchestrator: selected-radio launch for radio %s produced no launchable recipe",
                radio_profile_id,
            )
            return False
        return self._start_sequence("manual", queue)

    def start_manual_sequence(self, items: Optional[List[Dict[str, Any]]] = None) -> bool:
        if self._active:
            return False
        if not self.launch_allowed():
            return False
        # Explicit manual rows are already structured launch recipes.  Running
        # them back through the legacy catalog builder discarded instance
        # identity, monitoring, dependencies, working-directory/readiness, and
        # execution-scope facts that distinguish two radios' app instances.
        base_items = normalize_launch_items(items) if items is not None else self.get_launch_items()
        queue = self._build_queue(base_items, startup_only=False)
        if not queue:
            return False
        return self._start_sequence("manual", queue)

    def stop_sequence(self) -> None:
        if not self._active:
            return
        self._cancel_requested = True
        self._poll_timer.stop()
        self._finish_sequence(cancelled=True)

    def is_active(self) -> bool:
        return self._active

    def app_path_key(self, name: str) -> str:
        return str(LAUNCH_APP_META.get(name, {}).get("path_key", "") or "")

    def is_configured(self, name: str) -> bool:
        custom_cmd = self._custom_tool_command(name)
        if custom_cmd:
            return True
        meta = LAUNCH_APP_META.get(name, {})
        launch_cmd_key = str(meta.get("launch_cmd_key", "") or "")
        if launch_cmd_key:
            launch_cmd = self.settings.get(launch_cmd_key, "")
            if str(launch_cmd or "").strip():
                return True
        path_key = str(meta.get("path_key", "") or "")
        if path_key:
            raw = self.settings.get(path_key, "")
            if str(raw or "").strip():
                return True
        return False

    def _build_queue(self, items: List[Dict[str, Any]], startup_only: bool) -> List[Any]:
        queue: List[Any] = []
        catalog = set(self.launch_catalog_order())
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if name not in catalog:
                continue
            if startup_only:
                if not bool(item.get("startup", False)):
                    continue
            elif not bool(item.get("enabled", False)):
                continue
            has_override = bool(
                str(item.get("launch_path_override", "") or "").strip()
                or str(item.get("launch_command_override", "") or "").strip()
            )
            if not has_override and not self.is_configured(name):
                continue
            queue.append(dict(item) if has_override else name)
        return queue

    @staticmethod
    def _queue_item_name(item: Any) -> str:
        if isinstance(item, Mapping):
            return str(item.get("name", "") or "").strip()
        return str(item or "").strip()

    @staticmethod
    def _result_for(item: Any, *, status: str, detail: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "name": LaunchOrchestrator._queue_item_name(item),
            "status": status,
            "detail": detail,
        }
        if isinstance(item, Mapping):
            for key in (
                "instance_key",
                "instance_identity",
                "radio_ids",
                "radio_names",
                "rig_name",
                "rig_name_source",
                "application_data_root",
                "storage_mode",
                "effective_command",
                "working_directory",
                "profile_selector",
                "execution_scope",
                "startup_included",
                "bundle_enabled",
                "operator_starts",
                "radio_control_backend",
                "radio_control_host",
                "radio_control_port",
                "radio_control_app",
                "radio_control_gate_required",
            ):
                if key in item:
                    result[key] = item[key]
        return result

    def _schedule_advance_queue(self, delay_ms: int = 0) -> None:
        QTimer.singleShot(max(0, int(delay_ms)), self._advance_queue)

    @staticmethod
    def _coerce_delay_seconds(raw: Any, default: float) -> float:
        try:
            val = float(raw)
        except Exception:
            val = float(default)
        if val < 0.0:
            return 0.0
        if val > 120.0:
            return 120.0
        return float(val)

    def _program_ready_for_sequence(self, item: Any) -> bool:
        name = self._queue_item_name(item)
        if (
            isinstance(item, Mapping)
            and bool(item.get("radio_control_gate_required", False))
            and name == str(item.get("radio_control_app", "") or "").strip()
        ):
            # The accepted process inventory is intentionally immutable for
            # duplicate-launch safety, so it cannot contain a process that FIO
            # started later in this sequence.  Exact endpoint frequency
            # readback is stronger post-launch readiness evidence and must not
            # be suppressed by that pre-launch process snapshot.
            return self._radio_control_evidence_state(
                item,
                force=self._radio_control_probe_is_due(item),
            ) == "ready"
        launched_here = self._current_launch_process_for_item(item) is not None
        policy = item.get("readiness_policy", {}) if isinstance(item, Mapping) else {}
        if self._custom_rigctld_identity(item) is not None:
            info = self._cached_status_for_item(item, force=launched_here)
            return bool(
                not info.get("stale", True)
                and info.get("endpoint_occupied", False)
                and (self._current_launch_process_is_stable(item) if launched_here else info.get("running", False))
            )
        if name == "JS8Call":
            if isinstance(policy, Mapping) and not bool(policy.get("require_api", True)):
                return (
                    self._current_launch_process_is_stable(item)
                    if launched_here
                    else self._program_running(item)
                )
            if not launched_here and not self._program_running(item):
                return False
            return bool(
                self._cached_status_for_item(item, force=launched_here).get(
                    "reachable", False
                )
            )
        if name in {"FLRig", "FLDigi"} and isinstance(item, Mapping):
            if isinstance(policy, Mapping) and bool(policy.get("require_service", False)):
                if not launched_here and not self._program_running(item):
                    return False
                return bool(
                    self._cached_status_for_item(item, force=launched_here).get(
                        "reachable", False
                    )
                )
        if launched_here:
            return self._current_launch_process_is_stable(item)
        if not self._program_running(item):
            return False
        return True

    def _current_launch_process_for_item(self, item: Any) -> Any:
        if item is not getattr(self, "_current_item", None):
            return None
        return getattr(self, "_current_process", None)

    def _current_launch_return_code(self, item: Any) -> Optional[int]:
        process = self._current_launch_process_for_item(item)
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return None
        try:
            result = poll()
        except (ChildProcessError, ProcessLookupError):
            return -1
        except Exception as exc:
            log.warning("LaunchOrchestrator: could not poll current launch process: %s", exc)
            return -1
        if result is None:
            return None
        try:
            return int(result)
        except (TypeError, ValueError):
            return -1

    def _successful_launcher_exit_allowed(self) -> bool:
        if str(getattr(self, "_current_launch_description", "") or "") == "configured custom tool":
            return True
        current_name = str(getattr(self, "_current_name", "") or "").strip()
        if current_name and current_name not in LAUNCH_APP_META:
            return True
        command = getattr(self, "_current_cmd", None)
        if not isinstance(command, (list, tuple)) or not command:
            return False
        executable = os.path.basename(str(command[0] or "")).strip().casefold()
        return executable in {"open", "xdg-open"}

    def _current_launch_process_is_stable(self, item: Any) -> bool:
        if self._current_launch_process_for_item(item) is None:
            return False
        elapsed = max(
            0.0,
            time.monotonic()
            - float(getattr(self, "_current_started_monotonic", 0.0) or 0.0),
        )
        if elapsed < DEFAULT_PROCESS_LAUNCH_STABILITY_SEC:
            return False
        return_code = self._current_launch_return_code(item)
        return return_code is None or (
            return_code == 0 and self._successful_launcher_exit_allowed()
        )

    def _current_launch_exit_failure(self, item: Any) -> str:
        if self._current_launch_process_for_item(item) is None:
            return ""
        return_code = self._current_launch_return_code(item)
        if return_code is None:
            return ""
        if return_code == 0 and self._successful_launcher_exit_allowed():
            return ""
        return f"launch process exited with code {return_code} before readiness"

    @staticmethod
    def _normalized_readiness_timeout(raw: Any) -> int:
        try:
            timeout = int(raw or DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC)
        except (TypeError, ValueError):
            timeout = DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC
        if timeout == LEGACY_LAUNCH_READINESS_TIMEOUT_SEC:
            return DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC
        return timeout if timeout > 0 else DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC

    def _custom_rigctld_identity(self, item):
        name = self._queue_item_name(item)
        if name in LAUNCH_APP_META:
            return None
        raw = str(item.get("launch_command_override", "") or "") if isinstance(item, Mapping) else ""
        command = raw or self._custom_tool_command(name)
        arguments = item.get("launch_arguments", ()) if isinstance(item, Mapping) else ()
        if isinstance(arguments, (list, tuple)) and arguments:
            command += " " + shlex.join(tuple(str(value) for value in arguments))
        return parse_custom_rigctld(command)

    @staticmethod
    def _has_persisted_endpoint_identity(item: Any) -> bool:
        """Return whether one launch item owns a concrete service endpoint.

        A selected-radio launch plan contains only that radio's rows, so queue
        cardinality cannot prove that another radio's same-named process is a
        different instance.  The persisted identity plus endpoint is the
        authoritative discriminator for the service-bearing applications.
        """

        if isinstance(item, Mapping) and parse_custom_rigctld(str(item.get("launch_command_override") or "")) is not None:
            return True
        if not isinstance(item, Mapping) or not str(item.get("instance_identity", "") or "").strip():
            return False
        name = LaunchOrchestrator._queue_item_name(item)
        if name not in {"JS8Call", "FLRig", "FLDigi"}:
            return False
        policy = item.get("readiness_policy", {})
        if not isinstance(policy, Mapping):
            return False
        host = str(policy.get("host", "") or "").strip()
        try:
            port = int(policy.get("port", 0) or 0)
        except (TypeError, ValueError):
            return False
        return bool(host) and 0 < port <= 65535

    def _cached_status_for_item(
        self,
        item: Any,
        *,
        force: bool = False,
    ) -> Mapping[str, Any]:
        name = self._queue_item_name(item)
        policy = item.get("readiness_policy", {}) if isinstance(item, Mapping) else {}
        kwargs: Dict[str, Any] = {
            "force": bool(force),
            # The sequence-level preflight already published one fresh process
            # inventory.  A forced scoped request here means “probe this
            # endpoint now,” not “walk every process again.”
            "force_process_snapshot": False,
        }
        if name == "JS8Call" and isinstance(policy, Mapping):
            kwargs["host_override"] = str(policy.get("host", "") or "") or None
            try:
                kwargs["port_override"] = int(policy.get("port")) if policy.get("port") is not None else None
            except Exception:
                kwargs["port_override"] = None
        elif name == "FLRig" and isinstance(policy, Mapping):
            kwargs["flrig_host_override"] = str(policy.get("host", "") or "") or None
            try:
                kwargs["flrig_port_override"] = int(policy.get("port")) if policy.get("port") is not None else None
            except Exception:
                kwargs["flrig_port_override"] = None
        elif name == "FLDigi" and isinstance(policy, Mapping):
            kwargs["fldigi_host_override"] = str(policy.get("host", "") or "") or None
            try:
                kwargs["fldigi_port_override"] = int(policy.get("port")) if policy.get("port") is not None else None
            except Exception:
                kwargs["fldigi_port_override"] = None
        daemon = self._custom_rigctld_identity(item)
        if daemon is not None:
            kwargs["instance_identities"] = {name: {"target": shlex.join((daemon.executable, *daemon.arguments))}}
        try:
            snapshot = self.dependency_status.status_snapshot(**kwargs)
        except Exception:
            return {}
        key = "JS8Call_API" if name == "JS8Call" else name
        info = snapshot.get(key, {}) if isinstance(snapshot, Mapping) else {}
        return info if isinstance(info, Mapping) else {}

    @staticmethod
    def _radio_ids_for_item(item: Any) -> set[int]:
        if not isinstance(item, Mapping):
            return set()
        radio_ids: set[int] = set()
        for value in item.get("radio_ids", ()) or ():
            try:
                radio_id = int(value or 0)
            except (TypeError, ValueError):
                continue
            if radio_id > 0:
                radio_ids.add(radio_id)
        return radio_ids

    @staticmethod
    def _radio_id_for_item(item: Any) -> int:
        if not isinstance(item, Mapping):
            return 0
        for value in item.get("radio_ids", ()) or ():
            try:
                radio_id = int(value or 0)
            except (TypeError, ValueError):
                continue
            if radio_id > 0:
                return radio_id
        return 0

    @staticmethod
    def _radio_name_for_item(item: Any) -> str:
        if isinstance(item, Mapping):
            for value in item.get("radio_names", ()) or ():
                text = str(value or "").strip()
                if text:
                    return text
        radio_id = LaunchOrchestrator._radio_id_for_item(item)
        return f"Radio {radio_id}" if radio_id > 0 else "Radio"

    @staticmethod
    def _radio_control_status_key(item: Any) -> str:
        backend = str(item.get("radio_control_backend", "") or "").strip().lower() if isinstance(item, Mapping) else ""
        return {
            "flrig": "FLRig",
            "rigctld": "RigCtlD",
            "hamlib": "RigCtlD",
            "js8call": "JS8Call_API",
        }.get(backend, "")

    def _cached_radio_control_status(
        self,
        item: Any,
        *,
        force: bool = False,
    ) -> Mapping[str, Any]:
        if not isinstance(item, Mapping):
            return {}
        backend = str(item.get("radio_control_backend", "") or "").strip().lower()
        host = str(item.get("radio_control_host", "") or "").strip() or None
        try:
            port = int(item.get("radio_control_port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        kwargs: Dict[str, Any] = {
            "force": bool(force),
            "force_process_snapshot": False,
            "verify_control_readback": True,
            "control_backend": backend,
            "control_probe_only": True,
        }
        if backend == "flrig":
            kwargs.update(flrig_host_override=host, flrig_port_override=port or None)
        elif backend in {"rigctld", "hamlib"}:
            kwargs.update(rigctld_host_override=host, rigctld_port_override=port or None)
        elif backend == "js8call":
            kwargs.update(host_override=host, port_override=port or None)
        else:
            return {}
        try:
            snapshot = self.dependency_status.status_snapshot(**kwargs)
        except Exception:
            return {}
        key = self._radio_control_status_key(item)
        info = snapshot.get(key, {}) if isinstance(snapshot, Mapping) else {}
        return info if isinstance(info, Mapping) else {}

    def _radio_control_evidence_state(self, item: Any, *, force: bool = False) -> str:
        info = self._cached_radio_control_status(item, force=force)
        evidence = getattr(self, "_radio_control_last_evidence", {})
        evidence[self._radio_id_for_item(item)] = dict(info)
        self._radio_control_last_evidence = evidence
        try:
            checked_at = float(info.get("checked_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            checked_at = 0.0
        fresh_for_sequence = bool(
            str(info.get("source", "") or "").strip().lower() == "endpoint"
            and checked_at >= float(getattr(self, "_sequence_preflight_started_wall", 0.0) or 0.0)
        )
        if not fresh_for_sequence:
            return "pending"
        return "ready" if bool(info.get("radio_readback_ready", False)) else "unavailable"

    def _log_radio_control_readback_failure(self, item: Any) -> None:
        radio_id = self._radio_id_for_item(item)
        info = getattr(self, "_radio_control_last_evidence", {}).get(radio_id, {})
        log.warning(
            "LaunchOrchestrator: radio %s last control evidence: source=%s checked_at=%s "
            "reachable=%s readback_ready=%s online=%s frequency_hz=%s detail=%s",
            radio_id, info.get("source"), info.get("checked_at"),
            info.get("reachable"), info.get("radio_readback_ready"),
            info.get("radio_online"), info.get("frequency_hz"),
            str(info.get("tooltip") or "")[:300],
        )

    def _radio_control_probe_is_due(self, item: Any, *, immediate: bool = False) -> bool:
        radio_id = self._radio_id_for_item(item)
        if radio_id <= 0:
            return False
        now = time.monotonic()
        due_by_radio = getattr(self, "_radio_control_probe_due_monotonic", {})
        due_at = float(due_by_radio.get(radio_id, 0.0) or 0.0)
        if not immediate and now < due_at:
            return False
        due_by_radio[radio_id] = now + LAUNCH_RADIO_CONTROL_PROBE_INTERVAL_SEC
        self._radio_control_probe_due_monotonic = due_by_radio
        return True

    def set_schedule_handoff_provider(self, provider) -> None:
        """Install the scheduler's cache-only launch acknowledgement reader."""
        self._schedule_handoff_provider = provider

    def _begin_schedule_handoff(self, item: Any) -> None:
        radio_id = self._radio_id_for_item(item)
        self._radio_control_gate_states[radio_id] = "ready"
        self.sequence_progress.emit({
            "name": f"{self._radio_name_for_item(item)} control",
            "status": "ready",
            "detail": "fresh transceiver and frequency readback received",
            "counts_toward_total": False,
            "radio_ids": [radio_id],
        })
        self._current_name = f"{self._radio_name_for_item(item)} schedule"
        self._current_item = item
        self._current_cmd = None
        self._current_phase = "radio_schedule_apply"
        self._current_started_monotonic = time.monotonic()
        self._poll_timer.setInterval(LAUNCH_ENDPOINT_PREFLIGHT_POLL_MS)
        self._poll_timer.start()
        log.info("LaunchOrchestrator: radio %s waiting for current schedule frequency verification", radio_id)

    def _begin_radio_control_gate(self, item: Any) -> None:
        radio_id = self._radio_id_for_item(item)
        if radio_id <= 0:
            return
        states = getattr(self, "_radio_control_gate_states", {})
        states[radio_id] = "checking"
        self._radio_control_gate_states = states
        state = self._radio_control_evidence_state(
            item,
            force=self._radio_control_probe_is_due(item, immediate=True),
        )
        if state == "ready":
            self._begin_schedule_handoff(item)
            return
        self._current_name = f"{self._radio_name_for_item(item)} control"
        self._current_item = item
        self._current_cmd = None
        self._current_phase = "radio_control_gate"
        self._current_started_monotonic = time.monotonic()
        self._poll_timer.setInterval(LAUNCH_ENDPOINT_PREFLIGHT_POLL_MS)
        self._poll_timer.start()
        self.sequence_progress.emit(
            {
                "name": self._current_name,
                "status": "checking",
                "detail": "waiting for fresh radio frequency readback",
                "counts_toward_total": False,
                "radio_ids": [radio_id],
            }
        )

    def _radio_control_app_failed(self, item: Any) -> bool:
        if not isinstance(item, Mapping):
            return False
        control_app = str(item.get("radio_control_app", "") or "").strip()
        if not control_app:
            return False
        radio_id = self._radio_id_for_item(item)
        success_states = {"launched", "already_running"}
        matches = []
        for result in getattr(self, "_results", ()):
            if str(result.get("name", "") or "") != control_app:
                continue
            result_radios = self._radio_ids_for_item(result)
            if radio_id > 0 and result_radios and radio_id not in result_radios:
                continue
            matches.append(result)
        return bool(matches) and not any(
            str(result.get("status", "") or "") in success_states for result in matches
        )

    @staticmethod
    def _is_gated_radio_control_app(item: Any) -> bool:
        if not isinstance(item, Mapping):
            return False
        return bool(
            item.get("radio_control_gate_required", False)
            and str(item.get("name", "") or "").strip()
            == str(item.get("radio_control_app", "") or "").strip()
        )

    def _pending_queue_contains(self, names: set[str]) -> bool:
        if not names:
            return False
        if self._index >= len(self._queue):
            return False
        for item in self._queue[self._index :]:
            name = self._queue_item_name(item)
            if name in names:
                return True
        return False

    def _prepare_hardware_native_projection(self, item: Any) -> None:
        """Apply only an explicitly qualified, exact FLRig native projection."""

        if not isinstance(item, Mapping) or self._queue_item_name(item) != "FLRig":
            return
        readiness = item.get("readiness_policy", {})
        if not isinstance(readiness, Mapping):
            return
        projection_key = str(readiness.get("hardware_projection_key", "") or "").strip()
        if projection_key.casefold() != "flrig:fltk-prefs:v1":
            return
        radio_ids = item.get("radio_ids", ())
        if not isinstance(radio_ids, (list, tuple)) or len(radio_ids) != 1:
            raise RuntimeError("FLRig hardware projection requires one exact radio owner.")
        radio_id = int(radio_ids[0])
        lifecycle = self.hardware_lifecycle.latest()
        if (
            lifecycle is None
            or time.monotonic() - float(lifecycle.captured_monotonic)
            > HARDWARE_SNAPSHOT_MAX_AGE_SEC
        ):
            self.hardware_lifecycle.request_refresh()
            raise RuntimeError("FLRig hardware evidence is stale; wait for refresh and retry.")
        snapshot = lifecycle.radios.get(radio_id) if lifecycle is not None else None
        if snapshot is None:
            raise RuntimeError("FLRig hardware verification is not available yet.")
        current_bundle = self.multi_radio_store.get_radio_hardware_bundle(radio_id)
        if current_bundle is None or int(snapshot.bundle_generation) != int(
            current_bundle.get("generation", 0) or 0
        ):
            self.hardware_lifecycle.request_refresh()
            raise RuntimeError("FLRig hardware assignment changed; wait for refresh and retry.")
        cat = next((role for role in snapshot.roles if role.role == "cat"), None)
        observed = cat.observed_locator if cat is not None else {}
        locator = str(observed.get("by_id", "") or observed.get("path", "") or "").strip()
        if cat is None or cat.state not in {"exact", "port_bound", "unstable_confirmed"} or not locator:
            raise RuntimeError(cat.reason if cat is not None else "FLRig CAT hardware is not assigned.")
        target_value = str(readiness.get("flrig_preferences_target", "") or "").strip()
        if not target_value:
            raise RuntimeError(
                "FLRig connection is application-managed because no exact qualified preference target was reviewed."
            )
        apply_flrig_fltk_serial_projection(
            Path(target_value),
            locator,
            projection_key=projection_key,
        )

    def _post_ready_settle_delay_seconds(self, name: str) -> float:
        if self._index >= len(self._queue):
            return 0.0
        if name == "VarAC":
            raw = self.settings.get("launch_varac_settle_delay_sec", DEFAULT_VARAC_SETTLE_DELAY_SEC)
            return self._coerce_delay_seconds(raw, DEFAULT_VARAC_SETTLE_DELAY_SEC)
        if name == "JS8Call" and self._pending_queue_contains(JS8_DEPENDENT_APPS):
            raw = self.settings.get("launch_js8call_settle_delay_sec", DEFAULT_JS8CALL_DEPENDENT_DELAY_SEC)
            return self._coerce_delay_seconds(raw, DEFAULT_JS8CALL_DEPENDENT_DELAY_SEC)
        return 0.0

    def _start_sequence(self, trigger: str, queue: List[Any]) -> bool:
        self._active = True
        self._cancel_requested = False
        self._trigger = trigger
        self._queue = queue
        self._index = 0
        self._results = []
        self._current_name = None
        self._current_item = None
        self._current_cmd = None
        self._current_process = None
        self._current_launch_description = ""
        self._current_started_monotonic = 0.0
        self._current_phase = ""
        self._hardware_preflight_requested = {}
        self._hardware_preflight_verified = set()
        self._endpoint_preflight_verified = set()
        self._endpoint_preflight_requested = set()
        self._endpoint_preflight_clear = set()
        self._sequence_claimed_identities = set()
        self._radio_control_last_evidence = {}
        self._radio_control_gate_states = {}
        self._radio_control_probe_due_monotonic = {}
        self._sequence_attribution_candidates = self._station_process_attribution_candidates(
            queue
        )
        self._sequence_process_records = ()
        self._sequence_process_records_ready = False
        self._sequence_preflight_started_wall = time.time()
        self._process_preflight_pending = True
        self._process_preflight_generation += 1
        preflight_generation = self._process_preflight_generation
        self._process_preflight_reason = f"launch-preflight:{trigger}"
        try:
            latest = self.dependency_status.latest_snapshot()
            self._process_preflight_baseline_sequence = int(
                getattr(latest, "sequence", 0) or 0
            )
        except Exception:
            self._process_preflight_baseline_sequence = 0
        self._sequence_projection_warnings = dict(
            getattr(self, "_last_projection_warnings", {})
        )
        try:
            configured_timeout = self.settings.get(
                "launch_readiness_timeout_sec",
                DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC,
            )
        except Exception:
            configured_timeout = DEFAULT_LAUNCH_READINESS_TIMEOUT_SEC
        self._wait_timeout_sec = self._normalized_readiness_timeout(configured_timeout)
        self.sequence_started.emit(
            {
                "trigger": trigger,
                "queue": [dict(item) if isinstance(item, Mapping) else {"name": self._queue_item_name(item)} for item in queue],
                "projection_warnings": dict(self._sequence_projection_warnings),
            }
        )
        # A cold or stale process cache is unknown evidence, not proof that an
        # application is absent.  Force one station-wide inventory and wait for
        # its publication before any launch command can reach Popen.  Endpoint
        # owners receive an additional configured-port preflight below.
        try:
            self.dependency_status.refresh_now(
                reason=self._process_preflight_reason,
                force=True,
            )
        except Exception as exc:
            self._fail_launch_preflight(
                f"could not start process inventory: {exc}"
            )
            return True
        QTimer.singleShot(
            int(LAUNCH_PROCESS_PREFLIGHT_TIMEOUT_SEC * 1000.0),
            lambda generation=preflight_generation: self._on_launch_preflight_timeout(
                generation
            ),
        )
        # Test doubles and an already-completed refresh can publish before the
        # queued signal is delivered.  Re-check the immutable snapshot without
        # walking the process table on the GUI thread.
        self._accept_completed_launch_preflight()
        return True

    def _accept_completed_launch_preflight(self) -> bool:
        if not self._active or not self._process_preflight_pending:
            return False
        try:
            snapshot = self.dependency_status.latest_snapshot()
            sequence = int(getattr(snapshot, "sequence", 0) or 0)
            scope = str(getattr(snapshot, "scope", "") or "")
            reason = str(getattr(snapshot, "reason", "") or "")
        except Exception:
            return False
        if scope != LEGACY_PRIMARY_DEPENDENCY_SCOPE:
            return False
        if reason != str(getattr(self, "_process_preflight_reason", "") or ""):
            return False
        if sequence <= int(self._process_preflight_baseline_sequence or 0):
            return False
        self._sequence_process_records = tuple(
            dict(record)
            for record in (getattr(snapshot, "process_records", ()) or ())
            if isinstance(record, Mapping)
        )
        self._sequence_process_records_ready = True
        self._process_preflight_pending = False
        log.info(
            "LaunchOrchestrator: process preflight complete for %s launch (sequence=%s)",
            self._trigger or "unknown",
            sequence,
        )
        self._schedule_advance_queue(0)
        return True

    def _on_launch_preflight_snapshot_changed(self, snapshot: object) -> None:
        if not self._active or not self._process_preflight_pending:
            return
        if str(getattr(snapshot, "scope", "") or "") != LEGACY_PRIMARY_DEPENDENCY_SCOPE:
            return
        if str(getattr(snapshot, "reason", "") or "") != str(
            getattr(self, "_process_preflight_reason", "") or ""
        ):
            return
        self._accept_completed_launch_preflight()

    def _on_launch_preflight_timeout(self, generation: int) -> None:
        if generation != self._process_preflight_generation:
            return
        if not self._active or not self._process_preflight_pending:
            return
        self._fail_launch_preflight(
            "fresh process inventory was not available before the safety deadline"
        )

    def _fail_launch_preflight(self, detail: str) -> None:
        if not self._active:
            return
        self._process_preflight_pending = False
        message = f"launch safety preflight failed; nothing was started: {detail}"
        log.error("LaunchOrchestrator: %s", message)
        for item in self._queue:
            result = self._result_for(item, status="failed", detail=message)
            self._results.append(result)
            self.sequence_progress.emit(result)
        self._finish_sequence(cancelled=False)

    @staticmethod
    def _sequence_identity_key(item: Any) -> str:
        if not isinstance(item, Mapping):
            return ""
        identity = str(item.get("instance_identity", "") or "").strip()
        return f"instance:{identity}" if identity else ""

    @staticmethod
    def _hardware_preflight_key(item: Any) -> str:
        if not isinstance(item, Mapping):
            return ""
        readiness = item.get("readiness_policy", {})
        if not isinstance(readiness, Mapping):
            return ""
        projection_key = str(readiness.get("hardware_projection_key", "") or "").strip()
        radio_ids = item.get("radio_ids", ())
        if not projection_key or not isinstance(radio_ids, (list, tuple)) or len(radio_ids) != 1:
            return ""
        return f"{int(radio_ids[0])}|{projection_key.casefold()}"

    def _fresh_hardware_projection_state(
        self,
        item: Any,
        *,
        requested_generation: int,
    ) -> str:
        """Refresh one queue item's projection from a launch-time generation."""

        if not isinstance(item, dict):
            return "blocked"
        lifecycle = self.hardware_lifecycle.latest() if self.hardware_lifecycle is not None else None
        if lifecycle is None or int(lifecycle.generation) < int(requested_generation):
            return "pending"
        readiness = dict(item.get("readiness_policy", {}) or {})
        readiness.pop("hardware_launch_block", None)
        projection_key = str(readiness.get("hardware_projection_key", "") or "").strip()
        radio_ids = item.get("radio_ids", ())
        radio_id = int(radio_ids[0]) if isinstance(radio_ids, (list, tuple)) and len(radio_ids) == 1 else 0
        bundle = self.multi_radio_store.get_radio_hardware_bundle(radio_id) if radio_id > 0 else None
        snapshot = lifecycle.radios.get(radio_id) if bundle is not None else None
        if bundle is None or snapshot is None:
            readiness["hardware_launch_block"] = "Fresh radio hardware evidence is unavailable."
            item["readiness_policy"] = readiness
            return "blocked"
        if int(snapshot.bundle_generation) != int(bundle.get("generation", 0) or 0):
            readiness["hardware_launch_block"] = "The radio hardware assignment changed before launch."
            item["readiness_policy"] = readiness
            return "blocked"
        projection = build_hardware_launch_projection(
            self._queue_item_name(item),
            snapshot,
            projection_key=projection_key,
            receive_only=str(item.get("execution_scope", "") or "").strip().lower()
            == "receive_only",
        )
        if projection.blocked:
            readiness["hardware_launch_block"] = projection.reason
            item["readiness_policy"] = readiness
            return "blocked"
        environment = dict(item.get("environment", {}) or {})
        environment.update(dict(projection.environment))
        item["environment"] = environment
        readiness["hardware_projected_roles"] = projection.verified_roles
        readiness["hardware_inventory_generation"] = int(lifecycle.generation)
        item["readiness_policy"] = readiness
        return "ready"

    @staticmethod
    def _endpoint_preflight_key(item: Any) -> str:
        if not LaunchOrchestrator._has_persisted_endpoint_identity(item):
            return ""
        daemon = parse_custom_rigctld(str(item.get("launch_command_override") or ""))
        if daemon is not None:
            return "custom-rigctld:" + shlex.join((daemon.executable, *daemon.arguments))
        policy = item.get("readiness_policy", {})
        identity = str(item.get("instance_identity", "") or "").strip()
        name = LaunchOrchestrator._queue_item_name(item)
        host = str(policy.get("host", "") or "").strip().casefold()
        try:
            port = int(policy.get("port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        return f"{identity}|{name}|{host}|{port}"

    def _configured_endpoint_preflight_state(
        self,
        item: Any,
        endpoint_key: str,
    ) -> str:
        self._endpoint_preflight_requested = getattr(
            self,
            "_endpoint_preflight_requested",
            set(),
        )
        force = endpoint_key not in self._endpoint_preflight_requested
        if force:
            self._endpoint_preflight_requested.add(endpoint_key)
        info = self._cached_status_for_item(item, force=force)
        try:
            checked_at = float(info.get("checked_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            checked_at = 0.0
        source = str(info.get("source", "") or "").strip().lower()
        # The endpoint result must belong to this launch attempt.  A prior
        # cached failure must not authorize a spawn after an app was started
        # outside FIO, and the cold process fallback is never endpoint proof.
        evidence_complete = (
            source == "endpoint"
            and checked_at
            >= float(getattr(self, "_sequence_preflight_started_wall", 0.0) or 0.0)
        )
        if not evidence_complete:
            return "pending"
        if self._custom_rigctld_identity(item) is not None:
            return "occupied" if bool(info.get("endpoint_occupied", False)) else "clear"
        return "occupied" if bool(info.get("reachable", False)) else "clear"

    def _advance_queue(self) -> None:
        if not self._active:
            return
        if bool(getattr(self, "_process_preflight_pending", False)):
            return
        if self._cancel_requested:
            self._finish_sequence(cancelled=True)
            return
        if self._index >= len(self._queue):
            if self._queue:
                last_item = self._queue[-1]
                if isinstance(last_item, Mapping) and bool(
                    last_item.get("radio_control_gate_required", False)
                ):
                    radio_id = self._radio_id_for_item(last_item)
                    gate_state = getattr(self, "_radio_control_gate_states", {}).get(
                        radio_id,
                        "pending",
                    )
                    if gate_state not in {"ready", "blocked"} and not self._radio_control_app_failed(last_item):
                        self._begin_radio_control_gate(last_item)
                        return
            self._finish_sequence(cancelled=False)
            return
        queue_item = self._queue[self._index]
        name = self._queue_item_name(queue_item)
        if not name:
            self._index += 1
            self._schedule_advance_queue(0)
            return
        hardware_preflight_key = self._hardware_preflight_key(queue_item)
        if (
            hardware_preflight_key
            and hardware_preflight_key not in self._hardware_preflight_verified
        ):
            requested_generation = self._hardware_preflight_requested.get(
                hardware_preflight_key,
                0,
            )
            if requested_generation <= 0:
                if self.hardware_lifecycle is None:
                    self.hardware_lifecycle = shared_hardware_lifecycle_service(
                        self.multi_radio_store
                    )
                requested_generation = self.hardware_lifecycle.request_refresh()
                self._hardware_preflight_requested[hardware_preflight_key] = requested_generation
            hardware_state = self._fresh_hardware_projection_state(
                queue_item,
                requested_generation=requested_generation,
            )
            if hardware_state == "pending":
                self._current_name = name
                self._current_item = queue_item
                self._current_cmd = None
                self._current_phase = "hardware_preflight"
                self._current_started_monotonic = time.monotonic()
                self._poll_timer.setInterval(LAUNCH_ENDPOINT_PREFLIGHT_POLL_MS)
                self._poll_timer.start()
                return
            self._hardware_preflight_verified.add(hardware_preflight_key)
        readiness = queue_item.get("readiness_policy", {}) if isinstance(queue_item, Mapping) else {}
        hardware_block = (
            str(readiness.get("hardware_launch_block", "") or "").strip()
            if isinstance(readiness, Mapping)
            else ""
        )
        if hardware_block:
            self._index += 1
            result = self._result_for(
                queue_item,
                status="failed",
                detail=f"hardware launch blocked: {hardware_block}",
            )
            self._results.append(result)
            self.sequence_progress.emit(result)
            self._schedule_advance_queue(0)
            return
        if isinstance(queue_item, Mapping) and bool(
            queue_item.get("radio_control_gate_required", False)
        ):
            radio_id = self._radio_id_for_item(queue_item)
            gate_state = getattr(self, "_radio_control_gate_states", {}).get(
                radio_id,
                "pending",
            )
            control_app = str(queue_item.get("radio_control_app", "") or "").strip()
            if gate_state == "blocked":
                self._index += 1
                result = self._result_for(
                    queue_item,
                    status="blocked_radio_control",
                    detail=(
                        f"{self._radio_name_for_item(queue_item)} control did not provide "
                        "a fresh radio frequency readback"
                    ),
                )
                self._results.append(result)
                self.sequence_progress.emit(result)
                self._schedule_advance_queue(0)
                return
            if gate_state != "ready" and name != control_app:
                if self._radio_control_app_failed(queue_item):
                    self._radio_control_gate_states[radio_id] = "blocked"
                    log.warning(
                        "LaunchOrchestrator: skipping remaining %s apps because %s was not ready",
                        self._radio_name_for_item(queue_item),
                        control_app or "radio control",
                    )
                    self._schedule_advance_queue(0)
                    return
                self._begin_radio_control_gate(queue_item)
                return
        self._index += 1
        sequence_identity = self._sequence_identity_key(queue_item)
        claimed_identities = getattr(self, "_sequence_claimed_identities", set())
        if sequence_identity and sequence_identity in claimed_identities:
            result = self._result_for(
                queue_item,
                status="already_running",
                detail="already handled by this launch sequence",
            )
            self._results.append(result)
            self.sequence_progress.emit(result)
            self._schedule_advance_queue(0)
            return
        blocked_dependency = self._blocked_dependency_for(queue_item)
        if blocked_dependency:
            result = self._result_for(
                queue_item,
                status="blocked_dependency",
                detail=f"dependency {blocked_dependency} was not ready",
            )
            self._results.append(result)
            self.sequence_progress.emit(result)
            self._schedule_advance_queue(0)
            return
        identity_blocker = self._instance_launch_identity_blocker(queue_item)
        if identity_blocker:
            result = self._result_for(
                queue_item,
                status="failed",
                detail=identity_blocker,
            )
            self._results.append(result)
            self.sequence_progress.emit(result)
            log.warning(
                "LaunchOrchestrator: blocked unsafe %s launch identity: %s",
                name,
                identity_blocker,
            )
            self._schedule_advance_queue(0)
            return
        legacy_js8_profile = self._legacy_default_js8_profile_conflict(queue_item)
        if legacy_js8_profile:
            detail = (
                "an older CommsPlan-generated JS8Call profile is still running "
                f"({legacy_js8_profile}); close that JS8Call window or process, "
                "then choose Launch again to start the configured default profile"
            )
            result = self._result_for(
                queue_item,
                status="failed",
                detail=detail,
            )
            self._results.append(result)
            self.sequence_progress.emit(result)
            log.warning(
                "LaunchOrchestrator: blocked default JS8Call launch because legacy "
                "generated profile %s is still running",
                legacy_js8_profile,
            )
            self._schedule_advance_queue(0)
            return
        exact_process_running = self._configured_instance_process_running(queue_item)
        endpoint_key = self._endpoint_preflight_key(queue_item)
        if (
            endpoint_key
            and endpoint_key not in getattr(self, "_endpoint_preflight_verified", set())
        ):
            endpoint_state = self._configured_endpoint_preflight_state(
                queue_item,
                endpoint_key,
            )
            if endpoint_state == "pending":
                log.info(
                    "LaunchOrchestrator: verifying configured endpoint before %s launch",
                    name,
                )
                self._current_name = name
                self._current_item = queue_item
                self._current_cmd = None
                self._current_phase = "endpoint_preflight"
                self._current_started_monotonic = time.monotonic()
                self._poll_timer.setInterval(LAUNCH_ENDPOINT_PREFLIGHT_POLL_MS)
                self._poll_timer.start()
                return
            self._endpoint_preflight_verified = getattr(
                self,
                "_endpoint_preflight_verified",
                set(),
            )
            self._endpoint_preflight_verified.add(endpoint_key)
            if endpoint_state == "occupied":
                log.info(
                    "LaunchOrchestrator: skipped %s launch because its configured endpoint is active",
                    name,
                )
                if name == "JS8Call":
                    try:
                        self._persist_ready_js8_identity(
                            queue_item,
                            self._cached_status_for_item(queue_item),
                        )
                    except Exception as storage_exc:
                        log.warning(
                            "LaunchOrchestrator: JS8 endpoint persistence failed: %s",
                            storage_exc,
                        )
                daemon = self._custom_rigctld_identity(queue_item)
                owned = daemon is None or self._program_ready_for_sequence(queue_item)
                result = self._result_for(
                    queue_item,
                    status="already_running" if owned else "failed",
                    detail=("configured endpoint is already active" if owned else
                            "rigctld endpoint is occupied by an unverified process; duplicate launch skipped"),
                )
                self._results.append(result)
                if sequence_identity:
                    self._sequence_claimed_identities = getattr(
                        self,
                        "_sequence_claimed_identities",
                        set(),
                    )
                    self._sequence_claimed_identities.add(sequence_identity)
                self.sequence_progress.emit(result)
                self._schedule_advance_queue(0)
                return
            self._endpoint_preflight_clear = getattr(
                self,
                "_endpoint_preflight_clear",
                set(),
            )
            self._endpoint_preflight_clear.add(endpoint_key)
            if exact_process_running is True:
                waiting_for_control = self._is_gated_radio_control_app(queue_item)
                if waiting_for_control:
                    log.info(
                        "LaunchOrchestrator: exact %s process is running; waiting for its "
                        "configured radio-control endpoint",
                        name,
                    )
                else:
                    log.warning(
                        "LaunchOrchestrator: skipped duplicate %s launch; exact process is "
                        "running but its configured endpoint is not ready",
                        name,
                    )
                result = self._result_for(
                    queue_item,
                    status="already_running" if waiting_for_control else "failed",
                    detail=(
                        "exact configured process is active; waiting for endpoint and "
                        "radio readiness"
                        if waiting_for_control
                        else "configured process is running but its endpoint is not ready; "
                        "duplicate launch skipped"
                    ),
                )
                self._results.append(result)
                if sequence_identity:
                    self._sequence_claimed_identities = getattr(
                        self,
                        "_sequence_claimed_identities",
                        set(),
                    )
                    self._sequence_claimed_identities.add(sequence_identity)
                self.sequence_progress.emit(result)
                self._schedule_advance_queue(0)
                return
        # An exact executable + radio-selector match is terminal evidence that
        # this specific process-only instance is already running.  Do not let
        # the later family-level multi-instance branch reinterpret that match
        # as permission to start another copy merely because the queue also
        # contains a second radio's FLAmp, FLMsg, VarAC, or VARA row.
        if exact_process_running is True:
            log.info(
                "LaunchOrchestrator: skipped %s launch because its exact configured process is active",
                name,
            )
            daemon = self._custom_rigctld_identity(queue_item)
            verified = daemon is None or self._program_ready_for_sequence(queue_item)
            result = self._result_for(
                queue_item,
                status="already_running" if verified else "failed",
                detail=("exact configured process is already active" if verified else
                        "configured rigctld is running; readback unverified, duplicate launch skipped"),
            )
            self._results.append(result)
            if sequence_identity:
                self._sequence_claimed_identities = getattr(
                    self,
                    "_sequence_claimed_identities",
                    set(),
                )
                self._sequence_claimed_identities.add(sequence_identity)
            self.sequence_progress.emit(result)
            self._schedule_advance_queue(0)
            return
        unattributed_process = self._unattributed_process_blocker(
            queue_item,
            exact_process_running,
            endpoint_clear=(
                bool(endpoint_key)
                and endpoint_key
                in getattr(self, "_endpoint_preflight_clear", set())
            ),
        )
        if unattributed_process:
            log.warning(
                "LaunchOrchestrator: skipped duplicate-risk %s launch: %s",
                name,
                unattributed_process,
            )
            result = self._result_for(
                queue_item,
                status="failed",
                detail=unattributed_process,
            )
            self._results.append(result)
            if sequence_identity:
                self._sequence_claimed_identities = getattr(
                    self,
                    "_sequence_claimed_identities",
                    set(),
                )
                self._sequence_claimed_identities.add(sequence_identity)
            self.sequence_progress.emit(result)
            self._schedule_advance_queue(0)
            return
        if self._program_running(queue_item):
            same_name_identities = {
                str(value.get("instance_identity", "") or "")
                for value in self._queue
                if isinstance(value, Mapping) and self._queue_item_name(value) == name
            }
            has_distinct_instances = len(same_name_identities - {""}) > 1
            endpoint_scoped = name in {"JS8Call", "FLRig", "FLDigi"}
            ready = self._program_ready_for_sequence(queue_item)
            if ready and (not has_distinct_instances or endpoint_scoped):
                if name == "JS8Call":
                    try:
                        self._persist_ready_js8_identity(queue_item, self._cached_status_for_item(queue_item))
                    except Exception as storage_exc:
                        log.warning("LaunchOrchestrator: JS8 ready-state persistence failed: %s", storage_exc)
                result = self._result_for(queue_item, status="already_running", detail="already running")
                self._results.append(result)
                if sequence_identity:
                    self._sequence_claimed_identities = getattr(
                        self,
                        "_sequence_claimed_identities",
                        set(),
                    )
                    self._sequence_claimed_identities.add(sequence_identity)
                self.sequence_progress.emit(result)
                self._schedule_advance_queue(0)
                return
            # A selected-radio plan contains only that radio's rows.  A
            # different radio's same-named process must therefore not make us
            # wait forever on this radio's absent endpoint.  Only use this
            # recovery when no exact configured process identity is running;
            # an exact process may simply still be starting its service.
            selected_endpoint_requires_launch = (
                not has_distinct_instances
                and not ready
                and self._has_persisted_endpoint_identity(queue_item)
                and exact_process_running is not True
            )
            should_launch_distinct = (
                has_distinct_instances and (not ready or not endpoint_scoped)
            ) or selected_endpoint_requires_launch
            if not should_launch_distinct:
                self._current_name = name
                self._current_item = queue_item
                self._current_cmd = None
                self._current_process = None
                self._current_launch_description = ""
                self._current_phase = "readiness"
                self._current_started_monotonic = time.monotonic()
                self._poll_timer.setInterval(LAUNCH_READINESS_INITIAL_POLL_MS)
                self._poll_timer.start()
                return
        cmd, cmd_desc = self._resolve_launch_command(queue_item)
        if not cmd:
            result = self._result_for(queue_item, status="failed", detail="no launch command")
            self._results.append(result)
            self.sequence_progress.emit(result)
            self._schedule_advance_queue(0)
            return
        if self._is_self_launch_command(cmd):
            result = self._result_for(queue_item, status="blocked_self", detail="blocked self-launch target")
            self._results.append(result)
            self.sequence_progress.emit(result)
            log.warning("LaunchOrchestrator: blocked self-launch target for %s via %r", name, cmd)
            self._schedule_advance_queue(0)
            return
        try:
            self._prepare_hardware_native_projection(queue_item)
            self._materialize_item_managed_directories(queue_item)
            creationflags = 0
            if platform.system() == "Windows":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            cwd = self._infer_launch_cwd(name, cmd, cmd_desc, queue_item)
            raw_environment = queue_item.get("environment", {}) if isinstance(queue_item, Mapping) else {}
            environment = None
            if isinstance(raw_environment, Mapping) and raw_environment:
                environment = os.environ.copy()
                environment.update(
                    {
                        str(key): str(value)
                        for key, value in raw_environment.items()
                        if str(key).strip()
                    }
                )
            process = subprocess.Popen(
                cmd,
                shell=False,
                creationflags=creationflags,
                cwd=cwd,
                env=environment,
            )
            self._track_launched_process(process, label=name)
            self._schedule_process_window_title(queue_item, process)
            if sequence_identity:
                self._sequence_claimed_identities = getattr(
                    self,
                    "_sequence_claimed_identities",
                    set(),
                )
                self._sequence_claimed_identities.add(sequence_identity)
            if name == "JS8Call":
                try:
                    self._persist_planned_js8_storage(queue_item)
                except Exception as storage_exc:
                    # Storage metadata is diagnostic/reconciliation state.  A
                    # successful process start must not be reported as failed
                    # because this auxiliary persistence step had a problem.
                    log.warning("LaunchOrchestrator: JS8 storage-plan persistence failed: %s", storage_exc)
            try:
                self.dependency_status.refresh_now(reason=f"launch:{name}", force=True)
            except Exception:
                pass
            if cwd:
                log.info("LaunchOrchestrator: launched %s via %s (cwd=%s)", name, cmd_desc, cwd)
            else:
                log.info("LaunchOrchestrator: launched %s via %s", name, cmd_desc)
            self._current_name = name
            self._current_item = queue_item
            self._current_cmd = cmd
            self._current_process = process
            self._current_launch_description = cmd_desc
            self._current_phase = "readiness"
            self._current_started_monotonic = time.monotonic()
            self._poll_timer.setInterval(LAUNCH_READINESS_INITIAL_POLL_MS)
            self._poll_timer.start()
        except Exception as e:
            log.error("LaunchOrchestrator: failed launching %s via %s: %s", name, cmd_desc, e)
            result = self._result_for(queue_item, status="failed", detail=str(e))
            self._results.append(result)
            self.sequence_progress.emit(result)
            self._schedule_advance_queue(0)

    def _track_launched_process(self, process: Any, *, label: str = "") -> None:
        """Retain and non-blockingly reap a process started by FIO."""

        poll = getattr(process, "poll", None)
        if not callable(poll):
            return
        try:
            pid = int(getattr(process, "pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        key = pid if pid > 0 else id(process)
        owned = getattr(self, "_launched_processes", None)
        if not isinstance(owned, dict):
            owned = {}
            self._launched_processes = owned
        owned[key] = process
        labels = getattr(self, "_launched_process_labels", None)
        if not isinstance(labels, dict):
            labels = {}
            self._launched_process_labels = labels
        labels[key] = str(label or "application")[:160]
        self._reap_launched_processes()
        timer = getattr(self, "_process_reaper_timer", None)
        if owned and timer is not None:
            try:
                if not timer.isActive():
                    timer.start()
            except Exception:
                pass

    def _reap_launched_processes(self) -> None:
        """Poll owned children so exited launchers never remain as zombies."""

        owned = getattr(self, "_launched_processes", None)
        if not isinstance(owned, dict):
            return
        for key, process in tuple(owned.items()):
            try:
                return_code = process.poll()
            except (ChildProcessError, ProcessLookupError):
                return_code = -1
            except Exception as exc:
                log.warning("LaunchOrchestrator: could not poll launched process %s: %s", key, exc)
                continue
            if return_code is None:
                continue
            owned.pop(key, None)
            labels = getattr(self, "_launched_process_labels", {})
            label = labels.pop(key, "application")
            (log.warning if return_code else log.info)(
                "LaunchOrchestrator: reaped launched process name=%s pid=%s return_code=%s",
                label, key, return_code,
            )
        timer = getattr(self, "_process_reaper_timer", None)
        if not owned and timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

    @staticmethod
    def _window_title_for_item(item: Any) -> str:
        if not isinstance(item, Mapping):
            return ""
        readiness = item.get("readiness_policy", {})
        if not isinstance(readiness, Mapping):
            return ""
        saved = str(readiness.get("window_title") or "").strip()
        if saved:
            return saved
        name = LaunchOrchestrator._queue_item_name(item)
        radio_names = item.get("radio_names", ())
        if not isinstance(radio_names, (list, tuple)) or len(radio_names) != 1:
            return ""
        if name == "JS8Call":
            if str(item.get("rig_name_source", "") or "").strip() not in {
                "legacy_default",
                "settings_default",
            }:
                return ""
            # The legacy/default JS8 namespace must launch without
            # --rig-name so it continues to read the operator's configured
            # JS8Call.ini. Apply only a presentation title to the exact PID.
            return managed_instance_window_title("JS8Call", radio_names[0])
        if name != "VarAC":
            return ""
        nested = readiness.get("launch_recipe")
        recipe = nested if isinstance(nested, Mapping) else readiness
        if not (
            LaunchOrchestrator.is_truthy(readiness.get("structured_launch", False))
            or "executable" in recipe
            or "launch_executable" in recipe
            or "launch_arguments" in recipe
        ):
            return ""
        # Compatibility for already-saved structured VarAC rows: the stable
        # selected-radio context is sufficient to derive presentation text,
        # but never changes the executable/INI launch identity.
        return managed_instance_window_title("VarAC", radio_names[0])

    def _schedule_process_window_title(self, item: Any, process: Any) -> None:
        """Apply a non-native title without delaying or blocking launch.

        FLMsg and FLAmp consume their supported ``-title`` argument directly.
        VarAC has no qualified title argument. A migrated single-radio JS8Call
        must omit --rig-name to retain its native default settings file. FIO
        retries a PID-scoped OS title update for those two cases while the first
        window is being created. A compositor may reject this presentation-only
        request; that never changes process readiness or the launch result.
        """

        name = self._queue_item_name(item)
        if name not in {"VarAC", "JS8Call"}:
            return
        title = self._window_title_for_item(item)
        try:
            pid = int(getattr(process, "pid", 0) or 0)
        except (TypeError, ValueError):
            return
        if not title or pid <= 0:
            return

        def _attempt(remaining: int) -> None:
            if set_process_window_title(pid, title):
                log.info("LaunchOrchestrator: set %s window title to %s", name, title)
                return
            if remaining > 1:
                QTimer.singleShot(750, lambda: _attempt(remaining - 1))
                return
            log.warning(
                "LaunchOrchestrator: %s started, but the desktop did not permit "
                "the requested radio title '%s'; launch remains valid.",
                name,
                title,
            )

        QTimer.singleShot(250, lambda: _attempt(12))

    @staticmethod
    def _materialize_item_managed_directories(item: Any) -> tuple[Path, ...]:
        """Repair only directory targets authorized by a managed recipe.

        Final Save normally prepares these paths.  The launch preflight keeps
        older canonical recipes recoverable and prevents an absent ``cwd``
        from making an application exit without a useful FIO error.  VarAC and
        shared/operator-start components deliberately authorize no generic
        directories here.
        """

        if not isinstance(item, Mapping) or not item.get("instance_identity"):
            return ()
        policy = item.get("readiness_policy", {})
        if not isinstance(policy, Mapping):
            return ()
        component_key = {
            "FLRig": "flrig",
            "FLDigi": "fldigi",
            "FLMsg": "flmsg",
            "FLAmp": "flamp",
            "JS8Call": "js8call",
        }.get(LaunchOrchestrator._queue_item_name(item), "")
        if not component_key:
            return ()
        paths = managed_directories_from_component(policy, component_key=component_key)
        return materialize_managed_directories(paths)

    @staticmethod
    def _instance_launch_identity_blocker(item: Any) -> str:
        """Reject ambiguous multi-instance NBEMS launch identities.

        FLMsg and FLAmp share installation binaries across radios.  A
        radio-scoped row is launchable only when its native selector is part of
        the exact argv.  Deliberately station-shared utilities and explicit
        operator-start rows remain on their existing compatibility paths.
        """

        if not isinstance(item, Mapping):
            return ""
        name = LaunchOrchestrator._queue_item_name(item)
        if name not in {"FLMsg", "FLAmp"}:
            return ""
        scope = str(item.get("execution_scope", "standard") or "standard").strip().lower()
        if scope == "station_shared_utility" or bool(item.get("operator_starts", False)):
            return ""
        arguments = item.get("launch_arguments", ())
        if not isinstance(arguments, (list, tuple)):
            arguments = ()
        values = tuple(str(value or "").strip() for value in arguments)

        def _missing_value(flag: str) -> bool:
            try:
                index = values.index(flag)
            except ValueError:
                return True
            return index + 1 >= len(values) or not values[index + 1]

        required = (
            ("--flmsg-dir",)
            if name == "FLMsg"
            else (
                "--config-dir",
                "--arq-server-address",
                "--arq-server-port",
                "--xmlrpc-server-address",
                "--xmlrpc-server-port",
            )
        )
        missing = tuple(flag for flag in required if _missing_value(flag))
        if not missing:
            return ""
        readiness = item.get("readiness_policy", {})
        readiness = readiness if isinstance(readiness, Mapping) else {}
        evidence = readiness.get("evidence", {})
        evidence = evidence if isinstance(evidence, Mapping) else {}
        canonical_source = {
            "FLMsg": "nbems_native_radio_root",
            "FLAmp": "flamp_config_dir_and_endpoint_pair",
        }[name]
        profile_selector = str(item.get("profile_selector", "") or "").strip()
        # An adopted, version-qualified launcher may use a different native
        # argument grammar.  It remains safe only when it still supplies an
        # explicit per-instance selector and a non-empty exact argv.  Canonical
        # FIO recipes must always satisfy the current qualified grammar.
        if (
            values
            and profile_selector
            and str(evidence.get("source", "") or "").strip() != canonical_source
        ):
            return ""
        return (
            f"{name} radio-scoped launch identity is incomplete; missing "
            + ", ".join(missing)
            + ". Review or replace this software instance before launch."
        )

    def _persist_planned_js8_storage(self, item: Any) -> None:
        """Persist launch identity without claiming runtime verification.

        The later background reconciliation owns bounded message-file evidence
        checks.  A previously verified, different root is never replaced here.
        """

        if not isinstance(item, Mapping):
            return
        root = str(item.get("application_data_root", "") or "").strip()
        rig_name = str(item.get("rig_name", "") or "").strip()
        if not root or not rig_name:
            return
        for raw_radio_id in item.get("radio_ids", ()):
            try:
                profile = self.multi_radio_store.get_device_profile(int(raw_radio_id)) or {}
                instance_id = int(profile.get("js8_instance_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if instance_id <= 0:
                continue
            existing = self.multi_radio_store.get_js8_instance(instance_id) or {}
            existing_root = str(existing.get("application_data_root", "") or "").strip()
            existing_evidence = str(existing.get("storage_evidence", "") or "").strip()
            if (
                existing_root
                and existing_root != root
                and existing_evidence.startswith(("operator_confirmed", "runtime_verified"))
            ):
                log.warning(
                    "LaunchOrchestrator: retained verified JS8 storage root for %s; planned root differs",
                    profile.get("name", raw_radio_id),
                )
                self.multi_radio_store.save_js8_instance(
                    {
                        **dict(existing),
                        "id": instance_id,
                        "storage_mode": "unverified",
                        "storage_evidence": "mismatch:launch_planned",
                    }
                )
                continue
            expected_mode = str(item.get("expected_storage_mode", "unverified") or "unverified")
            updated = dict(existing)
            updated.update(
                {
                    "id": instance_id,
                    "rig_name": rig_name,
                    "rig_name_source": str(item.get("rig_name_source", "") or "managed"),
                    "application_data_root": root,
                    "all_path": str(Path(root) / "ALL.TXT"),
                    "directed_path": str(Path(root) / "DIRECTED.TXT"),
                    "inbox_path": str(Path(root) / "inbox.db3"),
                    "storage_mode": "shared" if expected_mode == "shared" else "unverified",
                    "storage_verified_utc": "",
                    "storage_evidence": "launch_planned",
                }
            )
            self.multi_radio_store.save_js8_instance(updated)

    def _blocked_dependency_for(self, item: Any) -> str:
        if not isinstance(item, Mapping):
            return ""
        dependencies = item.get("dependencies", [])
        if not isinstance(dependencies, list) or not dependencies:
            return ""
        radio_ids = {int(value) for value in item.get("radio_ids", []) if str(value).strip()}
        success_states = {"launched", "already_running"}
        queue_names = {self._queue_item_name(value) for value in self._queue}
        for dependency in (str(value).strip() for value in dependencies):
            if not dependency or dependency not in queue_names:
                continue
            matching: List[Mapping[str, Any]] = []
            for result in self._results:
                if str(result.get("name", "") or "") != dependency:
                    continue
                result_radios = {int(value) for value in result.get("radio_ids", []) if str(value).strip()}
                if radio_ids and result_radios and radio_ids.isdisjoint(result_radios):
                    continue
                matching.append(result)
            successful = [result for result in matching if str(result.get("status", "")) in success_states]
            if not successful:
                return dependency
            if radio_ids:
                covered_radios: set[int] = set()
                for result in successful:
                    covered_radios.update(int(value) for value in result.get("radio_ids", []) if str(value).strip())
                if not radio_ids.issubset(covered_radios):
                    return dependency
        return ""

    def _poll_current_readiness(self) -> None:
        if not self._active:
            self._poll_timer.stop()
            return
        if self._cancel_requested:
            self._poll_timer.stop()
            self._finish_sequence(cancelled=True)
            return
        name = self._current_name
        if not name:
            self._poll_timer.stop()
            self._schedule_advance_queue(0)
            return
        elapsed = max(0.0, time.monotonic() - self._current_started_monotonic)
        if self._current_phase == "radio_schedule_apply":
            radio_id = self._radio_id_for_item(self._current_item)
            try:
                provider = getattr(self, "_schedule_handoff_provider", None)
                status = provider(radio_id) if callable(provider) else "blocked"
            except Exception:
                log.exception("LaunchOrchestrator: radio %s schedule acknowledgement failed", radio_id)
                status = "blocked"
            if status in {"ready", "not_required"} or status == "blocked" or elapsed >= LAUNCH_SCHEDULE_HANDOFF_TIMEOUT_SEC:
                self._poll_timer.stop()
                passed = status in {"ready", "not_required"}
                if not passed:
                    self._radio_control_gate_states[radio_id] = "blocked"
                (log.info if passed else log.warning)(
                        "LaunchOrchestrator: radio %s schedule handoff %s status=%s elapsed_s=%.1f", radio_id,
                        status if passed else "unavailable; dependent apps blocked", status, elapsed)
                self.sequence_progress.emit({
                    "name": name, "status": "schedule_ready" if passed else "schedule_unavailable",
                    "detail": ("current frequency verified" if status == "ready" else
                               "automatic frequency change not required" if passed else
                               "Schedule verification timed out; review Station Health before retrying Launch Control."
                               if status == "pending" else
                               "Schedule verification is unavailable; review Station Health before retrying Launch Control."),
                    "counts_toward_total": False, "radio_ids": [radio_id],
                })
                self._current_name = None
                self._current_item = None
                self._current_cmd = None
                self._current_phase = ""
                self._schedule_advance_queue(0)
            return
        if self._current_phase == "radio_control_gate":
            item = self._current_item or {}
            radio_id = self._radio_id_for_item(item)
            state = self._radio_control_evidence_state(
                item,
                force=self._radio_control_probe_is_due(item),
            )
            if state == "ready":
                self._poll_timer.stop()
                self._begin_schedule_handoff(item)
                return
            if elapsed >= LAUNCH_ENDPOINT_PREFLIGHT_TIMEOUT_SEC:
                self._log_radio_control_readback_failure(item)
                self._poll_timer.stop()
                self._radio_control_gate_states[radio_id] = "blocked"
                radio_name = self._radio_name_for_item(item)
                control_app = str(item.get("radio_control_app", "") or "").strip() if isinstance(item, Mapping) else ""
                for result in self._results:
                    result_radios = self._radio_ids_for_item(result)
                    if (
                        control_app
                        and str(result.get("name", "") or "") == control_app
                        and (not result_radios or radio_id in result_radios)
                        and str(result.get("status", "") or "") == "already_running"
                    ):
                        result["status"] = "timeout"
                        result["detail"] = "application is running, but radio-control readback is unavailable"
                log.warning(
                    "LaunchOrchestrator: %s control unavailable; remaining radio apps will be skipped",
                    radio_name,
                )
                self.sequence_progress.emit(
                    {
                        "name": f"{radio_name} control",
                        "status": "unavailable",
                        "detail": "fresh transceiver and frequency readback was not available",
                        "counts_toward_total": False,
                        "radio_ids": [radio_id],
                    }
                )
                self._current_name = None
                self._current_item = None
                self._current_cmd = None
                self._current_phase = ""
                self._schedule_advance_queue(0)
                return
            return
        if self._current_phase == "hardware_preflight":
            item = self._current_item or {}
            hardware_key = self._hardware_preflight_key(item)
            requested_generation = self._hardware_preflight_requested.get(hardware_key, 0)
            state = self._fresh_hardware_projection_state(
                item,
                requested_generation=requested_generation,
            )
            if state == "pending" and elapsed < LAUNCH_ENDPOINT_PREFLIGHT_TIMEOUT_SEC:
                return
            if state == "pending":
                readiness = dict(item.get("readiness_policy", {}) or {})
                readiness["hardware_launch_block"] = (
                    "Fresh radio hardware evidence was not available before the launch deadline."
                )
                if isinstance(item, dict):
                    item["readiness_policy"] = readiness
            self._poll_timer.stop()
            if hardware_key:
                self._hardware_preflight_verified.add(hardware_key)
            self._current_name = None
            self._current_item = None
            self._current_cmd = None
            self._current_phase = ""
            self._schedule_advance_queue(0)
            return
        if self._current_phase == "endpoint_preflight":
            item = self._current_item or name
            endpoint_key = self._endpoint_preflight_key(item)
            state = self._configured_endpoint_preflight_state(item, endpoint_key)
            if state != "pending":
                self._poll_timer.stop()
                self._endpoint_preflight_verified = getattr(
                    self,
                    "_endpoint_preflight_verified",
                    set(),
                )
                self._endpoint_preflight_verified.add(endpoint_key)
                sequence_identity = self._sequence_identity_key(item)
                if state == "occupied":
                    log.info(
                        "LaunchOrchestrator: skipped %s launch because its configured endpoint is active",
                        name,
                    )
                    if name == "JS8Call":
                        try:
                            self._persist_ready_js8_identity(
                                item,
                                self._cached_status_for_item(item),
                            )
                        except Exception as storage_exc:
                            log.warning(
                                "LaunchOrchestrator: JS8 endpoint persistence failed: %s",
                                storage_exc,
                            )
                    result = self._result_for(
                        item,
                        status="already_running",
                        detail="configured endpoint is already active",
                    )
                    self._results.append(result)
                    if sequence_identity:
                        self._sequence_claimed_identities = getattr(
                            self,
                            "_sequence_claimed_identities",
                            set(),
                        )
                        self._sequence_claimed_identities.add(sequence_identity)
                    self.sequence_progress.emit(result)
                else:
                    self._endpoint_preflight_clear = getattr(
                        self,
                        "_endpoint_preflight_clear",
                        set(),
                    )
                    self._endpoint_preflight_clear.add(endpoint_key)
                    exact_process_running = self._configured_instance_process_running(item)
                    if exact_process_running is True:
                        waiting_for_control = self._is_gated_radio_control_app(item)
                        if waiting_for_control:
                            log.info(
                                "LaunchOrchestrator: exact %s process is running; waiting for "
                                "its configured radio-control endpoint",
                                name,
                            )
                        else:
                            log.warning(
                                "LaunchOrchestrator: skipped duplicate %s launch; exact process "
                                "is running but its configured endpoint is not ready",
                                name,
                            )
                        result = self._result_for(
                            item,
                            status="already_running" if waiting_for_control else "failed",
                            detail=(
                                "exact configured process is active; waiting for endpoint and "
                                "radio readiness"
                                if waiting_for_control
                                else "configured process is running but its endpoint is not "
                                "ready; duplicate launch skipped"
                            ),
                        )
                        self._results.append(result)
                        if sequence_identity:
                            self._sequence_claimed_identities = getattr(
                                self,
                                "_sequence_claimed_identities",
                                set(),
                            )
                            self._sequence_claimed_identities.add(sequence_identity)
                        self.sequence_progress.emit(result)
                    else:
                        # _advance_queue already consumed this item.  Revisit it
                        # once with fresh negative endpoint evidence so the normal
                        # identity, dependency, and command safety checks still run.
                        self._index = max(0, self._index - 1)
                self._current_name = None
                self._current_item = None
                self._current_cmd = None
                self._current_phase = ""
                self._schedule_advance_queue(0)
                return
            if elapsed >= LAUNCH_ENDPOINT_PREFLIGHT_TIMEOUT_SEC:
                self._poll_timer.stop()
                exact_process_running = self._configured_instance_process_running(item)
                waiting_for_control = bool(
                    exact_process_running is True
                    and self._is_gated_radio_control_app(item)
                )
                if waiting_for_control:
                    log.info(
                        "LaunchOrchestrator: exact %s process remains active after endpoint "
                        "preflight; continuing with bounded radio-control readiness",
                        name,
                    )
                else:
                    log.warning(
                        "LaunchOrchestrator: skipped %s launch because configured endpoint "
                        "verification timed out",
                        name,
                    )
                result = self._result_for(
                    item,
                    status="already_running" if waiting_for_control else "failed",
                    detail=(
                        "exact configured process is active; waiting for endpoint and "
                        "radio readiness"
                        if waiting_for_control
                        else "configured endpoint could not be verified; launch was skipped "
                        "to prevent a duplicate instance"
                    ),
                )
                self._results.append(result)
                if waiting_for_control:
                    sequence_identity = self._sequence_identity_key(item)
                    if sequence_identity:
                        self._sequence_claimed_identities = getattr(
                            self,
                            "_sequence_claimed_identities",
                            set(),
                        )
                        self._sequence_claimed_identities.add(sequence_identity)
                self.sequence_progress.emit(result)
                self._current_name = None
                self._current_item = None
                self._current_cmd = None
                self._current_phase = ""
                self._schedule_advance_queue(0)
                return
            return
        desired_interval = (
            LAUNCH_READINESS_RELAXED_POLL_MS
            if elapsed >= LAUNCH_READINESS_RELAX_AFTER_SEC
            else LAUNCH_READINESS_INITIAL_POLL_MS
        )
        if self._poll_timer.interval() != desired_interval:
            self._poll_timer.setInterval(desired_interval)
        exit_failure = self._current_launch_exit_failure(self._current_item or name)
        if exit_failure:
            self._poll_timer.stop()
            result = self._result_for(
                self._current_item or name,
                status="failed",
                detail=exit_failure,
            )
            self._results.append(result)
            self.sequence_progress.emit(result)
            self._current_name = None
            self._current_item = None
            self._current_cmd = None
            self._current_process = None
            self._current_launch_description = ""
            self._current_phase = ""
            self._schedule_advance_queue(0)
            return
        if self._program_ready_for_sequence(self._current_item or name):
            self._poll_timer.stop()
            if name == "JS8Call":
                try:
                    ready_info = self._cached_status_for_item(self._current_item or name)
                    self._persist_ready_js8_identity(self._current_item, ready_info)
                except Exception as storage_exc:
                    log.warning("LaunchOrchestrator: JS8 ready-state persistence failed: %s", storage_exc)
            delay_sec = self._post_ready_settle_delay_seconds(name)
            detail = f"ready in {elapsed:.1f}s"
            if delay_sec > 0:
                detail += f"; waiting {delay_sec:.1f}s before next launch"
            result = self._result_for(self._current_item or name, status="launched", detail=detail)
            self._results.append(result)
            ready_item = self._current_item
            control_ready = self._is_gated_radio_control_app(ready_item)
            self.sequence_progress.emit(result)
            self._current_name = None
            self._current_item = None
            self._current_cmd = None
            self._current_process = None
            self._current_launch_description = ""
            self._current_phase = ""
            if control_ready:
                self._begin_schedule_handoff(ready_item)
            else:
                self._schedule_advance_queue(int(delay_sec * 1000.0))
            return
        readiness_timeout = float(self._wait_timeout_sec)
        if (
            isinstance(self._current_item, Mapping)
            and bool(self._current_item.get("radio_control_gate_required", False))
            and name == str(self._current_item.get("radio_control_app", "") or "").strip()
        ):
            readiness_timeout = min(readiness_timeout, LAUNCH_RADIO_CONTROL_READY_TIMEOUT_SEC)
        if elapsed >= readiness_timeout:
            if self._is_gated_radio_control_app(self._current_item):
                self._log_radio_control_readback_failure(self._current_item)
            self._poll_timer.stop()
            result = self._result_for(
                self._current_item or name,
                status="timeout",
                detail=f"not ready after {readiness_timeout:g}s",
            )
            self._results.append(result)
            self.sequence_progress.emit(result)
            self._current_name = None
            self._current_item = None
            self._current_cmd = None
            self._current_process = None
            self._current_launch_description = ""
            self._current_phase = ""
            self._schedule_advance_queue(0)

    def _persist_ready_js8_identity(self, item: Any, status: Mapping[str, Any]) -> None:
        """Record an API-observed variant; file verification remains background-owned."""

        if not isinstance(item, dict):
            return
        version = str(status.get("version", "") or "").strip()
        variant_family = variant_family_from_version(version)
        if variant_family == "unknown":
            return
        rig_name = str(item.get("rig_name", "") or "").strip()
        resolution = resolve_js8_storage(
            {
                "variant_family": variant_family,
                "variant_version": version,
                "rig_name": rig_name,
                "rig_name_source": str(item.get("rig_name_source", "") or "managed"),
            },
            probe_existing=False,
        )
        root = str(resolution.data_root or "").strip()
        if not root:
            return
        item["application_data_root"] = root
        item["storage_mode"] = resolution.storage_mode
        item["expected_storage_mode"] = resolution.expected_mode
        for raw_radio_id in item.get("radio_ids", ()):
            try:
                profile = self.multi_radio_store.get_device_profile(int(raw_radio_id)) or {}
                instance_id = int(profile.get("js8_instance_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if instance_id <= 0:
                continue
            existing = self.multi_radio_store.get_js8_instance(instance_id) or {}
            existing_root = str(existing.get("application_data_root", "") or "").strip()
            existing_evidence = str(existing.get("storage_evidence", "") or "").strip()
            if (
                existing_root
                and existing_root != root
                and existing_evidence.startswith(("operator_confirmed", "runtime_verified"))
            ):
                log.warning(
                    "LaunchOrchestrator: API-observed JS8 identity differs from the verified storage root for %s",
                    profile.get("name", raw_radio_id),
                )
                self.multi_radio_store.save_js8_instance(
                    {
                        **dict(existing),
                        "id": instance_id,
                        "variant_family": variant_family,
                        "variant_version": version,
                        "storage_mode": "unverified",
                        "storage_evidence": f"mismatch:api_observed:{version}",
                    }
                )
                continue
            updated = dict(existing)
            updated.update(
                {
                    "id": instance_id,
                    "variant_family": variant_family,
                    "variant_version": version,
                    "rig_name": rig_name,
                    "rig_name_source": str(item.get("rig_name_source", "") or "managed"),
                    "application_data_root": root,
                    "all_path": str(Path(root) / "ALL.TXT"),
                    "directed_path": str(Path(root) / "DIRECTED.TXT"),
                    "inbox_path": str(Path(root) / "inbox.db3"),
                    "storage_mode": "shared" if resolution.expected_mode == "shared" else "unverified",
                    "storage_verified_utc": "",
                    "storage_evidence": f"api_observed:{version}",
                }
            )
            self.multi_radio_store.save_js8_instance(updated)

    def _configured_instance_process_running(self, item: Any) -> Optional[bool]:
        """Return exact process state, or ``None`` when it cannot be proven."""

        name = self._queue_item_name(item)
        if name == 'VarAC' and isinstance(item, Mapping):
            readiness = item.get('readiness_policy') or {}
            if isinstance(readiness, Mapping):
                target = str(readiness.get('process_executable') or '')
                args = item.get('launch_arguments') or readiness.get('launch_arguments') or ()
                if not target.lower().endswith('varac.exe'):
                    target = next((str(arg) for arg in args if str(arg).replace('\\','/').lower().endswith('/varac.exe')), target)
                if target and hasattr(getattr(self, 'status', None), 'cached_varac_process_state'):
                    records = self._launch_process_records()
                    kwargs = {} if records is None else dict(process_records=records, checked_monotonic=time.monotonic())
                    return self.status.cached_varac_process_state(target,
                        str(readiness.get('process_ini') or readiness.get('profile_selector') or ''),
                        str(readiness.get('wine_prefix') or (readiness.get('environment') or {}).get('WINEPREFIX') or ''),
                        **kwargs)['running']
        daemon = self._custom_rigctld_identity(item)
        if daemon is not None:
            records = self._launch_process_records()
            inventory = {} if records is None else {"process_records": records}
            return bool(self.status.cached_program_instance_running(
                "RigCtlD", daemon.executable, daemon.arguments, **inventory,
            ))
        if isinstance(item, Mapping) and item.get("instance_identity"):
            target = str(
                item.get("launch_command_override", "")
                or item.get("launch_path_override", "")
                or ""
            ).strip()
            if target:
                try:
                    arguments = item.get("launch_arguments", ())
                    if not isinstance(arguments, (list, tuple)):
                        arguments = ()
                    arguments = self._process_identity_arguments(name, arguments)
                    process_records = self._launch_process_records()
                    inventory = (
                        {}
                        if process_records is None
                        else {"process_records": process_records}
                    )
                    if not arguments:
                        excluded_options: tuple[str, ...] = ()
                        if (
                            name == "JS8Call"
                            and str(item.get("rig_name_source", "") or "").strip()
                            == "legacy_default"
                        ):
                            # For the native JS8Call default profile, omitting
                            # --rig-name is identity, not a wildcard. A named
                            # sibling uses the same executable but owns a
                            # different settings namespace and endpoint.
                            excluded_options = ("-r", "--rig-name")
                        if excluded_options:
                            return bool(
                                self.status.cached_program_instance_running(
                                    name,
                                    target,
                                    excluded_options=excluded_options,
                                    **inventory,
                                )
                            )
                        return bool(
                            self.status.cached_program_instance_running(
                                name,
                                target,
                                **inventory,
                            )
                        )
                    return bool(
                        self.status.cached_program_instance_running(
                            name,
                            target,
                            arguments,
                            **inventory,
                        )
                    )
                except Exception:
                    return None
        return None

    def _legacy_default_js8_profile_conflict(self, item: Any) -> str:
        """Return the obsolete generated rig name of a running default-profile sibling.

        The 2.0.1 compatibility launch intentionally removes ``--rig-name`` for
        an untouched ``default_js8_instance`` so JS8Call opens its established
        native settings namespace.  During an in-place update, however, the old
        FIO-generated instance can survive the FIO restart.  An argument-free
        exact-process check cannot distinguish that process from the native
        default and would silently credit the wrong profile.  Fresh launch-owned
        argv evidence lets us fail closed with an actionable transition message
        without terminating an operator-owned process or starting a duplicate.
        """

        if not isinstance(item, Mapping):
            return ""
        if self._queue_item_name(item) != "JS8Call":
            return ""
        if str(item.get("rig_name_source", "") or "").strip() != "legacy_default":
            return ""
        target = str(
            item.get("launch_command_override", "")
            or item.get("launch_path_override", "")
            or ""
        ).strip()
        process_records = self._launch_process_records()
        if not target or process_records is None:
            return ""
        matcher = getattr(self.status, "cached_program_instance_running", None)
        if not callable(matcher):
            return ""
        for record in process_records:
            cmdline = tuple(str(value or "").strip() for value in record.get("cmdline", ()))
            for index, token in enumerate(cmdline):
                normalized = token.casefold()
                rig_name = ""
                expected: tuple[str, ...] = ()
                if normalized in {"-r", "--rig-name"} and index + 1 < len(cmdline):
                    rig_name = cmdline[index + 1]
                    expected = (token, rig_name)
                elif normalized.startswith("--rig-name=") or normalized.startswith("-r="):
                    rig_name = token.split("=", 1)[1]
                    expected = (token,)
                if not rig_name.casefold().startswith("fio-default_js8_instance-"):
                    continue
                try:
                    if matcher(
                        "JS8Call",
                        target,
                        expected,
                        process_records=process_records,
                    ):
                        return rig_name
                except Exception:
                    return ""
        return ""

    def _launch_process_records(
        self,
    ) -> tuple[Mapping[str, object], ...] | None:
        """Return the one immutable inventory accepted for this sequence."""

        if not bool(getattr(self, "_sequence_process_records_ready", False)):
            return None
        return tuple(getattr(self, "_sequence_process_records", ()) or ())

    def _unattributed_process_blocker(
        self,
        item: Any,
        exact_process_running: Optional[bool],
        *,
        endpoint_clear: bool = False,
    ) -> str:
        """Fail closed when family processes cannot be fully attributed.

        A fresh launch-owned inventory can prove that no family process exists.
        It cannot safely prove one requested instance absent when one or more
        family processes were observed but their argv did not match every
        configured row. This is especially important for Wine launchers, which
        may replace the original wrapper argv after process creation.
        """

        if exact_process_running is True:
            return ""
        if self._queue_item_name(item) == 'VarAC' and exact_process_running is None:
            return 'Could not verify the configured VarAC instance; duplicate launch skipped. Review setup or check again.'
        # FLRig, FLDigi, and JS8Call own persisted per-radio endpoints.  Once
        # this launch sequence has freshly proved the requested endpoint clear,
        # another same-family process belongs to a different endpoint and must
        # not suppress the requested instance merely because a legacy/default
        # process lacks radio-selecting argv.  The occupied-endpoint and exact
        # configured-process checks run before this branch and remain terminal.
        # Process-only applications (including FLMsg/FLAmp and Wine-hosted
        # VarAC/VARA) deliberately retain the family-attribution fail-closed
        # rule below.
        if endpoint_clear and self._has_persisted_endpoint_identity(item):
            return ""
        name = self._queue_item_name(item)
        status = getattr(self, "status", None)
        counter = getattr(status, "cached_program_process_count", None)
        if not callable(counter):
            return ""
        try:
            process_records = self._launch_process_records()
            process_count = int(
                counter(name)
                if process_records is None
                else counter(name, process_records=process_records)
            )
        except Exception:
            return (
                "process attribution is unavailable after launch preflight; "
                "duplicate launch skipped"
            )
        if process_count <= 0:
            return ""

        attributed: set[str] = set()
        candidates = getattr(self, "_sequence_attribution_candidates", ()) or getattr(
            self,
            "_queue",
            (),
        )
        for candidate in candidates:
            if self._queue_item_name(candidate) != name:
                continue
            try:
                if self._configured_instance_process_running(candidate) is not True:
                    continue
            except Exception:
                continue
            if isinstance(candidate, Mapping):
                identity = str(candidate.get("instance_identity", "") or "").strip()
                target = str(
                    candidate.get("launch_command_override", "")
                    or candidate.get("launch_path_override", "")
                    or ""
                ).strip()
                arguments = candidate.get("launch_arguments", ())
                if not isinstance(arguments, (list, tuple)):
                    arguments = ()
                attributed.add(
                    identity
                    or repr((name, target, tuple(str(value) for value in arguments)))
                )
            else:
                attributed.add(str(candidate))
        if process_count <= len(attributed):
            return ""
        return (
            f"{name} process evidence is present but could not be attributed "
            "to every configured instance; duplicate launch skipped"
        )

    @staticmethod
    def _process_identity_arguments(
        name: str,
        arguments: Sequence[object],
    ) -> tuple[str, ...]:
        """Return only arguments that identify one runnable app instance.

        FLMsg and FLAmp accept ``-title`` as presentation metadata. Older
        launch paths and desktop wrappers can preserve that title as one argv
        value or split it into several values, so including it in an exact
        process match can incorrectly authorize a duplicate. Their qualified
        native selectors precede ``-title`` and remain mandatory; discard only
        the trailing presentation segment when comparing process identity.
        """

        values = tuple(str(value or "").strip() for value in arguments)
        if str(name or "").strip().casefold() not in {"flmsg", "flamp"}:
            return values
        for index, value in enumerate(values):
            if value.casefold() in {"-title", "--title"}:
                return values[:index]
        return values

    def _program_running(self, item: Any) -> bool:
        exact = self._configured_instance_process_running(item)
        if exact is not None:
            return exact
        return bool(self._cached_status_for_item(item).get("running", False))

    def _resolve_launch_command(self, item_or_name: Any) -> Tuple[Optional[List[str]], str]:
        name = self._queue_item_name(item_or_name)
        item = item_or_name if isinstance(item_or_name, Mapping) else {}
        structured = self._structured_launch_command(item, name)
        if structured is not None:
            # Managed VarAC recipes are already tokenized.  They may contain
            # spaces, backslashes, Windows drive paths, or Wine-visible paths;
            # feeding them through shlex would change the bytes.  shell=False
            # below receives this vector unchanged.
            return structured, "structured VarAC launch recipe"
        launch_arguments = self._launch_arguments_for(item)
        override_cmd = str(item.get("launch_command_override", "") or "").strip()
        if override_cmd:
            cmd = self._command_from_freeform(override_cmd)
            if cmd:
                return self._finalize_launch_command(name, cmd, launch_arguments), "radio launch command"
        override_path = str(item.get("launch_path_override", "") or "").strip()
        if override_path:
            cmd = self._command_from_config_path(name, override_path)
            if cmd:
                return self._finalize_launch_command(name, cmd, launch_arguments), "radio configured path"
            readiness = item.get('readiness_policy', {})
            operator_recipe = isinstance(readiness, Mapping) and self.is_truthy(readiness.get('operator_recipe_v1'))
            if operator_recipe and (os.path.isabs(override_path) or ' ' in override_path):
                return None, 'reviewed application target is unavailable'
            cmd = self._command_from_freeform(override_path)
            if cmd:
                return self._finalize_launch_command(name, cmd, launch_arguments), "radio configured command"
        readiness = item.get('readiness_policy', {})
        if isinstance(readiness, Mapping) and self.is_truthy(readiness.get('operator_recipe_v1')):
            # A saved explicit choice is never permission to launch a legacy
            # global path or another program found as a fallback.
            return None, 'reviewed application target is unavailable'
        custom_cmd = self._custom_tool_command(name)
        if custom_cmd:
            cmd = self._command_from_freeform(custom_cmd)
            if cmd:
                return self._finalize_launch_command(name, cmd, launch_arguments), "configured custom tool"
        meta = LAUNCH_APP_META.get(name, {})
        launch_cmd_key = str(meta.get("launch_cmd_key", "") or "")
        if launch_cmd_key:
            raw_launch_cmd = str(self.settings.get(launch_cmd_key, "") or "").strip()
            if raw_launch_cmd:
                cmd = self._command_from_freeform(raw_launch_cmd)
                if cmd:
                    return self._finalize_launch_command(name, cmd, launch_arguments), "configured launch command"
        path_key = str(meta.get("path_key", "") or "")
        raw = str(self.settings.get(path_key, "") or "").strip() if path_key else ""
        if raw:
            cmd = self._command_from_config_path(name, raw)
            if cmd:
                return self._finalize_launch_command(name, cmd, launch_arguments), "configured path"
            cmd = self._command_from_freeform(raw)
            if cmd:
                return self._finalize_launch_command(name, cmd, launch_arguments), "configured command"
        fallback = self._fallback_cmd(name)
        if fallback:
            return self._finalize_launch_command(name, fallback, launch_arguments), "fallback command"
        return None, "none"

    @staticmethod
    def _structured_launch_command(item: Mapping[str, Any], name: str) -> Optional[List[str]]:
        """Resolve an additive structured VarAC recipe without shell parsing."""

        if str(name or "").strip() != "VarAC":
            return None
        readiness = item.get("readiness_policy", {})
        if not isinstance(readiness, Mapping):
            return None
        nested = readiness.get("launch_recipe")
        recipe = nested if isinstance(nested, Mapping) else readiness
        if not (
            LaunchOrchestrator.is_truthy(readiness.get("structured_launch", False))
            or "executable" in recipe
            or "launch_executable" in recipe
            or "launch_arguments" in recipe
        ):
            return None
        executable = str(recipe.get("executable", recipe.get("launch_executable", "")) or "")
        if not executable:
            # A malformed structured recipe must not fall through to a stale
            # launch_cmd/path override.  Report an empty structured command so
            # the caller records a launch failure for operator repair.
            return []
        arguments = recipe.get("launch_arguments", recipe.get("arguments", item.get("launch_arguments", ())))
        if not isinstance(arguments, (list, tuple)):
            arguments = ()
        return [executable, *(str(argument) for argument in arguments)]

    @staticmethod
    def _launch_arguments_for(item: Mapping[str, Any]) -> List[str]:
        raw = item.get("launch_arguments", ())
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(argument) for argument in raw if str(argument or "")]

    def _finalize_launch_command(self, name: str, cmd: List[str], launch_arguments: List[str] | None = None) -> List[str]:
        if str(name or "").strip() == "VarAC":
            return self._wrap_varac_wine_if_needed(cmd)
        arguments = list(launch_arguments or ())
        if not arguments:
            return cmd
        # ``open`` forwards application arguments only after ``--args``.  The
        # planner deliberately keeps these arguments separate from bundle/path
        # selection so a direct executable and a macOS bundle stay equivalent.
        if cmd and os.path.basename(str(cmd[0])).casefold() == "open" and "--args" not in cmd:
            return [*cmd, "--args", *arguments]
        return [*cmd, *arguments]

    def _command_from_config_path(self, name: str, raw: str) -> Optional[List[str]]:
        p = Path(raw)
        if p.exists() and p.is_dir():
            if platform.system() == "Darwin" and p.suffix.lower() == ".app":
                cmd = self._command_from_app_bundle(name, p)
                if cmd:
                    return cmd
            for cand in LAUNCH_APP_META.get(name, {}).get("folder_candidates", []):
                fp = p / str(cand)
                if fp.exists() and fp.is_file():
                    cmd = self._command_for_file(fp)
                    if cmd:
                        return cmd
            return None
        if p.exists() and p.is_file():
            return self._command_for_file(p)
        return None

    def _command_from_freeform(self, raw: str) -> Optional[List[str]]:
        if platform.system() == "Windows":
            # Picker-created batch commands carry a literal quoted /c target.
            # shlex(posix=False) treats the escaped quotes and spaces as
            # separate tokens, so recover this narrow command shape directly.
            match = re.match(r"^\s*cmd(?:\.exe)?\s+/d\s+/c\s+(.+?)\s*$", raw, re.IGNORECASE)
            if match:
                target = match.group(1).strip()
                if target.startswith('"') and target.endswith('"'):
                    target = target[1:-1].replace('\\"', '"')
                elif target.startswith('\\"') and target.endswith('\\"'):
                    target = target[2:-2]
                if target:
                    if not (target.startswith('"') and target.endswith('"')):
                        target = f'"{target}"'
                    return ["cmd.exe", "/d", "/c", target]
        try:
            parts = shlex.split(raw, posix=platform.system() != "Windows")
            if not parts:
                return None
            return [self._normalize_command_token(p) for p in parts]
        except Exception:
            return None

    def _command_from_app_bundle(self, name: str, bundle: Path) -> Optional[List[str]]:
        candidates: List[Path] = []
        bundle_name = bundle.stem.strip()
        for cand in LAUNCH_APP_META.get(name, {}).get("folder_candidates", []):
            token = Path(str(cand)).stem
            candidates.append(bundle / "Contents" / "MacOS" / token)
            candidates.append(bundle / "Contents" / "MacOS" / token.lower())
            candidates.append(bundle / "Contents" / "MacOS" / token.upper())
        if bundle_name:
            candidates.append(bundle / "Contents" / "MacOS" / bundle_name)
            candidates.append(bundle / "Contents" / "MacOS" / bundle_name.lower())
            candidates.append(bundle / "Contents" / "MacOS" / bundle_name.upper())
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return [str(candidate)]
        return ["open", "-a", str(bundle)]

    @staticmethod
    def _normalize_command_token(token: str) -> str:
        txt = str(token or "")
        if not txt:
            return ""
        # ``shlex.split(..., posix=False)`` intentionally preserves the
        # surrounding quotes that protect Windows paths with spaces.  Popen
        # receives argv tokens, so remove only those outer balancing quotes;
        # embedded quotes remain part of the token.
        if (
            platform.system() == "Windows"
            and len(txt) >= 2
            and txt[0] == txt[-1]
            and txt[0] in {"\"", "'"}
        ):
            txt = txt[1:-1]
        # Expand user/env paths for shell=False launches so copied desktop-style
        # command values behave consistently in Launch Control.
        if "=" in txt and not txt.startswith("-"):
            key, value = txt.split("=", 1)
            expanded = os.path.expanduser(os.path.expandvars(value))
            return f"{key}={expanded}"
        return os.path.expanduser(os.path.expandvars(txt))

    def _command_for_file(self, path: Path) -> Optional[List[str]]:
        suffix = path.suffix.lower()
        if suffix == ".desktop" and platform.system() != "Windows":
            if shutil.which("xdg-open"):
                return ["xdg-open", str(path)]
            return [str(path)]
        if suffix == ".py":
            py_cmd = "python" if platform.system() == "Windows" else "python3"
            return [py_cmd, str(path)]
        if suffix == ".bash":
            return ["bash", str(path)]
        if suffix == ".sh":
            return [str(path)] if os.access(path, os.X_OK) else ["bash", str(path)]
        if suffix == ".ps1":
            if platform.system() == "Windows":
                return ["powershell.exe", "-NoProfile", "-File", str(path)]
            return ["pwsh", "-NoProfile", "-File", str(path)]
        if platform.system() == "Windows" and suffix in {".bat", ".cmd"}:
            # ``shell=False`` cannot execute cmd scripts directly through
            # CreateProcess; keep the launch safe and explicit instead.
            return ["cmd.exe", "/d", "/c", f'"{path}"']
        return [str(path)]

    @staticmethod
    def _token_looks_like_varac_exe(token: str) -> bool:
        txt = str(token or "").strip().lower()
        if not txt:
            return False
        normalized = txt.replace("\\", "/")
        return normalized.endswith("/varac.exe") or normalized == "varac.exe"

    @staticmethod
    def _is_wine_prefixed_command(cmd: List[str]) -> bool:
        for token in cmd:
            txt = str(token or "").strip()
            if not txt or "=" in txt:
                continue
            base = os.path.basename(txt).strip().lower()
            if base.startswith("wine"):
                return True
        return False

    def _wrap_varac_wine_if_needed(self, cmd: List[str]) -> List[str]:
        if platform.system() == "Windows":
            return cmd
        if not cmd:
            return cmd
        if self._is_wine_prefixed_command(cmd):
            return cmd
        if not any(self._token_looks_like_varac_exe(token) for token in cmd):
            return cmd
        wine_cmd = shutil.which("wine-stable") or shutil.which("wine") or shutil.which("wine64")
        if not wine_cmd:
            return cmd
        return [str(wine_cmd), *cmd]

    def _is_self_launch_command(self, cmd: List[str]) -> bool:
        """
        Prevent recursive launch-control self-launch loops.
        """
        parts = [str(p or "").strip() for p in cmd if str(p or "").strip()]
        if not parts:
            return False
        parts_lower = [p.lower() for p in parts]
        joined = " ".join(parts_lower)
        if "freqinout.main" in joined:
            return True
        if any("freqinout.exe" in p for p in parts_lower):
            return True
        if any(p.endswith("freqinout/main.py") or p.endswith("freqinout\\main.py") for p in parts_lower):
            return True

        # Python module/script invocation patterns.
        if len(parts_lower) >= 3 and parts_lower[1] == "-m" and parts_lower[2] == "freqinout.main":
            return True
        if len(parts_lower) >= 2 and (
            parts_lower[1].endswith("freqinout/main.py") or parts_lower[1].endswith("freqinout\\main.py")
        ):
            return True

        # Direct executable equivalence with current process executable/script.
        try:
            current_exec = os.path.basename(sys.executable).strip().lower()
        except Exception:
            current_exec = ""
        try:
            current_argv0 = os.path.basename(sys.argv[0]).strip().lower()
        except Exception:
            current_argv0 = ""
        first_base = os.path.basename(parts_lower[0]).strip().lower()
        if first_base and first_base in {current_exec, current_argv0}:
            for token in parts_lower[1:4]:
                if "freqinout.main" in token:
                    return True
                if token.endswith("freqinout/main.py") or token.endswith("freqinout\\main.py"):
                    return True
        return False

    def _fallback_cmd(self, name: str) -> Optional[List[str]]:
        fallback_candidates = LAUNCH_APP_META.get(name, {}).get("fallback_cmds", [])
        first_candidate = None
        for cand in fallback_candidates:
            cand_s = str(cand).strip()
            if not cand_s:
                continue
            if first_candidate is None:
                first_candidate = cand_s
            resolved = shutil.which(cand_s)
            if resolved:
                return [resolved]
        if first_candidate:
            return [first_candidate]
        return None

    def _infer_launch_cwd(
        self,
        name: str,
        cmd: List[str],
        cmd_desc: str,
        item: Any = None,
    ) -> Optional[str]:
        """
        For configured paths, launch from the app/script directory so relative
        resources resolve the same as direct desktop launch.
        """
        if not cmd:
            return None
        try:
            if isinstance(item, Mapping):
                configured = str(item.get("working_directory", "") or "").strip()
                if configured:
                    return str(Path(configured).expanduser())
            if name == "VarAC" and cmd_desc == "configured launch command":
                varac_root = str(self.settings.get("varac_path", "") or "").strip()
                if varac_root:
                    root = Path(varac_root).expanduser()
                    if root.exists() and root.is_dir():
                        return str(root)
                    if root.exists() and root.is_file():
                        return str(root.parent)
            if cmd_desc == "configured custom tool":
                first = Path(str(cmd[0])).expanduser() if cmd else None
                if first is not None and first.exists() and first.is_file():
                    return str(first.parent)
                if len(cmd) >= 2:
                    second = Path(str(cmd[1])).expanduser()
                    if second.exists() and second.is_file():
                        return str(second.parent)
            if cmd_desc != "configured path":
                return None
            first_name = os.path.basename(str(cmd[0])).lower()
            if first_name.startswith("wine") and len(cmd) >= 2:
                second = Path(str(cmd[1])).expanduser()
                if second.exists() and second.is_file():
                    return str(second.parent)
            first = Path(str(cmd[0])).expanduser()
            if first.exists() and first.is_file():
                return str(first.parent)
            if len(cmd) >= 2:
                second = Path(str(cmd[1])).expanduser()
                if second.exists() and second.is_file():
                    # Covers commands like: python C:\\path\\app.py
                    first_name = os.path.basename(str(cmd[0])).lower()
                    if first_name.startswith("python") or second.suffix.lower() == ".py":
                        return str(second.parent)
        except Exception:
            return None
        return None

    def _finish_sequence(self, cancelled: bool) -> None:
        self._poll_timer.stop()
        if cancelled and self._current_name:
            self._results.append(
                self._result_for(
                    self._current_item or self._current_name,
                    status="cancelled",
                    detail="sequence cancelled before readiness check completed",
                )
            )
        summary = self._build_summary(cancelled=cancelled)
        self._active = False
        self._process_preflight_pending = False
        self._process_preflight_generation += 1
        self._cancel_requested = False
        self._trigger = ""
        self._queue = []
        self._index = 0
        self._current_name = None
        self._current_item = None
        self._current_cmd = None
        self._current_process = None
        self._current_launch_description = ""
        self._current_started_monotonic = 0.0
        self._current_phase = ""
        self._hardware_preflight_requested = {}
        self._hardware_preflight_verified = set()
        self._endpoint_preflight_verified = set()
        self._endpoint_preflight_requested = set()
        self._endpoint_preflight_clear = set()
        self._sequence_claimed_identities = set()
        self._radio_control_last_evidence = {}
        self._radio_control_gate_states = {}
        self._radio_control_probe_due_monotonic = {}
        self._sequence_attribution_candidates = ()
        self._sequence_preflight_started_wall = 0.0
        self._process_preflight_reason = ""
        self.sequence_finished.emit(summary)

    def _build_summary(self, cancelled: bool) -> Dict[str, Any]:
        launched = sum(1 for r in self._results if r.get("status") == "launched")
        already_running = sum(1 for r in self._results if r.get("status") == "already_running")
        failed = sum(1 for r in self._results if r.get("status") == "failed")
        timeout = sum(1 for r in self._results if r.get("status") == "timeout")
        blocked_self = sum(1 for r in self._results if r.get("status") == "blocked_self")
        blocked_dependency = sum(1 for r in self._results if r.get("status") == "blocked_dependency")
        blocked_radio_control = sum(
            1 for r in self._results if r.get("status") == "blocked_radio_control"
        )
        cancelled_count = sum(1 for r in self._results if r.get("status") == "cancelled")
        return {
            "trigger": self._trigger,
            "cancelled": cancelled,
            "launched": launched,
            "already_running": already_running,
            "failed": failed,
            "timeout": timeout,
            "blocked_self": blocked_self,
            "blocked_dependency": blocked_dependency,
            "blocked_radio_control": blocked_radio_control,
            "cancelled_count": cancelled_count,
            "results": list(self._results),
            "projection_warnings": dict(
                getattr(self, "_sequence_projection_warnings", {})
            ),
        }
