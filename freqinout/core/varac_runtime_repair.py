"""Automatic bounded repair for obsolete FIO-managed Wine VARA runtimes."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from freqinout.core.config_backup import create_config_backup, restore_config_backup
from freqinout.core.config_varac_managed import (
    SUPPORTED_VARAC_WRITERS,
    VarACNativeConfigurationError,
    parse_varac_ini_bytes,
    render_varac_ini,
)
from freqinout.core.launch_bundle_store import LaunchBundleStore
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.software_identity_bundle import SoftwareIdentityComponent
from freqinout.core.station_launch_planner import StationLaunchPlanner
from freqinout.core.varac_native_preparation import (
    VarACManagedRuntimeRepair,
    prepare_managed_varac_runtime_repair,
)
from freqinout.core.varac_native_transaction import apply_varac_native_transaction


def _platform_key() -> str:
    return "linux-wine" if sys.platform.startswith("linux") else "windows" if sys.platform.startswith("win") else sys.platform


def _replace_resource(rows: Any, runtime: Path, ini: Path, *, identity: bool = False) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        kind = str(row.get("resource_type" if identity else "kind") or "").strip().casefold()
        if kind == "vara_runtime":
            row["value"] = str(runtime)
        elif kind == "vara_ini":
            row["value"] = str(ini)
        updated.append(row)
    return updated


def _vara_recipe_component(raw: Mapping[str, Any], runtime: Path, wine_prefix: str) -> dict[str, Any]:
    executable = runtime / "VARA.exe"
    component = dict(raw)
    component.update(
        {
            "component_key": "vara",
            "executable": "wine",
            "arguments": [str(executable)],
            "effective_command": ["wine", str(executable)],
            "effective_command_text": shlex.join(["wine", str(executable)]),
            "working_directory": str(runtime),
            "managed_directories": [str(runtime)],
            "environment": {"WINEPREFIX": wine_prefix},
            "dependencies": [],
            "execution_scope": "standard",
            "operator_starts": False,
        }
    )
    component.setdefault("readiness", {"kind": "process"})
    return component


def _persist_runtime_projection(
    store: MultiRadioStore,
    repair: VarACManagedRuntimeRepair,
    *,
    observed: Mapping[str, Any],
) -> Mapping[str, Any]:
    runtime = Path(repair.target_runtime or "")
    vara_ini = Path(repair.target_ini or runtime / "VARA.ini")
    node = store.get_varac_node(repair.node_id)
    if not isinstance(node, Mapping):
        raise KeyError(f"Unknown VarAC node id: {repair.node_id}")
    ini_path = Path(str(node.get("ini_path") or "")).expanduser().absolute()
    parts = ini_path.parts
    drive_index = next(
        (index for index, part in enumerate(parts) if part.casefold().startswith("drive_") and len(part) == 7),
        -1,
    )
    wine_prefix = str(Path(*parts[:drive_index])) if drive_index >= 0 else ""
    writer_key = str(node.get("native_writer_key") or "")
    if repair.plan is not None:
        writer_key = f"varac:{repair.plan.version}:{repair.plan.platform}:{repair.plan.operation}"
    saved_node = store.save_varac_node(
        {
            **dict(node),
            "vara_runtime_path": str(runtime),
            "vara_ini_path": str(vara_ini),
            "native_writer_key": writer_key,
            "desired_fingerprint": (
                repair.plan.plan_fingerprint if repair.plan is not None
                else str(node.get("desired_fingerprint") or "")
            ),
            "observed_fingerprint": str(
                observed.get("observed_fingerprint")
                or node.get("observed_fingerprint")
                or ""
            ),
            "native_verification_summary": (
                "Managed VARA runtime verified inside the VarAC Wine drive; CommsPlan projections synchronized."
            ),
        }
    )

    matching_manifests = [
        row for row in store.list_software_instance_manifests()
        if isinstance(row, Mapping)
        and str(row.get("family_key") or "") == "varac"
        and str(row.get("application_system_key") or "") == str(node.get("system_key") or "")
    ]
    for manifest in matching_manifests:
        evidence = dict(manifest.get("evidence") or {})
        recipe = dict(evidence.get("launch_recipe") or {})
        components = []
        found_vara = False
        for raw in recipe.get("components", ()) or ():
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("component_key") or "").strip().casefold() == "vara":
                components.append(_vara_recipe_component(raw, runtime, wine_prefix))
                found_vara = True
            else:
                components.append(dict(raw))
        if found_vara:
            recipe["components"] = components
            if repair.plan is not None:
                recipe["fingerprint"] = repair.plan.plan_fingerprint
            evidence["launch_recipe"] = recipe
        store.save_software_instance_manifest(
            {
                **dict(manifest),
                "resource_claims": _replace_resource(
                    manifest.get("resource_claims", ()), runtime, vara_ini
                ),
                "evidence": evidence,
            }
        )

    records = list(store.list_radio_software_identity_records(repair.radio_profile_id))
    if records:
        changed = False
        next_records = []
        for record in records:
            if record.family_key != "varac":
                next_records.append(record)
                continue
            components = []
            launch = dict(record.launch)
            argv_map = dict(launch.get("argv") or {})
            cwd_map = dict(launch.get("cwd") or {})
            env_map = dict(launch.get("env") or {})
            vara_argv = ("wine", str(runtime / "VARA.exe"))
            for component in record.components:
                if component.component_id.casefold() == "vara":
                    components.append(
                        SoftwareIdentityComponent(
                            component_id=component.component_id,
                            argv=vara_argv,
                            cwd=str(runtime),
                            env={"WINEPREFIX": wine_prefix},
                            dependencies=component.dependencies,
                            launch=component.launch,
                            readiness=component.readiness,
                        )
                    )
                    changed = True
                else:
                    components.append(component)
            argv_map["vara"] = list(vara_argv)
            cwd_map["vara"] = str(runtime)
            env_map["vara"] = {"WINEPREFIX": wine_prefix}
            launch.update({"argv": argv_map, "cwd": cwd_map, "env": env_map})
            next_records.append(
                replace(
                    record,
                    resources=tuple(
                        _replace_resource(record.resources, runtime, vara_ini, identity=True)
                    ),
                    components=tuple(components),
                    launch=launch,
                )
            )
        if changed:
            store.save_radio_software_identity_records(
                repair.radio_profile_id,
                next_records,
                expected_generation=store.radio_software_identity_generation(
                    repair.radio_profile_id
                ),
            )

    bundle = store.get_radio_launch_bundle(repair.radio_profile_id)
    items = []
    for raw in bundle.get("items", ()) or ():
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        items.append(
            {
                "name": str(row.get("name") or row.get("app_name") or ""),
                "instance_key": str(row.get("instance_key") or ""),
                "enabled": bool(row.get("enabled", True)),
                "startup": bool(
                    row.get("startup", row.get("launch_at_startup", False))
                ),
                "monitor_health": bool(row.get("monitor_health", True)),
                "launch_path_override": str(
                    row.get("launch_path_override")
                    or row.get("path_override")
                    or ""
                ),
                "launch_command_override": str(
                    row.get("launch_command_override")
                    or row.get("command_override")
                    or ""
                ),
                "dependencies": list(row.get("dependencies") or ()),
                "readiness_policy": dict(
                    row.get("readiness_policy") or row.get("readiness") or {}
                ),
            }
        )
    launch_changed = False
    for row in items:
        if str(row.get("name") or "").strip().casefold() != "vara":
            continue
        readiness = dict(row.get("readiness_policy") or {})
        readiness.update(
            {
                "structured_launch": True,
                "executable": "wine",
                "launch_arguments": [str(runtime / "VARA.exe")],
                "working_directory": str(runtime),
                "managed_directories": [str(runtime)],
                "environment": {"WINEPREFIX": wine_prefix},
            }
        )
        row.update(
            launch_path_override="wine",
            launch_command_override="",
            readiness_policy=readiness,
        )
        launch_changed = True
    if launch_changed:
        store.save_radio_launch_bundle(
            repair.radio_profile_id,
            launch_enabled=bool(bundle.get("launch_enabled", False)),
            items=items,
        )
    return {"node": saved_node, "radio_profile_id": repair.radio_profile_id}


def _ini_value(values: Mapping[str, Mapping[str, str]], section: str, key: str) -> str:
    for section_name, section_values in values.items():
        if str(section_name).strip().casefold() != section.casefold():
            continue
        for field_name, value in section_values.items():
            if str(field_name).strip().casefold() == key.casefold():
                return str(value or "").strip()
    return ""


def managed_varac_process_attribution(
    store: MultiRadioStore,
    status: Any,
) -> Mapping[str, Any]:
    """Attribute running VarAC/VARA processes to saved radio identities.

    A sibling node must not defer repair of the requested member.  Use the
    station's saved structured commands and arguments so Wine-hosted VarAC and
    VARA processes are attributed to their exact radio instead of by family.
    The ``complete`` flag stays false whenever family process counts cannot be
    reconciled, preserving the fail-closed rule for an unknown process.
    """

    profiles = tuple(store.list_device_profiles())
    profile_node_ids = {
        int(profile.get("id") or 0): int(profile.get("varac_node_id") or 0)
        for profile in profiles
        if int(profile.get("id") or 0) > 0
        and int(profile.get("varac_node_id") or 0) > 0
    }
    if not profile_node_ids:
        return {
            "running_node_ids": (),
            "observed_process_count": 0,
            "attributed_process_count": 0,
            "catalog_complete": True,
            "complete": True,
        }
    bundle_store = LaunchBundleStore(store.db_path)
    planner = StationLaunchPlanner()
    catalog: list[Mapping[str, Any]] = []
    catalog_complete = True
    for profile in profiles:
        profile_id = int(profile.get("id") or 0)
        if profile_id not in profile_node_ids:
            continue
        try:
            # This is an attribution inventory, not authorization to launch
            # multiple radios.  Planning one identity at a time preserves the
            # normal station-wide collision guard while allowing members of a
            # verified VarAC cluster to share their database by design.
            review = planner.plan_review(
                (profile,),
                {profile_id: bundle_store.get_bundle(profile_id)},
                trigger="varac-launch-policy-repair",
            )
            catalog.extend(review.queue())
        except Exception:
            catalog_complete = False
    running: set[int] = set()
    attributed: set[str] = set()
    for item in catalog:
        name = str(item.get("name") or "").strip()
        if name.casefold() not in {"varac", "vara"}:
            continue
        target = str(
            item.get("launch_command_override")
            or item.get("launch_path_override")
            or ""
        ).strip()
        arguments = item.get("launch_arguments", ())
        if not isinstance(arguments, (list, tuple)):
            arguments = ()
        if not target:
            continue
        try:
            exact_running = bool(
                status.program_instance_running(name, target, arguments)
            )
        except Exception:
            exact_running = False
        if not exact_running:
            continue
        attributed.add(
            repr(
                (
                    name.casefold(),
                    target,
                    tuple(str(value) for value in arguments),
                )
            )
        )
        for profile_id in item.get("radio_ids", ()) or ():
            node_id = profile_node_ids.get(int(profile_id or 0), 0)
            if node_id > 0:
                running.add(node_id)
    try:
        observed = sum(
            int(status.cached_program_process_count(name))
            for name in ("VarAC", "VARA")
        )
        complete = catalog_complete and observed <= len(attributed)
    except Exception:
        observed = -1
        complete = False
    return {
        "running_node_ids": tuple(sorted(running)),
        "observed_process_count": observed,
        "attributed_process_count": len(attributed),
        "catalog_complete": catalog_complete,
        "complete": complete,
    }


def running_managed_varac_node_ids(
    store: MultiRadioStore,
    status: Any,
) -> tuple[int, ...]:
    return tuple(
        managed_varac_process_attribution(store, status).get(
            "running_node_ids",
            (),
        )
    )


def repair_managed_varac_cluster_launch_policy(
    store: MultiRadioStore,
    *,
    backup_root: Path,
    process_running: bool = False,
    running_node_ids: tuple[int, ...] = (),
    platform_override: str = "",
) -> tuple[Mapping[str, Any], ...]:
    """Repair only managed cluster members whose VarAC does not start VARA.

    This intentionally does not reuse the full runtime-clone transaction: the
    existing VARA runtime is already the reviewed identity.  The correction
    backs up and atomically rewrites one qualified VarAC key, verifies it, and
    restores the backup if either native readback or FIO persistence fails.
    """

    platform_key = str(platform_override or _platform_key()).strip().casefold()
    if platform_key not in {"linux-wine", "windows"}:
        return ()
    running_ids = {int(value) for value in running_node_ids if int(value) > 0}
    profiles = store.list_device_profiles()
    profiles_by_id = {int(row.get("id") or 0): row for row in profiles}
    nodes = {int(row.get("id") or 0): row for row in store.list_varac_nodes()}
    results: list[Mapping[str, Any]] = []
    for membership in store.list_varac_cluster_members():
        if int(membership.get("enabled", 1) or 0) != 1:
            continue
        profile = profiles_by_id.get(int(membership.get("device_profile_id") or 0))
        if not isinstance(profile, Mapping):
            continue
        node = nodes.get(int(profile.get("varac_node_id") or 0))
        if not isinstance(node, Mapping):
            continue
        node_id = int(node.get("id") or 0)
        if str(node.get("native_management_state") or "operator").strip().casefold() != "managed":
            continue
        try:
            ini_path = Path(str(node.get("ini_path") or "")).expanduser()
            if not ini_path.is_file() or ini_path.is_symlink():
                raise VarACNativeConfigurationError(
                    "The managed cluster VarAC INI is unavailable or unsafe to update."
                )
            source = parse_varac_ini_bytes(ini_path, ini_path.read_bytes())
            current = _ini_value(
                source.values,
                "VARAHF_CONFIG",
                "VarahfLaunchOnModemConnect",
            )
            if current.casefold() == "on":
                continue
            writer_key = str(node.get("native_writer_key") or "")
            version = next(
                (
                    capability.version
                    for capability in SUPPORTED_VARAC_WRITERS
                    if capability.version in writer_key
                ),
                "",
            )
            capability = next(
                (
                    item
                    for item in SUPPORTED_VARAC_WRITERS
                    if item.version == version
                    and item.platform == platform_key
                    and item.operation == "update-member"
                    and item.layout_fingerprint == source.layout_fingerprint
                ),
                None,
            )
            if capability is None:
                raise VarACNativeConfigurationError(
                    "The managed cluster VarAC writer version or INI layout is not qualified."
                )
            if process_running or node_id in running_ids:
                results.append(
                    {
                        "node_id": node_id,
                        "state": "deferred",
                        "detail": "This VarAC or VARA cluster member is running.",
                    }
                )
                continue

            backup = create_config_backup(
                (ini_path,),
                reason="varac-cluster-launch-policy",
                backup_root=Path(backup_root),
            )
            if len(backup.items) != 1 or backup.items[0].status != "backed_up":
                raise OSError("Managed VarAC launch-policy backup failed.")
            payload = render_varac_ini(
                source,
                {"VARAHF_CONFIG": {"VarahfLaunchOnModemConnect": "ON"}},
                capability=capability,
            )
            descriptor, stage_name = tempfile.mkstemp(
                prefix=f".{ini_path.name}.fio-",
                suffix=".stage",
                dir=str(ini_path.parent),
            )
            stage = Path(stage_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(stage, ini_path.stat().st_mode & 0o777)
                staged = parse_varac_ini_bytes(stage, stage.read_bytes())
                if _ini_value(
                    staged.values,
                    "VARAHF_CONFIG",
                    "VarahfLaunchOnModemConnect",
                ).casefold() != "on":
                    raise VarACNativeConfigurationError(
                        "Managed VarAC launch-policy staged readback failed."
                    )
                os.replace(stage, ini_path)
                observed = parse_varac_ini_bytes(ini_path, ini_path.read_bytes())
                if _ini_value(
                    observed.values,
                    "VARAHF_CONFIG",
                    "VarahfLaunchOnModemConnect",
                ).casefold() != "on":
                    raise VarACNativeConfigurationError(
                        "Managed VarAC launch-policy readback failed."
                    )
                store.save_varac_node(
                    {
                        **dict(node),
                        "native_verification_summary": (
                            "Managed cluster VarAC launches its node-local VARA modem; native INI readback verified."
                        ),
                    }
                )
            except Exception:
                restore = restore_config_backup(backup)
                if not restore.ok:
                    raise VarACNativeConfigurationError(
                        "Managed VarAC launch-policy repair failed and backup restoration needs attention."
                    )
                raise
            finally:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass
            results.append(
                {
                    "node_id": node_id,
                    "state": "launch-policy-repaired",
                    "detail": "VarAC modem launch enabled and verified.",
                }
            )
        except Exception as exc:
            results.append(
                {
                    "node_id": node_id,
                    "state": "needs-attention",
                    "detail": str(exc),
                }
            )
    return tuple(results)


def repair_managed_varac_wine_runtime_paths(
    store: MultiRadioStore,
    *,
    backup_root: Path,
    process_running: bool = False,
    platform_override: str = "",
) -> tuple[Mapping[str, Any], ...]:
    """Repair all exact, qualified legacy managed paths; never guess."""

    platform_key = str(platform_override or _platform_key()).strip().casefold()
    if platform_key != "linux-wine":
        return ()
    profiles = store.list_device_profiles()
    memberships = store.list_varac_cluster_members()
    clusters = {int(row.get("id") or 0): row for row in store.list_varac_clusters()}
    results: list[Mapping[str, Any]] = []
    for node in store.list_varac_nodes():
        linked = [row for row in profiles if int(row.get("varac_node_id") or 0) == int(node.get("id") or 0)]
        if len(linked) != 1:
            continue
        profile = linked[0]
        membership = next(
            (row for row in memberships if int(row.get("device_profile_id") or 0) == int(profile.get("id") or 0) and int(row.get("enabled", 1) or 0) == 1),
            None,
        )
        cluster = clusters.get(int((membership or {}).get("cluster_id") or 0))
        repair = prepare_managed_varac_runtime_repair(
            node, profile, membership=membership, cluster=cluster,
            platform_override=platform_key,
        )
        if repair.state in {"current", "not-applicable"}:
            continue
        if repair.state == "needs-attention":
            results.append({"node_id": repair.node_id, "state": repair.state, "detail": repair.why})
            continue
        if process_running:
            results.append({"node_id": repair.node_id, "state": "deferred", "detail": "VarAC or VARA is running."})
            continue
        if repair.state == "reconcile":
            with store.guided_save_transaction() as transaction:
                persisted = _persist_runtime_projection(store, repair, observed={})
                transaction.complete()
            results.append({"node_id": repair.node_id, "state": "reconciled", "persisted": persisted})
            continue
        if repair.plan is None:
            continue
        outcome = apply_varac_native_transaction(
            store=store,
            plan=repair.plan,
            backup_root=backup_root,
            persist=lambda active_store, observed, prepared=repair: _persist_runtime_projection(
                active_store, prepared, observed=observed
            ),
            running_checker=(lambda _member: process_running),
        )
        results.append(
            {
                "node_id": repair.node_id,
                "state": "repaired" if outcome.ok else "recovery-required" if outcome.needs_recovery else "failed",
                "detail": outcome.error,
            }
        )
    return tuple(results)


__all__ = [
    "managed_varac_process_attribution",
    "repair_managed_varac_cluster_launch_policy",
    "repair_managed_varac_wine_runtime_paths",
    "running_managed_varac_node_ids",
]
