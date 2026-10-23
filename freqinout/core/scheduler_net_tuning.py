"""Radio-scoped one-shot net tuning on the existing scheduler endpoint lanes."""
from __future__ import annotations

import copy
import time

from freqinout.core.fldigi_tuning import normalize_tuning_preset, execute_tuning, tuning_failure_detail


class NetTuningMixin:
    def _net_tuning_context(self, radio_id):
        radio_id = int(radio_id or 0)
        # Explicitly suppress refresh: presentation must consume only publication.
        for lane in self.active_schedule_lanes(request_refresh=False):
            if int(lane.get("device_profile_id") or 0) != radio_id or lane.get("current_source") != "NET":
                continue
            entry = dict(lane.get("current_entry") or {})
            if not entry:
                break
            entry.update(target_scope="device_profile", target_device_profile_id=radio_id)
            start = self._active_row_occurrence_start(self._utc_now(), entry)
            if start is None:
                break
            endpoint = getattr(self, "_endpoint_keys_by_profile", {}).get(radio_id)
            route = getattr(endpoint, "canonical", "")
            epoch = getattr(self, "_endpoint_config_epochs", {}).get(route, 0)
            sessions = tuple(sorted((str(key[1]), str(token)) for key, token in
                             getattr(self, "_manual_net_sessions", {}).items() if key[0] == radio_id))
            identity = (self._entry_transition_signature(entry), str(entry.get("fldigi_tuning_json") or "{}"),
                        start.isoformat(), route, epoch, sessions)
            return entry, identity
        return {}, None

    def net_tuning_snapshot(self, radio_id) -> dict:
        radio_id = int(radio_id or 0)
        entry, identity = self._net_tuning_context(radio_id)
        state = getattr(self, "_net_tuning_states", {}).get(radio_id, {})
        current = bool(identity is not None and state.get("identity") == identity)
        recovery_current = bool(current and state.get("restore_stamp") is not None and
                                self._command_safety_gate().current(radio_id, state["restore_stamp"]))
        result = dict(state.get("result") or {}) if current else {}
        try:
            preset = normalize_tuning_preset(entry.get("fldigi_tuning_json"))
            available = bool(entry and preset["enabled"])
            detail = ("Apply once; continue adjusting tuning freely during the net." if available else
                      "Enable FLDigi tuning on this radio's active Net Schedule row.")
        except ValueError as exc:
            preset, available, detail = {}, False, str(exc)
        return dict(radio_id=radio_id, available=available, label=preset.get("label", "Center + Narrow"),
                    net_name=entry.get("net_name") or "Scheduled net", detail=detail,
                    pending=bool(current and state.get("pending")),
                    can_restore=bool(recovery_current and not state.get("pending") and state.get("restore")),
                    restore_filter_unknown=bool(current and state.get("restore") and state["restore"].get("filter_width_hz") is None),
                    audio_offset_hz=preset.get("audio_offset_hz"), filter_width_hz=preset.get("filter_width_hz"),
                    preserve_signal=preset.get("preserve_signal", True), result=result)

    def _publish_net_tuning(self, radio_id, state):
        states = getattr(self, "_net_tuning_states", None)
        if not isinstance(states, dict):
            states = self._net_tuning_states = {}
        states[int(radio_id)] = state
        self.net_tuning_changed.emit(int(radio_id))

    def apply_net_tuning(self, radio_id, *, restore=False, automatic=False) -> str:
        radio_id = int(radio_id or 0)
        entry, identity = self._net_tuning_context(radio_id)
        state = getattr(self, "_net_tuning_states", {}).get(radio_id, {})
        if state.get("pending"):
            # Never replace in-flight work, including work from an older occurrence.
            return "blocked"

        def blocked(detail):
            self._publish_net_tuning(radio_id, dict(identity=identity, pending=False,
                restore=state.get("restore") if state.get("identity") == identity else None,
                restore_stamp=state.get("restore_stamp"),
                result=dict(ok=False, detail=detail)))
            health = getattr(self, "_record_scheduler_health_issue", None)
            if automatic and callable(health):
                health(f"net-tuning:{radio_id}", detail, device_profile_id=radio_id,
                       source="NET", cooldown_sec=0.0)
            return "blocked"

        if getattr(self, "_shutdown_requested", False) or not entry:
            return blocked("No current scheduled net is available for this radio.")
        try:
            preset = normalize_tuning_preset(entry.get("fldigi_tuning_json"))
        except ValueError as exc:
            return blocked(str(exc))
        if not preset["enabled"]:
            return blocked("FLDigi tuning is disabled on this net's schedule row.")
        if automatic and self._manual_net_active_for_radio(radio_id):
            return blocked("Operator net control is active; use the tuning action manually.")
        previous = state.get("restore") if (state.get("identity") == identity
            and state.get("restore_stamp") is not None
            and self._command_safety_gate().current(radio_id, state["restore_stamp"])) else None
        if restore and not previous:
            return blocked("No previous tuning is available for this net occurrence.")
        effective, _group = self._entry_with_operating_group_overrides(entry)
        rig, js8, _varac, settings, target_id = self._control_context_for_entry(effective)
        mode = self._control_mode_for_context(settings, rig=rig, js8=js8)
        if target_id != radio_id or mode != "FLRIG" or not callable(getattr(rig, "get_net_tuning_state", None)):
            return blocked("This action requires this radio's configured FLRig and FLDigi connections.")
        endpoint = self._control_endpoint_key(mode, rig_client=rig, js8_client=js8, entry_key=(radio_id,))
        registry = self._ensure_endpoint_lane_registry()
        lane = registry.lane(endpoint)
        if lane is not None and lane.future is not None and not lane.future.done():
            return blocked("Another command is running for this radio. Try again when it finishes.")
        # Starting setup has its own once-only rule; cancel the legacy delayed
        # primary modem apply so it cannot restore the starting offset afterward.
        if self._entry_is_primary(entry, radio_id):
            self._fldigi_apply_token = getattr(self, "_fldigi_apply_token", 0) + 1
            self._fldigi_apply_pending = False
            self._fldigi_apply_after_ts = None
            self._net_fldigi_apply_allowed_once = False
            self._net_resume_apply_once = False
        self._invalidate_command_authority(radio_id)
        gate = self._command_safety_gate()
        stamp = gate.stamp(radio_id)
        target_frequency = [self._parse_freq_hz(str(effective.get("frequency") or "")) or 0]
        preflight, resources = self._capture_control_preflight(
            entry=copy.deepcopy(entry), source="NET", radio_id=radio_id,
            endpoint_key=endpoint, config_epoch=int(getattr(self, "_endpoint_config_epochs", {}).get(endpoint.canonical, 0)),
            rig=rig, js8=js8, control_mode=mode, freq_hz=target_frequency[0], band=effective.get("band") or "",
            ignore_coordination_prompt=False, ignore_js8_busy=False, ignore_varac_busy=False,
            target_frequency_reader=lambda: target_frequency[0])
        restore_values = copy.deepcopy(previous) if restore else None
        initial_mode = self._expected_fldigi_mode(effective) if automatic else None
        initial_offset = self._expected_fldigi_offset(effective) if automatic else None
        initial_vfo = str(effective.get("vfo") or "A").strip().upper()

        def operation():
            if not gate.acquire(resources):
                return dict(ok=False, safety_hold=True, reason_code="shared_resource_reserved")
            try:
                reason = preflight()
                if reason:
                    return dict(ok=False, safety_hold=True, reason_code=reason)
                if automatic and rig.get_active_vfo() != initial_vfo:
                    return dict(ok=False, detail="The VFO changed after net setup. Review tuning and apply manually.")
                if automatic and (initial_mode or initial_offset is not None):
                    # Automatic start completes the ordinary NET modem setup
                    # first. Preserve the scheduled net's signal, rather than
                    # the previous HF/SOP offset left on this radio.
                    for modem_mode, modem_offset in ((initial_mode, None), (None, initial_offset)):
                        if not modem_mode and modem_offset is None:
                            continue
                        reason = preflight()
                        if reason:
                            return dict(ok=False, safety_hold=True, reason_code=reason)
                        if not rig.set_fldigi_mode_offset(modem_mode, modem_offset):
                            return dict(ok=False, reason_code="fldigi_initial_setup_failed")
                    initial = rig.get_net_tuning_state()
                    if initial_offset is not None and initial.get("audio_offset_hz") != initial_offset:
                        return dict(ok=False, reason_code="fldigi_initial_setup_failed")
                data = execute_tuning(rig, preset, preflight=preflight,
                                      set_target=lambda hz: target_frequency.__setitem__(0, hz), restore=restore_values)
                return dict(ok=data["ok"], actual_state=data, detail=data.get("detail", ""))
            finally:
                # Peer RF Guard must not keep authorizing from the old dial
                # after any partial/complete tuning. Existing status lanes own
                # the next independent fresh observation.
                statuses = getattr(self, "_endpoint_status", None)
                try:
                    if statuses is not None:
                        statuses.invalidate(endpoint)
                finally:
                    gate.release(resources)

        pending_state = dict(identity=identity, pending=True, restore=previous, restore_stamp=stamp,
                             result=dict(ok=False, detail="Applying tuning…"))
        self._publish_net_tuning(radio_id, pending_state)

        def completed(done):
            def publish():
                if getattr(self, "_shutdown_requested", False):
                    return
                if getattr(self, "_net_tuning_states", {}).get(radio_id) is not pending_state:
                    return  # An older timeout/completion cannot replace newer work.
                if done.status == "timed_out":
                    # The underlying bounded lane may still be returning from
                    # an RPC. Revoke further writes before offering a retry.
                    gate.invalidate(radio_id)
                    detail = tuning_failure_detail("operation_timeout")
                    self._publish_net_tuning(radio_id, dict(identity=identity, pending=False,
                        result=dict(ok=False, detail=detail)))
                    health = getattr(self, "_record_scheduler_health_issue", None)
                    if callable(health):
                        health(f"net-tuning:{radio_id}", detail, device_profile_id=radio_id,
                               source="NET", cooldown_sec=0.0)
                    return
                # Never publish stale results into another net/session/config.
                _row, current_identity = self._net_tuning_context(radio_id)
                if not gate.current(radio_id, stamp) or current_identity != identity:
                    self._publish_net_tuning(radio_id, dict(identity=identity, pending=False,
                        result=dict(ok=False, detail="Net or command authority changed; inspect current tuning.")))
                    return
                data = done.actual_state()
                if not data:
                    data = dict(ok=False, detail=tuning_failure_detail(done.detail or done.reason_code or "Tuning could not be verified."))
                if done.status not in {"applied_and_verified", "applied_unverified"}:
                    data["ok"] = False
                recovery = previous
                if data.get("changed"):
                    recovery = data.get("before") or previous
                if restore and data.get("ok"):
                    recovery = None
                self._publish_net_tuning(radio_id, dict(identity=identity, pending=False, restore=recovery, restore_stamp=stamp, result=data))
                recorder = getattr(self, "_record_scheduler_event", None)
                if callable(recorder):
                    recorder("verified" if data.get("ok") else "failed", "net_tuning_result", source="NET",
                             entry=entry, device_profile_id=radio_id, action=preset["label"],
                             detail=data.get("detail") or "Tuning could not be verified.", throttle_sec=0.0)
                if data.get("ok"):
                    clear = getattr(self, "_clear_scheduler_health_issue", None)
                    if callable(clear):
                        clear(f"net-tuning:{radio_id}", source="NET")
                else:
                    health = getattr(self, "_record_scheduler_health_issue", None)
                    if callable(health):
                        health(f"net-tuning:{radio_id}", data.get("detail") or "Net tuning could not be verified.",
                               device_profile_id=radio_id, source="NET", cooldown_sec=0.0)
            self._queue_scheduler_thread_call(publish)

        if not automatic:
            registry.retry_now(endpoint)
        submission = registry.submit(endpoint, occurrence_id=f"net-tuning:{radio_id}:{time.monotonic_ns()}",
            operation=operation, completion=completed, timeout_s=max(8.0, self._control_timeout_s))
        if not submission.accepted:
            return blocked("The radio command lane is unavailable. Try again when it is ready.")
        return "queued"

    def _maybe_start_net_tuning(self, entry, radio_id):
        """Called only after initial NET schedule setup succeeds, never on drift."""
        if not radio_id or self._manual_net_active_for_radio(radio_id):
            return False
        try:
            preset = normalize_tuning_preset((entry or {}).get("fldigi_tuning_json"))
        except ValueError:
            return False
        if not preset["enabled"] or not preset["apply_at_start"]:
            return False
        current, identity = self._net_tuning_context(radio_id)
        if not current:
            return False
        effective_current, _ = self._entry_with_operating_group_overrides(current)
        effective_requested, _ = self._entry_with_operating_group_overrides(entry)
        if self._entry_transition_signature(effective_current) != self._entry_transition_signature(effective_requested):
            return False
        attempts = getattr(self, "_net_tuning_auto_occurrences", None)
        if not isinstance(attempts, dict):
            attempts = self._net_tuning_auto_occurrences = {}
        if attempts.get(radio_id) == identity:
            return False
        # Exactly one automatic opportunity. Failure requires an explicit click;
        # a late retry must never recenter traffic after the operator has tuned.
        attempts[radio_id] = identity
        return self.apply_net_tuning(radio_id, automatic=True) == "queued"
