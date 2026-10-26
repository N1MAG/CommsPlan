"""Small asynchronous bridge from the message reader to existing transport owners."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, Future, TimeoutError
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
import weakref

from PySide6.QtCore import QObject, Signal, Slot, Qt

from freqinout.core.manual_forward_runtime import ForwardSettingsReader, ManualForwardBackend
from freqinout.core.mesh.models import MeshSendRequest, MeshSendResult
from freqinout.core.mesh.settings import load_mesh_connection_configs
from freqinout.core.mesh.store import list_mesh_health
from freqinout.core.js8_send_service import _endpoint_tx_lock, preflight_js8_send, query_js8_selected_target, clear_js8_selected_target
from freqinout.core.multi_radio_store import normalize_ptt_group, normalize_resource_group
from freqinout.core.scheduler_command_safety import checked_ptt
from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry, JS8ApiEndpoint


class ManualForwardController(QObject):
    resultReady = Signal(object, object)
    meshRequested = Signal(object)

    def __init__(self, viewer, station_window):
        super().__init__(viewer)
        self.viewer = weakref.ref(viewer)
        self.station = weakref.ref(station_window)
        self.stopped = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="manual-forward")
        self._pending = {}
        self._pending_routes = {}
        self._lock = threading.Lock()
        self._jobs = 0
        settings = ForwardSettingsReader(Path(viewer.settings.db_path).absolute())
        self.backend = ManualForwardBackend(viewer._local_js8_db(), settings, guard=self.guard,
                                            js8_submit=self._js8_submit, mesh_submit=self._mesh_submit, stopped=self.stopped, describe=self.describe,
                                            js8_selection=self._js8_selection)
        self.resultReady.connect(self._deliver, Qt.QueuedConnection)
        self.meshRequested.connect(self._emit_mesh, Qt.QueuedConnection)

    def submit(self, action, data, callback):
        if self.stopped.is_set():
            callback(dict(ok=False, detail="Forwarding is stopped."))
            return
        with self._lock:
            if self._jobs >= 4:
                callback(dict(ok=False, detail="Forwarding is busy. Wait for the current request."))
                return
            self._jobs += 1
        payload = deepcopy(data)
        def run():
            try:
                return self.backend(action, payload)
            except Exception as exc:
                return dict(ok=False, detail=f"Forwarding could not complete: {exc}")
        future = self._pool.submit(run)
        def done(future):
            with self._lock:
                self._jobs -= 1
            if not self.stopped.is_set():
                try:
                    self.resultReady.emit(callback, future.result())
                except Exception:
                    pass
        future.add_done_callback(done)

    @Slot(object, object)
    def _deliver(self, callback, result):
        if not self.stopped.is_set():
            callback(result)

    def guard(self, destination):
        if self.stopped.is_set():
            return "Application shutdown has stopped forwarding."
        window = self.station()
        if window is None or getattr(window, "_shutdown_close_pending", False):
            return "Station runtime is unavailable."
        settings = self.backend.settings
        if float(settings.get("schedule_suspend_until", 0) or 0) > time.time():
            return "The station is suspended. Review station controls before sending."
        if destination["transport"] != "js8":
            ident = destination["identity"]
            health = list_mesh_health(self.backend.db_path, read_only=True)
            row = next((r for r in health if r.get("adapter_id") == ident["adapter_id"]), None)
            if not row or not row.get("connected"):
                return "Connect the selected Local Mesh device before sending."
            try:
                stamp = datetime.fromisoformat(str(row["updated_utc"]).replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, TypeError):
                return "Local Mesh readiness is unverified. Refresh its connection."
            if not 0 <= time.time() - stamp <= 30:
                return "Local Mesh readiness is stale. Refresh its connection."
            return ""
        ident = destination["identity"]
        manager, scheduler = getattr(window, "station_runtime_manager", None), getattr(window, "scheduler", None)
        if manager is None or scheduler is None:
            return "Radio safety evidence is unavailable."
        runtime = manager.get_runtime_for_device(ident["radio_id"])
        profile = getattr(runtime, "profile", {})
        if (not runtime or str(profile.get("js8_instance_id") or "") != ident["instance_id"]
                or str(profile.get("js8_host") or "") != ident["host"]
                or int(profile.get("js8_port") or 0) != ident["port"]
                or profile.get("device_class") == "observer" or not profile.get("runtime_active")):
            return "The selected radio/JS8Call ownership changed. Open a fresh review."
        status = scheduler.get_endpoint_status_summaries()
        row = status.get(ident["radio_id"], {})
        if not row.get("ptt_known") or row.get("ptt_stale", True) or row.get("stale", True):
            return "The selected radio's PTT/readback evidence is unavailable or stale."
        if row.get("ptt_active") or row.get("fldigi_busy") or row.get("js8_busy"):
            return "The selected radio is busy or transmitting."
        varac = dict(row.get("varac_status") or {})
        if varac.get("busy") or varac.get("db_transfer_active") or varac.get("reason") in {"transfer", "file_wait"}:
            return "VarAC has protected activity on the selected radio."
        manual = scheduler.manual_control_state_snapshot(ident["radio_id"])
        if getattr(manual, "state", "") in {"manual_hold", "manual_suspend"}:
            return "The selected radio has an active hold or suspension. Review its controls before sending."
        shared = scheduler.get_shared_ptt_status_for_target(ident["radio_id"])
        if shared.get("blocked") or not shared.get("evidence_known") or shared.get("evidence_stale"):
            return shared.get("reason") or "Shared PTT safety evidence is unavailable."
        snapshots = manager.get_runtime_snapshots(cache_only=True)
        snap = next((s for s in snapshots if s.device_profile_id == ident["radio_id"]), None)
        if snap is None or not snap.current_frequency_hz or not row.get("frequency_hz") or row.get("frequency_stale", True):
            return "The selected radio's current tuning cannot be verified."
        if int(snap.current_frequency_hz) != int(row["frequency_hz"]):
            return "The selected radio's tuning changed. Wait for current readback before sending."
        reviewed = destination.get("review_frequency_hz")
        if reviewed is not None and int(reviewed) != int(row["frequency_hz"]):
            return "The selected radio's tuning changed since this review. Open a fresh review."
        conflict = manager.evaluate_rf_conflict_for_device(target_device_profile_id=ident["radio_id"],
                   target_band=snap.current_band, target_frequency_hz=snap.current_frequency_hz,
                   source="MANUAL_FORWARD", force=False, status_by_device=status)
        if conflict is not None:
            return getattr(conflict, "detail", "") or getattr(conflict, "summary", "") or "Review the RF Guard conflict before forwarding."
        return ""

    def describe(self, destination):
        destination = dict(destination)
        if destination["transport"] == "js8":
            window = self.station()
            scheduler = getattr(window, "scheduler", None)
            row = scheduler.get_endpoint_status_summaries().get(destination["identity"]["radio_id"], {}) if scheduler else {}
            frequency = row.get("frequency_hz")
            destination["review_frequency_hz"] = frequency
            destination["context"] = f"Current tuning: {int(frequency) / 1e6:.3f} MHz · no retuning requested." if frequency else "Current tuning is unverified."
        else:
            destination["context"] = "Send through the selected Mesh adapter and destination."
        return destination

    def _js8_selection(self, destination, clear, expected, cancelled):
        if cancelled():
            return dict(ok=False, available=False, detail="JS8 selection request cancelled because its ownership changed or the application stopped.")
        ident = destination["identity"]
        client = JS8ApiClientRegistry.get(JS8ApiEndpoint(ident["host"], ident["port"]), timeout_s=0.8, auto_reconnect=True)
        if clear:
            result = clear_js8_selected_target(client, expected, timeout_s=0.8, cancelled=cancelled)
            if cancelled():
                return dict(ok=False, available=False, detail="JS8 selection ownership changed or the application stopped; check selection again.")
            state = result.state
            return dict(ok=result.cleared, available=state.available, target=state.target, detail=result.detail)
        state = query_js8_selected_target(client, timeout_s=0.8)
        if cancelled():
            return dict(ok=False, available=False, detail="JS8 selection ownership changed during the check.")
        return dict(ok=state.available, available=state.available, target=state.target, detail=state.detail or ("JS8Call has a selected target." if state.target else "No selection is set in JS8Call."))

    def _js8_submit(self, destination, text, fence):
        window = self.station()
        scheduler = getattr(window, "scheduler", None)
        if scheduler is None:
            return "failed", "Station scheduler is unavailable."
        ident = destination["identity"]
        radio_id = ident["radio_id"]
        gate = scheduler._command_safety_gate()
        stamp = gate.stamp(radio_id)
        keys = getattr(scheduler, "_endpoint_keys_by_profile", {})
        key = keys.get(radio_id)
        if key is None:
            return "failed", "Selected radio endpoint identity is unavailable."
        resources = {"endpoint:" + key.canonical}
        manager = getattr(window, "station_runtime_manager", None)
        runtime = manager.get_runtime_for_device(radio_id) if manager is not None else None
        ptt_group = normalize_ptt_group(getattr(runtime, "profile", {}).get("ptt_group"))
        if ptt_group:
            resources.add("ptt:" + ptt_group)
        # Match the scheduler's shared-resource keys; unrelated radios stay free.
        for policy in tuple(getattr(manager, "_rf_conflict_policies", ())):
            ids = {int(policy.get("source_device_id") or 0), int(policy.get("target_device_id") or 0)}
            if radio_id not in ids or not bool(int(policy.get("enabled", 1) or 0)):
                continue
            resources.add("rf:" + ":".join(str(i) for i in sorted(ids)))
            trigger = dict(policy.get("trigger") or {})
            for kind in ("antenna", "amplifier", "frontend", "band_overlap", "advanced_frequency"):
                groups = trigger.get(kind + "_groups") or []
                for group in groups.split(",") if isinstance(groups, str) else groups:
                    normalized = normalize_resource_group(group)
                    if normalized:
                        resources.add(f"rf:{kind}:{normalized}")
        resources = tuple(sorted(resources))
        if not gate.acquire(resources):
            return "failed", "The selected radio has another control operation in progress."
        try:
            reason = self.guard(destination)
            if reason:
                return "failed", reason
            client = JS8ApiClientRegistry.get(JS8ApiEndpoint(ident["host"], ident["port"]), timeout_s=0.8, auto_reconnect=True)
            # Lock is shared with existing JS8 send paths. Never clear external text/selection.
            lock = _endpoint_tx_lock(client)
            if not lock.acquire(timeout=1.0):
                return "failed", "JS8Call is busy with another CommsPlan action."
            try:
                connected_at = client.last_connected_ts
                preflight = preflight_js8_send(client, text, timeout_s=0.6)
                if not preflight.ok or preflight.issues or preflight.tx_enabled is not True or preflight.tx_queue_depth != 0:
                    return "failed", "; ".join(i.detail for i in preflight.issues) or "JS8Call transmit readiness could not be fully verified."
                if not gate.current(radio_id, stamp) or client.last_connected_ts != connected_at:
                    return "failed", "Radio control or JS8Call connection changed during review. Try a fresh review."
                reason = self.guard(destination)
                if reason or self.stopped.is_set():
                    return "failed", reason or "Application stopped before send."
                reason = fence()
                if reason:
                    return "failed", reason
                runtime = manager.get_runtime_for_device(radio_id) if manager is not None else None
                rig = getattr(runtime, "rig_client", None)
                try:
                    if checked_ptt(rig):
                        return "failed", "The selected radio began transmitting before submission."
                    reviewed_frequency = destination.get("review_frequency_hz")
                    if reviewed_frequency is None:
                        return "failed", "The reviewed JS8Call tuning is unavailable. Open a fresh review."
                    tuning = client.request("RIG.GET_FREQ", expect_types=("RIG.FREQ",), timeout_s=0.6)
                    if int(tuning.params.get("DIAL") or 0) != int(reviewed_frequency):
                        return "failed", "JS8Call's dial frequency does not match the reviewed radio tuning."
                    # Re-read external JS8 UI selection after slower safety/DB work.
                    selected = client.request("RX.GET_CALL_SELECTED", expect_types=("RX.CALL_SELECTED",), timeout_s=0.6)
                    selected_value = str(selected.value or selected.params.get("CALLSIGN") or selected.params.get("CALL") or "").strip()
                    if selected_value:
                        return "failed", "JS8Call's selected target changed. Deselect it and open a fresh review."
                except Exception:
                    return "failed", "Final PTT or JS8Call selected-target evidence is unavailable."
                if not gate.current(radio_id, stamp) or client.last_connected_ts != connected_at or self.stopped.is_set():
                    return "failed", "Station control changed before the final submission."
                reason = fence()
                if reason:
                    return "failed", reason
                # Once submission starts, errors are ambiguous, not safe retry authority.
                try:
                    client.send("TX.SEND_MESSAGE", value=text)
                except Exception:
                    return "unknown", "JS8Call submission result is uncertain. Check JS8Call before any further action."
                return "submitted", "Submitted to the selected JS8Call API; transmission and recipient receipt are unconfirmed."
            finally:
                lock.release()
        finally:
            gate.release(resources)

    def _mesh_submit(self, request):
        future = Future()
        with self._lock:
            self._pending[request.request_id] = future
            self._pending_routes[request.request_id] = (request.adapter_id, request.destination_kind, request.destination_id, request.channel_id)
        self.meshRequested.emit(request)
        try:
            result = future.result(timeout=request.timeout_sec + 2)
            state = {"accepted": "submitted", "sent": "sent", "acked": "acked"}.get(result.state, "unknown")
            if result.evidence in {"runtime_unavailable", "runtime_stopped", "audit_unavailable", "policy_denied"}:
                state = "failed"
            return state, result.detail or result.evidence or "Mesh result received without further delivery evidence."
        except TimeoutError:
            return "unknown", "Mesh completion is unknown. No automatic retry is permitted."
        finally:
            with self._lock:
                self._pending.pop(request.request_id, None)
                self._pending_routes.pop(request.request_id, None)

    @Slot(object)
    def _emit_mesh(self, request):
        viewer = self.viewer()
        if not self.stopped.is_set() and viewer is not None:
            viewer.meshSendRequested.emit(request)

    def receive_mesh_result(self, result):
        if not isinstance(result, MeshSendResult) or not result.request_id.startswith("cp-forward-"):
            return False
        with self._lock:
            future = self._pending.get(result.request_id)
            expected = getattr(self, "_pending_routes", {}).get(result.request_id)
        if future is not None and not future.done():
            actual = (result.adapter_id, result.destination_kind, result.destination_id, result.channel_id)
            if result.transport == "meshcore" and result.destination_kind == "direct" and not result.channel_id and expected is not None:
                # MeshCore direct addressing has no channel field in native ACKs.
                actual = (*actual[:3], expected[3])
            if expected is not None and expected != actual:
                future.set_exception(RuntimeError("Mesh result did not match the reviewed destination"))
            else:
                future.set_result(result)
        # Always consume our results; never attribute a late relay result to Compose.
        return True

    def shutdown(self):
        self.stopped.set()
        with self._lock:
            pending = tuple(self._pending.values())
        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError("Application shutdown"))
        self._pool.shutdown(wait=False, cancel_futures=True)
