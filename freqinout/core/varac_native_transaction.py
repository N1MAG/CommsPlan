"""Durable orchestration for native VarAC/VARA configuration applies.

Third-party INI files and FIO's SQLite store cannot share one atomic primitive.
This module therefore journals intent first, delegates the reversible external
write, commits FIO state in one guided transaction, and completes the journal
last.  Recovery only rolls back or completes already-committed evidence; it
never performs an unreviewed forward configuration change.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from freqinout.core.config_backup import (
    ConfigBackupItem,
    ConfigBackupResult,
    restore_config_backup,
)
from freqinout.core.config_varac_managed import (
    VarACNativeApplyResult,
    VarACNativeClusterPlan,
    VarACNativeConfigurationError,
    VarACNativeMemberPlan,
    apply_varac_native_cluster_plan,
    rollback_varac_native_cluster_apply,
)
from freqinout.core.multi_radio_store import MultiRadioStore


@dataclass(frozen=True)
class VarACNativeTransactionResult:
    journal_id: str
    apply_result: VarACNativeApplyResult | None
    persisted: Any = None
    committed: bool = False
    needs_recovery: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.committed and not self.needs_recovery and not self.error


@dataclass(frozen=True)
class VarACNativeExternalSession:
    """Durable external-apply state awaiting FIO's guided DB commit.

    Qt integrations use this split phase so filesystem work stays on a bounded
    worker while the existing guided-save transaction can remain the sole
    owner of its SQLite connection.  A session is never evidence of success
    until ``mark_varac_native_fio_committed`` and
    ``complete_varac_native_external_session`` have both run.
    """

    journal_id: str
    plan: VarACNativeClusterPlan
    apply_result: VarACNativeApplyResult | None
    observed: Mapping[str, Any]
    needs_recovery: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.apply_result is not None
            and self.apply_result.ok
            and not self.needs_recovery
            and not self.error
        )


def begin_varac_native_external_apply(
    *,
    store: MultiRadioStore,
    plan: VarACNativeClusterPlan,
    backup_root: Path,
    running_checker: Callable[[VarACNativeMemberPlan], bool] | None = None,
    journal_id: str | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> VarACNativeExternalSession:
    """Apply and verify native files, leaving a durable external_applied row.

    This function performs filesystem I/O and must not run on the GUI thread.
    It intentionally does not persist any radio, cluster, membership, or launch
    assignment.  Callers either commit those together and mark ``fio_committed``
    or compensate with ``rollback_varac_native_external_session``.
    """

    entry_id = str(journal_id or uuid.uuid4())
    writer_key = _writer_key(plan)
    desired = _desired_evidence(plan)
    targets = [
        str(target)
        for member in plan.members
        for target in (member.target_path, member.vara_target_runtime_folder)
    ]
    targets.extend(str(path) for path in plan.managed_directories)
    store.begin_varac_native_apply_journal(
        journal_id=entry_id,
        plan_fingerprint=plan.plan_fingerprint,
        operation=plan.operation,
        writer_key=writer_key,
        targets=targets,
        desired=desired,
    )
    journal_state = "pending"

    def record_writer_state(state: str, backup: ConfigBackupResult | None) -> None:
        nonlocal journal_state
        try:
            store.update_varac_native_apply_journal(
                entry_id,
                state=state,
                expected_states=(journal_state,),
                backup_manifest=_backup_payload(backup) if backup is not None else None,
                durable=True,
            )
        except Exception as exc:
            raise VarACNativeConfigurationError(
                f"Unable to durably journal native VarAC state {state}: {exc}"
            ) from exc
        journal_state = state

    if failure_injector is not None:
        failure_injector("before_external_apply")
    apply_result = apply_varac_native_cluster_plan(
        plan,
        backup_root=backup_root,
        running_checker=running_checker,
        state_callback=record_writer_state,
    )
    if not apply_result.ok:
        recovery_required = bool(
            apply_result.backup is not None
            and (apply_result.restore is None or not apply_result.restore.ok)
        )
        final_state = "recovery_required" if recovery_required else "rolled_back"
        store.update_varac_native_apply_journal(
            entry_id,
            state=final_state,
            expected_states=(journal_state,),
            backup_manifest=_backup_payload(apply_result.backup),
            error=apply_result.error,
            durable=True,
        )
        return VarACNativeExternalSession(
            journal_id=entry_id,
            plan=plan,
            apply_result=apply_result,
            observed={},
            needs_recovery=recovery_required,
            error=apply_result.error,
        )

    try:
        observed = _observed_evidence(plan, apply_result)
        store.update_varac_native_apply_journal(
            entry_id,
            state="external_applied",
            expected_states=(journal_state,),
            observed=observed,
            backup_manifest=_backup_payload(apply_result.backup),
            durable=True,
        )
    except Exception as exc:
        rollback = rollback_varac_native_cluster_apply(apply_result)
        recovery_required = rollback.restore is None or not rollback.restore.ok
        final_state = "recovery_required" if recovery_required else "rolled_back"
        try:
            store.update_varac_native_apply_journal(
                entry_id,
                state=final_state,
                expected_states=(journal_state,),
                backup_manifest=_backup_payload(apply_result.backup),
                error=str(exc),
                durable=True,
            )
        except Exception:
            recovery_required = True
        return VarACNativeExternalSession(
            journal_id=entry_id,
            plan=plan,
            apply_result=rollback,
            observed={},
            needs_recovery=recovery_required,
            error=str(exc),
        )
    return VarACNativeExternalSession(
        journal_id=entry_id,
        plan=plan,
        apply_result=apply_result,
        observed=observed,
    )


def mark_varac_native_fio_committed(
    store: MultiRadioStore,
    session: VarACNativeExternalSession,
) -> Mapping[str, Any]:
    """Mark FIO state inside the caller's active guided-save transaction."""

    if not session.ok:
        raise ValueError("A verified external VarAC session is required before CommsPlan commit.")
    return store.update_varac_native_apply_journal(
        session.journal_id,
        state="fio_committed",
        expected_states=("external_applied",),
        observed=session.observed,
        backup_manifest=_backup_payload(session.apply_result.backup),
        durable=False,
    )


def complete_varac_native_external_session(
    store: MultiRadioStore,
    session: VarACNativeExternalSession,
) -> Mapping[str, Any]:
    """Complete the durable journal after the guided DB transaction commits."""

    if not session.ok:
        raise ValueError("A verified external VarAC session is required before completion.")
    return store.update_varac_native_apply_journal(
        session.journal_id,
        state="complete",
        expected_states=("fio_committed",),
        observed=session.observed,
        backup_manifest=_backup_payload(session.apply_result.backup),
        durable=True,
    )


def rollback_varac_native_external_session(
    store: MultiRadioStore,
    session: VarACNativeExternalSession,
    *,
    error: str = "CommsPlan guided save did not commit.",
) -> VarACNativeTransactionResult:
    """Compensate a verified external apply after FIO save cancellation/failure."""

    journal = store.get_varac_native_apply_journal(session.journal_id)
    if journal is None:
        raise ValueError(
            f"Native VarAC journal {session.journal_id} is unavailable; rollback was not attempted."
        )
    journal_state = str(journal.get("state") or "").strip().lower()
    if journal_state == "rolled_back":
        # Retry/review recovery can converge on the same session through more
        # than one UI cleanup path.  The first rollback owns the filesystem
        # restore; later requests are an idempotent no-op.
        return VarACNativeTransactionResult(
            journal_id=session.journal_id,
            apply_result=session.apply_result,
            committed=False,
            needs_recovery=False,
        )
    if journal_state in {"fio_committed", "complete"}:
        # Once the FIO transaction owns the result, restoring the old native
        # files would split committed database and application state.  Treat a
        # late cleanup request as already resolved without touching the files.
        return VarACNativeTransactionResult(
            journal_id=session.journal_id,
            apply_result=session.apply_result,
            committed=True,
            needs_recovery=False,
        )
    if journal_state == "recovery_required":
        return VarACNativeTransactionResult(
            journal_id=session.journal_id,
            apply_result=session.apply_result,
            committed=False,
            needs_recovery=True,
            error=str(journal.get("error") or error),
        )
    if journal_state != "external_applied":
        raise ValueError(
            "Native VarAC rollback requires an external_applied journal; "
            f"found {journal_state or 'unknown'}. No files were changed."
        )

    if session.apply_result is None:
        return VarACNativeTransactionResult(
            journal_id=session.journal_id,
            apply_result=None,
            needs_recovery=session.needs_recovery,
            error=session.error or error,
        )
    rollback = rollback_varac_native_cluster_apply(session.apply_result)
    recovery_required = rollback.restore is None or not rollback.restore.ok
    final_state = "recovery_required" if recovery_required else "rolled_back"
    store.update_varac_native_apply_journal(
        session.journal_id,
        state=final_state,
        expected_states=("external_applied",),
        observed=session.observed,
        backup_manifest=_backup_payload(session.apply_result.backup),
        error=error,
        durable=True,
    )
    return VarACNativeTransactionResult(
        journal_id=session.journal_id,
        apply_result=rollback,
        committed=False,
        needs_recovery=recovery_required,
        error=error,
    )


def apply_varac_native_transaction(
    *,
    store: MultiRadioStore,
    plan: VarACNativeClusterPlan,
    backup_root: Path,
    persist: Callable[[MultiRadioStore, Mapping[str, Any]], Any],
    running_checker: Callable[[VarACNativeMemberPlan], bool] | None = None,
    journal_id: str | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> VarACNativeTransactionResult:
    """Apply one reviewed plan and FIO mutation with compensating rollback."""
    session = begin_varac_native_external_apply(
        store=store,
        plan=plan,
        backup_root=backup_root,
        running_checker=running_checker,
        journal_id=journal_id,
        failure_injector=failure_injector,
    )
    if not session.ok:
        return VarACNativeTransactionResult(
            journal_id=session.journal_id,
            apply_result=session.apply_result,
            needs_recovery=session.needs_recovery,
            error=session.error,
        )

    persisted: Any = None
    try:
        if failure_injector is not None:
            failure_injector("before_fio_transaction")
        with store.guided_save_transaction() as transaction:
            persisted = persist(store, session.observed)
            mark_varac_native_fio_committed(store, session)
            if failure_injector is not None:
                failure_injector("before_fio_commit")
            transaction.complete()
    except Exception as exc:
        return rollback_varac_native_external_session(
            store,
            session,
            error=str(exc),
        )

    try:
        if failure_injector is not None:
            failure_injector("after_fio_commit")
        complete_varac_native_external_session(store, session)
    except Exception as exc:
        # External and FIO state are already committed.  Leave fio_committed
        # durable so bounded startup recovery can finish, never roll forward.
        return VarACNativeTransactionResult(
            journal_id=session.journal_id,
            apply_result=session.apply_result,
            persisted=persisted,
            committed=True,
            needs_recovery=True,
            error=f"Native apply committed; journal completion is pending: {exc}",
        )
    return VarACNativeTransactionResult(
        journal_id=session.journal_id,
        apply_result=session.apply_result,
        persisted=persisted,
        committed=True,
    )


def recover_unfinished_varac_native_applies(
    store: MultiRadioStore,
) -> Tuple[Mapping[str, Any], ...]:
    """Resolve bounded crash states without applying any new native settings."""

    resolved: list[Mapping[str, Any]] = []
    for row in store.list_unfinished_varac_native_applies():
        entry_id = str(row.get("id", "") or "")
        state = str(row.get("state", "") or "").strip().lower()
        if not entry_id or state == "recovery_required":
            continue
        if state == "fio_committed":
            resolved.append(
                store.update_varac_native_apply_journal(
                    entry_id,
                    state="complete",
                    expected_states=("fio_committed",),
                    observed=row.get("observed"),
                    backup_manifest=row.get("backup_manifest"),
                    durable=True,
                )
            )
            continue
        if state == "pending":
            resolved.append(
                store.update_varac_native_apply_journal(
                    entry_id,
                    state="rolled_back",
                    expected_states=("pending",),
                    error="Recovered pending intent before any journaled external promotion.",
                    durable=True,
                )
            )
            continue
        backup = _backup_from_payload(row.get("backup_manifest"))
        if backup is None:
            resolved.append(
                store.update_varac_native_apply_journal(
                    entry_id,
                    state="recovery_required",
                    expected_states=(state,),
                    error="Native backup evidence is unavailable; automatic rollback was not attempted.",
                    durable=True,
                )
            )
            continue
        restore = restore_config_backup(backup)
        next_state = "rolled_back" if restore.ok else "recovery_required"
        resolved.append(
            store.update_varac_native_apply_journal(
                entry_id,
                state=next_state,
                expected_states=(state,),
                observed=row.get("observed"),
                backup_manifest=row.get("backup_manifest"),
                error=(
                    "Recovered unfinished native apply from its exact backup."
                    if restore.ok
                    else "Native rollback is incomplete; operator recovery is required."
                ),
                durable=True,
            )
        )
    return tuple(resolved)


def _writer_key(plan: VarACNativeClusterPlan) -> str:
    return ":".join(
        (
            "varac",
            plan.version,
            plan.platform,
            plan.operation,
            plan.capability.layout_fingerprint,
            plan.capability.vara_layout_fingerprint,
        )
    )


def _desired_evidence(plan: VarACNativeClusterPlan) -> Mapping[str, Any]:
    return {
        "plan_fingerprint": plan.plan_fingerprint,
        "shared_db_path": plan.shared_db_path,
        "shared_bbs_path": plan.shared_bbs_path,
        "shared_bbs_archive_path": plan.shared_bbs_archive_path,
        "managed_directories": [str(path) for path in plan.managed_directories],
        "ptt_lock_enabled": bool(plan.ptt_lock_enabled),
        "email_gateway_sender_member_id": plan.email_gateway_sender_member_id,
        "members": [
            {
                "member_id": member.member_id,
                "varac_ini": str(member.target_path),
                "vara_ini": str(member.vara_target_path),
                "vara_runtime": str(member.vara_target_runtime_folder),
                "launch_argv": list(member.launch_command),
            }
            for member in plan.members
        ],
    }


def _observed_evidence(
    plan: VarACNativeClusterPlan,
    result: VarACNativeApplyResult,
) -> Mapping[str, Any]:
    files = [member.target_path for member in plan.members]
    for member in plan.members:
        files.extend(
            path
            for path in sorted(member.vara_target_runtime_folder.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
    digests = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    joined = "\n".join(f"{path}:{digests[path]}" for path in sorted(digests))
    return {
        "plan_fingerprint": plan.plan_fingerprint,
        "observed_fingerprint": hashlib.sha256(joined.encode("utf-8")).hexdigest(),
        "target_digests": digests,
        "created_directories": [str(path) for path in result.created_directories],
        "apply_items": [asdict(item) for item in result.items],
    }


def _backup_payload(backup: ConfigBackupResult | None) -> Mapping[str, Any]:
    return asdict(backup) if backup is not None else {}


def _backup_from_payload(value: Any) -> Optional[ConfigBackupResult]:
    if not isinstance(value, Mapping):
        return None
    try:
        items = tuple(ConfigBackupItem(**dict(item)) for item in value.get("items", ()))
        return ConfigBackupResult(
            backup_dir=str(value["backup_dir"]),
            reason=str(value["reason"]),
            created_at=str(value["created_at"]),
            items=items,
            manifest_path=str(value["manifest_path"]),
        )
    except Exception:
        return None
