"""Component-scoped recovery for legacy managed FLMsg/FLAmp identities.

The repair is deliberately pure.  It prepares a complete merged profile,
manifest, canonical identity record, and launch bundle, but performs no I/O.
The settings store owns the single optimistic transaction that persists the
reviewed result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
from typing import Any, Mapping, Sequence

from freqinout.core.guided_launch_recipes import (
    recipe_draft_updates,
    resolve_fast_light_managed_recipe,
)
from freqinout.core.software_identity_bundle import (
    build_guided_identity_records,
    identity_record_from_mapping,
    identity_record_to_mapping,
)


_MESSAGE_COMPONENTS = frozenset({"flmsg", "flamp"})
_FLDIGI_COMPANION_FLAGS = frozenset(
    {"--arq-server-address", "--arq-server-port", "--flmsg-dir", "--auto-dir"}
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _component_index(components: Sequence[object]) -> dict[str, dict[str, Any]]:
    return {
        _text(item.get("component_key")).casefold(): dict(item)
        for item in components
        if isinstance(item, Mapping) and _text(item.get("component_key"))
    }


def _identity_component_index(components: Sequence[object]) -> dict[str, dict[str, Any]]:
    return {
        _text(item.get("component_id")).casefold(): dict(item)
        for item in components
        if isinstance(item, Mapping) and _text(item.get("component_id"))
    }


def _launch_preferences(item: Mapping[str, Any] | None) -> tuple[bool, bool, bool]:
    value = item or {}
    return (
        bool(value.get("enabled", True)),
        bool(value.get("launch_at_startup", value.get("startup", False))),
        bool(value.get("monitor_health", value.get("enabled", True))),
    )


def _launch_item_from_component(
    component: Mapping[str, Any],
    *,
    bundle_id: str,
    prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    key = _text(component.get("component_key")).casefold()
    enabled, startup, monitor = _launch_preferences(prior)
    executable = _text(component.get("executable"))
    arguments = [str(item) for item in component.get("arguments", ()) or ()]
    readiness = dict(component.get("readiness") or {})
    readiness.update(
        {
            "executable": executable,
            "launch_arguments": arguments,
            "working_directory": _text(component.get("working_directory")),
            "profile_selector": _text(component.get("profile_selector")),
            "configuration_roots": list(component.get("configuration_roots", ()) or ()),
            "data_roots": list(component.get("data_roots", ()) or ()),
            "endpoints": [
                dict(item)
                for item in component.get("endpoints", ()) or ()
                if isinstance(item, Mapping)
            ],
            "evidence": dict(component.get("evidence") or {}),
            "confidence": _text(component.get("confidence")) or "verified",
            "operator_starts": bool(component.get("operator_starts", False)),
            "effective_command": [executable, *arguments] if executable else [],
            "effective_command_text": shlex.join([executable, *arguments]) if executable else "",
            "environment": dict(component.get("environment") or {}),
            "execution_scope": _text(component.get("execution_scope")) or "standard",
        }
    )
    if component.get("managed_directories"):
        readiness["managed_directories"] = list(component.get("managed_directories") or ())
    name = {"fldigi": "FLDigi", "flmsg": "FLMsg", "flamp": "FLAmp"}[key]
    instance_key = _text((prior or {}).get("instance_key"))
    if key == "flamp" and "station-shared" in instance_key.casefold():
        instance_key = ""
    return {
        "name": name,
        "instance_key": instance_key or f"{bundle_id}:{key}",
        "enabled": enabled,
        "startup": startup,
        "monitor_health": monitor,
        "launch_command_override": _text((prior or {}).get("command_override")),
        "launch_path_override": executable,
        "dependencies": list(component.get("dependencies", ()) or ()),
        "readiness_policy": readiness,
        "execution_scope": _text(component.get("execution_scope")) or "standard",
    }


def _portable_child(profile: Mapping[str, Any]) -> str:
    child = _text(profile.get("name")) or "Radio"
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in child).strip("-._") or "Radio"


def _first_root(component: Mapping[str, Any], key: str) -> str:
    roots = component.get(key, ()) or ()
    return _text(roots[0]) if isinstance(roots, (tuple, list)) and roots else ""


def _merge_argument_pairs(
    original: Sequence[object], proposed: Sequence[object], flags: frozenset[str]
) -> list[str]:
    """Replace only named option/value pairs, preserving every other argument."""

    wanted: dict[str, str] = {}
    proposed_args = [str(value) for value in proposed]
    index = 0
    while index < len(proposed_args):
        flag = proposed_args[index]
        if flag in flags and index + 1 < len(proposed_args):
            wanted[flag] = proposed_args[index + 1]
            index += 2
        else:
            index += 1
    merged: list[str] = []
    original_args = [str(value) for value in original]
    index = 0
    while index < len(original_args):
        flag = original_args[index]
        if flag in flags:
            index += 2 if index + 1 < len(original_args) else 1
            continue
        merged.append(flag)
        index += 1
    for flag in ("--arq-server-address", "--arq-server-port", "--flmsg-dir", "--auto-dir"):
        if flag in wanted:
            merged.extend((flag, wanted[flag]))
    return merged


def legacy_repair_source_fingerprint(
    profile: Mapping[str, Any],
    application: Mapping[str, Any],
    launch_bundle: Mapping[str, Any],
    manifest: Mapping[str, Any] | None = None,
) -> str:
    """Fence a legacy repair against any saved-source change after review."""

    payload = {
        "profile": dict(profile),
        "application": dict(application),
        "launch_bundle": dict(launch_bundle),
        "manifest": dict(manifest or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_legacy_fast_light_repair_inputs(
    *,
    profile: Mapping[str, Any],
    application: Mapping[str, Any],
    launch_bundle: Mapping[str, Any],
    arq_port: int,
    platform: object | None = None,
    storage_home: Path | None = None,
    existing_manifest: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], tuple[Any, ...], str]:
    """Build an in-memory canonical baseline from one unambiguous legacy assignment.

    Nothing is persisted here.  Saved launch rows take precedence over newly
    derived defaults so FLRig/FLDigi identity is retained during the repair.
    """

    application_key = _text(application.get("system_key"))
    if not application_key:
        raise ValueError("The linked Fast Light application has no stable identity.")
    bundle_id = _text((existing_manifest or {}).get("instance_key")) or f"fast_light:{application_key}"
    use_flmsg = bool(int(profile.get("use_flmsg", 0) or 0))
    use_flamp = bool(int(profile.get("use_flamp", 0) or 0))
    paths = {
        "flrig": _text(application.get("flrig_path") or profile.get("flrig_path")),
        "fldigi": _text(application.get("fldigi_path") or profile.get("fldigi_path")),
        "flmsg": _text(profile.get("flmsg_path") or application.get("flmsg_path")),
        "flamp": _text(profile.get("flamp_path") or application.get("flamp_path")),
    }
    for key, label, selected in (
        ("flrig", "FLRig", bool(int(profile.get("use_flrig", 0) or 0))),
        ("fldigi", "FLDigi", bool(int(profile.get("use_fldigi", 0) or 0))),
        ("flmsg", "FLMsg", use_flmsg),
        ("flamp", "FLAmp", use_flamp),
    ):
        if selected and not paths[key]:
            raise ValueError(f"The saved {label} executable is missing; choose it before repair.")

    draft = {
        "family_key": "fast_light",
        "mode": "managed",
        "draft_instance_key": bundle_id,
        "instance_key": bundle_id,
        "application_system_key": application_key,
        "instance_name": _text(profile.get("name")) or "Radio",
        "owner_label": _text(profile.get("name")) or "Radio",
        "radio_role": "observer" if _text(profile.get("device_class")).casefold() == "observer" else "transceiver",
        "application_path": paths["flrig"],
        "secondary_application_path": paths["fldigi"],
        "flmsg_application_path": paths["flmsg"],
        "flamp_application_path": paths["flamp"],
        "use_flmsg": use_flmsg,
        "use_flamp": use_flamp,
        "host": _text(profile.get("flrig_host") or application.get("flrig_host")) or "127.0.0.1",
        "port": int(profile.get("flrig_port") or application.get("flrig_port") or 0),
        "secondary_port": int(profile.get("fldigi_port") or application.get("fldigi_port") or 0),
        "arq_port": int(arq_port),
    }
    resolution = resolve_fast_light_managed_recipe(
        draft, managed_root="", platform=platform, storage_home=storage_home or Path.home()
    )
    if not resolution.persistable:
        raise ValueError(resolution.recovery_action or "CommsPlan could not derive a safe legacy repair.")
    updates = recipe_draft_updates(resolution)
    baseline_recipe = dict(updates["launch_recipe"])
    components = _component_index(baseline_recipe.get("components", ()) or ())
    launch_by_name = {
        _text(item.get("app_name") or item.get("name")).casefold(): item
        for item in launch_bundle.get("items", ()) or ()
        if isinstance(item, Mapping)
    }
    for key in tuple(components):
        item = launch_by_name.get(key)
        if not isinstance(item, Mapping):
            continue
        readiness = item.get("readiness", item.get("readiness_policy", {}))
        readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
        component = dict(components[key])
        executable = _text(
            item.get("path_override")
            or item.get("launch_path_override")
            or readiness.get("executable")
            or component.get("executable")
        )
        arguments = [str(value) for value in readiness.get("launch_arguments", ()) or ()]
        component_readiness = {
            name: value
            for name, value in readiness.items()
            if name
            not in {
                "executable",
                "launch_arguments",
                "working_directory",
                "profile_selector",
                "configuration_roots",
                "data_roots",
                "managed_directories",
                "endpoints",
                "effective_command",
                "effective_command_text",
                "environment",
                "execution_scope",
                "operator_starts",
            }
        }
        component.update(
            {
                "executable": executable,
                "arguments": arguments,
                "working_directory": _text(readiness.get("working_directory")),
                "profile_selector": _text(readiness.get("profile_selector")),
                "configuration_roots": list(readiness.get("configuration_roots", ()) or ()),
                "data_roots": list(readiness.get("data_roots", ()) or ()),
                "managed_directories": list(readiness.get("managed_directories", ()) or ()),
                "endpoints": list(readiness.get("endpoints", ()) or ()),
                "effective_command": [executable, *arguments] if executable else [],
                "effective_command_text": shlex.join([executable, *arguments]) if executable else "",
                "readiness": component_readiness,
                "operator_starts": bool(readiness.get("operator_starts", not executable)),
            }
        )
        components[key] = component
    baseline_recipe["components"] = [
        components[_text(item.get("component_key")).casefold()]
        for item in baseline_recipe.get("components", ()) or ()
        if isinstance(item, Mapping) and _text(item.get("component_key")).casefold() in components
    ]
    from freqinout.core.guided_launch_recipes import recipe_resolution_from_mapping

    baseline_recipe = recipe_resolution_from_mapping(baseline_recipe).to_mapping()
    draft.update(updates)
    draft.update(
        {
            "launch_recipe": baseline_recipe,
            "launch_recipe_fingerprint": baseline_recipe["fingerprint"],
            "management_mode": _text((existing_manifest or {}).get("management_mode")) or "fio_managed",
            "configuration_path": _text((existing_manifest or {}).get("configuration_path")),
            "storage_path": _text((existing_manifest or {}).get("data_root")),
            "secondary_storage_path": _text(profile.get("fldigi_checkin_dir")),
            "flmsg_message_path": _text(profile.get("flmsg_message_path")),
            "flamp_receive_path": _text(profile.get("flamp_message_path")),
            "ports": list(updates.get("ports", ())),
            "resource_claims": list(updates.get("resource_claims", ())),
        }
    )
    records = build_guided_identity_records(profile, {"fast_light": draft}, ("fast_light",))
    manifest = dict(existing_manifest or {})
    manifest.update(
        {
            "instance_key": bundle_id,
            "family_key": "fast_light",
            "application_system_key": application_key,
            "management_mode": draft["management_mode"],
            "provenance": _text(manifest.get("provenance")) or "legacy_repair",
            "executable_path": paths["flrig"],
            "configuration_path": draft["configuration_path"],
            "configuration_root": draft["configuration_path"],
            "data_root": draft["storage_path"],
            "ports": draft["ports"],
            "resource_claims": draft["resource_claims"],
            "evidence": {
                **dict(manifest.get("evidence") or {}),
                "launch_recipe": baseline_recipe,
                "launch_recipe_fingerprint": baseline_recipe["fingerprint"],
                "legacy_repair_bootstrap": True,
            },
        }
    )
    return manifest, records, legacy_repair_source_fingerprint(
        profile, application, launch_bundle, existing_manifest
    )


@dataclass(frozen=True)
class FastLightComponentRepairPlan:
    radio_profile_id: int
    identity_generation: int
    manifest_instance_key: str
    manifest_updated_utc: str
    arq_port: int
    profile: Mapping[str, Any]
    manifest: Mapping[str, Any]
    identity_records: tuple[Any, ...]
    launch_enabled: bool
    launch_items: tuple[Mapping[str, Any], ...]
    changed_components: tuple[str, ...]
    bootstrap_legacy: bool = False
    source_fingerprint: str = ""

    @property
    def summary(self) -> str:
        changed = ", ".join(self.changed_components)
        return (
            f"Update {changed} for this radio and pair FLAmp with FLDigi ARQ port "
            f"{self.arq_port}. FLRig identity and unrelated software remain unchanged."
        )


def build_fast_light_component_repair_plan(
    *,
    profile: Mapping[str, Any],
    manifest: Mapping[str, Any],
    identity_records: Sequence[Any],
    identity_generation: int,
    launch_bundle: Mapping[str, Any],
    arq_port: int,
    platform: object | None = None,
    storage_home: Path | None = None,
) -> FastLightComponentRepairPlan:
    """Return one complete merged repair without mutating the inputs."""

    radio_id = int(profile.get("id", 0) or 0)
    if radio_id <= 0:
        raise ValueError("Select one saved radio before repairing FLMsg/FLAmp.")
    if not (1 <= int(arq_port) <= 65535):
        raise ValueError("A distinct FLAmp ARQ port is required for component repair.")

    fast_light_records = [record for record in identity_records if record.family_key == "fast_light"]
    if len(fast_light_records) != 1:
        raise ValueError("The selected radio does not have one unambiguous canonical Fast Light identity.")
    current_record = fast_light_records[0]
    bundle_id = current_record.bundle_id
    if _text(manifest.get("instance_key")) != bundle_id:
        raise ValueError("Fast Light manifest and canonical identity do not refer to the same instance.")

    evidence = manifest.get("evidence", {})
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    current_recipe = evidence.get("launch_recipe", {})
    if not isinstance(current_recipe, Mapping):
        raise ValueError("The saved Fast Light identity has no reviewed launch recipe to repair.")
    current_components = _component_index(current_recipe.get("components", ()) or ())
    if "fldigi" not in current_components:
        raise ValueError("The saved Fast Light identity has no FLDigi component.")

    flrig_component = current_components.get("flrig", {})
    fldigi_component = current_components["fldigi"]
    flmsg_component = current_components.get("flmsg", {})
    flamp_component = current_components.get("flamp", {})
    child = _portable_child(profile)
    operator_home = Path(storage_home) if storage_home is not None else Path.home()
    system = str(platform or os.sys.platform or "").strip().casefold()
    default_nbems = (
        operator_home / "NBEMS.files"
        if system in {"windows", "win", "win32", "cygwin"}
        else operator_home / ".nbems"
    )
    nbems_root = (
        _first_root(flmsg_component, "configuration_roots")
        or _first_root(flamp_component, "configuration_roots")
        or str(default_nbems / "instances" / child)
    )
    flamp_receive = str(Path(nbems_root) / "FLAMP" / "rx")
    flamp_outgoing = str(Path(nbems_root) / "FLAMP" / "tx")
    host = _text(profile.get("fldigi_host") or profile.get("flrig_host")) or "127.0.0.1"

    draft = {
        "family_key": "fast_light",
        "mode": "managed",
        "draft_instance_key": bundle_id,
        "instance_key": bundle_id,
        "instance_name": _text(profile.get("name")) or child,
        "owner_label": _text(profile.get("name")) or child,
        "radio_role": "observer" if _text(profile.get("device_class")).casefold() == "observer" else "transceiver",
        "application_path": _text(flrig_component.get("executable")),
        "secondary_application_path": _text(fldigi_component.get("executable")),
        "flmsg_application_path": _text(profile.get("flmsg_path") or flmsg_component.get("executable")),
        "flamp_application_path": _text(profile.get("flamp_path") or flamp_component.get("executable")),
        "use_flmsg": bool(int(profile.get("use_flmsg", 0) or 0)),
        "use_flamp": bool(int(profile.get("use_flamp", 0) or 0)),
        "host": host,
        "port": int(profile.get("flrig_port", 0) or 0),
        "secondary_port": int(profile.get("fldigi_port", 0) or 0),
        "arq_port": int(arq_port),
        "flrig_native_root": _first_root(flrig_component, "configuration_roots"),
        "fldigi_native_root": _first_root(fldigi_component, "configuration_roots"),
        "flmsg_native_root": nbems_root,
        "flamp_native_root": nbems_root,
        "flamp_receive_path": flamp_receive,
        "flamp_outgoing_path": flamp_outgoing,
        "launch_at_startup": False,
    }
    resolution = resolve_fast_light_managed_recipe(
        draft,
        managed_root="",
        platform=platform,
        storage_home=operator_home,
    )
    if not resolution.persistable:
        raise ValueError(resolution.recovery_action or "CommsPlan could not derive a safe FLMsg/FLAmp repair.")
    updates = recipe_draft_updates(resolution)
    proposed_recipe = dict(updates["launch_recipe"])
    proposed_components = _component_index(proposed_recipe.get("components", ()) or ())

    # Keep the established FLRig recipe byte-for-byte.  FLDigi retains its
    # executable/profile/XML-RPC identity; only the companion selectors needed
    # by FLMsg/FLAmp are taken from the qualified recipe.
    if flrig_component:
        proposed_components["flrig"] = dict(flrig_component)
    repaired_fldigi = dict(fldigi_component)
    fldigi_arguments = _merge_argument_pairs(
        fldigi_component.get("arguments", ()) or (),
        proposed_components["fldigi"].get("arguments", ()) or (),
        _FLDIGI_COMPANION_FLAGS,
    )
    fldigi_executable = _text(repaired_fldigi.get("executable"))
    repaired_fldigi["arguments"] = fldigi_arguments
    repaired_fldigi["effective_command"] = [fldigi_executable, *fldigi_arguments]
    repaired_fldigi["effective_command_text"] = shlex.join(
        [fldigi_executable, *fldigi_arguments]
    )
    repaired_fldigi_readiness = dict(repaired_fldigi.get("readiness") or {})
    if "launch_arguments" in repaired_fldigi_readiness:
        repaired_fldigi_readiness["launch_arguments"] = list(fldigi_arguments)
    if "effective_command" in repaired_fldigi_readiness:
        repaired_fldigi_readiness["effective_command"] = [
            fldigi_executable,
            *fldigi_arguments,
        ]
    if "effective_command_text" in repaired_fldigi_readiness:
        repaired_fldigi_readiness["effective_command_text"] = shlex.join(
            [fldigi_executable, *fldigi_arguments]
        )
    repaired_fldigi["readiness"] = repaired_fldigi_readiness
    proposed_components["fldigi"] = repaired_fldigi

    ordered_component_keys = [
        _text(item.get("component_key")).casefold()
        for item in current_recipe.get("components", ()) or ()
        if isinstance(item, Mapping)
    ]
    for key in ("fldigi", "flmsg", "flamp"):
        if key not in ordered_component_keys:
            ordered_component_keys.append(key)
    proposed_recipe["components"] = [
        proposed_components[key] for key in ordered_component_keys if key in proposed_components
    ]
    # Recompute the canonical recipe fingerprint after the surgical merge.
    from freqinout.core.guided_launch_recipes import recipe_resolution_from_mapping

    repaired_resolution = recipe_resolution_from_mapping(proposed_recipe)
    repaired_recipe = repaired_resolution.to_mapping()
    repaired_components = _component_index(repaired_recipe.get("components", ()) or ())

    draft.update(updates)
    draft.update(
        {
            "launch_recipe": repaired_recipe,
            "launch_recipe_fingerprint": repaired_recipe["fingerprint"],
            "instance_key": bundle_id,
            "management_mode": _text(manifest.get("management_mode")) or "fio_managed",
            "configuration_path": _text(manifest.get("configuration_path")),
            "storage_path": _text(manifest.get("data_root")),
            "secondary_storage_path": _text(profile.get("fldigi_checkin_dir")),
            "flmsg_message_path": str(Path(nbems_root) / "ICS" / "messages"),
            "flmsg_templates_path": str(Path(nbems_root) / "ICS" / "templates"),
            "flmsg_auto_path": str(Path(nbems_root) / "WRAP" / "auto"),
            "flamp_receive_path": flamp_receive,
            "flamp_outgoing_path": flamp_outgoing,
            "ports": list(updates.get("ports", ())),
            "resource_claims": list(updates.get("resource_claims", ())),
        }
    )
    modern_record = build_guided_identity_records(profile, {"fast_light": draft}, ("fast_light",))[0]
    old_record_mapping = dict(identity_record_to_mapping(current_record))
    modern_mapping = dict(identity_record_to_mapping(modern_record))
    old_identity_components = _identity_component_index(old_record_mapping.get("components", ()) or ())
    modern_identity_components = _identity_component_index(modern_mapping.get("components", ()) or ())

    # Preserve FLRig exactly.  For FLDigi, only argv changes to add the shared
    # NBEMS root and distinct ARQ listener required by the repaired children.
    repaired_identity_components = dict(old_identity_components)
    repaired_identity_components["fldigi"] = {
        **old_identity_components["fldigi"],
        "argv": list(modern_identity_components["fldigi"]["argv"]),
    }
    for key in _MESSAGE_COMPONENTS:
        if key in modern_identity_components:
            prior_launch = dict(old_identity_components.get(key, {}).get("launch") or {})
            next_component = dict(modern_identity_components[key])
            next_launch = dict(next_component.get("launch") or {})
            next_launch.update({
                name: value
                for name, value in prior_launch.items()
                if name in {"at_startup", "monitor_health"}
            })
            next_component["launch"] = next_launch
            repaired_identity_components[key] = next_component

    repaired_record_mapping = dict(old_record_mapping)
    repaired_record_mapping["components"] = [
        repaired_identity_components[_text(item.get("component_id")).casefold()]
        for item in old_record_mapping.get("components", ()) or ()
        if isinstance(item, Mapping)
        and _text(item.get("component_id")).casefold() in repaired_identity_components
    ]
    for key in ("flmsg", "flamp"):
        if key not in {
            _text(item.get("component_id")).casefold()
            for item in repaired_record_mapping["components"]
            if isinstance(item, Mapping)
        } and key in repaired_identity_components:
            repaired_record_mapping["components"].append(repaired_identity_components[key])
    repaired_record_mapping["paths"] = modern_mapping["paths"]
    preserved_resources = [
        dict(item)
        for item in old_record_mapping.get("resources", ()) or ()
        if isinstance(item, Mapping)
        and not _text(item.get("resource_type")).casefold().startswith(("flmsg", "flamp"))
    ]
    repaired_record_mapping["resources"] = preserved_resources + [
        dict(item)
        for item in modern_mapping.get("resources", ()) or ()
        if isinstance(item, Mapping)
        and _text(item.get("resource_type")).casefold().startswith(("flmsg", "flamp"))
    ]
    repaired_record_mapping["endpoints"] = modern_mapping["endpoints"]
    repaired_launch = dict(old_record_mapping.get("launch") or {})
    repaired_readiness = dict(old_record_mapping.get("readiness") or {})
    for section in ("argv", "cwd", "env", "dependencies"):
        section_value = dict(repaired_launch.get(section) or {})
        modern_section = dict(modern_mapping.get("launch", {}).get(section) or {})
        for key in ("fldigi", "flmsg", "flamp"):
            if key in modern_section:
                section_value[key] = modern_section[key]
        repaired_launch[section] = section_value
    modern_readiness = dict(modern_mapping.get("readiness") or {})
    for key in ("flmsg", "flamp"):
        if key in modern_readiness:
            repaired_readiness[key] = modern_readiness[key]
    repaired_record_mapping["launch"] = repaired_launch
    repaired_record_mapping["readiness"] = repaired_readiness
    verification = dict(repaired_record_mapping.get("verification") or {})
    verification_evidence = dict(verification.get("evidence") or {})
    verification_evidence["launch_recipe_fingerprint"] = repaired_recipe["fingerprint"]
    verification["evidence"] = verification_evidence
    repaired_record_mapping["verification"] = verification
    repaired_record = identity_record_from_mapping(repaired_record_mapping)
    merged_records = tuple(
        repaired_record if record.family_key == "fast_light" else record
        for record in identity_records
    )

    repaired_manifest = dict(manifest)
    repaired_evidence = dict(evidence)
    repaired_evidence["launch_recipe"] = repaired_recipe
    repaired_evidence["launch_recipe_fingerprint"] = repaired_recipe["fingerprint"]
    repaired_manifest["evidence"] = repaired_evidence
    repaired_manifest["ports"] = list(updates.get("ports", ()))
    prior_claims = [
        dict(item)
        for item in manifest.get("resource_claims", ()) or ()
        if isinstance(item, Mapping)
        and not _text(item.get("kind")).casefold().startswith(("flmsg", "flamp"))
    ]
    repaired_manifest["resource_claims"] = prior_claims + [
        dict(item)
        for item in updates.get("resource_claims", ()) or ()
        if isinstance(item, Mapping)
        and _text(item.get("kind")).casefold().startswith(("flmsg", "flamp"))
    ]

    repaired_profile = dict(profile)
    if bool(int(profile.get("use_flmsg", 0) or 0)):
        repaired_profile["flmsg_message_path"] = str(Path(nbems_root) / "ICS" / "messages")
    if bool(int(profile.get("use_flamp", 0) or 0)):
        repaired_profile["flamp_message_path"] = flamp_receive

    prior_items = [dict(item) for item in launch_bundle.get("items", ()) if isinstance(item, Mapping)]
    prior_by_name = {_text(item.get("app_name") or item.get("name")).casefold(): item for item in prior_items}
    replacements = {
        key: _launch_item_from_component(
            repaired_components[key],
            bundle_id=bundle_id,
            prior=prior_by_name.get(key),
        )
        for key in ("fldigi", "flmsg", "flamp")
        if key in repaired_components
    }
    merged_launch_items: list[Mapping[str, Any]] = []
    emitted: set[str] = set()
    for item in prior_items:
        key = _text(item.get("app_name") or item.get("name")).casefold()
        if key in replacements:
            merged_launch_items.append(replacements[key])
            emitted.add(key)
        else:
            merged_launch_items.append({
                "name": _text(item.get("app_name") or item.get("name")),
                "instance_key": _text(item.get("instance_key")),
                "enabled": bool(item.get("enabled", True)),
                "startup": bool(item.get("launch_at_startup", item.get("startup", False))),
                "monitor_health": bool(item.get("monitor_health", True)),
                "launch_command_override": _text(item.get("command_override") or item.get("launch_command_override")),
                "launch_path_override": _text(item.get("path_override") or item.get("launch_path_override")),
                "dependencies": list(item.get("dependencies", ()) or ()),
                "readiness_policy": dict(item.get("readiness") or item.get("readiness_policy") or {}),
                "execution_scope": _text(
                    (item.get("readiness") or {}).get("execution_scope")
                    if isinstance(item.get("readiness"), Mapping)
                    else item.get("execution_scope")
                ) or "standard",
            })
    for key in ("fldigi", "flmsg", "flamp"):
        if key in replacements and key not in emitted:
            merged_launch_items.append(replacements[key])

    changed = tuple(
        {"flmsg": "FLMsg", "flamp": "FLAmp"}[key]
        for key in ("flmsg", "flamp")
        if key in repaired_components
    )
    return FastLightComponentRepairPlan(
        radio_profile_id=radio_id,
        identity_generation=int(identity_generation),
        manifest_instance_key=bundle_id,
        manifest_updated_utc=_text(manifest.get("updated_utc")),
        arq_port=int(arq_port),
        profile=repaired_profile,
        manifest=repaired_manifest,
        identity_records=merged_records,
        launch_enabled=bool(launch_bundle.get("launch_enabled", False)),
        launch_items=tuple(merged_launch_items),
        changed_components=changed,
    )


__all__ = [
    "FastLightComponentRepairPlan",
    "build_fast_light_component_repair_plan",
    "build_legacy_fast_light_repair_inputs",
    "legacy_repair_source_fingerprint",
]
