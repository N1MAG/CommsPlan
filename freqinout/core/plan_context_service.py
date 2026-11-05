from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.runtime_policy_selection_service import DurableRuntimeSelectionService
from freqinout.core.shared_state import (
    AssignedPlan,
    FrequencyPlan,
    RadioProfile,
    RuntimePolicy,
)
from freqinout.core.shared_state_persistence import (
    SharedStatePersistenceAdapter,
    SharedStateSnapshot,
)


@dataclass(frozen=True)
class PlanContext:
    radio_profile_id: str
    radio_name: str
    frequency_plan_id: Optional[str] = None
    frequency_plan_name: str = ""
    assigned_plan_id: Optional[str] = None
    assignment_category: str = ""
    scheduler_mode: str = ""
    temporary_override: bool = False
    runtime_active: bool = False
    runtime_primary: bool = False
    operator_suppressed: bool = False
    scheduler_enabled: bool = False
    messages_enabled: bool = False
    map_enabled: bool = False
    launch_enabled: bool = False
    net_control_enabled: bool = False
    receive_only: bool = False
    source_ref_count: int = 0
    schedule_ref_count: int = 0
    frequency_ref_count: int = 0
    group_ref_count: int = 0
    current_frequency: str = ""
    next_target: str = ""
    scheduler_state: str = "unknown"
    top_blocker: str = ""

    @property
    def radio_label(self) -> str:
        return self.radio_name or self.radio_profile_id or "No radio"

    @property
    def plan_label(self) -> str:
        return self.frequency_plan_name or self.frequency_plan_id or "No assigned Frequency Plan"

    @property
    def summary_label(self) -> str:
        return f"{self.radio_label} - {self.plan_label}"

    @property
    def scheduler_participating(self) -> bool:
        return self.runtime_active and not self.operator_suppressed and self.scheduler_enabled


@dataclass(frozen=True)
class PlanContextSnapshot:
    contexts: tuple[PlanContext, ...]
    selected_radio_id: Optional[str]
    primary_runtime_radio_id: Optional[str]
    active_runtime_radio_ids: tuple[str, ...]
    startup_mode: str
    warnings: tuple[str, ...] = ()

    @property
    def primary_context(self) -> Optional[PlanContext]:
        if not self.primary_runtime_radio_id:
            return None
        return self.context_for_radio(self.primary_runtime_radio_id)

    def context_for_radio(self, radio_profile_id: Optional[str]) -> Optional[PlanContext]:
        if not radio_profile_id:
            return None
        for context in self.contexts:
            if context.radio_profile_id == radio_profile_id:
                return context
        return None

    @property
    def active_contexts(self) -> tuple[PlanContext, ...]:
        active = set(self.active_runtime_radio_ids)
        return tuple(context for context in self.contexts if context.radio_profile_id in active)


class PlanContextService:
    """Read-only plan/radio context composed from shared multi-rig state."""

    def __init__(
        self,
        store: Optional[MultiRadioStore] = None,
        *,
        selection_service: Optional[DurableRuntimeSelectionService] = None,
    ) -> None:
        self.store = store or MultiRadioStore()
        self.selection_service = selection_service
        self._cached: Optional[PlanContextSnapshot] = None

    def invalidate(self) -> None:
        self._cached = None

    def snapshot(self, *, refresh: bool = False) -> PlanContextSnapshot:
        if self._cached is not None and not refresh:
            return self._cached
        shared = self._shared_snapshot()
        selection = shared.selection_state
        contexts = tuple(self._context_from_radio(shared, radio) for radio in shared.radio_profiles)
        selected_radio_id = selection.settings_radio_id or selection.primary_runtime_radio_id
        self._cached = PlanContextSnapshot(
            contexts=contexts,
            selected_radio_id=selected_radio_id,
            primary_runtime_radio_id=selection.primary_runtime_radio_id,
            active_runtime_radio_ids=selection.active_runtime_radio_ids,
            startup_mode=shared.startup_mode,
            warnings=shared.warnings,
        )
        return self._cached

    def primary_context(self, *, refresh: bool = False) -> Optional[PlanContext]:
        return self.snapshot(refresh=refresh).primary_context

    def active_contexts(self, *, refresh: bool = False) -> tuple[PlanContext, ...]:
        return self.snapshot(refresh=refresh).active_contexts

    def context_for_radio(self, radio_profile_id: Optional[str], *, refresh: bool = False) -> Optional[PlanContext]:
        return self.snapshot(refresh=refresh).context_for_radio(radio_profile_id)

    def context_for_tab(self, tab_id: str, *, refresh: bool = False) -> Optional[PlanContext]:
        snapshot = self.snapshot(refresh=refresh)
        radio_id: Optional[str] = None
        if self.selection_service is not None:
            radio_id = self.selection_service.tab_radio_id(str(tab_id))
        radio_id = radio_id or snapshot.selected_radio_id or snapshot.primary_runtime_radio_id
        return snapshot.context_for_radio(radio_id)

    def _shared_snapshot(self) -> SharedStateSnapshot:
        adapter = SharedStatePersistenceAdapter(self.store)
        shared = adapter.snapshot()
        if self.selection_service is None:
            return shared
        selection = self.selection_service.state(runtime_status=shared.runtime_status)
        return replace(shared, selection_state=selection)

    def _context_from_radio(self, shared: SharedStateSnapshot, radio: RadioProfile) -> PlanContext:
        assignment = self._assignment_for_radio(shared, radio.id)
        plan = self._plan_for_assignment(shared, assignment)
        policy = self._policy_for_radio(shared, radio.id)
        selection = shared.selection_state
        return PlanContext(
            radio_profile_id=radio.id,
            radio_name=radio.name,
            frequency_plan_id=plan.id if plan else None,
            frequency_plan_name=plan.name if plan else "",
            assigned_plan_id=assignment.id if assignment else None,
            assignment_category=assignment.assignment_category if assignment else "",
            scheduler_mode=assignment.scheduler_mode if assignment else "",
            temporary_override=assignment.temporary_override_active if assignment else False,
            runtime_active=radio.id in selection.active_runtime_radio_ids,
            runtime_primary=radio.id == selection.primary_runtime_radio_id,
            operator_suppressed=policy.operator_suppressed if policy else False,
            scheduler_enabled=policy.scheduler_enabled if policy else False,
            messages_enabled=policy.message_view_enabled if policy else False,
            map_enabled=policy.map_link_enabled if policy else False,
            launch_enabled=policy.launch_control_enabled if policy else False,
            net_control_enabled=policy.net_control_participation_enabled if policy else False,
            receive_only=assignment.receive_only if assignment else False,
            source_ref_count=len(plan.source_refs) if plan else 0,
            schedule_ref_count=len(plan.schedule_refs) if plan else 0,
            frequency_ref_count=len(plan.frequency_refs) if plan else 0,
            group_ref_count=len(plan.group_refs) if plan else 0,
            top_blocker=self._top_blocker(policy),
        )

    @staticmethod
    def _assignment_for_radio(shared: SharedStateSnapshot, radio_id: str) -> Optional[AssignedPlan]:
        for assignment in shared.assigned_plans:
            if assignment.radio_profile_id == radio_id and assignment.active:
                return assignment
        return None

    @staticmethod
    def _plan_for_assignment(shared: SharedStateSnapshot, assignment: Optional[AssignedPlan]) -> Optional[FrequencyPlan]:
        if assignment is None:
            return None
        for plan in shared.frequency_plans:
            if plan.id == assignment.frequency_plan_id:
                return plan
        return None

    @staticmethod
    def _policy_for_radio(shared: SharedStateSnapshot, radio_id: str) -> Optional[RuntimePolicy]:
        for policy in shared.runtime_policies:
            if policy.radio_profile_id == radio_id:
                return policy
        return None

    @staticmethod
    def _top_blocker(policy: Optional[RuntimePolicy]) -> str:
        if policy is None:
            return "Runtime policy unavailable."
        if policy.operator_suppressed:
            return "Radio is suppressed from runtime participation."
        if policy.scheduler_control == 'paused':
            return 'Scheduling paused — operator. Use Resume for this radio.'
        if not policy.scheduler_enabled:
            return 'The assigned Operating Model does not allow scheduling.'
        return ""
