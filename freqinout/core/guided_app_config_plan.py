from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence, Tuple

from freqinout.core.config_autodiscovery import APP_DISPLAY_NAMES, RadioInstanceProposal, discover_varac_local_assets
from freqinout.core.config_backup import (
    ConfigBackupResult,
    ConfigRestoreResult,
    create_config_backup,
    restore_config_backup,
)
from freqinout.core.config_js8_managed import (
    JS8CallManagedProfilePlan,
    apply_js8call_multisettings_plan,
    build_js8call_managed_profile_plans,
    verify_js8call_multisettings_plan,
)
from freqinout.core.config_managed_profiles import build_flrig_fldigi_managed_profile_plans
from freqinout.core.managed_directory_contract import (
    managed_directories_from_recipe,
    materialize_managed_directories,
)
from freqinout.core.guided_radio_software_model import (
    NativeWriterCapability,
    NativeWriterOperation,
    NativeWriterRegistry,
    SoftwareFamily,
)


def _native_platform() -> str:
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


GUIDED_NATIVE_WRITER_REGISTRY = NativeWriterRegistry(
    tuple(
        NativeWriterCapability(
            writer_key=f"js8-{variant}-{version}-{platform}-{operation.value}",
            family=SoftwareFamily.JS8CALL,
            variant=variant,
            version=version,
            platform=platform,
            operation=operation,
            preview_supported=True,
            backup_supported=True,
            readback_supported=True,
            restore_supported=True,
        )
        for variant, version in (
            ("js8call_2_2", "2.2.0"),
            ("js8call_improved_3_0_3", "3.0.3"),
            ("js8call_subspace_4_1", "4.1.0.478"),
        )
        for platform in ("linux", "macos", "windows")
        for operation in (NativeWriterOperation.CREATE, NativeWriterOperation.UPDATE)
    )
)


@dataclass(frozen=True)
class GuidedAppConfigAction:
    action_id: str
    app_id: str
    instance_name: str
    action_type: str
    target: str
    summary: str
    requires_backup: bool
    writes_external_config: bool
    manual_review_required: bool = False
    details: Mapping[str, str] = field(default_factory=dict)
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def display_name(self) -> str:
        return APP_DISPLAY_NAMES.get(self.app_id, self.app_id)


@dataclass(frozen=True)
class GuidedAppConfigPlan:
    actions: Tuple[GuidedAppConfigAction, ...]
    review_items: Tuple[str, ...]
    blocked: bool = False

    @property
    def backup_required(self) -> bool:
        return any(action.requires_backup for action in self.actions)

    @property
    def manual_review_required(self) -> bool:
        return any(action.manual_review_required for action in self.actions) or bool(self.review_items)


@dataclass(frozen=True)
class GuidedAppConfigApplyItem:
    action_id: str
    app_id: str
    action_type: str
    target: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class GuidedAppConfigApplyResult:
    items: Tuple[GuidedAppConfigApplyItem, ...]
    backup: ConfigBackupResult | None = None
    restore: ConfigRestoreResult | None = None

    @property
    def ok(self) -> bool:
        return not any(item.status in {"failed", "rolled_back"} for item in self.items)

    @property
    def external_writes_applied(self) -> bool:
        return any(item.status == "applied" and item.action_type != "create_directory" for item in self.items)


def with_canonical_managed_directory_actions(
    plan: GuidedAppConfigPlan,
    drafts: Mapping[str, Any],
) -> GuidedAppConfigPlan:
    """Replace derived mkdir actions with the exact persisted recipe targets.

    This is the shared Add Radio / Software Administration seam.  It is
    intentionally limited to managed JS8Call and Fast Light recipes; VarAC's
    qualified native transaction owns its directory preparation, while
    operator/adopted integrations never authorize generic directory creation.
    """

    canonical_actions: list[GuidedAppConfigAction] = []
    replaced_app_ids: set[str] = set()
    seen_targets: set[str] = set()
    for raw_family, raw_draft in drafts.items():
        if not isinstance(raw_draft, Mapping):
            continue
        family = str(raw_family or raw_draft.get("family_key") or "").strip().lower()
        if family not in {"js8call", "fast_light"}:
            continue
        mode = str(raw_draft.get("mode") or "").strip().lower()
        ownership = str(raw_draft.get("ownership") or "").strip().lower()
        if mode != "managed" or ownership != "fio-managed":
            continue
        recipe = raw_draft.get("launch_recipe")
        if not isinstance(recipe, Mapping):
            continue
        components = tuple(
            component
            for component in recipe.get("components", ()) or ()
            if isinstance(component, Mapping)
        )
        replaced_app_ids.update(
            str(component.get("component_key") or "").strip().lower()
            for component in components
            if str(component.get("component_key") or "").strip()
        )
        instance_name = str(
            raw_draft.get("instance_name")
            or raw_draft.get("owner_label")
            or raw_draft.get("draft_instance_key")
            or family
        ).strip()
        for component_key, target in managed_directories_from_recipe(recipe):
            normalized = str(Path(target).expanduser())
            if normalized in seen_targets:
                continue
            seen_targets.add(normalized)
            canonical_actions.append(
                GuidedAppConfigAction(
                    action_id=f"{family}:{component_key}:canonical-dir:{len(canonical_actions) + 1}",
                    app_id=component_key,
                    instance_name=instance_name,
                    action_type="create_directory",
                    target=target,
                    summary=(
                        f"Create the reviewed CommsPlan-managed {APP_DISPLAY_NAMES.get(component_key, component_key)} "
                        f"folder for {instance_name}."
                    ),
                    requires_backup=False,
                    writes_external_config=False,
                    details={
                        "source": "canonical_launch_recipe",
                        "ownership": "fio-managed",
                    },
                )
            )

    if not replaced_app_ids:
        return plan
    retained = tuple(
        action
        for action in plan.actions
        if not (
            str(action.action_type or "").strip() == "create_directory"
            and str(action.app_id or "").strip().lower() in replaced_app_ids
        )
    )
    return GuidedAppConfigPlan(
        actions=(*retained, *canonical_actions),
        review_items=plan.review_items,
        blocked=plan.blocked,
    )


def rollback_guided_external_app_config_apply(
    result: GuidedAppConfigApplyResult,
) -> GuidedAppConfigApplyResult:
    """Restore the exact native-file backup retained by a reviewed apply.

    This is also used when a later FIO persistence stage fails after native
    readback succeeded. Directory preparation is intentionally not inferred or
    removed; only the exact qualified native-writer targets are restored.
    """

    if result.backup is None:
        return result
    restore_result = restore_config_backup(result.backup)
    restored = bool(restore_result.ok)
    items = tuple(
        GuidedAppConfigApplyItem(
            action_id=item.action_id,
            app_id=item.app_id,
            action_type=item.action_type,
            target=item.target,
            status=("rolled_back" if item.status in {"applied", "failed"} and restored else item.status),
            detail=(
                f"{item.detail} Native configuration backup was restored."
                if restored and item.status in {"applied", "failed"}
                else item.detail
            ),
        )
        for item in result.items
    )
    return GuidedAppConfigApplyResult(items=items, backup=result.backup, restore=restore_result)


def qualified_native_writer_for_action(
    action: GuidedAppConfigAction,
    *,
    writer_registry: NativeWriterRegistry = GUIDED_NATIVE_WRITER_REGISTRY,
) -> NativeWriterCapability | None:
    """Return the exact writer that may apply this reviewed action, if any."""

    return _qualified_writer(action, writer_registry)


def build_guided_external_app_config_plan(
    proposals: Sequence[RadioInstanceProposal],
    *,
    config_root: Path,
    app_paths: Mapping[str, str] | None = None,
    callsign: str = "",
    grid: str = "",
    include_varac: bool = False,
    allow_external_writes: bool = True,
    js8_control_route: str = "flrig",
    radio_label: str = "",
) -> GuidedAppConfigPlan:
    """Return a reviewable external-app setup plan without writing files."""

    paths = dict(app_paths or {})
    actions: list[GuidedAppConfigAction] = []
    review_items: list[str] = []
    seen_dirs: set[str] = set()

    selected_varac = include_varac or any(proposal.varac_enabled for proposal in proposals)
    if selected_varac:
        _add_varac_integration_action(actions, proposals, paths)

    _add_commstat_shared_binding_action(actions, proposals, paths)

    if not allow_external_writes:
        selected_apps = sorted({app for proposal in proposals for app in proposal.enabled_apps})
        app_text = ", ".join(APP_DISPLAY_NAMES.get(app, app) for app in selected_apps) if selected_apps else "selected apps"
        read_only_review = [f"Read-only setup will remember {app_text} references in CommsPlan without changing external app configuration."]
        if selected_varac:
            read_only_review.extend(_varac_review_items())
        return GuidedAppConfigPlan(
            actions=tuple(actions),
            review_items=tuple(read_only_review),
        )

    for proposal in proposals:
        fast_plans = build_flrig_fldigi_managed_profile_plans(
            proposal,
            config_root=config_root,
            app_paths=paths,
        )
        for plan in fast_plans:
            # These legacy plan objects retain native-writer review details,
            # but their historical config_root paths are not canonical launch
            # paths.  Add Radio and Software Administration append mkdir
            # actions from the already-reviewed launch recipe through
            # ``with_canonical_managed_directory_actions``.
            actions.append(
                GuidedAppConfigAction(
                    action_id=f"{plan.instance_name}:{plan.app_id}:write-managed-config",
                    app_id=plan.app_id,
                    instance_name=plan.instance_name,
                    action_type="write_managed_config",
                    target=str(plan.config_dir),
                    summary=(
                        f"Prepare {plan.display_name} profile {plan.instance_name} "
                        f"on {plan.expected_host}:{plan.expected_port}."
                    ),
                    requires_backup=True,
                    writes_external_config=True,
                    details={
                        "executable_path": plan.executable_path,
                        "config_dir": str(plan.config_dir),
                        "launch_args": " ".join(plan.launch_args),
                        "expected_host": plan.expected_host,
                        "expected_port": str(plan.expected_port),
                    },
                    notes=tuple(plan.notes),
                )
            )

    js8_plans = build_js8call_managed_profile_plans(
        proposals,
        config_root=config_root,
        js8call_path=paths.get("js8call", ""),
        callsign=callsign,
        grid=grid,
        control_route=js8_control_route,
        radio_label=radio_label,
        platform=str(paths.get("js8_writer_platform", "") or "") or None,
        storage_home=(
            Path(str(paths.get("js8_storage_home", "") or "")).expanduser()
            if str(paths.get("js8_storage_home", "") or "").strip()
            else None
        ),
    )
    for plan in js8_plans:
        for directory in (plan.config_dir, plan.save_dir, plan.forms_dir):
            _add_directory_action(
                actions,
                seen_dirs,
                app_id="js8call",
                instance_name=plan.instance_name,
                directory=directory,
            )
        if plan.control_route == "flrig":
            summary = (
                f"Prepare JS8Call profile {plan.profile_name} with FLRig "
                f"{plan.flrig_host}:{plan.flrig_port} and API port {plan.tcp_port}."
            )
            route_notes = ("Some JS8Call builds may still require operator confirmation in JS8Call after profile creation.",)
        else:
            summary = f"Prepare JS8Call profile {plan.profile_name} with API port {plan.tcp_port}; {plan.rig_summary}"
            route_notes = ("Confirm JS8Call's radio/CAT selection in JS8Call before relying on frequency control.",)
        actions.append(
            GuidedAppConfigAction(
                action_id=f"{plan.instance_name}:js8call:update-multisettings",
                app_id="js8call",
                instance_name=plan.instance_name,
                action_type="update_js8_multisettings",
                target=str(plan.settings_path),
                summary=summary,
                requires_backup=True,
                writes_external_config=True,
                details={
                    "executable_path": plan.executable_path,
                    "profile_name": plan.profile_name,
                    "config_dir": str(plan.config_dir),
                    "settings_path": str(plan.settings_path),
                    "save_dir": str(plan.save_dir),
                    "forms_dir": str(plan.forms_dir),
                    "directed_path": str(plan.directed_path),
                    "all_path": str(plan.all_path),
                    "inbox_path": str(plan.inbox_path),
                    "application_data_root": str(plan.application_data_root),
                    "rig_name": plan.rig_name,
                    "application_name": plan.application_name,
                    "control_route": plan.control_route,
                    "rig_summary": plan.rig_summary,
                    "settings": dict(plan.settings),
                    "flrig_port": str(plan.flrig_port),
                    "tcp_port": str(plan.tcp_port),
                    "udp_port": str(plan.udp_port),
                    # A distinct --rig-name owns a distinct native settings
                    # file.  The detected/default JS8Call.ini is evidence, not
                    # the write target for this new identity.
                    "js8call_ini_path": str(plan.settings_path),
                    "source_js8call_ini_path": str(paths.get("js8call_ini_path", "") or ""),
                    "writer_family": "js8call",
                    "writer_variant": str(paths.get("js8_variant_family", "") or ""),
                    "writer_version": str(paths.get("js8_variant_version", "") or ""),
                    "writer_platform": str(paths.get("js8_writer_platform", "") or _native_platform()),
                    "writer_operation": str(paths.get("js8_writer_operation", "") or "create"),
                },
                notes=(
                    "JS8Call native settings writes require a recoverable snapshot of the exact rig-specific target before apply.",
                    *route_notes,
                ),
            )
        )

    if selected_varac:
        review_items.extend(_varac_review_items())

    return GuidedAppConfigPlan(actions=tuple(actions), review_items=tuple(review_items))


def apply_guided_external_app_config_plan(
    plan: GuidedAppConfigPlan,
    *,
    allow_external_writes: bool = False,
    backup_root: Path | None = None,
    backup_reason: str = "guided-app-config",
    writer_registry: NativeWriterRegistry = GUIDED_NATIVE_WRITER_REGISTRY,
) -> GuidedAppConfigApplyResult:
    """Apply the safe portions of a guided app configuration plan.

    Directory preparation is safe and idempotent. External app writes remain
    opt-in and only run when the action has an explicit, supported target.
    """

    items: list[GuidedAppConfigApplyItem] = []
    write_targets = (
        _guided_plan_external_write_targets(plan, writer_registry=writer_registry)
        if allow_external_writes
        else ()
    )
    backup_result = (
        create_config_backup(write_targets, reason=backup_reason, backup_root=backup_root)
        if write_targets
        else None
    )
    backup_failed = bool(
        backup_result is not None and any(item.status == "failed" for item in backup_result.items)
    )
    js8_plans_by_action = _js8_multisettings_plans_by_action(plan)
    external_write_attempted = False
    for action in plan.actions:
        action_type = str(action.action_type or "").strip()
        target = str(action.target or "").strip()
        if action_type == "create_directory":
            try:
                if not target:
                    raise ValueError("Managed directory target is blank.")
                materialize_managed_directories((target,))
                items.append(
                    GuidedAppConfigApplyItem(
                        action_id=action.action_id,
                        app_id=action.app_id,
                        action_type=action_type,
                        target=target,
                        status="applied",
                        detail="Directory ready.",
                    )
                )
            except (OSError, ValueError) as exc:
                items.append(
                    GuidedAppConfigApplyItem(
                        action_id=action.action_id,
                        app_id=action.app_id,
                        action_type=action_type,
                        target=target,
                        status="failed",
                        detail=str(exc),
                    )
                )
            continue
        if not action.writes_external_config:
            items.append(
                GuidedAppConfigApplyItem(
                    action_id=action.action_id,
                    app_id=action.app_id,
                    action_type=action_type,
                    target=target,
                    status="remembered",
                    detail="CommsPlan-side integration reference only.",
                )
            )
            continue
        if not allow_external_writes:
            items.append(
                GuidedAppConfigApplyItem(
                    action_id=action.action_id,
                    app_id=action.app_id,
                    action_type=action_type,
                    target=target,
                    status="skipped",
                    detail="External app writes were not enabled.",
                )
            )
            continue
        if _qualified_writer(action, writer_registry) is None:
            items.append(
                GuidedAppConfigApplyItem(
                    action_id=action.action_id,
                    app_id=action.app_id,
                    action_type=action_type,
                    target=target,
                    status="operator_action_required",
                    detail=(
                        "No exact supported native writer matches this application variant, version, platform, and operation. "
                        "CommsPlan configuration was not written; complete the native profile in the application and verify it in Health."
                    ),
                )
            )
            continue
        if backup_failed:
            items.append(
                GuidedAppConfigApplyItem(
                    action_id=action.action_id,
                    app_id=action.app_id,
                    action_type=action_type,
                    target=target,
                    status="failed",
                    detail="Backup failed; external app write was not attempted.",
                )
            )
            continue
        if action_type == "update_js8_multisettings":
            ini_path = str(action.details.get("js8call_ini_path", "") or "").strip()
            js8_plan = js8_plans_by_action.get(action.action_id)
            if ini_path and js8_plan is not None:
                try:
                    external_write_attempted = True
                    applied_path = apply_js8call_multisettings_plan(js8_plan, ini_path=Path(ini_path))
                    if not verify_js8call_multisettings_plan(js8_plan, ini_path=Path(applied_path)):
                        raise OSError("JS8Call MultiSettings readback did not match the reviewed write plan.")
                    items.append(
                        GuidedAppConfigApplyItem(
                            action_id=action.action_id,
                            app_id=action.app_id,
                            action_type=action_type,
                            target=str(applied_path),
                            status="applied",
                            detail="JS8Call MultiSettings profile updated.",
                        )
                    )
                except OSError as exc:
                    items.append(
                        GuidedAppConfigApplyItem(
                            action_id=action.action_id,
                            app_id=action.app_id,
                            action_type=action_type,
                            target=ini_path,
                            status="failed",
                            detail=str(exc),
                        )
                    )
                continue
        items.append(
            GuidedAppConfigApplyItem(
                action_id=action.action_id,
                app_id=action.app_id,
                action_type=action_type,
                target=target,
                status="skipped",
                detail="No supported explicit external writer is available for this action yet.",
            )
        )
    restore_result: ConfigRestoreResult | None = None
    if (
        external_write_attempted
        and backup_result is not None
        and any(item.status == "failed" for item in items)
    ):
        rolled_back = rollback_guided_external_app_config_apply(
            GuidedAppConfigApplyResult(items=tuple(items), backup=backup_result)
        )
        items = list(rolled_back.items)
        restore_result = rolled_back.restore
    return GuidedAppConfigApplyResult(items=tuple(items), backup=backup_result, restore=restore_result)


def _guided_plan_external_write_targets(
    plan: GuidedAppConfigPlan,
    *,
    writer_registry: NativeWriterRegistry,
) -> Tuple[Path, ...]:
    targets = []
    for action in plan.actions:
        if not action.writes_external_config:
            continue
        if _qualified_writer(action, writer_registry) is None:
            continue
        if str(action.action_type or "").strip() == "update_js8_multisettings":
            ini_path = str(action.details.get("js8call_ini_path", "") or "").strip()
            if ini_path:
                targets.append(Path(ini_path).expanduser())
    return tuple(targets)


def _qualified_writer(
    action: GuidedAppConfigAction,
    registry: NativeWriterRegistry,
) -> NativeWriterCapability | None:
    details = action.details
    family = str(details.get("writer_family") or action.app_id or "").strip().lower()
    variant = str(details.get("writer_variant") or "").strip().lower()
    version = str(details.get("writer_version") or "").strip().lower()
    platform = str(details.get("writer_platform") or "").strip().lower()
    operation = str(details.get("writer_operation") or "").strip().lower()
    capability = registry.lookup(
        family=family,
        variant=variant,
        version=version,
        platform=platform,
        operation=operation,
    )
    if capability is None:
        return None
    if str(action.action_type or "").strip() == "update_js8_multisettings":
        ini_path = str(details.get("js8call_ini_path") or "").strip()
        if not ini_path or Path(ini_path).expanduser().suffix.casefold() != ".ini":
            return None
    return capability


def _js8_multisettings_plans_by_action(plan: GuidedAppConfigPlan) -> Mapping[str, JS8CallManagedProfilePlan]:
    out: dict[str, JS8CallManagedProfilePlan] = {}
    for action in plan.actions:
        if str(action.action_type or "").strip() != "update_js8_multisettings":
            continue
        details = action.details
        profile_name = str(details.get("profile_name", "") or action.instance_name or "").strip()
        if not profile_name:
            continue
        reviewed_settings = details.get("settings")
        settings = (
            {str(key): str(value) for key, value in reviewed_settings.items()}
            if isinstance(reviewed_settings, Mapping)
            else {
                "TCPEnabled": "true",
                "AcceptTCPRequests": "true",
                "TCPServer": "127.0.0.1",
                "TCPServerPort": str(details.get("tcp_port", "") or ""),
                "TCPMaxConnections": "2",
                "UDPEnabled": "true",
                "UDPServerPort": str(details.get("udp_port", "") or ""),
                "SaveDir": str(details.get("save_dir", "") or ""),
            }
        )
        control_route = str(details.get("control_route", "") or "flrig").strip().lower()
        if control_route == "flrig":
            settings["Rig"] = "FLRig FLRig"
            settings["CATNetworkPort"] = f"127.0.0.1:{details.get('flrig_port', '')}"
        out[action.action_id] = JS8CallManagedProfilePlan(
            profile_name=profile_name,
            instance_name=str(action.instance_name or profile_name),
            executable_path=str(details.get("executable_path", "") or ""),
            config_dir=Path(details.get("config_dir", "") or "."),
            settings_path=Path(details.get("settings_path", "") or details.get("js8call_ini_path", "") or "."),
            save_dir=Path(details.get("save_dir", "") or "."),
            forms_dir=Path(details.get("forms_dir", "") or "."),
            directed_path=Path(details.get("directed_path", "") or "."),
            all_path=Path(details.get("all_path", "") or "."),
            inbox_path=Path(details.get("inbox_path", "") or "."),
            application_data_root=Path(details.get("application_data_root", "") or "."),
            rig_name=str(details.get("rig_name", "") or ""),
            application_name=str(details.get("application_name", "") or "JS8Call"),
            flrig_host="127.0.0.1",
            flrig_port=_int_text(details.get("flrig_port"), 0),
            tcp_host="127.0.0.1",
            tcp_port=_int_text(details.get("tcp_port"), 0),
            udp_port=_int_text(details.get("udp_port"), 0),
            control_route=control_route,
            rig_summary=str(details.get("rig_summary", "") or ""),
            settings={key: value for key, value in settings.items() if str(value or "").strip()},
        )
    return out


def _int_text(value: object, default: int) -> int:
    try:
        return int(str(value if value is not None else "").strip() or default)
    except (TypeError, ValueError):
        return default


def _varac_review_items() -> Tuple[str, str]:
    return (
        "VarAC guided setup is read/import only: CommsPlan remembers paths and monitors VarAC data without rewriting VarAC.ini or VarAC DB.",
        "Use the dedicated VarAC BBS settings workflow for explicit [BBS] section sync; cluster membership remains read-only in this release.",
    )


def _add_commstat_shared_binding_action(
    actions: list[GuidedAppConfigAction],
    proposals: Sequence[RadioInstanceProposal],
    paths: Mapping[str, str],
) -> None:
    """Add one station-shared CommStat process with radio-owned JS8 routes.

    CommStat is not cloned per radio.  Each selected radio contributes a
    binding to its distinct JS8Call endpoint while the launch identity stays
    station-scoped and therefore de-duplicates in the launch planner.
    """

    bindings = tuple(
        proposal
        for proposal in proposals
        if {"js8call", "commstat"}.issubset(
            {str(app or "").strip().lower() for app in proposal.enabled_apps}
        )
    )
    if not bindings:
        return
    radio_bindings = ", ".join(
        f"{proposal.instance_name}:{proposal.name}" for proposal in bindings
    )
    target = str(
        paths.get("commstat_launch_path")
        or paths.get("commstat")
        or "CommsPlan station-shared CommStat service"
    )
    actions.append(
        GuidedAppConfigAction(
            action_id="commstat:station-shared-bindings",
            app_id="commstat",
            instance_name="Station CommStat",
            action_type="remember_shared_service_bindings",
            target=target,
            summary=(
                "Use one station-shared CommStat process and bind it to each "
                "selected radio's distinct JS8Call endpoint."
            ),
            requires_backup=False,
            writes_external_config=False,
            details={
                "execution_scope": "station_shared_utility",
                "instance_key": "commstat:station-shared",
                "radio_bindings": radio_bindings,
                "radio_keys": ", ".join(proposal.instance_name for proposal in bindings),
            },
            notes=(
                "CommsPlan launches or monitors one CommStat process for the station.",
                "Each radio keeps its own JS8Call API/profile binding; CommStat is not duplicated per radio.",
            ),
        )
    )


def _add_varac_integration_action(
    actions: list[GuidedAppConfigAction],
    proposals: Sequence[RadioInstanceProposal],
    paths: Mapping[str, str],
) -> None:
    instance_names = ", ".join(proposal.instance_name for proposal in proposals if proposal.varac_enabled) or "selected radio"
    details = {
        "install_path": str(paths.get("varac_install_path") or paths.get("varac") or ""),
        "ini_path": str(paths.get("varac_ini_path") or ""),
        "db_path": str(paths.get("varac_db_path") or ""),
        "incoming_dir": str(paths.get("varac_incoming_dir") or paths.get("varac_inbox_dir") or ""),
        "outgoing_dir": str(paths.get("varac_outgoing_dir") or paths.get("varac_outbox_dir") or ""),
        "bbs_dir": str(paths.get("varac_bbs_dir") or ""),
        "bbs_archive_dir": str(paths.get("varac_bbs_archive_dir") or ""),
        "launch_cmd": str(paths.get("varac_launch_cmd") or paths.get("launch_cmd") or ""),
    }
    varac_assets = discover_varac_local_assets(app_paths=paths)
    for asset in varac_assets:
        if asset.asset_id in {
            "traffic_log",
            "app_log",
            "qso_log",
            "callsign_tags",
            "alert_tags",
            "templates",
            "bbs_archive",
        } and asset.path:
            details[f"{asset.asset_id}_path"] = asset.path
        if asset.asset_id == "ini" and asset.detail:
            details["ini_detail"] = asset.detail
        if asset.asset_id == "db" and asset.detail:
            details["db_detail"] = asset.detail
    found_labels = tuple(asset.label for asset in varac_assets if asset.exists)
    if found_labels:
        details["readable_assets"] = ", ".join(found_labels)
    target = details["install_path"] or details["ini_path"] or details["db_path"] or "CommsPlan VarAC integration settings"
    actions.append(
        GuidedAppConfigAction(
            action_id="varac:remember-integration",
            app_id="varac",
            instance_name=instance_names,
            action_type="remember_integration",
            target=target,
            summary="Remember VarAC paths and enable read/import integration in CommsPlan without changing VarAC configuration.",
            requires_backup=False,
            writes_external_config=False,
            manual_review_required=True,
            details=details,
            notes=(
                "CommsPlan may read VarAC.ini, the VarAC database, logs, and message/BBS folders for discovery and ingest.",
                "Guided setup does not write VarAC.ini or the VarAC database.",
                "VarAC owns scheduler/frequency control for VarAC-only radios.",
            ),
        )
    )


def _add_directory_action(
    actions: list[GuidedAppConfigAction],
    seen_dirs: set[str],
    *,
    app_id: str,
    instance_name: str,
    directory: Path,
) -> None:
    key = str(Path(directory))
    if key in seen_dirs:
        return
    seen_dirs.add(key)
    actions.append(
        GuidedAppConfigAction(
            action_id=f"{instance_name}:{app_id}:create-dir:{len(seen_dirs)}",
            app_id=app_id,
            instance_name=instance_name,
            action_type="create_directory",
            target=key,
            summary=f"Create CommsPlan-managed {APP_DISPLAY_NAMES.get(app_id, app_id)} folder for {instance_name}.",
            requires_backup=False,
            writes_external_config=False,
        )
    )
