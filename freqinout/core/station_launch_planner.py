from __future__ import annotations

import hashlib
import json
from pathlib import PurePath
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from freqinout.core.launch_bundle_store import normalize_launch_items
from freqinout.core.receiver_software_stack import (
    STANDARD_EXECUTION_SCOPE,
    execution_scope,
    is_observer_profile,
    validate_observer_launch_items,
)
from freqinout.core.js8_storage import (
    expected_storage_mode,
    normalize_rig_name,
    resolve_js8_storage,
    rig_name_collision_key,
    stable_managed_rig_name,
)
from freqinout.core.multi_instance_review import (
    blocking_issue_message,
    validate_multi_instance_launch_records,
)


DEFAULT_DEPENDENCIES = {
    "FLDigi": ("FLRig",),
    "FLAmp": ("FLDigi",),
    "FLMsg": ("FLDigi",),
    "JS8Spotter": ("JS8Call",),
    "CommStat": ("JS8Call",),
}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _structured_launch_facts(item: Mapping[str, Any], name: str) -> Dict[str, Any]:
    """Return a managed structured recipe without interpreting command text.

    The additive readiness JSON extension is deliberately scoped to VarAC here:
    JS8Call/Fast Light rows retain their existing recipe projection.  A VarAC
    row with structured arguments is authoritative even if an older
    ``launch_cmd``/command override is still present beside it.
    """

    if str(name or "").strip() != "VarAC":
        return {}
    readiness = item.get("readiness_policy", {})
    if not isinstance(readiness, Mapping):
        return {}
    nested = readiness.get("launch_recipe")
    recipe = nested if isinstance(nested, Mapping) else readiness
    has_structured = bool(
        _truthy(readiness.get("structured_launch", False))
        or "executable" in recipe
        or "launch_executable" in recipe
        or "launch_arguments" in recipe
    )
    if not has_structured:
        return {}
    executable = str(recipe.get("executable", recipe.get("launch_executable", "")) or "")
    arguments = recipe.get("launch_arguments", recipe.get("arguments", ()))
    if not isinstance(arguments, (list, tuple)):
        arguments = ()
    environment = recipe.get("environment", {})
    if not isinstance(environment, Mapping):
        environment = {}
    cwd = str(recipe.get("working_directory", recipe.get("cwd", "")) or "")
    return {
        "executable": executable,
        "arguments": tuple(str(value) for value in arguments),
        "environment": {str(key): str(value) for key, value in environment.items() if str(key).strip()},
        "working_directory": cwd,
    }


@dataclass(frozen=True)
class PlannedInstance:
    name: str
    instance_key: str
    instance_identity: str
    radio_ids: Tuple[int, ...]
    radio_names: Tuple[str, ...]
    launch_path_override: str = ""
    launch_command_override: str = ""
    launch_arguments: Tuple[str, ...] = ()
    working_directory: str = ""
    environment: Tuple[Tuple[str, str], ...] = ()
    profile_selector: str = ""
    rig_name: str = ""
    rig_name_source: str = ""
    application_data_root: str = ""
    storage_mode: str = "unverified"
    expected_storage_mode: str = "unverified"
    effective_command: Tuple[str, ...] = ()
    dependencies: Tuple[str, ...] = ()
    readiness_policy: Tuple[Tuple[str, Any], ...] = ()
    configuration_paths: Tuple[Tuple[str, str], ...] = ()
    execution_scope: str = STANDARD_EXECUTION_SCOPE
    known_recipe: bool = False
    startup_included: bool = False
    bundle_enabled: bool = False
    operator_starts: bool = False
    monitor_health: bool = True
    radio_control_backend: str = "manual"
    radio_control_host: str = ""
    radio_control_port: int = 0
    radio_control_app: str = ""
    radio_control_gate_required: bool = False

    def as_queue_item(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "instance_key": self.instance_key,
            "instance_identity": self.instance_identity,
            "radio_ids": list(self.radio_ids),
            "radio_names": list(self.radio_names),
            "launch_path_override": self.launch_path_override,
            "launch_command_override": self.launch_command_override,
            "launch_arguments": list(self.launch_arguments),
            "working_directory": self.working_directory,
            "environment": dict(self.environment),
            "profile_selector": self.profile_selector,
            "rig_name": self.rig_name,
            "rig_name_source": self.rig_name_source,
            "application_data_root": self.application_data_root,
            "storage_mode": self.storage_mode,
            "expected_storage_mode": self.expected_storage_mode,
            "effective_command": list(self.effective_command),
            "dependencies": list(self.dependencies),
            "readiness_policy": dict(self.readiness_policy),
            "configuration_paths": dict(self.configuration_paths),
            "execution_scope": self.execution_scope,
            "known_recipe": self.known_recipe,
            "startup_included": self.startup_included,
            "bundle_enabled": self.bundle_enabled,
            "operator_starts": self.operator_starts,
            "monitor_health": self.monitor_health,
            "radio_control_backend": self.radio_control_backend,
            "radio_control_host": self.radio_control_host,
            "radio_control_port": self.radio_control_port,
            "radio_control_app": self.radio_control_app,
            "radio_control_gate_required": self.radio_control_gate_required,
        }


@dataclass(frozen=True)
class LaunchPlan:
    trigger: str
    scope_radio_id: Optional[int]
    instances: Tuple[PlannedInstance, ...]

    def queue(self) -> List[Dict[str, Any]]:
        return [instance.as_queue_item() for instance in self.instances]


class StationLaunchPlanner:
    """Pure planner shared by preview, startup, and selected-radio manual launch."""

    def plan_startup(
        self,
        profiles: Sequence[Mapping[str, Any]],
        bundles: Mapping[int, Mapping[str, Any]],
        *,
        scope_radio_id: Optional[int] = None,
        trigger: str = "startup",
    ) -> LaunchPlan:
        return self._plan(
            profiles,
            bundles,
            scope_radio_id=scope_radio_id,
            trigger=trigger,
            review_all=False,
        )

    def plan_review(
        self,
        profiles: Sequence[Mapping[str, Any]],
        bundles: Mapping[int, Mapping[str, Any]],
        *,
        scope_radio_id: Optional[int] = None,
        trigger: str = "review",
    ) -> LaunchPlan:
        """Return every configured recipe, including explicit operator-start rows.

        Review is intentionally broader than execution: it exposes disabled
        bundle/startup policy without making either state launchable.  Startup
        and manual execution continue to use :meth:`plan_startup`.
        """

        return self._plan(
            profiles,
            bundles,
            scope_radio_id=scope_radio_id,
            trigger=trigger,
            review_all=True,
        )

    def _plan(
        self,
        profiles: Sequence[Mapping[str, Any]],
        bundles: Mapping[int, Mapping[str, Any]],
        *,
        scope_radio_id: Optional[int],
        trigger: str,
        review_all: bool,
    ) -> LaunchPlan:
        candidates: List[Tuple[int, int, PlannedInstance]] = []
        ordered_profiles = sorted(
            (profile for profile in profiles if isinstance(profile, Mapping)),
            key=lambda row: (int(row.get("display_order", 0) or 0), int(row.get("id", 0) or 0)),
        )
        for profile in ordered_profiles:
            radio_id = int(profile.get("id", 0) or 0)
            if radio_id <= 0 or (scope_radio_id is not None and radio_id != int(scope_radio_id)):
                continue
            explicit_selected_radio = (
                str(trigger or "").strip().lower() == "manual"
                and scope_radio_id is not None
                and radio_id == int(scope_radio_id)
            )
            if (
                not review_all
                and not explicit_selected_radio
                and int(profile.get("runtime_active", 0) or 0) != 1
            ):
                continue
            bundle = bundles.get(radio_id, {})
            bundle_enabled = _truthy(bundle.get("launch_enabled", False))
            if not review_all and not bundle_enabled:
                continue
            normalized_items = normalize_launch_items(bundle.get("items", []))
            control_backend = str(
                profile.get("control_backend", profile.get("control_via", "manual"))
                or "manual"
            ).strip().lower()
            if control_backend == "hamlib":
                control_backend = "rigctld"
            control_app = {
                "flrig": "FLRig",
                "js8call": "JS8Call",
            }.get(control_backend, "")
            if control_backend == "flrig":
                control_host = str(profile.get("flrig_host", "127.0.0.1") or "127.0.0.1")
                control_port = int(profile.get("flrig_port", 12345) or 12345)
            elif control_backend == "rigctld":
                control_host = str(profile.get("rig_host", "127.0.0.1") or "127.0.0.1")
                control_port = int(profile.get("rig_port", 4532) or 4532)
            elif control_backend == "js8call":
                control_host = str(profile.get("js8_host", "127.0.0.1") or "127.0.0.1")
                control_port = int(profile.get("js8_port", 2442) or 2442)
            else:
                control_host = ""
                control_port = 0
            control_gate_required = bool(
                not is_observer_profile(profile)
                and control_backend not in {"", "manual", "none"}
            )
            if review_all:
                normalized_items = self._with_configured_review_components(profile, normalized_items)
            if is_observer_profile(profile):
                validate_observer_launch_items(normalized_items)
            for order, raw_item in enumerate(normalized_items):
                item = self._with_profile_overrides(profile, raw_item)
                name = str(item["name"])
                structured_facts = _structured_launch_facts(item, name)
                if structured_facts:
                    # Structured VarAC launch facts are canonical.  Preserve
                    # them in the planned item and suppress any stale legacy
                    # command that could otherwise be reparsed by Launch
                    # Control.
                    item["launch_path_override"] = structured_facts["executable"]
                    item["launch_command_override"] = ""
                    item_readiness = dict(item.get("readiness_policy", {}))
                    item_readiness["launch_arguments"] = list(structured_facts["arguments"])
                    item_readiness["environment"] = dict(structured_facts["environment"])
                    if structured_facts["working_directory"]:
                        item_readiness["working_directory"] = structured_facts["working_directory"]
                    item["readiness_policy"] = item_readiness
                else:
                    item_readiness = item.get("readiness_policy", {})
                operator_start_row = bool(
                    isinstance(item_readiness, Mapping)
                    and _truthy(item_readiness.get("operator_starts", False))
                )
                parent_managed_row = bool(
                    isinstance(item_readiness, Mapping)
                    and _truthy(item_readiness.get("parent_managed", False))
                )
                if not item["enabled"] or (
                    not review_all
                    and (not item["startup"] or operator_start_row or parent_managed_row)
                ):
                    continue
                dependencies = tuple(item["dependencies"] or DEFAULT_DEPENDENCIES.get(name, ()))
                identity = self._identity(profile, item)
                readiness = dict(item["readiness_policy"])
                operator_starts = _truthy(readiness.pop("operator_starts", False))
                working_directory = str(readiness.pop("working_directory", "") or "").strip()
                profile_selector = str(readiness.pop("profile_selector", "") or "").strip()
                raw_environment = readiness.pop("environment", {})
                environment = tuple(
                    sorted(
                        (str(key), str(value))
                        for key, value in raw_environment.items()
                        if str(key).strip()
                    )
                ) if isinstance(raw_environment, Mapping) else ()
                if name == "JS8Call":
                    readiness.setdefault("host", str(profile.get("js8_host", "127.0.0.1") or "127.0.0.1"))
                    readiness.setdefault("port", int(profile.get("js8_port", 2442) or 2442))
                    readiness.setdefault("require_api", True)
                elif name == "FLRig":
                    readiness.setdefault("host", str(profile.get("flrig_host", "127.0.0.1") or "127.0.0.1"))
                    readiness.setdefault("port", int(profile.get("flrig_port", 12345) or 12345))
                    readiness.setdefault("require_service", True)
                elif name == "FLDigi":
                    readiness.setdefault("host", str(profile.get("fldigi_host", "127.0.0.1") or "127.0.0.1"))
                    readiness.setdefault("port", int(profile.get("fldigi_port", 7362) or 7362))
                    readiness.setdefault("require_service", True)
                js8_values = self._js8_launch_values(profile, item) if name == "JS8Call" else {}
                configured_arguments = readiness.pop("launch_arguments", ())
                if not isinstance(configured_arguments, (list, tuple)):
                    configured_arguments = ()
                instance = PlannedInstance(
                    name=name,
                    instance_key=str(item["instance_key"]),
                    instance_identity=identity,
                    radio_ids=(radio_id,),
                    radio_names=(str(profile.get("name", radio_id) or radio_id),),
                    launch_path_override=str(item["launch_path_override"]),
                    launch_command_override=str(item["launch_command_override"]),
                    launch_arguments=tuple(
                        js8_values.get("launch_arguments", ())
                        if name == "JS8Call"
                        else (str(value) for value in configured_arguments)
                    ),
                    working_directory=working_directory,
                    environment=environment,
                    profile_selector=profile_selector,
                    rig_name=str(js8_values.get("rig_name", "")),
                    rig_name_source=str(js8_values.get("rig_name_source", "")),
                    application_data_root=str(js8_values.get("application_data_root", "")),
                    storage_mode=str(js8_values.get("storage_mode", "unverified")),
                    expected_storage_mode=str(js8_values.get("expected_storage_mode", "unverified")),
                    dependencies=dependencies,
                    readiness_policy=tuple(sorted(readiness.items())),
                    configuration_paths=self._configuration_paths(name, profile),
                    execution_scope=execution_scope(item),
                    known_recipe=bool(
                        str(item.get("launch_path_override", "") or "").strip()
                        or str(item.get("launch_command_override", "") or "").strip()
                        or bool(structured_facts)
                        or operator_starts
                    ),
                    startup_included=bool(item["startup"]),
                    bundle_enabled=bundle_enabled,
                    operator_starts=operator_starts,
                    monitor_health=bool(item.get("monitor_health", True)),
                    radio_control_backend=control_backend,
                    radio_control_host=control_host,
                    radio_control_port=control_port,
                    radio_control_app=control_app,
                    radio_control_gate_required=control_gate_required,
                )
                effective_order = -1 if control_app and name == control_app else order
                candidates.append((int(profile.get("display_order", 0) or 0), effective_order, instance))

        # Resolve dependencies inside each radio transaction.  A FLRig emitted
        # for radio A must never satisfy FLDigi planning for radio B.
        radio_groups: Dict[Tuple[int, int], List[Tuple[int, int, PlannedInstance]]] = {}
        for value in candidates:
            instance = value[2]
            radio_id = int(instance.radio_ids[0]) if instance.radio_ids else 0
            radio_groups.setdefault((value[0], radio_id), []).append(value)
        radio_ordered_candidates: List[Tuple[int, int, PlannedInstance]] = []
        for group_key in sorted(radio_groups):
            radio_ordered_candidates.extend(self._dependency_order(radio_groups[group_key]))

        deduped: Dict[str, Tuple[int, int, PlannedInstance]] = {}
        for profile_order, item_order, instance in radio_ordered_candidates:
            prior = deduped.get(instance.instance_identity)
            if prior is None:
                deduped[instance.instance_identity] = (profile_order, item_order, instance)
                continue
            existing = prior[2]
            merged = PlannedInstance(
                name=existing.name,
                instance_key=existing.instance_key,
                instance_identity=existing.instance_identity,
                radio_ids=tuple(dict.fromkeys((*existing.radio_ids, *instance.radio_ids))),
                radio_names=tuple(dict.fromkeys((*existing.radio_names, *instance.radio_names))),
                launch_path_override=existing.launch_path_override,
                launch_command_override=existing.launch_command_override,
                launch_arguments=existing.launch_arguments,
                working_directory=existing.working_directory,
                environment=existing.environment,
                profile_selector=existing.profile_selector,
                rig_name=existing.rig_name,
                rig_name_source=existing.rig_name_source,
                application_data_root=existing.application_data_root,
                storage_mode=existing.storage_mode,
                expected_storage_mode=existing.expected_storage_mode,
                effective_command=existing.effective_command,
                dependencies=existing.dependencies,
                readiness_policy=existing.readiness_policy,
                configuration_paths=existing.configuration_paths,
                execution_scope=existing.execution_scope,
                known_recipe=existing.known_recipe,
                startup_included=existing.startup_included,
                bundle_enabled=existing.bundle_enabled,
                operator_starts=existing.operator_starts,
                monitor_health=existing.monitor_health,
                radio_control_backend=existing.radio_control_backend,
                radio_control_host=existing.radio_control_host,
                radio_control_port=existing.radio_control_port,
                radio_control_app=existing.radio_control_app,
                radio_control_gate_required=existing.radio_control_gate_required,
            )
            deduped[instance.instance_identity] = (prior[0], prior[1], merged)
        ordered = list(deduped.values())
        instances = tuple(value[2] for value in ordered)
        self._validate_js8_launch_collisions(instances)
        issues = validate_multi_instance_launch_records([instance.as_queue_item() for instance in instances])
        if blocking_issue_message(issues):
            raise ValueError(blocking_issue_message(issues))
        return LaunchPlan(trigger=trigger, scope_radio_id=scope_radio_id, instances=instances)

    @staticmethod
    def _with_configured_review_components(
        profile: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Expose configured Fast Light components missing from legacy bundles.

        Older profiles often persisted FLMsg/FLAmp selection and executable
        paths without component launch rows.  Review must still show an exact
        per-component state, but it must not silently opt those recipes into
        startup.  The durable instance key remains radio-scoped unless the
        profile explicitly records an intentionally shared key.
        """

        result = [dict(item) for item in items]
        existing_names = {str(item.get("name", "") or "").strip() for item in result}
        radio_id = int(profile.get("id", 0) or 0)
        fast_key = str(
            profile.get("fast_light_system_key", "")
            or profile.get("fast_light_instance_key", "")
            or f"radio-{radio_id}"
        ).strip()
        scope = "receive_only" if is_observer_profile(profile) else STANDARD_EXECUTION_SCOPE
        for name, use_key, path_key in (
            ("FLRig", "use_flrig", "flrig_path"),
            ("FLDigi", "use_fldigi", "fldigi_path"),
            ("FLMsg", "use_flmsg", "flmsg_path"),
            ("FLAmp", "use_flamp", "flamp_path"),
        ):
            if name in existing_names or not _truthy(profile.get(use_key, False)):
                continue
            # Observer profiles can use FLDigi/FLMsg/FLAmp only; FLRig would
            # imply radio control authority and remains invalid.
            if is_observer_profile(profile) and name == "FLRig":
                continue
            prefix = name.casefold()
            explicit_key = str(profile.get(f"{prefix}_instance_key", "") or "").strip()
            result.append(
                {
                    "name": name,
                    "instance_key": explicit_key or f"fast-light:{fast_key}:{prefix}",
                    "enabled": True,
                    "startup": False,
                    "monitor_health": False,
                    "launch_path_override": str(profile.get(path_key, "") or "").strip(),
                    "launch_command_override": "",
                    "dependencies": list(DEFAULT_DEPENDENCIES.get(name, ())),
                    "readiness_policy": {
                        "execution_scope": scope,
                        "operator_starts": True,
                        "readiness": "operator_confirmed",
                    },
                    "execution_scope": scope,
                }
            )
        return result

    @staticmethod
    def _configuration_paths(name: str, profile: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
        """Expose only launch-relevant native resources to pure preflight checks."""

        values: Dict[str, Any]
        if name == "JS8Call":
            values = {
                "profile_path": profile.get("js8_profile_path", ""),
                "data_root": profile.get("js8_message_storage_root", profile.get("application_data_root", "")),
                "directed_path": profile.get("js8_directed_path", ""),
            }
        elif name == "FLRig":
            values = {"configuration_path": profile.get("flrig_config_path", "")}
        elif name in {"FLDigi", "FLMsg", "FLAmp"}:
            prefix = name.casefold()
            values = {
                "configuration_path": profile.get(f"{prefix}_config_path", ""),
                "message_path": profile.get(f"{prefix}_message_path", ""),
            }
        elif name == "SDR++":
            values = {
                "receiver_target": profile.get("sdr_target", ""),
                "receiver_endpoint": profile.get("sdr_endpoint", ""),
            }
        elif name == "VarAC":
            values = {
                "ini_path": profile.get("varac_ini_path", ""),
                "db_path": profile.get("varac_db_path", ""),
                "incoming_path": profile.get("varac_incoming_path", ""),
                "outbox_dir": profile.get("varac_outbox_dir", ""),
            }
        else:
            return ()
        return tuple(
            (key, str(value or "").strip())
            for key, value in values.items()
            if str(value or "").strip()
        )

    @staticmethod
    def _js8_launch_values(profile: Mapping[str, Any], item: Mapping[str, Any]) -> Dict[str, Any]:
        command = str(item.get("launch_command_override", "") or "").strip()
        rig_name = ""
        rig_source = ""
        legacy_default = False
        launch_arguments: Tuple[str, ...] = ()
        readiness = item.get("readiness_policy", {})
        readiness = readiness if isinstance(readiness, Mapping) else {}
        component_evidence = readiness.get("evidence", {})
        component_evidence = component_evidence if isinstance(component_evidence, Mapping) else {}
        settings_identity_mode = str(
            component_evidence.get("settings_identity_mode")
            or readiness.get("settings_identity_mode")
            or ""
        ).strip().casefold()
        if _truthy(readiness.get('operator_recipe_v1')):
            # Operator-selected argv is the saved authority. Labels and legacy
            # projection defaults must never manufacture a native namespace.
            arguments = readiness.get('launch_arguments', ())
            if not isinstance(arguments, (list,tuple)):
                raise ValueError('The reviewed JS8Call argument vector is invalid.')
            arguments = tuple(str(arg) for arg in arguments)
            selectors = []
            for index,arg in enumerate(arguments):
                if arg in {'--rig-name','-r'}:
                    if index + 1 >= len(arguments):
                        raise ValueError('The reviewed JS8Call profile argument has no value.')
                    selectors.append(arguments[index+1])
                elif arg.startswith('--rig-name='):
                    selectors.append(arg.split('=',1)[1])
            if settings_identity_mode == 'default' and selectors:
                raise ValueError('Default JS8Call profile cannot include a named-profile argument.')
            selected_name = str(readiness.get('rig_name') or '')
            if settings_identity_mode == 'isolated' and selectors != [selected_name]:
                raise ValueError('The reviewed named JS8Call profile and arguments disagree.')
            rig_name = selected_name if settings_identity_mode == 'isolated' else selectors[0] if len(selectors) == 1 else ''
            return {'launch_arguments':arguments,'rig_name':rig_name,
                    'rig_name_source':'settings_default' if settings_identity_mode == 'default' else 'operator_recipe',
                    'application_data_root':str(profile.get('js8_message_storage_root') or profile.get('application_data_root') or ''),
                    'storage_mode':str(profile.get('js8_storage_mode') or profile.get('storage_mode') or 'unverified'),
                    'expected_storage_mode':str(profile.get('js8_storage_mode') or profile.get('storage_mode') or 'unverified')}
        explicit_default = settings_identity_mode == "default"
        if command:
            rig_name = StationLaunchPlanner._rig_name_from_command(command)
            if rig_name:
                rig_source = "command_override"
        if explicit_default and rig_name:
            raise ValueError(
                "JS8Call is set to use existing default settings, but its launch override contains --rig-name."
            )
        if explicit_default:
            rig_source = "settings_default"
        elif not rig_name:
            stored_rig = str(profile.get("js8_rig_name", profile.get("rig_name", "")) or "").strip()
            system_key = str(profile.get("js8_instance_system_key", "") or "").strip()
            instance_name = str(profile.get("js8_instance_name", "") or "").strip()
            legacy_default = StationLaunchPlanner._uses_native_default_js8_profile(
                system_key=system_key,
                instance_name=instance_name,
                stored_rig=stored_rig,
            )
            if legacy_default:
                # The single-radio migration owns JS8Call's native default
                # profile. Adding --rig-name changes both the title and the
                # settings filename, which makes a correctly configured
                # package launch look like a new/unconfigured installation.
                # Keep the native default namespace; LaunchOrchestrator adds
                # the radio label to the window title on supported desktops.
                rig_source = "legacy_default"
            elif stored_rig:
                rig_name = normalize_rig_name(stored_rig)
                rig_source = "persisted"
            elif not system_key and not instance_name:
                raise ValueError(
                    "JS8Call requires a persisted instance system key or name to generate a stable rig name."
                )
            else:
                # Compatibility fallback for old rows that predate an explicit
                # persisted rig identity.  New guided instances always persist
                # the reviewed radio-derived rig name.
                rig_name = stable_managed_rig_name(system_key=system_key, name=instance_name)
                rig_source = "legacy_managed_fallback"
            if rig_name:
                launch_arguments = ("--rig-name", rig_name)
        storage_values = {
            **dict(profile),
            "rig_name": rig_name,
            "rig_name_source": rig_source,
        }
        stored_rig = str(profile.get("js8_rig_name", profile.get("rig_name", "")) or "").strip()
        if (
            stored_rig
            and not legacy_default
            and rig_name_collision_key(stored_rig) != rig_name_collision_key(rig_name)
        ):
            # A verified root belongs to the rig identity that produced it.
            # Changing --rig-name must derive a new Qt namespace instead of
            # carrying the prior rig's root forward.
            storage_values["application_data_root"] = ""
            storage_values["js8_message_storage_root"] = ""
            storage_values["storage_evidence"] = ""
            storage_values["js8_storage_evidence"] = ""
            storage_values["storage_mode"] = "unverified"
            storage_values["js8_storage_mode"] = "unverified"
        if (
            expected_storage_mode(
                storage_values.get("js8_variant_family", storage_values.get("variant_family", "unknown")),
                storage_values.get("js8_variant_version", storage_values.get("variant_version", "")),
            )
            == "unverified"
            and StationLaunchPlanner._launch_target_looks_subspace(item)
        ):
            # A Subspace-specific launch target identifies the reviewed family.
            # Like 2.2.0 and Improved 3.0.3 it receives a distinct --rig-name
            # namespace and rig-scoped storage candidate.
            storage_values["variant_family"] = "js8call_subspace_4_1"
            storage_values["variant_version"] = ""
        storage = resolve_js8_storage(storage_values, probe_existing=False)
        return {
            "launch_arguments": launch_arguments,
            "rig_name": rig_name,
            "rig_name_source": rig_source,
            "application_data_root": storage.data_root,
            "storage_mode": storage.storage_mode,
            "expected_storage_mode": storage.expected_mode,
        }

    @staticmethod
    def _uses_native_default_js8_profile(
        *,
        system_key: str,
        instance_name: str,
        stored_rig: str,
    ) -> bool:
        """Return true only for the untouched single-radio migration identity.

        The migration key is stable across renamed radios. An operator-set rig
        name must win, so a non-empty value qualifies only when it is the old
        machine-generated fallback derived from that migration key.
        """

        if str(system_key or "").strip().casefold() != "default_js8_instance":
            return False
        normalized_stored = normalize_rig_name(stored_rig)
        if not normalized_stored:
            return True
        generated = stable_managed_rig_name(
            system_key=system_key,
            name=instance_name,
        )
        return rig_name_collision_key(normalized_stored) == rig_name_collision_key(generated)

    @staticmethod
    def _rig_name_from_command(command: str) -> str:
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"Invalid JS8Call launch command: {exc}") from exc
        values: List[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            value = ""
            if token in {"-r", "--rig-name"}:
                if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
                    raise ValueError("JS8Call --rig-name requires a non-empty value.")
                value = tokens[index + 1]
                index += 1
            elif token.startswith("--rig-name="):
                value = token.split("=", 1)[1]
            elif token.startswith("-r="):
                value = token.split("=", 1)[1]
            if token in {"--rig-name=", "-r="}:
                raise ValueError("JS8Call --rig-name requires a non-empty value.")
            if value:
                values.append(value)
            index += 1
        if len(values) > 1:
            raise ValueError("JS8Call launch command contains duplicate or conflicting rig-name options.")
        if not values:
            return ""
        try:
            return normalize_rig_name(values[0])
        except ValueError as exc:
            raise ValueError(f"Invalid JS8Call rig name: {exc}") from exc

    @staticmethod
    def _validate_js8_launch_collisions(instances: Sequence[PlannedInstance]) -> None:
        js8_instances = [
            instance
            for instance in instances
            if instance.name == "JS8Call" and StationLaunchPlanner._is_local_js8_instance(instance)
        ]
        rig_names: Dict[str, PlannedInstance] = {}
        roots: Dict[str, PlannedInstance] = {}
        for instance in js8_instances:
            rig_key = rig_name_collision_key(instance.rig_name)
            prior_rig = rig_names.get(rig_key)
            if prior_rig is not None:
                raise ValueError(
                    "JS8Call local profiles use the same effective rig name "
                    f"'{instance.rig_name}': {', '.join((*prior_rig.radio_names, *instance.radio_names))}."
                )
            rig_names[rig_key] = instance
            root = str(instance.application_data_root or "").strip()
            if not root or instance.expected_storage_mode != "rig_scoped":
                continue
            root_key = root.casefold()
            prior_root = roots.get(root_key)
            if prior_root is not None:
                raise ValueError(
                    "JS8Call local profiles resolve to the same message-storage root "
                    f"'{root}': {', '.join((*prior_root.radio_names, *instance.radio_names))}."
                )
            roots[root_key] = instance

    @staticmethod
    def _launch_target_looks_subspace(item: Mapping[str, Any]) -> bool:
        values = [str(item.get("launch_path_override", "") or "")]
        try:
            values.extend(shlex.split(str(item.get("launch_command_override", "") or "")))
        except ValueError:
            values.append(str(item.get("launch_command_override", "") or ""))
        for raw in values:
            normalized = str(raw or "").strip().replace("\\", "/")
            if not normalized:
                continue
            parts = {part.casefold() for part in PurePath(normalized).parts}
            basename = PurePath(normalized).name.casefold()
            if basename in {"js8call-subspace", "js8call-subspace.exe", "subspace", "subspace.exe"}:
                return True
            if parts.intersection({"subspace-edition", "js8call-subspace", "js8call subspace"}):
                return True
        return False

    @staticmethod
    def _is_local_js8_instance(instance: PlannedInstance) -> bool:
        readiness = dict(instance.readiness_policy)
        host = str(readiness.get("host", "127.0.0.1") or "127.0.0.1").strip().casefold()
        return host in {"127.0.0.1", "localhost", "::1"}

    @staticmethod
    def _with_profile_overrides(profile: Mapping[str, Any], item: Mapping[str, Any]) -> Dict[str, Any]:
        effective = dict(item)
        name = str(effective.get("name", "") or "")
        structured_facts = _structured_launch_facts(effective, name)
        if structured_facts:
            # A structured VarAC recipe wins over the legacy profile command
            # and installation-path fields.  The orchestrator receives the
            # executable and argv as separate values and never reparses them.
            effective["launch_path_override"] = structured_facts["executable"]
            effective["launch_command_override"] = ""
        elif not str(effective.get("launch_command_override", "") or "").strip() and name == "VarAC":
            effective["launch_command_override"] = str(profile.get("launch_cmd", "") or "").strip()
        if not structured_facts and not str(effective.get("launch_path_override", "") or "").strip():
            path_key = {
                "FLRig": "flrig_path",
                "FLDigi": "fldigi_path",
                "FLAmp": "flamp_path",
                "FLMsg": "flmsg_path",
                "VarAC": "varac_install_path",
                "JS8Call": "js8_install_path",
                "JS8Spotter": "spotter_launch_path",
                "CommStat": "commstat_launch_path",
            }.get(name)
            if path_key:
                effective["launch_path_override"] = str(profile.get(path_key, "") or "").strip()
        return effective

    @staticmethod
    def _identity(profile: Mapping[str, Any], item: Mapping[str, Any]) -> str:
        command = str(item.get("launch_command_override", "") or "").strip()
        path = str(item.get("launch_path_override", "") or "").strip()
        try:
            normalized_command = shlex.join(shlex.split(command)) if command else ""
        except ValueError:
            normalized_command = command
        name = str(item.get("name", "") or "").strip()
        instance_key = str(item.get("instance_key", "") or "").strip()
        readiness = item.get("readiness_policy", {})
        if not isinstance(readiness, Mapping):
            readiness = {}
        arguments = readiness.get("launch_arguments", ())
        if not isinstance(arguments, (list, tuple)):
            arguments = ()
        endpoint: Dict[str, Any] = {}
        if name == "JS8Call":
            endpoint = {
                "host": str(profile.get("js8_host", "127.0.0.1") or "127.0.0.1"),
                "port": int(profile.get("js8_port", 2442) or 2442),
                "profile": str(profile.get("js8_profile_path", "") or ""),
            }
        elif name == "FLRig":
            endpoint = {"host": profile.get("flrig_host"), "port": profile.get("flrig_port")}
        elif name == "FLDigi":
            endpoint = {"host": profile.get("fldigi_host"), "port": profile.get("fldigi_port")}
        elif name == "VarAC":
            endpoint = {"ini": profile.get("varac_ini_path"), "node": profile.get("varac_node_id")}
        payload = {
            "name": name.casefold(),
            "instance_key": instance_key.casefold(),
            "command": normalized_command,
            "path": path,
            "arguments": [str(value) for value in arguments],
            "working_directory": str(readiness.get("working_directory", "") or "").strip(),
            "environment": {
                str(key): str(value)
                for key, value in readiness.get("environment", {}).items()
            } if isinstance(readiness.get("environment", {}), Mapping) else {},
            "profile_selector": str(readiness.get("profile_selector", "") or "").strip(),
            "operator_starts": _truthy(readiness.get("operator_starts", False)),
            "execution_scope": execution_scope(item),
            "endpoint": endpoint,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _dependency_order(
        values: List[Tuple[int, int, PlannedInstance]],
    ) -> List[Tuple[int, int, PlannedInstance]]:
        remaining = sorted(values, key=lambda value: (value[0], value[1], value[2].name.casefold()))
        output: List[Tuple[int, int, PlannedInstance]] = []
        emitted_names: set[str] = set()
        available_names = {value[2].name.casefold() for value in remaining}
        while remaining:
            ready_index = next(
                (
                    index
                    for index, value in enumerate(remaining)
                    if all(
                        dep.casefold() in emitted_names or dep.casefold() not in available_names
                        for dep in value[2].dependencies
                    )
                ),
                None,
            )
            if ready_index is None:
                cycle_names = ", ".join(value[2].name for value in remaining)
                raise ValueError(f"Launch dependency cycle: {cycle_names}")
            value = remaining.pop(ready_index)
            output.append(value)
            emitted_names.add(value[2].name.casefold())
        return output
