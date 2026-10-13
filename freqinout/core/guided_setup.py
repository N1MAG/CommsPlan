from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, Tuple

from freqinout.core.config_autodiscovery import APP_DISPLAY_NAMES, RadioInstanceProposal
from freqinout.core.guided_app_config_plan import (
    GuidedAppConfigPlan,
    build_guided_external_app_config_plan,
    qualified_native_writer_for_action,
)

GUIDED_SETUP_APP_IDS: Tuple[str, ...] = (
    "flrig",
    "fldigi",
    "flmsg",
    "flamp",
    "js8call",
    "js8spotter",
    "external_js8spotter",
    "commstat",
    "varac",
)
GUIDED_SETUP_MANAGED_APP_IDS = {"flrig", "fldigi", "flmsg", "flamp", "js8call"}


SETUP_MODE_READ_ONLY = "read_only"
SETUP_MODE_MANAGED = "managed"

LANE_JS8_ONLY = "js8_only"
LANE_FAST_LIGHT = "fast_light"
LANE_TRI_MODE = "tri_mode"
LANE_CUSTOM_MIX = "custom"
LANE_VARAC = "varac"
LANE_VARAC_CLUSTER = "varac_cluster"
LANE_SDR_OBSERVER = "sdr_observer"

CONTROL_FLRIG = "flrig"
CONTROL_RIGCTLD = "rigctld"
CONTROL_JS8CALL = "js8call"
CONTROL_NONE = "none"
CONTROL_LATER = "later"

SCHEDULE_EXISTING_PLAN = "existing_plan"
SCHEDULE_JS8_STANDARD = "js8_standard"
SCHEDULE_DAILY_NO_NETS = "daily_no_nets"
SCHEDULE_DAILY_PLUS_NETS = "daily_plus_nets"
SCHEDULE_SOP_CONDITION = "sop_condition"
SCHEDULE_NONE = "no_schedule"

NET_COMPONENT_NO_NETS = "no_nets"

APP_INSTANCE_EXISTING = "existing_app_instance"
APP_INSTANCE_MANAGED = "managed_app_instance"
APP_INSTANCE_MANUAL = "manual_app_instance"


@dataclass(frozen=True)
class GuidedSetupChoice:
    choice_id: str
    label: str
    recommended: bool = False
    detail: str = ""


@dataclass(frozen=True)
class GuidedSetupStep:
    step_id: str
    title: str
    prompt: str
    choices: Tuple[GuidedSetupChoice, ...] = field(default_factory=tuple)
    status: str = "needs_input"
    hint: str = ""
    advanced_available: bool = False


@dataclass(frozen=True)
class GuidedSetupProposalItem:
    item_id: str
    title: str
    summary: str
    status: str = "ready"
    writes_external_config: bool = False
    requires_backup: bool = False


@dataclass(frozen=True)
class GuidedScheduleComponent:
    component_id: str
    label: str
    component_type: str
    sentinel: bool = False
    detail: str = ""


@dataclass(frozen=True)
class GuidedSetupBlueprint:
    lane: str
    setup_mode: str
    radio_label: str
    selected_apps: Tuple[str, ...]
    control_route: str
    schedule_choices: Tuple[GuidedSetupChoice, ...]
    net_components: Tuple[GuidedScheduleComponent, ...]
    steps: Tuple[GuidedSetupStep, ...]
    proposal_items: Tuple[GuidedSetupProposalItem, ...]
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def writes_external_config(self) -> bool:
        return any(item.writes_external_config for item in self.proposal_items)

    @property
    def backup_required(self) -> bool:
        return any(item.requires_backup for item in self.proposal_items)


@dataclass(frozen=True)
class GuidedSetupCapabilityPolicy:
    lane: str
    visible_apps: Tuple[str, ...]
    hidden_apps: Tuple[str, ...]
    control_routes: Tuple[str, ...]
    default_control_route: str
    fio_frequency_control_allowed: bool
    scheduler_assignment_allowed: bool
    qsy_controls_visible: bool
    external_writes_allowed: bool
    external_writes_require_backup: bool
    read_only_notes: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GuidedSetupPreview:
    lines: Tuple[str, ...]
    control_summary: str
    guided_path: str
    schedule_summary: str
    backup_required: bool
    qsy_controls_visible: bool
    scheduler_assignment_allowed: bool


@dataclass(frozen=True)
class GuidedSetupFieldVisibilityInput:
    setup_type_choice: str = ""
    backend: str = CONTROL_FLRIG
    device_class: str = "tx_rx"
    use_flrig: bool = False
    use_fldigi: bool = False
    use_flmsg: bool = False
    use_flamp: bool = False
    use_js8call: bool = False
    use_js8spotter: bool = False
    use_commstat: bool = False
    use_varac: bool = False
    optional_open: bool = False


@dataclass(frozen=True)
class GuidedSetupFieldVisibility:
    setup_started: bool
    observer_mode: bool
    flrig_fields: bool
    rigctld_fields: bool
    js8_fields: bool
    observer_fields: bool
    fldigi_fields: bool
    flmsg_fields: bool
    flamp_fields: bool
    varac_fields: bool
    optional_fields: bool
    technical_identity_fields: bool
    software_choices: bool
    configure_automatically: bool
    connection_group: bool
    setup_steps: bool


@dataclass(frozen=True)
class GuidedSetupWizardView:
    current_step_id: str
    current_index: int
    steps: Tuple[Tuple[str, str], ...]
    previous_label: str
    next_label: str
    detail: str
    visible_sections: Tuple[str, ...]
    can_go_back: bool
    can_go_next: bool


@dataclass(frozen=True)
class GuidedSetupLanePreset:
    lane: str
    device_class: str
    backend: str
    selected_apps: Tuple[str, ...]

    @property
    def app_map(self) -> Mapping[str, bool]:
        selected = set(self.selected_apps)
        return {app_id: app_id in selected for app_id in GUIDED_SETUP_APP_IDS}


@dataclass(frozen=True)
class GuidedSetupFlowItem:
    item_id: str
    title: str
    detail: str
    status: str = "ready"


@dataclass(frozen=True)
class GuidedScheduleDecision:
    status: str
    step_detail: str
    status_text: str
    review_text: str


@dataclass(frozen=True)
class GuidedAutofillReview:
    status_text: str
    visible_lines: Tuple[str, ...]
    detail_lines: Tuple[str, ...]


@dataclass(frozen=True)
class GuidedSetupSession:
    blueprint: GuidedSetupBlueprint
    answers: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def answer_map(self) -> Mapping[str, str]:
        return dict(self.answers)

    @property
    def current_step(self) -> GuidedSetupStep | None:
        return guided_setup_current_step(self.blueprint, self.answer_map)

    @property
    def ready_for_review(self) -> bool:
        return self.current_step is None


def start_guided_setup_session(blueprint: GuidedSetupBlueprint) -> GuidedSetupSession:
    return GuidedSetupSession(blueprint=blueprint)


def guided_setup_current_step(
    blueprint: GuidedSetupBlueprint,
    answers: Mapping[str, str] | None = None,
) -> GuidedSetupStep | None:
    answer_map = dict(answers or {})
    for step in blueprint.steps:
        if step.step_id not in answer_map:
            return step
    return None


def answer_guided_setup_step(
    session: GuidedSetupSession,
    step_id: str,
    choice_id: str,
) -> GuidedSetupSession:
    steps = tuple(session.blueprint.steps)
    step = next((candidate for candidate in steps if candidate.step_id == step_id), None)
    if step is None:
        raise ValueError(f"Unknown guided setup step: {step_id}")
    valid_choices = {choice.choice_id for choice in step.choices}
    if choice_id not in valid_choices:
        raise ValueError(f"Invalid choice '{choice_id}' for guided setup step '{step_id}'")

    answer_map = dict(session.answers)
    current = guided_setup_current_step(session.blueprint, answer_map)
    if step_id not in answer_map and current is not None and current.step_id != step_id:
        raise ValueError(
            f"Guided setup step '{step_id}' cannot be answered before current step '{current.step_id}'"
        )

    answers = dict(session.answers)
    answers[step_id] = choice_id
    step_order = [candidate.step_id for candidate in steps]
    answered_index = step_order.index(step_id)
    ordered = tuple((candidate.step_id, answers[candidate.step_id]) for candidate in steps[: answered_index + 1])
    return GuidedSetupSession(blueprint=session.blueprint, answers=ordered)


def guided_setup_review_items(
    session: GuidedSetupSession,
) -> Tuple[GuidedSetupProposalItem, ...]:
    if not session.ready_for_review:
        return tuple()
    return session.blueprint.proposal_items


def selected_app_map_for_blueprint(blueprint: GuidedSetupBlueprint) -> Mapping[str, bool]:
    selected = {app_id: False for app_id in GUIDED_SETUP_APP_IDS}
    for app_id in blueprint.selected_apps:
        selected[str(app_id)] = True
    return selected


def guided_setup_selected_apps_from_flags(
    *,
    control_backend: str = "",
    use_flrig: bool = False,
    use_fldigi: bool = False,
    use_flmsg: bool = False,
    use_flamp: bool = False,
    use_js8call: bool = False,
    use_js8spotter: bool = False,
    use_external_js8spotter: bool = False,
    use_commstat: bool = False,
    use_varac: bool = False,
) -> Tuple[str, ...]:
    """Return normalized app selections from UI flags and the control backend.

    FIO Spotter, external JS8Spotter, and CommStat are JS8Call-adjacent
    workflows in FIO; selecting any of them keeps JS8Call in the app set so
    lane inference and preview text stay stable before the UI redraw completes.
    """

    backend = _normalized_control_route(control_backend)
    apps: list[str] = []
    if use_flrig or backend == CONTROL_FLRIG:
        apps.append("flrig")
    if use_fldigi:
        apps.append("fldigi")
    if use_flmsg:
        apps.append("flmsg")
    if use_flamp:
        apps.append("flamp")
    if use_js8call or use_js8spotter or use_external_js8spotter or use_commstat or backend == CONTROL_JS8CALL:
        apps.append("js8call")
    if use_js8spotter:
        apps.append("js8spotter")
    if use_external_js8spotter:
        apps.append("external_js8spotter")
    if use_commstat:
        apps.append("commstat")
    if use_varac:
        apps.append("varac")
    return tuple(apps)


def guided_setup_app_label(app_id: str) -> str:
    """Return the operator-facing app label used by guided setup surfaces."""

    key = str(app_id or "").strip().lower()
    if key == "js8spotter":
        return "CommsPlan Spotter"
    if key == "external_js8spotter":
        return "External JS8Spotter"
    return APP_DISPLAY_NAMES.get(key, key.upper())


def infer_guided_setup_lane(
    enabled_apps: Sequence[str] = (),
    *,
    varac_selected: bool = False,
    receive_only: bool = False,
) -> str:
    """Infer the guided setup lane from app selections.

    This keeps Settings and the guided wizard from each inventing their own
    interpretation of JS8-only, Fast Light, mixed, VarAC, and observer setups.
    """

    if receive_only and not enabled_apps and not varac_selected:
        return LANE_SDR_OBSERVER
    app_set = {
        str(app or "").strip().lower()
        for app in enabled_apps
        if str(app or "").strip()
    }
    app_set.discard("external_js8spotter")
    if varac_selected and app_set <= {"varac"}:
        return LANE_VARAC
    if "varac" in app_set and app_set <= {"varac"}:
        return LANE_VARAC
    if "js8call" in app_set and ({"flrig", "fldigi", "flmsg", "flamp"} & app_set):
        return LANE_TRI_MODE
    if "js8call" in app_set:
        return LANE_JS8_ONLY
    if {"flrig", "fldigi", "flmsg", "flamp"} & app_set:
        return LANE_FAST_LIGHT
    if varac_selected:
        return LANE_VARAC
    return LANE_JS8_ONLY


def infer_guided_control_route(control_backend: str = "") -> str:
    """Normalize a saved/UI backend value into a guided setup route."""

    return _normalized_control_route(control_backend)


def guided_setup_capability_policy(blueprint: GuidedSetupBlueprint) -> GuidedSetupCapabilityPolicy:
    """Return the UI/control policy for a guided setup lane.

    This is intentionally a core helper so Settings and future setup wizard
    screens do not each rediscover which app fields or frequency controls are
    valid for a lane.
    """

    visible_apps = tuple(app for app in GUIDED_SETUP_APP_IDS if app in set(blueprint.selected_apps))
    hidden_apps = tuple(app for app in GUIDED_SETUP_APP_IDS if app not in set(visible_apps))
    lane_key = _normalize_lane(blueprint.lane)
    route_key = _normalized_control_route(blueprint.control_route)
    read_only_notes: list[str] = []

    if lane_key == LANE_VARAC:
        control_routes = (CONTROL_NONE,)
        default_route = CONTROL_NONE
        fio_control = False
        scheduler_allowed = False
        read_only_notes.append("VarAC owns scheduler/frequency control; CommsPlan monitors VarAC messages, BBS, and logs.")
    elif lane_key == LANE_VARAC_CLUSTER:
        control_routes = (CONTROL_NONE,)
        default_route = CONTROL_NONE
        fio_control = False
        scheduler_allowed = False
        read_only_notes.append("VarAC cluster setup is read/import only in guided setup.")
    elif lane_key == LANE_SDR_OBSERVER:
        control_routes = (CONTROL_NONE, CONTROL_LATER)
        default_route = CONTROL_NONE if route_key == CONTROL_NONE else CONTROL_LATER
        fio_control = False
        scheduler_allowed = False
        read_only_notes.append("Receive-only observer devices cannot be tuned by CommsPlan.")
    elif lane_key == LANE_JS8_ONLY:
        control_routes = (CONTROL_JS8CALL, CONTROL_FLRIG, CONTROL_RIGCTLD, CONTROL_NONE, CONTROL_LATER)
        default_route = route_key if route_key in control_routes else CONTROL_JS8CALL
        fio_control = default_route in {CONTROL_JS8CALL, CONTROL_FLRIG, CONTROL_RIGCTLD}
        scheduler_allowed = fio_control
    else:
        control_routes = (CONTROL_FLRIG, CONTROL_RIGCTLD, CONTROL_NONE, CONTROL_LATER)
        default_route = route_key if route_key in control_routes else CONTROL_FLRIG
        fio_control = default_route in {CONTROL_FLRIG, CONTROL_RIGCTLD}
        scheduler_allowed = fio_control

    writes_allowed = allow_external_app_writes(blueprint)
    writes_require_backup = writes_allowed and blueprint.backup_required
    return GuidedSetupCapabilityPolicy(
        lane=lane_key,
        visible_apps=visible_apps,
        hidden_apps=hidden_apps,
        control_routes=control_routes,
        default_control_route=default_route,
        fio_frequency_control_allowed=fio_control,
        scheduler_assignment_allowed=scheduler_allowed,
        qsy_controls_visible=fio_control,
        external_writes_allowed=writes_allowed,
        external_writes_require_backup=writes_require_backup,
        read_only_notes=tuple(read_only_notes),
    )


def guided_setup_field_visibility(
    blueprint: GuidedSetupBlueprint,
    state: GuidedSetupFieldVisibilityInput,
) -> GuidedSetupFieldVisibility:
    """Return Add Radio field visibility from guided setup state.

    The Settings dialog owns widgets; the core owns the product rule that a
    guided lane should expose only the fields needed for that setup path.
    """

    policy = guided_setup_capability_policy(blueprint)
    visible_apps = set(policy.visible_apps)
    setup_type_choice = str(state.setup_type_choice or "").strip()
    setup_started = bool(setup_type_choice)
    backend = _normalized_control_route(str(state.backend or ""))
    device_class = str(state.device_class or "").strip().lower()
    observer_mode = device_class == "observer"

    flrig_fields = (
        setup_started
        and not observer_mode
        and policy.fio_frequency_control_allowed
        and (backend == CONTROL_FLRIG or (bool(state.use_flrig) and "flrig" in visible_apps))
    )
    rigctld_fields = (
        setup_started
        and not observer_mode
        and policy.fio_frequency_control_allowed
        and backend == CONTROL_RIGCTLD
    )
    js8_fields = setup_started and not observer_mode and bool(state.use_js8call) and "js8call" in visible_apps
    observer_fields = setup_started and observer_mode
    fldigi_fields = setup_started and not observer_mode and bool(state.use_fldigi) and "fldigi" in visible_apps
    flmsg_fields = setup_started and not observer_mode and bool(state.use_flmsg) and "flmsg" in visible_apps
    flamp_fields = setup_started and not observer_mode and bool(state.use_flamp) and "flamp" in visible_apps
    varac_fields = setup_started and not observer_mode and bool(state.use_varac) and "varac" in visible_apps
    technical_identity_fields = setup_type_choice == "custom"
    software_choices = setup_type_choice == "custom" and not observer_mode
    configure_automatically = setup_started and not observer_mode
    connection_group = any(
        (
            flrig_fields,
            rigctld_fields,
            js8_fields,
            observer_fields,
            fldigi_fields,
            flmsg_fields,
            flamp_fields,
            varac_fields,
        )
    )
    return GuidedSetupFieldVisibility(
        setup_started=setup_started,
        observer_mode=observer_mode,
        flrig_fields=flrig_fields,
        rigctld_fields=rigctld_fields,
        js8_fields=js8_fields,
        observer_fields=observer_fields,
        fldigi_fields=fldigi_fields,
        flmsg_fields=flmsg_fields,
        flamp_fields=flamp_fields,
        varac_fields=varac_fields,
        optional_fields=bool(state.optional_open),
        technical_identity_fields=technical_identity_fields,
        software_choices=software_choices,
        configure_automatically=configure_automatically,
        connection_group=connection_group,
        setup_steps=not observer_mode,
    )


ADD_RADIO_WIZARD_STEPS: Tuple[Tuple[str, str], ...] = (
    ("radio", "Radio"),
    # ``model`` is the durable/persistence term.  The guided workflow uses
    # operator language so this step answers what FIO will do with the radio,
    # without making a schedule or database concept part of normal setup.
    ("model", "CommsPlan Behavior"),
    ("software", "Software"),
    ("connection", "Connections"),
    ("guard", "Safety"),
    ("schedule", "Schedule"),
    ("review", "Review & Save"),
)


def guided_setup_wizard_view(
    current_step_id: str,
    *,
    connection_visible: bool = True,
    software_visible: bool = True,
    radio_role: str = "transceiver",
) -> GuidedSetupWizardView:
    """Return stable UI-ready state for the Add Radio guided setup wizard.

    ``connection_visible`` and ``software_visible`` remain accepted for older
    callers, but no longer remove or renumber a step.  A step with no active
    family content explains that it is not used for the current draft.
    """

    requested = str(current_step_id or "").strip().lower()
    visible_steps = ADD_RADIO_WIZARD_STEPS
    step_ids = [step_id for step_id, _label in visible_steps]
    if requested not in step_ids:
        requested = "radio"
    current_index = step_ids.index(requested)
    previous_label = visible_steps[current_index - 1][1] if current_index > 0 else ""
    next_label = visible_steps[current_index + 1][1] if current_index < len(visible_steps) - 1 else ""
    role_key = str(radio_role or "").strip().casefold()
    observer_mode = role_key in {"observer", "receive_only", "receive-only", "sdr"}
    software_detail = (
        "Choose the receive-only software set and configure each selected instance responsibility."
        if observer_mode
        else "Choose the software set and configure each selected instance responsibility."
    )
    connection_detail = (
        "Review receiver control and receive/decode companion connections separately."
        if observer_mode
        else "Review the connections owned by each selected software component."
    )
    guard_detail = (
        "Configure Receiver Guard resources without granting PTT or transmit authority."
        if observer_mode
        else "Confirm RF Guard supported bands and shared resources before assigning a Radio Schedule."
    )
    schedule_detail = (
        "Choose a Receive Schedule for verified retuning or reminder-only operation."
        if observer_mode
        else "Choose the Radio Schedule after RF Guard details are known."
    )
    detail_by_step = {
        "radio": "Choose the radio model, name, role, and setup type.",
        "model": (
            "Choose how CommsPlan should use this receive-only radio."
            if observer_mode
            else "Choose how CommsPlan should use this radio."
        ),
        "software": software_detail,
        "connection": connection_detail,
        "guard": guard_detail,
        "schedule": schedule_detail,
        "review": "Review the complete radio, software, launch, safety, and schedule plan before saving.",
    }
    visible_sections = (requested,)
    return GuidedSetupWizardView(
        current_step_id=requested,
        current_index=current_index,
        steps=visible_steps,
        previous_label=previous_label,
        next_label=next_label,
        detail=detail_by_step.get(requested, "Review this setup step."),
        visible_sections=visible_sections,
        can_go_back=current_index > 0,
        can_go_next=current_index < len(visible_steps) - 1,
    )


def radio_proposal_for_blueprint(
    blueprint: GuidedSetupBlueprint,
    base_proposal: RadioInstanceProposal,
) -> RadioInstanceProposal:
    """Scope an existing radio proposal to the apps selected by the guided lane."""

    selected = tuple(app for app in base_proposal.enabled_apps if app in set(blueprint.selected_apps))
    return RadioInstanceProposal(
        name=base_proposal.name,
        instance_name=base_proposal.instance_name,
        index=base_proposal.index,
        enabled_apps=selected,
        ports=base_proposal.ports,
        varac_enabled=base_proposal.varac_enabled or "varac" in set(blueprint.selected_apps),
        notes=base_proposal.notes,
    )


def radio_proposals_for_blueprint(
    blueprint: GuidedSetupBlueprint,
    base_proposals: Sequence[RadioInstanceProposal],
) -> Tuple[RadioInstanceProposal, ...]:
    return tuple(radio_proposal_for_blueprint(blueprint, proposal) for proposal in base_proposals)


def allow_external_app_writes(blueprint: GuidedSetupBlueprint) -> bool:
    return blueprint.setup_mode == SETUP_MODE_MANAGED and any(
        app in GUIDED_SETUP_MANAGED_APP_IDS for app in blueprint.selected_apps
    )


def build_app_config_plan_for_blueprint(
    blueprint: GuidedSetupBlueprint,
    base_proposals: Sequence[RadioInstanceProposal],
    *,
    config_root: Path,
    app_paths: Mapping[str, str] | None = None,
    callsign: str = "",
    grid: str = "",
) -> GuidedAppConfigPlan:
    """Build the external-app plan through the guided setup policy boundary."""

    scoped_proposals = radio_proposals_for_blueprint(blueprint, base_proposals)
    include_varac = "varac" in set(blueprint.selected_apps) or any(
        proposal.varac_enabled for proposal in base_proposals
    )
    return build_guided_external_app_config_plan(
        scoped_proposals,
        config_root=config_root,
        app_paths=app_paths,
        callsign=callsign,
        grid=grid,
        include_varac=include_varac,
        allow_external_writes=allow_external_app_writes(blueprint),
        js8_control_route=blueprint.control_route,
        radio_label=blueprint.radio_label,
    )


def build_guided_setup_preview(
    blueprint: GuidedSetupBlueprint,
    plan: GuidedAppConfigPlan,
) -> GuidedSetupPreview:
    """Return compact, UI-ready review text for guided setup surfaces."""

    policy = guided_setup_capability_policy(blueprint)
    lines: list[str] = []
    lane_key = _normalize_lane(blueprint.lane)

    if lane_key == LANE_VARAC and set(blueprint.selected_apps) <= {"varac"}:
        lines.append("VarAC-only radio: CommsPlan supports BBS and message monitoring, but VarAC handles frequency scheduling.")

    control_summary = guided_setup_control_summary(policy)
    if control_summary:
        lines.append(control_summary)

    guided_path = guided_setup_path_summary(blueprint)
    if guided_path:
        lines.append(guided_path)

    schedule_summary = guided_setup_schedule_summary(blueprint)
    if schedule_summary:
        lines.append(schedule_summary)

    if plan.backup_required:
        lines.append("Backup required before CommsPlan writes app profiles.")

    fio_side_actions = [
        action
        for action in plan.actions
        if not action.writes_external_config and str(action.action_type or "") == "remember_integration"
    ]
    for action in fio_side_actions[:3]:
        lines.append("- " + action.summary)
        if str(action.app_id or "").strip().lower() == "varac":
            remembered = _remembered_varac_labels(action.details)
            if remembered:
                lines.append("  VarAC references: " + ", ".join(remembered) + ".")

    write_actions = [action for action in plan.actions if action.writes_external_config]
    for action in write_actions[:4]:
        lines.append("- " + action.summary)
    if len(write_actions) > 4:
        lines.append(f"- {len(write_actions) - 4} more app setup action(s).")

    for item in plan.review_items[:2]:
        lines.append("- " + item)

    return GuidedSetupPreview(
        lines=tuple(line for line in lines if str(line or "").strip()),
        control_summary=control_summary,
        guided_path=guided_path,
        schedule_summary=schedule_summary,
        backup_required=plan.backup_required,
        qsy_controls_visible=policy.qsy_controls_visible,
        scheduler_assignment_allowed=policy.scheduler_assignment_allowed,
    )


def guided_app_config_review_lines(plan: GuidedAppConfigPlan) -> Tuple[str, ...]:
    """Return a compact operator review of FIO-side vs external app setup."""

    write_actions = [action for action in plan.actions if action.writes_external_config]
    remember_actions = [action for action in plan.actions if not action.writes_external_config]
    lines: list[str] = []
    if write_actions:
        qualified_actions = [
            action for action in write_actions
            if qualified_native_writer_for_action(action) is not None
        ]
        operator_actions = [
            action for action in write_actions
            if qualified_native_writer_for_action(action) is None
        ]
        apps = []
        seen: set[str] = set()
        for action in write_actions:
            label = guided_setup_app_label(action.app_id)
            if label and label not in seen:
                apps.append(label)
                seen.add(label)
        app_text = ", ".join(apps[:4]) if apps else "external apps"
        if len(apps) > 4:
            app_text = f"{app_text}, +{len(apps) - 4} more"
        if qualified_actions:
            lines.append(
                f"App Configuration: Final Save can apply {len(qualified_actions)} exact qualified profile "
                f"change(s) after backup and readback; {len(operator_actions)} action(s) remain operator-managed."
            )
        else:
            lines.append(
                f"App Configuration: Final Save will remember {app_text}; all {len(operator_actions)} native "
                "profile action(s) require the operator because no exact writer is qualified."
            )
    elif remember_actions:
        apps = []
        seen = set()
        for action in remember_actions:
            label = guided_setup_app_label(action.app_id)
            if label and label not in seen:
                apps.append(label)
                seen.add(label)
        app_text = ", ".join(apps) if apps else "selected apps"
        lines.append(f"App Configuration: save will remember {app_text} paths; no external app files will be changed.")
    else:
        lines.append("App Configuration: no external app setup changes.")

    if any(str(action.app_id or "").strip().lower() == "varac" for action in plan.actions) or any(
        "VarAC" in str(item or "") for item in plan.review_items
    ):
        lines.append("VarAC: read/import only; CommsPlan will not change VarAC.ini or VarAC.db.")

    for item in plan.review_items:
        text = str(item or "").strip()
        if text and text not in lines:
            lines.append(text)
    for action in write_actions[:5]:
        summary = str(action.summary or "").strip()
        if summary:
            lines.append(summary)
    if len(write_actions) > 5:
        lines.append(f"{len(write_actions) - 5} more app configuration action(s).")
    return tuple(lines)


def normalize_guided_radio_profile_payload(
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    """Return a capability-safe radio profile payload for guided setup saves.

    The profile store uses ``manual`` as the persisted runtime backend when
    FIO should not tune a radio. Guided setup uses ``none`` as the policy term.
    This helper keeps that translation explicit and prevents a VarAC-only radio
    from inheriting FLRig frequency control from default UI state.
    """

    values = dict(payload)
    use_varac = _truthy(values.get("use_varac"))
    backend = _normalized_control_route(str(values.get("control_backend", "") or ""))
    use_fast_light = any(_truthy(values.get(key)) for key in ("use_fldigi", "use_flmsg", "use_flamp"))
    use_flrig = _truthy(values.get("use_flrig"))
    use_js8 = any(_truthy(values.get(key)) for key in ("use_js8call", "use_js8spotter", "use_commstat"))
    varac_only_without_explicit_flrig = use_varac and not use_fast_light and not use_js8 and (
        backend != CONTROL_FLRIG or not use_flrig
    )
    if varac_only_without_explicit_flrig:
        values["control_backend"] = "manual"
        values["use_scheduler"] = False
        values["scheduler_enabled"] = False
        values.pop("guided_frequency_plan_id", None)
        values.pop("guided_open_plan_manager_after_save", None)
        values["use_flrig"] = False
        values["use_fldigi"] = False
        values["use_flmsg"] = False
        values["use_flamp"] = False
        values["use_js8call"] = False
        values["use_js8spotter"] = False
        values["use_commstat"] = False
    return values


def guided_setup_control_summary(policy: GuidedSetupCapabilityPolicy) -> str:
    if not policy.qsy_controls_visible:
        return "Control: monitor/import only. CommsPlan will not show scheduler/QSY controls for this radio."
    if policy.default_control_route == CONTROL_JS8CALL:
        return "Control: JS8Call owns the radio/CAT route for CommsPlan scheduler and QSY actions."
    if policy.default_control_route == CONTROL_FLRIG:
        return "Control: FLRig owns the radio route for CommsPlan scheduler and QSY actions."
    if policy.default_control_route == CONTROL_RIGCTLD:
        return "Control: RigCtlD owns the radio route for CommsPlan scheduler and QSY actions."
    return ""


def guided_setup_path_summary(blueprint: GuidedSetupBlueprint) -> str:
    guided_steps = [
        str(step.title or "").strip()
        for step in blueprint.steps
        if str(step.title or "").strip()
    ]
    if not guided_steps:
        return ""
    return "Guided path: " + " -> ".join(guided_steps[:5]) + "."


def guided_setup_schedule_summary(blueprint: GuidedSetupBlueprint) -> str:
    """Return operator-facing schedule choices for the selected setup lane."""

    lane_key = _normalize_lane(blueprint.lane)
    choice_ids = {choice.choice_id for choice in blueprint.schedule_choices}
    if choice_ids == {SCHEDULE_NONE} and lane_key in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        return "Schedule: monitor only. VarAC keeps its own scheduler and CommsPlan will not offer QSY controls."

    labels: list[str] = []
    if SCHEDULE_EXISTING_PLAN in choice_ids:
        labels.append("existing Frequency Plan")
    if SCHEDULE_JS8_STANDARD in choice_ids:
        labels.append("JS8Call Standard")
    if SCHEDULE_DAILY_NO_NETS in choice_ids:
        labels.append("Daily with No Nets")
    if SCHEDULE_DAILY_PLUS_NETS in choice_ids:
        labels.append("Daily + Nets")
    if SCHEDULE_SOP_CONDITION in choice_ids:
        labels.append("SOP condition plan")
    if SCHEDULE_NONE in choice_ids:
        labels.append("monitor only")
    if not labels:
        return ""
    return "Schedule choices: " + ", ".join(labels) + "."


def guided_setup_schedule_decision(
    *,
    scheduler_assignment_allowed: bool,
    selected_plan_name: str = "",
    plan_count: int = 0,
    open_plan_manager: bool = False,
    selected_schedule_choice: str = "",
) -> GuidedScheduleDecision:
    """Return one consistent schedule decision for guided setup UI surfaces."""

    selected = str(selected_plan_name or "").strip()
    schedule_choice = str(selected_schedule_choice or "").strip()
    try:
        count = int(plan_count or 0)
    except Exception:
        count = 0
    if not scheduler_assignment_allowed:
        step_text = "Monitor/import only; the external app keeps its schedule."
        text = "CommsPlan will monitor/import this radio; the external app keeps its own frequency schedule."
        return GuidedScheduleDecision(
            status="ready",
            step_detail=step_text,
            status_text=text,
            review_text="No CommsPlan-controlled schedule or QSY controls will be saved for this radio.",
        )
    if schedule_choice == SCHEDULE_NONE:
        text = "No Frequency Plan will be assigned now. This radio is ready; a plan can be assigned later."
        return GuidedScheduleDecision(
            status="complete_later",
            step_detail=text,
            status_text=text,
            review_text="No Frequency Plan assigned during setup.",
        )
    if schedule_choice and schedule_choice != SCHEDULE_EXISTING_PLAN:
        path_labels = {
            SCHEDULE_JS8_STANDARD: "JS8Call Standard",
            SCHEDULE_DAILY_NO_NETS: "Daily with No Nets",
            SCHEDULE_DAILY_PLUS_NETS: "Daily + Nets",
            SCHEDULE_SOP_CONDITION: "SOP condition plan",
        }
        path_label = path_labels.get(schedule_choice, "Frequency Plan")
        text = f"Plan Builder will open after save to build or choose a {path_label} plan."
        return GuidedScheduleDecision(
            status="needs_input",
            step_detail=f"Build or choose a {path_label} plan in Plan Builder after save.",
            status_text=text,
            review_text=f"Open Plan Builder after save for {path_label}.",
        )
    if selected:
        return GuidedScheduleDecision(
            status="review",
            step_detail=f"Selected plan: {selected}. RF Guard runs during assignment.",
            status_text=f"CommsPlan will assign '{selected}' to this radio with RF Guard.",
            review_text=f"Assign '{selected}' with RF Guard.",
        )
    if bool(open_plan_manager) or count <= 0:
        text = (
            "Plan Builder will open after save so you can build or choose this radio's Frequency Plan."
        )
        if count <= 0:
            text = (
                "No Frequency Plans exist yet. Save this radio, then Plan Builder will open so you can build "
                "a Daily with No Nets, Net, or SOP-based plan."
            )
        return GuidedScheduleDecision(
            status="needs_input",
            step_detail="Save this radio, then build its Frequency Plan in Plan Builder.",
            status_text=text,
            review_text="Open Plan Builder after save so a Frequency Plan can be built and assigned.",
        )
    text = (
        "Choose a Frequency Plan to run the RF Guard antenna check now, or assign one later."
    )
    return GuidedScheduleDecision(
        status="needs_input",
        step_detail="Choose a Frequency Plan so RF Guard can compare it with this radio's antenna bands.",
        status_text=text,
        review_text="No Frequency Plan selected; RF Guard will compare antenna bands when a plan is assigned.",
    )


def guided_setup_autofill_review(
    *,
    filled: Sequence[str] = (),
    preserved: Sequence[str] = (),
    detection_notes: Sequence[str] = (),
    radio_apps_base_used: bool = False,
    visible_limit: int = 4,
) -> GuidedAutofillReview:
    """Return bounded, operator-readable Configure Automatically review copy."""

    filled_items = tuple(str(item or "").strip() for item in filled if str(item or "").strip())
    preserved_items = tuple(str(item or "").strip() for item in preserved if str(item or "").strip())
    notes = tuple(str(item or "").strip() for item in detection_notes if str(item or "").strip())
    limit = max(1, int(visible_limit or 4))

    def _summary_line(label: str, items: Sequence[str], *, max_names: int = 3) -> str:
        shown = tuple(items[:max_names])
        suffix = f", +{len(items) - len(shown)} more" if len(items) > len(shown) else ""
        names = ", ".join(shown)
        return f"{label} {len(items)}: {names}{suffix}" if names else f"{label} {len(items)}"

    if filled_items:
        status = "Configure Automatically found settings. Review before Save."
    else:
        status = "Configure Automatically did not find new blank fields to fill."

    detail: list[str] = []
    if filled_items:
        detail.append("Filled: " + ", ".join(filled_items))
    if preserved_items:
        detail.append("Kept existing: " + ", ".join(preserved_items))
    if radio_apps_base_used:
        detail.append("Used Radio Apps Base Folder for app detection.")
    detail.extend(notes)
    if not detail:
        detail.append("No blank fields could be filled from the current scan.")

    visible: list[str] = []
    if filled_items:
        visible.append(_summary_line("Filled", filled_items))
    if preserved_items and len(visible) < limit:
        visible.append(f"Kept existing: {len(preserved_items)} field(s) unchanged.")
    if radio_apps_base_used and len(visible) < limit:
        visible.append("Used Radio Apps Base Folder.")
    for note in notes:
        if len(visible) >= limit:
            break
        visible.append(note)
    if not visible:
        visible.append("No blank fields could be filled from the current scan.")

    remaining = len(detail) - len(visible)
    if remaining > 0:
        visible.append(f"{remaining} more detail item(s) available.")

    return GuidedAutofillReview(
        status_text=status,
        visible_lines=tuple(visible),
        detail_lines=tuple(detail),
    )


def guided_setup_flow_items(
    blueprint: GuidedSetupBlueprint,
    plan: GuidedAppConfigPlan,
    *,
    schedule_decision: GuidedScheduleDecision | None = None,
    setup_started: bool = True,
) -> Tuple[GuidedSetupFlowItem, ...]:
    """Return the setup flow as compact, UI-ready steps."""

    if not bool(setup_started):
        return (
            GuidedSetupFlowItem(
                item_id="radio",
                title="Radio",
                detail="Choose the radio or SDR model and setup type.",
                status="needs_input",
            ),
            GuidedSetupFlowItem(
                item_id="software",
                title="Software",
                detail="Choose the software this radio uses.",
                status="needs_input",
            ),
            GuidedSetupFlowItem(
                item_id="connection",
                title="Connection",
                detail="CommsPlan will show only the endpoint fields that apply after setup type is selected.",
                status="needs_input",
            ),
            GuidedSetupFlowItem(
                item_id="guard",
                title="RF Guard",
                detail="Confirm supported bands and shared RF paths before schedule assignment.",
                status="needs_input",
            ),
            GuidedSetupFlowItem(
                item_id="schedule",
                title="Schedule",
                detail="Choose or assign a Frequency Plan after RF Guard details are reviewed.",
                status="needs_input",
            ),
            GuidedSetupFlowItem(
                item_id="review",
                title="Review",
                detail="Review the completed setup before saving.",
                status="needs_input",
            ),
        )

    policy = guided_setup_capability_policy(blueprint)
    app_labels = [
        guided_setup_app_label(app_id)
        for app_id in blueprint.selected_apps
        if str(app_id or "").strip()
    ]
    control_summary = guided_setup_control_summary(policy)
    schedule_summary = guided_setup_schedule_summary(blueprint)
    if schedule_decision is not None:
        schedule_detail = str(schedule_decision.step_detail or schedule_decision.status_text or "").strip()
        schedule_status = str(schedule_decision.status or "needs_input").strip().lower()
    elif policy.scheduler_assignment_allowed:
        schedule_detail = "Choose or assign a Frequency Plan after saving this radio."
        schedule_status = "needs_input"
    else:
        schedule_detail = (
            schedule_summary.removeprefix("Schedule choices: ").removeprefix("Schedule: ").rstrip(".")
            if schedule_summary
            else "Monitor/import only. CommsPlan will not control frequency for this radio."
        )
        schedule_status = "ready"
    if plan.backup_required:
        review_detail = "Review detected settings, then back up and apply the app configuration changes."
        review_status = "backup"
    elif plan.manual_review_required:
        review_detail = "Review the detected paths and save the CommsPlan-side integration settings."
        review_status = "review"
    else:
        review_detail = "Save the radio after the required fields look correct."
        review_status = "ready"
    radio_label_text = str(blueprint.radio_label or "").strip()
    radio_ready = bool(radio_label_text and radio_label_text.casefold() != "radio")
    return (
        GuidedSetupFlowItem(
            item_id="radio",
            title="Radio",
            detail=radio_label_text if radio_ready else "Choose the radio or SDR model.",
            status="ready" if radio_ready else "needs_input",
        ),
        GuidedSetupFlowItem(
            item_id="software",
            title="Software",
            detail=", ".join(app_labels) if app_labels else "Choose the software this radio uses.",
            status="ready" if app_labels else "needs_input",
        ),
        GuidedSetupFlowItem(
            item_id="connection",
            title="Connection",
            detail=control_summary.removeprefix("Control: ").rstrip(".") if control_summary else "Enter the endpoint CommsPlan should use.",
            status="review" if plan.manual_review_required else "ready",
        ),
        GuidedSetupFlowItem(
            item_id="guard",
            title="RF Guard",
            detail="Supported bands and shared RF paths are checked before schedule assignment.",
            status="ready",
        ),
        GuidedSetupFlowItem(
            item_id="schedule",
            title="Schedule",
            detail=schedule_detail,
            status=schedule_status,
        ),
        GuidedSetupFlowItem(
            item_id="review",
            title="Review",
            detail=review_detail,
            status=review_status,
        ),
    )


def guided_setup_next_flow_item(
    blueprint: GuidedSetupBlueprint,
    plan: GuidedAppConfigPlan,
    *,
    schedule_decision: GuidedScheduleDecision | None = None,
    setup_started: bool = True,
) -> GuidedSetupFlowItem:
    """Return the next operator-visible setup item that needs attention."""

    items = guided_setup_flow_items(
        blueprint,
        plan,
        schedule_decision=schedule_decision,
        setup_started=setup_started,
    )
    for item in items:
        if str(item.status or "ready").strip().lower() != "ready":
            return item
    return items[-1] if items else GuidedSetupFlowItem(
        item_id="review",
        title="Review",
        detail="Review the setup before saving.",
        status="ready",
    )


def guided_setup_next_action_text(
    blueprint: GuidedSetupBlueprint,
    plan: GuidedAppConfigPlan,
    *,
    schedule_decision: GuidedScheduleDecision | None = None,
    setup_started: bool = True,
) -> str:
    """Return one concise instruction for the next guided setup action."""

    item = guided_setup_next_flow_item(
        blueprint,
        plan,
        schedule_decision=schedule_decision,
        setup_started=setup_started,
    )
    title = str(item.title or "Review").strip()
    detail = str(item.detail or "").strip()
    if detail:
        return f"Next: {title} - {detail}"
    return f"Next: {title}"


def guided_setup_flow_summary_lines(
    blueprint: GuidedSetupBlueprint,
    plan: GuidedAppConfigPlan,
    *,
    schedule_decision: GuidedScheduleDecision | None = None,
    setup_started: bool = True,
) -> Tuple[str, ...]:
    """Return plain stepper lines for existing Qt labels."""

    lines = []
    for idx, item in enumerate(
        guided_setup_flow_items(
            blueprint,
            plan,
            schedule_decision=schedule_decision,
            setup_started=setup_started,
        ),
        start=1,
    ):
        status = str(item.status or "ready").replace("_", " ").title()
        lines.append(f"{idx}. {item.title}: {item.detail} ({status})")
    return tuple(lines)


def guided_setup_operator_guidance_lines(
    blueprint: GuidedSetupBlueprint,
    plan: GuidedAppConfigPlan,
) -> Tuple[str, ...]:
    """Return short, operator-facing guidance for the selected setup path."""

    policy = guided_setup_capability_policy(blueprint)
    lane_key = _normalize_lane(blueprint.lane)
    app_names = [
        guided_setup_app_label(app_id)
        for app_id in blueprint.selected_apps
        if str(app_id or "").strip()
    ]
    lines: list[str] = []

    if app_names:
        lines.append("CommsPlan will configure only: " + ", ".join(app_names) + ".")
    else:
        lines.append("CommsPlan will set up this radio as a receive-only monitor.")

    if lane_key == LANE_JS8_ONLY:
        lines.append("JS8Call-only setup hides FLDigi, FLMsg, and FLAmp fields.")
        if policy.default_control_route == CONTROL_JS8CALL:
            lines.append("Choose the JS8Call profile, API port, and message files if CommsPlan finds more than one.")
    elif lane_key in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        lines.append("VarAC stays monitor/import only here; VarAC keeps its own frequency scheduler.")
    elif lane_key == LANE_TRI_MODE:
        lines.append("Choose the matching FastLight, JS8Call, and VarAC app set for this radio.")
    elif lane_key == LANE_FAST_LIGHT:
        lines.append("Choose the matching FLRig/FLDigi/Fast Light app set for this radio.")

    if policy.scheduler_assignment_allowed:
        lines.append("Schedule assignment can be reviewed with RF Guard before the radio follows it.")
    else:
        lines.append("CommsPlan will not show QSY or scheduler controls for this setup.")

    if plan.backup_required:
        lines.append("External app settings require a backup before CommsPlan applies changes.")
    elif plan.manual_review_required:
        lines.append("Review the detected paths, then save the CommsPlan integration settings.")
    else:
        lines.append("Review the fields, then save the radio profile.")

    return tuple(lines)


def generated_radio_label(
    hamlib_short_name: str,
    existing_labels: Sequence[str] = (),
    *,
    fallback_prefix: str = "Radio",
) -> str:
    """Return a stable operator-facing radio label without touching saved config."""

    base = _clean_radio_label(hamlib_short_name) or _clean_radio_label(fallback_prefix) or "Radio"
    existing = {_label_match_key(value) for value in existing_labels if str(value or "").strip()}
    if _label_match_key(base) not in existing:
        return base
    index = 1
    while True:
        candidate = f"{base} {index}"
        if _label_match_key(candidate) not in existing:
            return candidate
        index += 1


def guided_radio_label_base(
    radio_payload: Mapping[str, object] | None,
    *,
    fallback_prefix: str = "Radio",
) -> str:
    """Return the short radio model label to feed into generated_radio_label."""

    payload = radio_payload or {}
    model_name = _clean_radio_label(payload.get("model_name", ""))
    if model_name:
        return model_name
    display_name = _clean_radio_label(payload.get("display_name", ""))
    manufacturer = _clean_radio_label(payload.get("manufacturer", ""))
    if display_name and manufacturer:
        prefix = f"{manufacturer} "
        if display_name.lower().startswith(prefix.lower()):
            short_display = _clean_radio_label(display_name[len(prefix) :])
            if short_display:
                return short_display
    return display_name or _clean_radio_label(fallback_prefix) or "Radio"


def guided_setup_apps_for_lane(lane: str) -> Tuple[str, ...]:
    lane_key = _normalize_lane(lane)
    if lane_key == LANE_JS8_ONLY:
        return ("js8call",)
    if lane_key == LANE_FAST_LIGHT:
        return ("flrig", "fldigi", "flmsg", "flamp")
    if lane_key == LANE_TRI_MODE:
        return ("flrig", "fldigi", "flmsg", "flamp", "js8call", "varac")
    if lane_key in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        return ("varac",)
    if lane_key == LANE_SDR_OBSERVER:
        return tuple()
    return tuple()


def guided_setup_lane_preset(lane: str) -> GuidedSetupLanePreset:
    """Return the UI-safe defaults for selecting a guided setup lane."""

    lane_key = _normalize_lane(lane)
    if lane_key == LANE_SDR_OBSERVER:
        return GuidedSetupLanePreset(
            lane=lane_key,
            device_class="observer",
            backend="manual",
            selected_apps=tuple(),
        )
    if lane_key == LANE_JS8_ONLY:
        return GuidedSetupLanePreset(
            lane=lane_key,
            device_class="tx_rx",
            backend=CONTROL_JS8CALL,
            selected_apps=guided_setup_apps_for_lane(lane_key),
        )
    if lane_key in {LANE_FAST_LIGHT, LANE_TRI_MODE}:
        return GuidedSetupLanePreset(
            lane=lane_key,
            device_class="tx_rx",
            backend=CONTROL_FLRIG,
            selected_apps=guided_setup_apps_for_lane(lane_key),
        )
    if lane_key in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        return GuidedSetupLanePreset(
            lane=lane_key,
            device_class="tx_rx",
            backend="manual",
            selected_apps=guided_setup_apps_for_lane(lane_key),
        )
    return GuidedSetupLanePreset(
        lane=lane_key,
        device_class="tx_rx",
        backend="manual",
        selected_apps=tuple(),
    )


def guided_setup_software_hint(
    *,
    setup_type_choice: str = "",
    setup_type_label: str = "",
    selected_apps: Sequence[str] = (),
) -> str:
    """Return the concise setup-type guidance shown above app connection fields."""

    choice = str(setup_type_choice or "").strip()
    label = str(setup_type_label or "").strip()
    prefix = f"Setup type: {label}. " if choice and label else ""
    if not choice:
        return "Choose a setup type above. CommsPlan will then show only the fields needed for that radio path."
    if choice == "custom":
        return prefix + "Custom software mix is selected. Choose only the applications that belong to this radio."

    app_labels: list[str] = []
    seen: set[str] = set()
    for app_id in selected_apps:
        key = str(app_id or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        app_labels.append(guided_setup_app_label(key))
    app_summary = ", ".join(app_labels) if app_labels else "monitor/import only"
    return prefix + "This setup uses: " + app_summary + "."


def guided_setup_role_hint(
    blueprint: GuidedSetupBlueprint,
    state: GuidedSetupFieldVisibilityInput,
) -> str:
    """Return the concise role/control hint for guided setup surfaces."""

    policy = guided_setup_capability_policy(blueprint)
    if str(state.device_class or "").strip().lower() == "observer" or policy.lane == LANE_SDR_OBSERVER:
        return (
            "Observer radios track or monitor RF activity without taking over frequency control. "
            "Set the SDR endpoint if this observer should appear in readiness and runtime summaries."
        )
    if policy.lane in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        return (
            "VarAC owns frequency scheduling and radio control for this setup. "
            "CommsPlan stores VarAC paths for message monitoring, BBS, launch, and readiness."
        )
    route = policy.default_control_route
    if route == CONTROL_JS8CALL:
        return (
            "JS8Call is the frequency-control route for this radio. "
            "CommsPlan can still track related Spotter, CommStat, or other message integrations that belong to this radio."
        )
    if route == CONTROL_RIGCTLD:
        return (
            "RigCtlD is the frequency-control route for this radio. "
            "Choose only the software integrations that belong to this radio's bundle."
        )
    if route == CONTROL_NONE:
        return (
            "CommsPlan will track this radio for planning and coordination while frequency changes remain operator-managed."
        )
    return (
        "FLRig is the frequency-control route for this radio. "
        "Turn on only the other software integrations that belong to this radio's bundle."
    )


def guided_schedule_choices_for_lane(
    lane: str,
    *,
    receive_only: bool = False,
) -> Tuple[GuidedSetupChoice, ...]:
    lane_key = _normalize_lane(lane)
    if lane_key in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        return (
            GuidedSetupChoice(
                SCHEDULE_NONE,
                "No schedule / monitor only",
                recommended=True,
                detail="VarAC has its own scheduler. CommsPlan monitors VarAC messages, BBS, and activity for this radio.",
            ),
        )
    choices = [
        GuidedSetupChoice(SCHEDULE_EXISTING_PLAN, "Use an existing Frequency Plan"),
    ]
    if lane_key == LANE_JS8_ONLY:
        choices.append(
            GuidedSetupChoice(
                SCHEDULE_JS8_STANDARD,
                "JS8Call standard frequencies",
                recommended=True,
                detail="Available from the control bar even before a named plan exists.",
            )
        )
    choices.extend(
        (
            GuidedSetupChoice(SCHEDULE_DAILY_NO_NETS, "Daily schedule with no nets"),
            GuidedSetupChoice(SCHEDULE_DAILY_PLUS_NETS, "Daily schedule + net schedule"),
            GuidedSetupChoice(SCHEDULE_SOP_CONDITION, "SOP condition plan"),
            GuidedSetupChoice(SCHEDULE_NONE, "No schedule / monitor only", recommended=receive_only),
        )
    )
    return tuple(choices)


def guided_net_components() -> Tuple[GuidedScheduleComponent, ...]:
    return (
        GuidedScheduleComponent(
            component_id=NET_COMPONENT_NO_NETS,
            label="No Nets",
            component_type="net_schedule",
            sentinel=True,
            detail="Use this when the plan has a Daily schedule but no net overlay.",
        ),
    )


def build_guided_setup_blueprint(
    *,
    lane: str,
    hamlib_short_name: str = "",
    existing_radio_labels: Sequence[str] = (),
    setup_mode: str = SETUP_MODE_READ_ONLY,
    control_route: str = CONTROL_LATER,
    js8call_uses_flrig: bool = False,
    receive_only: bool = False,
    include_spotter: bool = False,
    include_external_spotter: bool = False,
    include_commstat: bool = False,
    include_varac: bool = False,
) -> GuidedSetupBlueprint:
    """Build a UI-ready guided setup blueprint without doing discovery or writes."""

    lane_key = _normalize_lane(lane)
    mode_key = _normalize_setup_mode(setup_mode)
    route_key = _normalized_control_route(control_route)
    if lane_key == LANE_JS8_ONLY and js8call_uses_flrig and route_key in {CONTROL_LATER, CONTROL_JS8CALL}:
        route_key = CONTROL_FLRIG
    if lane_key in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        route_key = CONTROL_NONE
    radio_label = generated_radio_label(hamlib_short_name, existing_radio_labels)
    apps = list(guided_setup_apps_for_lane(lane_key))
    if include_spotter and "js8call" in apps:
        apps.append("js8spotter")
    if include_external_spotter and "js8call" in apps:
        apps.append("external_js8spotter")
    if include_commstat and "js8call" in apps:
        apps.append("commstat")
    if include_varac and "varac" not in apps:
        apps.append("varac")

    schedule_choices = guided_schedule_choices_for_lane(lane_key, receive_only=receive_only)
    net_components = guided_net_components()
    steps = _build_steps(
        lane_key=lane_key,
        radio_label=radio_label,
        control_route=route_key,
        receive_only=receive_only,
        schedule_choices=schedule_choices,
    )
    proposal_items = _build_proposal_items(
        lane_key=lane_key,
        mode_key=mode_key,
        radio_label=radio_label,
        apps=tuple(apps),
        control_route=route_key,
        receive_only=receive_only,
    )
    notes = []
    if lane_key == LANE_JS8_ONLY:
        notes.append("Fast Light fields are not part of JS8Call-only setup.")
        notes.append("JS8Call Standard frequencies should remain available from the control bar.")
    if lane_key == LANE_VARAC:
        notes.append("VarAC setup uses the radio name directly and does not imply FLRig.")
        notes.append("VarAC-only radios use VarAC's scheduler; CommsPlan does not show scheduler/QSY frequency controls.")
        notes.append("VarAC guided setup stores integration settings in CommsPlan and does not write VarAC.ini or VarAC DB.")
    elif include_varac or "varac" in apps:
        notes.append("VarAC is included as a read/import integration; other selected app lanes keep their normal control rules.")
    if mode_key == SETUP_MODE_READ_ONLY:
        notes.append("Read-only setup stores references in CommsPlan but does not change external app configuration.")
    elif lane_key == LANE_VARAC:
        notes.append("Managed VarAC setup is limited to CommsPlan-side integration settings until a separate backup-gated VarAC write design is implemented.")
    else:
        notes.append("Managed setup may write reviewed app configuration after backup approval.")
    if receive_only:
        notes.append("Receive-only devices may follow receive-only plans but cannot receive transmit-capable plans.")
    return GuidedSetupBlueprint(
        lane=lane_key,
        setup_mode=mode_key,
        radio_label=radio_label,
        selected_apps=tuple(apps),
        control_route=route_key,
        schedule_choices=schedule_choices,
        net_components=net_components,
        steps=steps,
        proposal_items=proposal_items,
        notes=tuple(notes),
    )


def _build_steps(
    *,
    lane_key: str,
    radio_label: str,
    control_route: str,
    receive_only: bool,
    schedule_choices: Tuple[GuidedSetupChoice, ...],
) -> Tuple[GuidedSetupStep, ...]:
    station_choices = (
        GuidedSetupChoice(LANE_TRI_MODE, "TriMode - FastLight/JS8Call/VarAC", recommended=lane_key == LANE_TRI_MODE),
        GuidedSetupChoice(LANE_CUSTOM_MIX, "Custom software mix", recommended=lane_key == LANE_CUSTOM_MIX),
        GuidedSetupChoice(LANE_JS8_ONLY, "JS8Call", recommended=lane_key == LANE_JS8_ONLY),
        GuidedSetupChoice(LANE_FAST_LIGHT, "Fast Light: FLDigi / FLMsg / FLAmp", recommended=lane_key == LANE_FAST_LIGHT),
        GuidedSetupChoice(LANE_VARAC, "VarAC", recommended=lane_key == LANE_VARAC),
        GuidedSetupChoice(LANE_VARAC_CLUSTER, "VarAC Cluster / BBS", recommended=lane_key == LANE_VARAC_CLUSTER),
        GuidedSetupChoice(LANE_SDR_OBSERVER, "Receive-only monitoring", recommended=receive_only or lane_key == LANE_SDR_OBSERVER),
    )
    control_choices = _guided_control_choices_for_lane(
        lane_key=lane_key,
        radio_label=radio_label,
        control_route=control_route,
    )
    return (
        GuidedSetupStep(
            step_id="station_use",
            title="Station Use",
            prompt=f"What will this {radio_label} do in CommsPlan?",
            choices=station_choices,
            status="ready",
        ),
        GuidedSetupStep(
            step_id="frequency_control",
            title="Frequency Control",
            prompt=f"How should CommsPlan change frequency for this {radio_label}?",
            choices=control_choices,
            status="ready" if control_route != CONTROL_LATER else "needs_input",
            hint="If CommsPlan cannot control frequency, the scheduler can advise but cannot tune this radio.",
            advanced_available=True,
        ),
        _guided_app_instance_step(lane_key=lane_key, radio_label=radio_label),
        GuidedSetupStep(
            step_id="schedule_intent",
            title="Schedule",
            prompt="Should this radio follow a schedule?",
            choices=schedule_choices,
            status="needs_input",
        ),
    )


def _guided_app_instance_step(*, lane_key: str, radio_label: str) -> GuidedSetupStep:
    lane = _normalize_lane(lane_key)
    if lane == LANE_VARAC:
        return GuidedSetupStep(
            step_id="app_instance",
            title="VarAC Setup",
            prompt=f"Which VarAC setup belongs to {radio_label}?",
            choices=(
                GuidedSetupChoice(
                    APP_INSTANCE_MANAGED,
                    "Create a distinct standalone VarAC instance",
                    recommended=True,
                    detail="Starts separate from every existing node or cluster; cluster membership must be chosen explicitly.",
                ),
                GuidedSetupChoice(APP_INSTANCE_EXISTING, "Use detected VarAC config"),
                GuidedSetupChoice(APP_INSTANCE_MANUAL, "Choose VarAC files manually"),
            ),
            status="needs_input",
            hint="CommsPlan stores VarAC paths for monitoring and BBS support; VarAC keeps frequency scheduling.",
        )
    if lane == LANE_VARAC_CLUSTER:
        return GuidedSetupStep(
            step_id="app_instance",
            title="VarAC Cluster / BBS",
            prompt=f"Which VarAC cluster or BBS assets should CommsPlan monitor for {radio_label}?",
            choices=(
                GuidedSetupChoice(APP_INSTANCE_EXISTING, "Use detected VarAC cluster/BBS assets", recommended=True),
                GuidedSetupChoice(APP_INSTANCE_MANUAL, "Choose VarAC cluster/BBS files manually"),
            ),
            status="needs_input",
            hint="Cluster setup remains read/import only in this guided release.",
        )
    if lane == LANE_FAST_LIGHT:
        return GuidedSetupStep(
            step_id="app_instance",
            title="Fast Light Apps",
            prompt=f"Which FLRig, FLDigi, FLMsg, and FLAmp setup belongs to {radio_label}?",
            choices=(
                GuidedSetupChoice(APP_INSTANCE_EXISTING, "Use detected Fast Light apps", recommended=True),
                GuidedSetupChoice(APP_INSTANCE_MANAGED, "Create CommsPlan-managed Fast Light setup"),
                GuidedSetupChoice(APP_INSTANCE_MANUAL, "Choose Fast Light apps manually"),
            ),
            status="needs_input",
        )
    if lane == LANE_TRI_MODE:
        return GuidedSetupStep(
            step_id="app_instance",
            title="Mixed App Stack",
            prompt=f"Which FastLight, JS8Call, and VarAC setup belongs to {radio_label}?",
            choices=(
                GuidedSetupChoice(APP_INSTANCE_EXISTING, "Use detected paired app stack", recommended=True),
                GuidedSetupChoice(APP_INSTANCE_MANAGED, "Create CommsPlan-managed app stack"),
                GuidedSetupChoice(APP_INSTANCE_MANUAL, "Choose app paths and profiles manually"),
            ),
            status="needs_input",
        )
    if lane == LANE_SDR_OBSERVER:
        return GuidedSetupStep(
            step_id="app_instance",
            title="Receive Source",
            prompt=f"Which receive-only source should CommsPlan monitor for {radio_label}?",
            choices=(
                GuidedSetupChoice(APP_INSTANCE_EXISTING, "Use detected receiver or log source", recommended=True),
                GuidedSetupChoice(APP_INSTANCE_MANUAL, "Enter receive/log paths manually"),
            ),
            status="needs_input",
            hint="Receive-only sources can feed messages and map awareness but cannot be tuned by CommsPlan.",
        )
    return GuidedSetupStep(
        step_id="app_instance",
        title="JS8Call Instance",
        prompt=f"Which JS8Call belongs to {radio_label}?",
        choices=(
            GuidedSetupChoice(APP_INSTANCE_EXISTING, "Use detected JS8Call instance/profile", recommended=True),
            GuidedSetupChoice(APP_INSTANCE_MANAGED, "Create a CommsPlan-managed JS8Call instance"),
            GuidedSetupChoice(APP_INSTANCE_MANUAL, "Choose JS8Call app/profile manually"),
        ),
        status="needs_input",
    )


def _build_proposal_items(
    *,
    lane_key: str,
    mode_key: str,
    radio_label: str,
    apps: Tuple[str, ...],
    control_route: str,
    receive_only: bool,
) -> Tuple[GuidedSetupProposalItem, ...]:
    writes = mode_key == SETUP_MODE_MANAGED
    items = [
        GuidedSetupProposalItem(
            item_id="radio_profile",
            title="Radio",
            summary=f"Prepare {radio_label} as {'receive-only' if receive_only else 'transmit/receive'} {lane_key.replace('_', ' ')}.",
        ),
        GuidedSetupProposalItem(
            item_id="apps",
            title="Apps",
            summary="Use apps: " + (", ".join(apps) if apps else "none required for this lane"),
            writes_external_config=writes and any(app not in {"varac"} for app in apps),
            requires_backup=writes and any(app not in {"varac"} for app in apps),
        ),
        GuidedSetupProposalItem(
            item_id="control_route",
            title="Control",
            summary=f"Frequency control route: {control_route}.",
        ),
    ]
    if lane_key == LANE_JS8_ONLY:
        items.append(
            GuidedSetupProposalItem(
                item_id="no_fast_light",
                title="Skipped",
                summary="FLDigi, FLMsg, and FLAmp are skipped for JS8Call-only setup.",
                status="skipped",
            )
        )
        items.append(
            GuidedSetupProposalItem(
                item_id="js8_standard_frequencies",
                title="Frequencies",
                summary="JS8Call Standard frequencies remain available for control-bar QSY.",
            )
        )
    if lane_key == LANE_VARAC_CLUSTER:
        items.append(
            GuidedSetupProposalItem(
                item_id="varac_cluster_read_only",
                title="VarAC Cluster",
                summary="Cluster paths may be remembered, but cluster membership remains read-only in this release.",
                status="needs_review",
            )
        )
    if lane_key == LANE_VARAC:
        items.append(
            GuidedSetupProposalItem(
                item_id="varac_no_fio_frequency_control",
                title="VarAC Scheduling",
                summary=f"VarAC owns scheduling for {radio_label}; CommsPlan keeps BBS and message monitoring enabled without scheduler/QSY controls.",
                status="needs_review",
            )
        )
        items.append(
            GuidedSetupProposalItem(
                item_id="varac_read_import_only",
                title="VarAC Integration",
                summary="CommsPlan will remember VarAC paths and read VarAC data, but guided setup will not write VarAC.ini or VarAC DB.",
                status="needs_review",
            )
        )
    if mode_key == SETUP_MODE_MANAGED and not apps:
        items.append(
            GuidedSetupProposalItem(
                item_id="managed_no_external_writes",
                title="Managed Setup",
                summary="No external app configuration writes are needed for this managed receive-only setup.",
                status="ready",
            )
        )
    return tuple(items)


def _guided_control_choices_for_lane(
    *,
    lane_key: str,
    radio_label: str,
    control_route: str,
) -> Tuple[GuidedSetupChoice, ...]:
    route_key = _normalized_control_route(control_route)
    if lane_key in {LANE_VARAC, LANE_VARAC_CLUSTER}:
        return (
            GuidedSetupChoice(
                CONTROL_NONE,
                "VarAC controls its own frequency",
                recommended=True,
                detail="CommsPlan monitors VarAC messages, BBS, and logs without offering scheduler/QSY controls.",
            ),
        )
    if lane_key == LANE_SDR_OBSERVER:
        return (
            GuidedSetupChoice(
                CONTROL_NONE,
                "Monitor only",
                recommended=route_key == CONTROL_NONE,
                detail="Receive-only devices can feed messages and map data but cannot be tuned by CommsPlan.",
            ),
            GuidedSetupChoice(CONTROL_LATER, "I will configure this later", recommended=route_key == CONTROL_LATER),
        )
    if lane_key == LANE_JS8_ONLY:
        choices = (
            GuidedSetupChoice(CONTROL_JS8CALL, f"JS8Call controls the {radio_label}", recommended=route_key == CONTROL_JS8CALL),
            GuidedSetupChoice(CONTROL_FLRIG, f"FLRig controls the {radio_label}", recommended=route_key == CONTROL_FLRIG),
            GuidedSetupChoice(CONTROL_RIGCTLD, f"RigCtlD controls the {radio_label}", recommended=route_key == CONTROL_RIGCTLD),
            GuidedSetupChoice(CONTROL_NONE, "CommsPlan should not control frequency", recommended=route_key == CONTROL_NONE),
            GuidedSetupChoice(CONTROL_LATER, "I will configure this later", recommended=route_key == CONTROL_LATER),
        )
        return choices
    return (
        GuidedSetupChoice(CONTROL_FLRIG, f"FLRig controls the {radio_label}", recommended=route_key == CONTROL_FLRIG),
        GuidedSetupChoice(CONTROL_RIGCTLD, f"RigCtlD controls the {radio_label}", recommended=route_key == CONTROL_RIGCTLD),
        GuidedSetupChoice(CONTROL_NONE, "CommsPlan should not control frequency", recommended=route_key == CONTROL_NONE),
        GuidedSetupChoice(CONTROL_LATER, "I will configure this later", recommended=route_key == CONTROL_LATER),
    )


def _normalize_lane(value: str) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "js8": LANE_JS8_ONLY,
        "js8call": LANE_JS8_ONLY,
        "js8call_only": LANE_JS8_ONLY,
        "fastlight": LANE_FAST_LIGHT,
        "fast_light": LANE_FAST_LIGHT,
        "tri": LANE_TRI_MODE,
        "trimode": LANE_TRI_MODE,
        "tri_mode": LANE_TRI_MODE,
        "varac_bbs": LANE_VARAC_CLUSTER,
        "varac_cluster_bbs": LANE_VARAC_CLUSTER,
        "observer": LANE_SDR_OBSERVER,
        "sdr": LANE_SDR_OBSERVER,
        "receive_only": LANE_SDR_OBSERVER,
    }
    return aliases.get(key, key if key else LANE_JS8_ONLY)


def _normalize_setup_mode(value: str) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in {"managed", "fio_managed", "write", "writable"}:
        return SETUP_MODE_MANAGED
    return SETUP_MODE_READ_ONLY


def _normalized_control_route(value: str) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in {CONTROL_FLRIG, CONTROL_RIGCTLD, CONTROL_JS8CALL, CONTROL_NONE}:
        return key
    if key in {"rigctl", "hamlib"}:
        return CONTROL_RIGCTLD
    if key in {"js8", "js8_cat", "js8call_cat"}:
        return CONTROL_JS8CALL
    if key in {"varac", "varac_cat", "varac_control", "varac_owned"}:
        return CONTROL_NONE
    if key in {"manual", "no_control"}:
        return CONTROL_NONE
    return CONTROL_LATER


def _clean_radio_label(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _label_match_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _remembered_varac_labels(details: Mapping[str, str]) -> Tuple[str, ...]:
    detail_labels = (
        ("install_path", "install"),
        ("ini_path", "INI"),
        ("db_path", "DB"),
        ("incoming_dir", "incoming"),
        ("outgoing_dir", "outbox"),
        ("bbs_dir", "BBS"),
        ("bbs_archive_dir", "BBS archive"),
        ("launch_cmd", "launch"),
    )
    return tuple(
        label
        for key, label in detail_labels
        if str(details.get(key, "") or "").strip()
    )


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
