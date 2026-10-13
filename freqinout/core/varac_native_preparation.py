"""Bounded discovery and immutable preparation for native VarAC clusters.

The GUI supplies reviewed intent and durable FIO snapshots.  This module may
read only the explicitly selected VarAC/VARA sources and produces either one
immutable writer plan or a presentation explaining why native apply is not
safe.  It never writes native files or FIO state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from freqinout.core.config_varac_managed import (
    DEFAULT_SUPPORTED_VARAC_VERSIONS,
    VarACIniSource,
    VarACMemberInput,
    VarACNativeClusterPlan,
    VarACNativeClusterRequest,
    VarACNativeConfigurationError,
    VarARuntimeInput,
    build_varac_native_cluster_plan,
    parse_vara_ini_bytes,
    parse_varac_ini_bytes,
    snapshot_target_state,
    snapshot_vara_runtime_files,
)
from freqinout.core.varac_bbs_config import varac_path_to_host_path


@dataclass(frozen=True)
class VarACNativePreparationResult:
    state: str
    presentation: Mapping[str, Any]
    draft_fingerprint: str
    generation: int
    plan: VarACNativeClusterPlan | None = None
    error: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "ready" and self.plan is not None


@dataclass(frozen=True)
class VarACManagedRuntimeRepair:
    """One unambiguous legacy managed-runtime correction."""

    state: str
    node_id: int
    radio_profile_id: int
    target_runtime: Path | None = None
    target_ini: Path | None = None
    plan: VarACNativeClusterPlan | None = None
    why: str = ""


def native_draft_fingerprint(draft: Mapping[str, Any]) -> str:
    """Fingerprint operator intent while excluding host-generated native facts.

    Managed create/join preparation owns these targets.  Publishing them into
    the canonical assistant draft must not make the just-prepared plan stale;
    changing source/topology/policy evidence still changes the fingerprint.
    """

    generated_fields = {
        "configuration_path",
        "storage_path",
        "secondary_storage_path",
        "outbox_path",
        "bbs_path",
        "bbs_archive_path",
        "cluster_bbs_path",
        "cluster_bbs_archive_path",
        "working_directory",
        "launch_command",
        "vara_runtime_path",
        "vara_ini_path",
        "port",
        "secondary_port",
        "udp_port",
    }
    clean = {
        str(key): value
        for key, value in dict(draft or {}).items()
        if str(key)
        not in {
            "varac_native_presentation",
            "varac_native_generation",
            "varac_native_plan_fingerprint",
            "_varac_native_apply_request",
            *generated_fields,
        }
    }
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_managed_varac_runtime_repair(
    node: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    membership: Mapping[str, Any] | None = None,
    cluster: Mapping[str, Any] | None = None,
    platform_override: str = "",
) -> VarACManagedRuntimeRepair:
    """Prepare a bounded repair for the obsolete Linux host-root VARA layout.

    A native path already pointing at a verified Wine runtime needs only an
    FIO projection reconciliation. A blank or Z: path from the old writer is
    repaired through the qualified update-member writer with backup/readback.
    """

    node_id = int(node.get("id") or 0)
    radio_id = int(profile.get("id") or 0)
    platform_key = _platform_key(platform_override)
    if platform_key != "linux-wine":
        return VarACManagedRuntimeRepair("not-applicable", node_id, radio_id)
    if str(node.get("native_management_state") or "operator").strip().casefold() != "managed":
        return VarACManagedRuntimeRepair("not-applicable", node_id, radio_id)
    writer_key = str(node.get("native_writer_key") or "")
    version = next((value for value in DEFAULT_SUPPORTED_VARAC_VERSIONS if value in writer_key), "")
    if not version:
        return VarACManagedRuntimeRepair(
            "needs-attention", node_id, radio_id,
            why="The saved managed VarAC writer version is not qualified.",
        )
    try:
        source_ini_path = _required_file(node.get("ini_path"), "existing VarAC.ini")
        source_ini = parse_varac_ini_bytes(source_ini_path, source_ini_path.read_bytes())
        drive_root = _wine_drive_root(source_ini_path)
        if drive_root is None:
            raise ValueError("The saved VarAC.ini is not inside a verified Wine drive_<letter> folder.")
        wine_prefix = _wine_prefix(source_ini_path)
        configured = _value(_section(source_ini.values, "VARAHF_CONFIG"), "VarahfMainPath")
        configured_host = (
            Path(varac_path_to_host_path(configured, ini_path=source_ini_path)).expanduser()
            if configured else None
        )
        if (
            configured_host is not None
            and _wine_drive_root(configured_host) == drive_root
            and configured_host.name.casefold() == "vara.exe"
            and configured_host.is_file()
            and configured_host.with_name("VARA.ini").is_file()
        ):
            target_runtime = configured_host.parent
            stored_runtime = Path(str(node.get("vara_runtime_path") or "")).expanduser()
            if stored_runtime.absolute() == target_runtime.absolute():
                return VarACManagedRuntimeRepair("current", node_id, radio_id)
            return VarACManagedRuntimeRepair(
                "reconcile", node_id, radio_id,
                target_runtime=target_runtime,
                target_ini=target_runtime / "VARA.ini",
                why="The native INI is correct; CommsPlan's older saved runtime projection is stale.",
            )

        source_runtime = Path(str(node.get("vara_runtime_path") or "")).expanduser()
        if not source_runtime.is_dir() or source_runtime.is_symlink():
            raise ValueError("The legacy managed VARA runtime is unavailable or unsafe to copy.")
        source_vara_ini = _required_file(source_runtime / "VARA.ini", "source VARA.ini")
        source_vara = parse_vara_ini_bytes(source_vara_ini, source_vara_ini.read_bytes())
        source_files = snapshot_vara_runtime_files(source_runtime)
        executable = _resolve_varac_executable(node.get("install_path"))
        member_number = _positive_int((membership or {}).get("instance_number")) or 1
        source_ports = _source_vara_ports(source_ini, source_vara)
        target_base = drive_root / f"VARA-{_slug(str(profile.get('name') or node.get('name') or node_id))}"
        target_runtime = _next_available_managed_runtime(target_base, reserved=())
        member = _member(
            member_id=f"node:{node_id}", source_ini=source_ini,
            target_ini=source_ini_path, member_number=member_number,
            executable=executable, source_vara_root=source_runtime,
            source_vara=source_vara, source_files=source_files,
            target_vara_root=target_runtime, ports=source_ports,
            platform_key=platform_key,
        )
        shared_db = str((cluster or {}).get("shared_db_path") or node.get("db_path") or "").strip()
        if not shared_db:
            raise ValueError("The managed VarAC database path is unavailable.")
        shared_bbs = str(
            (cluster or {}).get("shared_bbs_path")
            or profile.get("varac_bbs_dir")
            or executable.parent / "BBS"
        )
        shared_archive = str(
            (cluster or {}).get("shared_bbs_archive_path")
            or profile.get("varac_bbs_archive_dir")
            or Path(shared_bbs) / "Archive"
        )
        plan = build_varac_native_cluster_plan(
            VarACNativeClusterRequest(
                version=version, platform=platform_key, operation="update-member",
                members=(member,), shared_db_path=shared_db,
                shared_bbs_path=shared_bbs,
                shared_bbs_archive_path=shared_archive,
                native_shared_db_path=_native_varac_path(
                    Path(shared_db), platform_key=platform_key, wine_prefix=wine_prefix
                ),
                allowed_roots=_minimal_roots(
                    drive_root, source_ini_path.parent, source_runtime, target_runtime,
                    Path(shared_db).expanduser().parent, Path(shared_bbs).expanduser(),
                    Path(shared_archive).expanduser(),
                ),
                ptt_lock_enabled=bool((cluster or {}).get("ptt_lock_enabled", False)),
                wine_executable="wine",
            )
        )
        return VarACManagedRuntimeRepair(
            "ready", node_id, radio_id, target_runtime=target_runtime,
            target_ini=target_runtime / "VARA.ini", plan=plan,
            why="Move the obsolete managed VARA runtime into the verified Wine drive.",
        )
    except (OSError, ValueError, VarACNativeConfigurationError) as exc:
        return VarACManagedRuntimeRepair("needs-attention", node_id, radio_id, why=str(exc))


def prepare_varac_native_configuration(
    draft: Mapping[str, Any],
    *,
    varac_nodes: Sequence[Mapping[str, Any]],
    device_profiles: Sequence[Mapping[str, Any]],
    varac_clusters: Sequence[Mapping[str, Any]],
    varac_members: Sequence[Mapping[str, Any]],
    managed_root: Path,
    generation: int,
    platform_override: str = "",
    process_running: bool = False,
) -> VarACNativePreparationResult:
    """Prepare the exact supported create/join plan from reviewed evidence.

    Preparation is read-only, so a running VarAC/VARA process is retained as
    an apply-time warning.  The transactional writer independently checks the
    live process state before backup or mutation and remains the safety gate.
    """

    intent = dict(draft or {})
    fingerprint = native_draft_fingerprint(intent)
    arrangement = str(intent.get("cluster_path") or "standalone").strip().lower()
    if str(intent.get("family_key") or "").strip().lower() != "varac":
        return _result("manual setup required", "This preparation route is only for VarAC.", intent, fingerprint, generation)
    if str(intent.get("mode") or "").strip().lower() != "managed":
        return _result("manual setup required", "Choose a new CommsPlan-managed VarAC instance to use native configuration.", intent, fingerprint, generation)
    if arrangement not in {"create_cluster", "join_cluster"}:
        return _result("manual setup required", "Choose Create cluster or Join cluster before preparing native VarAC.", intent, fingerprint, generation)
    platform_key = _platform_key(platform_override)
    if platform_key not in {"windows", "linux-wine"}:
        return _result(
            "manual setup required",
            "Native VarAC writing is qualified only for Windows and Linux/Wine in this release.",
            intent,
            fingerprint,
            generation,
            platform=platform_key,
        )
    try:
        plan = _build_plan(
            intent,
            nodes=tuple(dict(row) for row in varac_nodes),
            profiles=tuple(dict(row) for row in device_profiles),
            clusters=tuple(dict(row) for row in varac_clusters),
            memberships=tuple(dict(row) for row in varac_members),
            managed_root=Path(managed_root).expanduser(),
            platform_key=platform_key,
        )
    except (OSError, ValueError, VarACNativeConfigurationError) as exc:
        detail = str(exc).strip() or "The selected native VarAC evidence is incomplete or ambiguous."
        state = "stop required" if "running" in detail.casefold() else "needs attention"
        if "unsupported" in detail.casefold() or "not qualified" in detail.casefold():
            state = "manual setup required"
        return _result(state, detail, intent, fingerprint, generation, platform=platform_key, error=detail)

    sender = plan.email_gateway_sender_member_id or "No email gateway"
    member_numbers = ", ".join(
        f"{member.member_id}: {member.changes['VARAC_CLUSTER']['InstanceNumber']}"
        for member in plan.members
    )
    new_member = plan.members[-1]
    # The canonical application fact is the executable, even when the
    # operator selected the containing VarAC directory.
    prepared_application_path = str(
        new_member.launch_command[1]
        if plan.platform == "linux-wine" and len(new_member.launch_command) > 1
        else new_member.launch_command[0]
    )
    member_root = new_member.vara_target_runtime_folder.parent
    managed_directories = tuple(plan.managed_directories)
    prepared_incoming = managed_directories[0] if managed_directories else member_root / "incoming"
    prepared_outbox = managed_directories[1] if len(managed_directories) > 1 else member_root / "outbox"
    vara_values = new_member.changes.get("VARAHF_CONFIG", {})
    apply_warning = (
        " VarAC or VARA is currently running; close both applications before final Save so CommsPlan can apply the reviewed files safely."
        if process_running
        else ""
    )
    presentation = {
        "state": "ready",
        "why": (
            "Exact VarAC 13.2.7 source, paths, ports, runtime copies, and target state are qualified and ready for transactional apply."
            + apply_warning
        ),
        "arrangement": arrangement,
        "affected_radios": tuple(_affected_radio_names(intent, plan, device_profiles)),
        "shared_database_summary": plan.shared_db_path,
        "member_numbers_summary": member_numbers,
        "ptt_lock_summary": "Enabled" if plan.ptt_lock_enabled else "Disabled",
        "email_gateway_sender_summary": sender,
        "writer_version": plan.version,
        "writer_platform": plan.platform,
        "writer_operation": plan.operation,
        "writer_qualified": True,
        "apply_requires_stopped_process": bool(process_running),
        "application_path": prepared_application_path,
        "varac_ini_path": str(new_member.target_path),
        "configuration_path": str(new_member.target_path),
        "storage_path": str(plan.shared_db_path),
        "secondary_storage_path": str(prepared_incoming),
        "outbox_path": str(prepared_outbox),
        "bbs_path": str(plan.shared_bbs_path),
        "bbs_archive_path": str(plan.shared_bbs_archive_path),
        "cluster_bbs_path": str(plan.shared_bbs_path),
        "cluster_bbs_archive_path": str(plan.shared_bbs_archive_path),
        "working_directory": str(new_member.working_directory),
        "vara_runtime_path": str(new_member.vara_target_runtime_folder),
        "vara_ini_path": str(new_member.vara_target_path),
        "launch_command": _display_argv(new_member.launch_command),
        "launch_argv": tuple(new_member.launch_command),
        "launch_environment": {"WINEPREFIX": str(new_member.wine_prefix)}
        if new_member.wine_prefix else {},
        "port": int(vara_values.get("VarahfMainPort") or 0),
        "secondary_port": int(vara_values.get("VarahfMainKissPort") or 0),
        "ports_summary": _ports_summary(plan),
        "fingerprints_summary": plan.plan_fingerprint,
        "generation": int(generation),
        "draft_fingerprint": fingerprint,
        "plan_fingerprint": plan.plan_fingerprint,
    }
    return VarACNativePreparationResult(
        state="ready",
        presentation=presentation,
        draft_fingerprint=fingerprint,
        generation=int(generation),
        plan=plan,
    )


def _build_plan(
    draft: Mapping[str, Any],
    *,
    nodes: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    managed_root: Path,
    platform_key: str,
) -> VarACNativeClusterPlan:
    arrangement = str(draft.get("cluster_path") or "").strip().lower()
    new_key = str(draft.get("draft_instance_key") or draft.get("owner_draft_key") or draft.get("instance_name") or "new-varac").strip()
    new_label = str(draft.get("instance_name") or draft.get("owner_label") or "New VarAC").strip()
    new_slug = _slug(new_label or new_key)
    # VarAC's native multi-instance contract is one executable installation
    # with one distinct INI beside that installation.  Mailbox data retains
    # its separately derived station location; the VARA target is selected
    # after Wine-prefix evidence is available below.
    managed_member_root = managed_root / new_slug / "varac-native"

    existing_node: Mapping[str, Any] | None = None
    existing_profile: Mapping[str, Any] | None = None
    cluster: Mapping[str, Any] | None = None
    operation = "create-member"
    if arrangement == "create_cluster":
        node_id = _positive_int(draft.get("existing_standalone_node_id"))
        if node_id:
            existing_node = _unique(nodes, "id", node_id, "selected standalone VarAC node")
        else:
            existing_node, existing_profile = _unique_linked_standalone(
                nodes,
                profiles,
                memberships,
            )
            node_id = _positive_int(existing_node.get("id"))
        if existing_profile is None:
            existing_profile = _profile_for_node(profiles, node_id)
        shared_db = str(draft.get("cluster_shared_database") or existing_node.get("db_path") or "").strip()
        if not shared_db:
            raise ValueError("The existing standalone VarAC database is unknown; choose a shared database before preparing.")
        operation = "convert-standalone"
    else:
        cluster_token = str(draft.get("cluster_id") or "").strip()
        cluster = _cluster_by_token(clusters, cluster_token)
        shared_db = str(cluster.get("shared_db_path") or "").strip()
        if not shared_db:
            raise ValueError("The selected cluster has no verified shared VarAC database.")
        member_rows = [row for row in memberships if int(row.get("cluster_id") or 0) == int(cluster.get("id") or 0) and int(row.get("enabled", 1) or 0) == 1]
        if not member_rows:
            raise ValueError("The selected cluster has no enabled member that can provide qualified source evidence.")
        seed_profile_id = int(member_rows[0].get("device_profile_id") or 0)
        existing_profile = _unique(profiles, "id", seed_profile_id, "existing cluster member")
        existing_node = _unique(nodes, "id", int(existing_profile.get("varac_node_id") or 0), "existing cluster VarAC node")

    source_ini_path = _required_file(existing_node.get("ini_path"), "existing VarAC.ini")
    source_ini = parse_varac_ini_bytes(source_ini_path, source_ini_path.read_bytes())
    wine_prefix = _wine_prefix(source_ini.path) if platform_key == "linux-wine" else ""
    source_executable = _resolve_varac_executable(
        draft.get("application_path") or existing_node.get("install_path")
    )
    default_bbs = source_executable.parent / "BBS"
    explicit_bbs = str(
        draft.get("cluster_bbs_path")
        or draft.get("bbs_path")
        or draft.get("varac_bbs_dir")
        or ""
    ).strip()
    explicit_archive = str(
        draft.get("cluster_bbs_archive_path")
        or draft.get("bbs_archive_path")
        or draft.get("varac_bbs_archive_dir")
        or ""
    ).strip()
    inherited_bbs = ""
    inherited_archive = ""
    if arrangement == "create_cluster" and existing_profile is not None:
        inherited_bbs = str(existing_profile.get("varac_bbs_dir") or "").strip()
        inherited_archive = str(existing_profile.get("varac_bbs_archive_dir") or "").strip()
    elif cluster is not None:
        inherited_bbs = str(cluster.get("shared_bbs_path") or "").strip()
        inherited_archive = str(cluster.get("shared_bbs_archive_path") or "").strip()
    shared_bbs = Path(explicit_bbs or inherited_bbs or default_bbs).expanduser()
    shared_bbs_archive = Path(
        explicit_archive or inherited_archive or shared_bbs / "Archive"
    ).expanduser()
    new_ini_target = source_executable.parent / f"VarAC-{new_slug}.ini"
    version = _qualified_version(
        str(draft.get("version") or "").strip(),
        str(existing_node.get("native_writer_key") or "").strip(),
        source_executable,
    )
    source_vara_root = _resolve_vara_runtime(existing_node, source_ini)
    source_vara_ini = _required_file(source_vara_root / "VARA.ini", "source VARA.ini")
    source_vara = parse_vara_ini_bytes(source_vara_ini, source_vara_ini.read_bytes())
    source_files = snapshot_vara_runtime_files(source_vara_root)

    # VarAC is a Windows process even when launched through Wine.  Keep every
    # FIO-created VARA runtime inside the discovered Wine drive so VarAC sees a
    # native drive-letter path (for example C:\\VARA-ft-710\\VARA.exe), never a
    # host-root Z: projection below .freqinout.  Fixtures and unusual qualified
    # A qualified Linux/Wine plan requires concrete drive_<letter> evidence.
    # Falling back to FIO's host-side managed root makes VarAC persist a Z:
    # path that is fragile and violates the native application layout.
    wine_drive_root = _wine_drive_root(source_ini.path) if platform_key == "linux-wine" else None
    if platform_key == "linux-wine" and wine_drive_root is None:
        raise ValueError(
            "The selected VarAC.ini is not inside a verified Wine drive_<letter> folder; "
            "choose the Wine VarAC instance before CommsPlan prepares a VARA runtime."
        )
    vara_target_parent = wine_drive_root or managed_member_root
    new_vara_target_base = (
        vara_target_parent / f"VARA-{new_slug}"
        if wine_drive_root
        else vara_target_parent / "VARA"
    )

    reserved_vara_targets: list[Path] = []

    occupied = _occupied_ports(nodes)
    source_ports = _source_vara_ports(source_ini, source_vara)
    occupied.update(source_ports)
    new_ports = _allocate_port_bundle(occupied, start=max(8300, source_ports[0] + 10))
    new_member_number = _positive_int(draft.get("cluster_instance_number"))
    if not new_member_number:
        raise ValueError("Choose a positive VarAC cluster member number before preparing.")
    members: list[VarACMemberInput] = []
    if arrangement == "create_cluster":
        existing_number = _positive_int(draft.get("existing_standalone_member_number")) or 1
        if existing_number == new_member_number:
            raise ValueError("The existing and new VarAC members need different member numbers.")
        existing_member_id = f"node:{int(existing_node.get('id') or 0)}"
        existing_slug = _slug(
            str(existing_profile.get("name") or existing_node.get("name") or existing_member_id)
        )
        existing_target_base = (
            wine_drive_root / f"VARA-{existing_slug}"
            if wine_drive_root
            else managed_root / existing_slug / "varac-native" / "VARA"
        )
        existing_target_root = _next_available_managed_runtime(
            existing_target_base,
            reserved=reserved_vara_targets,
        )
        reserved_vara_targets.append(existing_target_root)
        members.append(
            _member(
                member_id=existing_member_id,
                source_ini=source_ini,
                target_ini=source_ini_path,
                member_number=existing_number,
                executable=_resolve_varac_executable(existing_node.get("install_path")),
                source_vara_root=source_vara_root,
                source_vara=source_vara,
                source_files=source_files,
                target_vara_root=existing_target_root,
                ports=source_ports,
                platform_key=platform_key,
            )
        )
    new_vara_target = _next_available_managed_runtime(
        new_vara_target_base,
        reserved=reserved_vara_targets,
    )
    members.append(
        _member(
            member_id=new_key,
            source_ini=source_ini,
            target_ini=new_ini_target,
            member_number=new_member_number,
            executable=source_executable,
            source_vara_root=source_vara_root,
            source_vara=source_vara,
            source_files=source_files,
            target_vara_root=new_vara_target,
            ports=new_ports,
            platform_key=platform_key,
        )
    )
    sender_choice = str(draft.get("email_gateway_sender_choice") or "none").strip().lower()
    sender = ""
    if sender_choice == "existing_member" and arrangement == "create_cluster":
        sender = members[0].member_id
    elif sender_choice == "new_member":
        sender = new_key
    elif sender_choice not in {"", "none"}:
        raise ValueError("Choose No email gateway, existing member, or new member before preparing.")

    incoming_path, outbox_path = _member_mailbox_paths(
        draft,
        profiles=profiles,
        existing_profile=existing_profile,
        managed_member_root=managed_member_root,
        member_label=new_label,
    )
    if _paths_overlap(incoming_path, outbox_path):
        raise ValueError("VarAC incoming and outbox folders must be distinct node-local paths.")
    for local_label, local_path in (("incoming", incoming_path), ("outbox", outbox_path)):
        for shared_label, shared_path in (("BBS", shared_bbs), ("BBS archive", shared_bbs_archive)):
            if _paths_overlap(local_path, shared_path):
                raise ValueError(
                    f"VarAC {local_label} is node-local and cannot overlap the cluster-shared {shared_label} path."
                )
    if _paths_overlap(shared_bbs, shared_bbs_archive) and not _is_parent_path(
        shared_bbs, shared_bbs_archive
    ):
        raise ValueError("The VarAC BBS archive cannot contain or replace the shared BBS folder.")
    roots = _minimal_roots(
        managed_root,
        source_ini_path.parent,
        source_executable.parent,
        source_vara_root,
        Path(shared_db).expanduser().parent,
        shared_bbs,
        shared_bbs_archive,
        incoming_path,
        outbox_path,
        *((wine_drive_root,) if wine_drive_root is not None else ()),
    )
    return build_varac_native_cluster_plan(
        VarACNativeClusterRequest(
            version=version,
            platform=platform_key,
            operation=operation,
            members=tuple(members),
            shared_db_path=str(Path(shared_db).expanduser()),
            shared_bbs_path=str(shared_bbs),
            shared_bbs_archive_path=str(shared_bbs_archive),
            managed_directories=(incoming_path, outbox_path, shared_bbs, shared_bbs_archive),
            native_shared_db_path=_native_varac_path(
                Path(shared_db).expanduser(),
                platform_key=platform_key,
                wine_prefix=wine_prefix,
            ),
            allowed_roots=roots,
            ptt_lock_enabled=bool(draft.get("cluster_ptt_lock", False)),
            email_gateway_sender_member_id=sender,
            wine_executable="wine",
        )
    )


def _member(
    *,
    member_id: str,
    source_ini: VarACIniSource,
    target_ini: Path,
    member_number: int,
    executable: Path,
    source_vara_root: Path,
    source_vara: VarACIniSource,
    source_files: tuple[Any, ...],
    target_vara_root: Path,
    ports: tuple[int, int, int],
    platform_key: str,
) -> VarACMemberInput:
    command, kiss, monitor = ports
    target_exe = target_vara_root / "VARA.exe"
    target_vara_ini = target_vara_root / "VARA.ini"
    wine_prefix = _wine_prefix(source_ini.path) if platform_key == "linux-wine" else ""
    configured_target_exe = _native_varac_path(
        target_exe,
        platform_key=platform_key,
        wine_prefix=wine_prefix,
    )
    return VarACMemberInput(
        member_id=member_id,
        source=source_ini,
        target=snapshot_target_state(target_ini),
        member_number=member_number,
        vara_settings={
            "VarahfMainPath": configured_target_exe,
            "VarahfMainPort": str(command),
            "VarahfMainHost": "127.0.0.1",
            "VarahfEnableKissInterface": "ON",
            "VarahfMainKissPort": str(kiss),
            "VarahfMonitorPath": configured_target_exe,
            "VarahfMonitorPort": str(monitor),
            # A managed cluster member is launched through VarAC.  VarAC must
            # own the child modem start so FIO and VarAC cannot race to spawn
            # the same node-local VARA runtime.
            "VarahfLaunchOnModemConnect": "ON",
        },
        executable_path=str(executable),
        vara_runtime=VarARuntimeInput(
            source_runtime_folder=source_vara_root,
            target_runtime_folder=target_vara_root,
            source=source_vara,
            target=snapshot_target_state(target_vara_ini),
            settings={
                "Setup": {
                    "TCP Command Port": str(command),
                    "Enable KISS": "1",
                    "KISS Port": str(kiss),
                },
                "Monitor": {"Monitor Mode": "1"},
            },
            files=source_files,
            configured_main_executable_path=configured_target_exe,
            configured_monitor_executable_path=configured_target_exe,
        ),
        working_directory=str(target_ini.parent),
        wine_prefix=wine_prefix,
        launch_ini_path=_native_varac_path(
            target_ini,
            platform_key=platform_key,
            wine_prefix=wine_prefix,
        ),
    )


def _source_vara_ports(varac: VarACIniSource, vara: VarACIniSource) -> tuple[int, int, int]:
    section = _section(varac.values, "VARAHF_CONFIG")
    setup = _section(vara.values, "Setup")
    monitor_section = _section(vara.values, "Monitor")
    command = _port(_value(section, "VarahfMainPort") or _value(setup, "TCP Command Port"), "VARA command port")
    kiss = _port(_value(section, "VarahfMainKissPort") or _value(setup, "KISS Port"), "VARA KISS port")
    monitor = _port(_value(section, "VarahfMonitorPort") or _value(monitor_section, "Monitor Port") or command + 3, "VARA monitor port")
    return command, kiss, monitor


def _occupied_ports(nodes: Sequence[Mapping[str, Any]]) -> set[int]:
    ports: set[int] = set()
    for node in nodes:
        path = Path(str(node.get("ini_path") or "")).expanduser()
        if not path.is_file() or path.is_symlink():
            continue
        try:
            source = parse_varac_ini_bytes(path, path.read_bytes())
            command, kiss, monitor = _source_vara_ports(source, source)
        except Exception:
            continue
        ports.update((command, command + 1, kiss, monitor))
    return ports


def _allocate_port_bundle(occupied: set[int], *, start: int) -> tuple[int, int, int]:
    command = max(1024, int(start))
    while command <= 65520:
        proposed = (command, command + 1, command + 2, command + 3)
        if not any(port in occupied for port in proposed):
            return command, command + 2, command + 3
        command += 10
    raise ValueError("CommsPlan could not allocate a non-conflicting local VARA port bundle.")


def _qualified_version(hint: str, writer_key: str, executable: Path) -> str:
    candidates = [hint]
    candidates.extend(re.findall(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", writer_key))
    for candidate in candidates:
        if candidate in DEFAULT_SUPPORTED_VARAC_VERSIONS:
            return candidate
    with executable.open("rb") as handle:
        raw = handle.read(64 * 1024 * 1024)
    for version in DEFAULT_SUPPORTED_VARAC_VERSIONS:
        if version.encode("ascii") in raw or version.encode("utf-16le") in raw:
            return version
    raise ValueError("The detected VarAC version is not qualified for native writing; configure this cluster manually.")


def _resolve_varac_executable(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    candidates = (path, path / "VarAC.exe", path / "varac.exe") if path.suffix else (path / "VarAC.exe", path / "varac.exe", path)
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise ValueError("VarAC.exe was not found in the selected application location.")


def _resolve_vara_runtime(node: Mapping[str, Any], source: VarACIniSource) -> Path:
    explicit = str(node.get("vara_runtime_path") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
    else:
        configured = _value(_section(source.values, "VARAHF_CONFIG"), "VarahfMainPath")
        candidate = Path(varac_path_to_host_path(configured, ini_path=source.path)).expanduser().parent
    if not candidate.is_dir() or candidate.is_symlink():
        raise ValueError("The source VARA runtime folder could not be verified.")
    return candidate


def _platform_key(override: str) -> str:
    value = str(override or "").strip().lower()
    if value:
        return value
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux-wine"
    return "macos-wine" if sys.platform == "darwin" else sys.platform


def _result(state: str, why: str, draft: Mapping[str, Any], fingerprint: str, generation: int, *, platform: str = "", error: str = "") -> VarACNativePreparationResult:
    presentation = {
        "state": state,
        "why": why,
        "arrangement": str(draft.get("cluster_path") or ""),
        "affected_radios": tuple(filter(None, (str(draft.get("owner_label") or draft.get("instance_name") or "").strip(),))),
        "writer_platform": platform,
        "writer_qualified": False,
        "generation": int(generation),
        "draft_fingerprint": fingerprint,
    }
    return VarACNativePreparationResult(state, presentation, fingerprint, int(generation), error=error)


def _section(values: Mapping[str, Mapping[str, str]], wanted: str) -> Mapping[str, str]:
    for name, section in values.items():
        if str(name).casefold() == wanted.casefold():
            return section
    return {}


def _value(values: Mapping[str, Any], wanted: str) -> str:
    for key, value in values.items():
        if str(key).casefold() == wanted.casefold():
            return str(value or "").strip()
    return ""


def _port(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable or invalid.") from exc
    if not 1 <= number <= 65535:
        raise ValueError(f"{label} is outside the valid port range.")
    return number


def _positive_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _unique(rows: Sequence[Mapping[str, Any]], key: str, value: Any, label: str) -> Mapping[str, Any]:
    matches = [row for row in rows if str(row.get(key) or "") == str(value or "")]
    if len(matches) != 1:
        raise ValueError(f"The {label} is no longer available. Return to the VarAC arrangement and choose it again.")
    return matches[0]


def _unique_linked_standalone(
    nodes: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Recover one durable standalone topology identity when UI metadata is absent.

    Application completeness is intentionally not considered here.  A linked
    node remains topology evidence even when native preparation later needs to
    explain a missing path.  This fallback is safe only for exactly one linked,
    enabled node outside every current cluster membership.
    """

    member_profile_ids = {
        _positive_int(row.get("device_profile_id"))
        for row in memberships
        if _positive_int(row.get("device_profile_id"))
        and int(row.get("enabled", 1) or 0) == 1
    }
    profiles_by_node: dict[int, list[Mapping[str, Any]]] = {}
    for profile in profiles:
        profile_id = _positive_int(profile.get("id"))
        node_id = _positive_int(profile.get("varac_node_id"))
        if not node_id or profile_id in member_profile_ids:
            continue
        profiles_by_node.setdefault(node_id, []).append(profile)

    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for node in nodes:
        node_id = _positive_int(node.get("id"))
        if not node_id or int(node.get("enabled", 1) or 0) != 1:
            continue
        linked_profiles = profiles_by_node.get(node_id, ())
        if len(linked_profiles) == 1:
            candidates.append((node, linked_profiles[0]))

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(
            "No linked standalone VarAC node is available. Review the VarAC arrangement or its saved radio assignment."
        )
    raise ValueError(
        "More than one standalone VarAC node is available. Choose the node in the VarAC arrangement before preparing."
    )


def _profile_for_node(profiles: Sequence[Mapping[str, Any]], node_id: int) -> Mapping[str, Any]:
    matches = [row for row in profiles if int(row.get("varac_node_id") or 0) == int(node_id)]
    if len(matches) != 1:
        raise ValueError("The existing standalone VarAC node must belong to exactly one radio.")
    return matches[0]


def _cluster_by_token(clusters: Sequence[Mapping[str, Any]], token: str) -> Mapping[str, Any]:
    matches = [row for row in clusters if token and token.casefold() in {str(row.get("id") or "").casefold(), str(row.get("cluster_id") or "").casefold()}]
    if len(matches) != 1:
        raise ValueError("The existing VarAC cluster is missing or ambiguous; choose it again.")
    return matches[0]


def _required_file(value: Any, label: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"The {label} could not be verified.")
    return path


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "varac-member"


def _next_available_managed_runtime(
    preferred: Path,
    *,
    reserved: Sequence[Path] = (),
) -> Path:
    """Choose a fresh managed VARA runtime without weakening writer safety.

    Native apply intentionally refuses to replace any existing runtime folder.
    Preparation therefore allocates a readable sibling when the canonical name
    is already occupied by an earlier FIO attempt or another reviewed member.
    Broken symlinks count as occupied, and no existing path is removed or
    reused.  Repeated read-only preparation remains stable until the filesystem
    itself changes because it always chooses the first available name.
    """

    reserved_keys = {_lexical_runtime_key(path) for path in reserved}
    for suffix in range(1, 10_001):
        candidate = preferred if suffix == 1 else preferred.with_name(
            f"{preferred.name}-{suffix}"
        )
        if _lexical_runtime_key(candidate) in reserved_keys:
            continue
        if candidate.exists() or candidate.is_symlink():
            continue
        return candidate
    raise ValueError(
        f"No unused managed VARA runtime name is available below {preferred.parent}."
    )


def _lexical_runtime_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(Path(path).expanduser())))


def _member_mailbox_paths(
    draft: Mapping[str, Any],
    *,
    profiles: Sequence[Mapping[str, Any]],
    existing_profile: Mapping[str, Any] | None,
    managed_member_root: Path,
    member_label: str,
) -> tuple[Path, Path]:
    """Derive distinct member mailboxes beside the reviewed station mailboxes.

    A new cluster member should stay in the operator's established VarAC data
    area when the existing member provides that evidence.  These paths remain
    node-local; only their parent location is inherited.  Explicit reviewed
    values always win, and a station with no mailbox evidence retains the
    conservative FIO-managed-root fallback.
    """

    explicit_incoming = _reviewed_path_override(draft, "secondary_storage_path")
    explicit_outbox = _reviewed_path_override(draft, "outbox_path")
    existing_incoming = str(
        (existing_profile or {}).get("varac_incoming_path")
        or (existing_profile or {}).get("varac_incoming_dir")
        or ""
    ).strip()
    existing_outbox = str((existing_profile or {}).get("varac_outbox_dir") or "").strip()
    has_reviewed_mailbox_parent = bool(existing_incoming or existing_outbox)

    incoming_parent = (
        Path(existing_incoming).expanduser().parent
        if existing_incoming
        else Path(existing_outbox).expanduser().parent
        if existing_outbox
        else managed_member_root
    )
    outbox_parent = (
        Path(existing_outbox).expanduser().parent
        if existing_outbox
        else Path(existing_incoming).expanduser().parent
        if existing_incoming
        else managed_member_root
    )
    label = _filesystem_label(member_label)
    occupied = _profile_mailbox_paths(profiles)

    if not explicit_incoming and not explicit_outbox and has_reviewed_mailbox_parent:
        return _next_available_mailbox_pair(
            incoming_parent,
            outbox_parent,
            label,
            occupied,
        )

    if explicit_incoming:
        incoming = Path(explicit_incoming).expanduser()
    elif has_reviewed_mailbox_parent:
        incoming = _next_available_mailbox_path(incoming_parent, label, "In", occupied)
    else:
        incoming = managed_member_root / "incoming"
    occupied.append(incoming)
    if explicit_outbox:
        outbox = Path(explicit_outbox).expanduser()
    elif has_reviewed_mailbox_parent:
        outbox = _next_available_mailbox_path(outbox_parent, label, "Out", occupied)
    else:
        outbox = managed_member_root / "outbox"
    return incoming, outbox


def _filesystem_label(value: str) -> str:
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(value or "").strip())
    label = re.sub(r"\s+", "-", label).strip(" .-")
    return label or "VarAC-Member"


def _reviewed_path_override(draft: Mapping[str, Any], key: str) -> str:
    """Return an operator correction, not a value published by an older plan."""

    value = str(draft.get(key) or "").strip()
    presentation = draft.get("varac_native_presentation")
    generated = (
        str(presentation.get(key) or "").strip()
        if isinstance(presentation, Mapping)
        else ""
    )
    return "" if value and generated and value == generated else value


def _profile_mailbox_paths(profiles: Sequence[Mapping[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    for profile in profiles:
        for key in ("varac_incoming_path", "varac_incoming_dir", "varac_outbox_dir"):
            value = str(profile.get(key) or "").strip()
            if value:
                candidate = Path(value).expanduser()
                if candidate not in paths:
                    paths.append(candidate)
    return paths


def _next_available_mailbox_path(
    parent: Path,
    label: str,
    suffix: str,
    occupied: Sequence[Path],
) -> Path:
    attempt = 1
    while True:
        numbered = label if attempt == 1 else f"{label}-{attempt}"
        candidate = Path(parent).expanduser() / f"{numbered}_{suffix}"
        if not any(_paths_overlap(candidate, current) for current in occupied):
            return candidate
        attempt += 1


def _next_available_mailbox_pair(
    incoming_parent: Path,
    outbox_parent: Path,
    label: str,
    occupied: Sequence[Path],
) -> tuple[Path, Path]:
    attempt = 1
    while True:
        numbered = label if attempt == 1 else f"{label}-{attempt}"
        incoming = Path(incoming_parent).expanduser() / f"{numbered}_In"
        outbox = Path(outbox_parent).expanduser() / f"{numbered}_Out"
        candidates = (incoming, outbox)
        if not _paths_overlap(incoming, outbox) and not any(
            _paths_overlap(candidate, current)
            for candidate in candidates
            for current in occupied
        ):
            return candidates
        attempt += 1


def _minimal_roots(*paths: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for path in paths:
        candidate = Path(path).expanduser()
        if candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


def _is_parent_path(parent: Path, child: Path) -> bool:
    left = Path(parent).expanduser().absolute()
    right = Path(child).expanduser().absolute()
    return left != right and left in right.parents


def _paths_overlap(left: Path, right: Path) -> bool:
    first = Path(left).expanduser().absolute()
    second = Path(right).expanduser().absolute()
    return first == second or first in second.parents or second in first.parents


def _wine_prefix(ini_path: Path) -> str:
    parts = ini_path.expanduser().parts
    for index, part in enumerate(parts):
        if re.fullmatch(r"drive_[a-zA-Z]", part):
            return str(Path(*parts[:index]))
    return str(Path(os.environ.get("WINEPREFIX", "~/.wine")).expanduser())


def _wine_drive_root(path: Path) -> Path | None:
    """Return the concrete Wine drive containing *path*, when evidenced."""

    parts = Path(path).expanduser().absolute().parts
    for index, part in enumerate(parts):
        if re.fullmatch(r"drive_[a-zA-Z]", part):
            return Path(*parts[: index + 1])
    return None


def _native_varac_path(path: Path, *, platform_key: str, wine_prefix: str) -> str:
    """Return the path spelling the Windows VarAC process can consume."""

    candidate = Path(path).expanduser().absolute()
    if platform_key != "linux-wine":
        return str(candidate)
    prefix = Path(wine_prefix).expanduser().absolute()
    try:
        relative = candidate.relative_to(prefix)
    except ValueError:
        relative = None
    if relative is not None and relative.parts:
        drive_match = re.fullmatch(r"drive_([a-zA-Z])", relative.parts[0])
        if drive_match:
            suffix = "\\".join(relative.parts[1:])
            return f"{drive_match.group(1).upper()}:\\{suffix}" if suffix else f"{drive_match.group(1).upper()}:\\"
    # Z: remains valid for operator-owned data such as a shared BBS/NAS path,
    # but managed executable/configuration identities are rejected by the
    # preparation precondition before reaching this fallback.
    return "Z:\\" + "\\".join(candidate.parts[1:])


def _display_argv(argv: Iterable[str]) -> str:
    return " ".join(f'"{item}"' if any(ch.isspace() for ch in item) else item for item in argv)


def _ports_summary(plan: VarACNativeClusterPlan) -> str:
    return "; ".join(
        f"{member.member_id}: command {member.vara_changes['Setup']['TCP Command Port']}, KISS {member.vara_changes['Setup']['KISS Port']}"
        for member in plan.members
    )


def _affected_radio_names(draft: Mapping[str, Any], plan: VarACNativeClusterPlan, profiles: Sequence[Mapping[str, Any]]) -> Iterable[str]:
    by_node = {int(row.get("varac_node_id") or 0): str(row.get("name") or "").strip() for row in profiles}
    for member in plan.members:
        if member.member_id.startswith("node:"):
            yield by_node.get(int(member.member_id.split(":", 1)[1]), member.member_id)
        else:
            yield str(draft.get("owner_label") or draft.get("instance_name") or member.member_id).strip()


__all__ = [
    "VarACManagedRuntimeRepair",
    "VarACNativePreparationResult",
    "native_draft_fingerprint",
    "prepare_managed_varac_runtime_repair",
    "prepare_varac_native_configuration",
]
