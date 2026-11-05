from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from uuid import uuid4

from freqinout.core.multi_radio_store import (
    EFFECTIVE_ASSIGNMENT_STATES,
    MultiRadioStore,
    _effective_assignment_for_device,
    project_runtime_active_device_to_legacy_settings_if_single_active,
)
from freqinout.core.multi_rig_runtime_status import (
    STARTUP_FRESH_DEFAULT_READY,
    STARTUP_MIGRATED,
    MultiRigRuntimeStatus,
    build_multi_rig_runtime_status,
    device_profile_id_from_radio_id,
    radio_shared_state_id,
)
from freqinout.core.shared_state import RuntimePolicy, RuntimeSelectionState, SelectionWriteError
from freqinout.core.scheduler_permission import (
    PERMISSION_VERSION, model_allows_scheduling, pause_state, record_pause,
    reconcile_permission, needs_reconciliation, audit_change, pause_from_rows,
)


SOURCE_SETTINGS = "settings"
SOURCE_MIGRATION = "migration"
SOURCE_RUNTIME_POLICY = "runtime_policy"
SOURCE_SCHEDULER = "scheduler"
SOURCE_LAUNCH_ORCHESTRATOR = "launch_orchestrator"

CAPABILITY_SCHEDULER = "scheduler"
CAPABILITY_BACKGROUND_INGEST = "background_ingest"
CAPABILITY_MESSAGES = "messages"
CAPABILITY_MAP = "map"
CAPABILITY_LAUNCH = "launch"
CAPABILITY_NET_CONTROL = "net_control"

CAPABILITIES = frozenset(
    {
        CAPABILITY_SCHEDULER,
        CAPABILITY_BACKGROUND_INGEST,
        CAPABILITY_MESSAGES,
        CAPABILITY_MAP,
        CAPABILITY_LAUNCH,
        CAPABILITY_NET_CONTROL,
    }
)

_POLICY_COLUMNS = {
    CAPABILITY_SCHEDULER: "scheduler_enabled",
    CAPABILITY_BACKGROUND_INGEST: "background_ingest_enabled",
    CAPABILITY_MESSAGES: "messages_enabled",
    CAPABILITY_MAP: "map_enabled",
    CAPABILITY_LAUNCH: "launch_enabled",
    CAPABILITY_NET_CONTROL: "net_control_enabled",
}

_POLICY_WRITE_SOURCES = frozenset({SOURCE_SETTINGS, SOURCE_RUNTIME_POLICY})
_PRIMARY_WRITE_SOURCES = frozenset({SOURCE_SETTINGS, SOURCE_MIGRATION, SOURCE_RUNTIME_POLICY})
_ACTIVE_WRITE_SOURCES = frozenset({SOURCE_SCHEDULER, SOURCE_LAUNCH_ORCHESTRATOR})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bool(value: object, default: bool = False) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _check_source(action: str, source: str, allowed: Iterable[str]) -> None:
    allowed_set = frozenset(allowed)
    if source not in allowed_set:
        allowed_text = ", ".join(sorted(allowed_set))
        raise SelectionWriteError(f"{action} requires source in {{{allowed_text}}}; got {source!r}.")


def _runtime_ready(status: MultiRigRuntimeStatus) -> bool:
    return status.startup_mode in {STARTUP_FRESH_DEFAULT_READY, STARTUP_MIGRATED}


def _policy_from_row(row: Mapping[str, Any]) -> RuntimePolicy:
    launch_enabled = _bool(row.get("launch_enabled"), False)
    return RuntimePolicy(
        radio_profile_id=radio_shared_state_id(row.get("radio_profile_id")),
        scheduler_control=str(row.get("scheduler_control") or "enabled"),
        scheduler_enabled=_bool(row.get("scheduler_enabled"), True),
        background_ingest_enabled=_bool(row.get("background_ingest_enabled"), True),
        messages_enabled=_bool(row.get("messages_enabled"), True),
        map_enabled=_bool(row.get("map_enabled"), True),
        launch_enabled=launch_enabled,
        launch_control_participation=launch_enabled,
        net_control_enabled=_bool(row.get("net_control_enabled"), True),
        operator_suppressed=_bool(row.get("operator_suppressed"), False),
        updated_at_utc=str(row.get("updated_utc") or _utc_now_iso()),
    )


class DurableRuntimePolicyStore:
    def __init__(self, store: Optional[MultiRadioStore] = None) -> None:
        self.store = store or MultiRadioStore()

    def _status(self, runtime_status: Optional[MultiRigRuntimeStatus]) -> MultiRigRuntimeStatus:
        return runtime_status or build_multi_rig_runtime_status(self.store)

    def _ensure_ready(self, runtime_status: Optional[MultiRigRuntimeStatus], *, write: bool = False) -> MultiRigRuntimeStatus:
        status = self._status(runtime_status)
        if write and not _runtime_ready(status):
            raise SelectionWriteError(f"Runtime policy write is not available during startup mode {status.startup_mode!r}.")
        return status

    def _device_row(self, conn, device_id: int) -> Optional[Dict[str, Any]]:
        row = conn.execute("SELECT * FROM device_profiles WHERE id=?", (int(device_id),)).fetchone()
        return dict(row) if row is not None else None

    def _effective_operating_row(self, conn, device_id: int) -> Dict[str, Any]:
        assignment = _effective_assignment_for_device(conn, int(device_id))
        if not assignment:
            return {}
        row = conn.execute(
            'SELECT * FROM operating_profiles WHERE id=?',
            (int(assignment['operating_profile_id']),),
        ).fetchone()
        return dict(row) if row is not None else {}

    def _seed_policy_values(self, conn, device_row: Mapping[str, Any]) -> Dict[str, int]:
        operating = self._effective_operating_row(conn, int(device_row.get("id", 0) or 0))
        launch_enabled = _bool(operating.get("use_launch_control"), False) and _bool(device_row.get("launch_enabled"), False)
        return {
            "scheduler_enabled": int(model_allows_scheduling(operating)),
            "background_ingest_enabled": 1 if _bool(operating.get("use_background_ingest"), True) else 0,
            "messages_enabled": 1 if _bool(operating.get("use_messages"), True) else 0,
            "map_enabled": 1 if _bool(operating.get("use_map"), True) else 0,
            "launch_enabled": 1 if launch_enabled else 0,
            "net_control_enabled": 1 if _bool(operating.get("use_net_control_tabs"), True) else 0,
            "operator_suppressed": 0 if _bool(device_row.get("enabled"), True) else 1,
        }

    def _ensure_policy_row(self, conn, device_id: int) -> Dict[str, Any]:
        """Return an existing policy row or insert seeded defaults without committing.

        The caller owns the transaction boundary and must commit or roll back the
        connection after calling this helper.
        """
        device = self._device_row(conn, int(device_id))
        if not device:
            raise KeyError(f"Unknown device profile id: {device_id}")
        row = conn.execute("SELECT * FROM runtime_policies WHERE radio_profile_id=?", (int(device_id),)).fetchone()
        if row is not None:
            reconcile_permission(conn, device_id, self._effective_operating_row(conn, device_id), source='runtime policy')
            return self._resolved_policy_row(conn, device_id)
        values = self._seed_policy_values(conn, device)
        now = _utc_now_iso()
        conn.execute(
            """
            INSERT INTO runtime_policies (
                radio_profile_id,
                scheduler_enabled,
                scheduler_permission_version,
                background_ingest_enabled,
                messages_enabled,
                map_enabled,
                launch_enabled,
                net_control_enabled,
                operator_suppressed,
                created_utc,
                updated_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(device_id),
                values["scheduler_enabled"],
                PERMISSION_VERSION,
                values["background_ingest_enabled"],
                values["messages_enabled"],
                values["map_enabled"],
                values["launch_enabled"],
                values["net_control_enabled"],
                values["operator_suppressed"],
                now,
                now,
            ),
        )
        return self._resolved_policy_row(conn, device_id)

    def _resolved_policy_row(self, conn, device_id: int) -> Dict[str, Any]:
        row = dict(conn.execute('SELECT * FROM runtime_policies WHERE radio_profile_id=?', (int(device_id),)).fetchone())
        paused = pause_state(conn, device_id)['operator_paused']
        row['scheduler_control'] = 'paused' if paused else 'enabled' if row['scheduler_enabled'] else 'model_prohibited'
        row['scheduler_enabled'] = bool(row['scheduler_enabled'] and not paused)
        return row

    def _detached_policy_row(self, conn, device_id: int) -> Dict[str, Any]:
        """A private catalogue is a projection, never a migration/save owner."""
        device = self._device_row(conn, device_id)
        if device is None:
            raise KeyError(f'Unknown device profile id: {device_id}')
        cursor = conn.execute('SELECT * FROM runtime_policies WHERE radio_profile_id=?', (device_id,))
        row = cursor.fetchone()
        policy = dict(row) if row is not None else {}
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_schedule_authorizations'").fetchone()
        approval = conn.execute('SELECT * FROM operator_schedule_authorizations WHERE radio_id=?', (device_id,)).fetchone() if present else None
        paused = pause_from_rows(dict(approval) if approval is not None else {}, policy)['operator_paused']
        allowed = model_allows_scheduling(self._effective_operating_row(conn, device_id))
        return {**self._seed_policy_values(conn, device), **policy, 'radio_profile_id': device_id,
                'scheduler_enabled': allowed and not paused,
                'scheduler_control': 'paused' if paused else 'enabled' if allowed else 'model_prohibited'}

    def get_policy(self, radio_profile_id: str, runtime_status: Optional[MultiRigRuntimeStatus] = None) -> RuntimePolicy:
        status = self._ensure_ready(runtime_status)
        device_id = device_profile_id_from_radio_id(radio_profile_id)
        if not _runtime_ready(status):
            return RuntimePolicy(radio_profile_id=radio_shared_state_id(device_id), operator_suppressed=True)
        if getattr(self.store, 'detached_configuration', False):
            with self.store.connect_readonly() as conn:
                return _policy_from_row(self._detached_policy_row(conn, device_id))
        with self.store.configuration_read_transaction():
            with self.store.connect_readonly() as conn:
                present = conn.execute('SELECT 1 FROM runtime_policies WHERE radio_profile_id=?', (device_id,)).fetchone()
                if present and not needs_reconciliation(conn, device_id, self._effective_operating_row(conn, device_id)):
                    return _policy_from_row(self._resolved_policy_row(conn, device_id))
        with self.store.configuration_save_transaction() as transaction:
            with self.store.connect() as conn:
                row = self._ensure_policy_row(conn, device_id)
            transaction.complete()
        return _policy_from_row(row)

    def list_policies(self, runtime_status: Optional[MultiRigRuntimeStatus] = None) -> tuple[RuntimePolicy, ...]:
        status = self._ensure_ready(runtime_status)
        if not _runtime_ready(status):
            return ()
        if getattr(self.store, 'detached_configuration', False):
            with self.store.connect_readonly() as conn:
                ids = [int(row[0]) for row in conn.execute('SELECT id FROM device_profiles ORDER BY display_order ASC,id ASC')]
                return tuple(_policy_from_row(self._detached_policy_row(conn, device_id)) for device_id in ids)
        with self.store.configuration_read_transaction():
            with self.store.connect_readonly() as conn:
                ids = [int(row[0]) for row in conn.execute('SELECT id FROM device_profiles ORDER BY display_order ASC,id ASC')]
                ready = all(conn.execute('SELECT 1 FROM runtime_policies WHERE radio_profile_id=?', (device_id,)).fetchone()
                            and not needs_reconciliation(conn, device_id, self._effective_operating_row(conn, device_id))
                            for device_id in ids)
                if ready:
                    return tuple(_policy_from_row(self._resolved_policy_row(conn, device_id)) for device_id in ids)
        policies: list[RuntimePolicy] = []
        with self.store.configuration_save_transaction() as transaction:
            with self.store.connect() as conn:
                rows = conn.execute("SELECT id FROM device_profiles ORDER BY display_order ASC, id ASC").fetchall()
                for row in rows:
                    policies.append(_policy_from_row(self._ensure_policy_row(conn, int(row[0]))))
            transaction.complete()
        return tuple(policies)

    def is_radio_allowed(
        self,
        radio_profile_id: str,
        capability: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> bool:
        capability = str(capability or "").strip().lower()
        if capability not in CAPABILITIES:
            raise ValueError(f"Unknown runtime policy capability: {capability}")
        status = self._ensure_ready(runtime_status)
        if not _runtime_ready(status):
            return False
        policy = self.get_policy(radio_profile_id, runtime_status=status)
        if policy.operator_suppressed:
            return False
        return bool(getattr(policy, _POLICY_COLUMNS[capability]))

    def list_participating_radios(
        self,
        capability: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> tuple[str, ...]:
        capability = str(capability or "").strip().lower()
        if capability not in CAPABILITIES:
            raise ValueError(f"Unknown runtime policy capability: {capability}")
        status = self._ensure_ready(runtime_status)
        if not _runtime_ready(status):
            return ()
        allowed: list[str] = []
        for radio_id in status.active_radio_ids:
            if self.is_radio_allowed(radio_id, capability, runtime_status=status):
                allowed.append(radio_id)
        return tuple(allowed)

    def _set_policy_value(self, device_id: int, column: str, enabled: bool) -> RuntimePolicy:
        with self.store.configuration_save_transaction() as transaction:
            with self.store.connect() as conn:
                self._ensure_policy_row(conn, int(device_id))
                now = _utc_now_iso()
                conn.execute(
                    f"UPDATE runtime_policies SET {column}=?, updated_utc=? WHERE radio_profile_id=?",
                    (1 if enabled else 0, now, int(device_id)),
                )
                row = self._resolved_policy_row(conn, int(device_id))
            transaction.complete()
        return _policy_from_row(row)

    def set_capability(
        self,
        radio_profile_id: str,
        capability: str,
        enabled: bool,
        *,
        source: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
        expected_pause_token: str = "",
    ) -> RuntimePolicy:
        _check_source(f"set_capability({capability})", source, _POLICY_WRITE_SOURCES)
        self._ensure_ready(runtime_status, write=True)
        capability = str(capability or "").strip().lower()
        if capability not in CAPABILITIES:
            raise ValueError(f"Unknown runtime policy capability: {capability}")
        if capability == CAPABILITY_SCHEDULER:
            device_id = device_profile_id_from_radio_id(radio_profile_id)
            with self.store.configuration_save_transaction() as transaction:
                with self.store.connect() as conn:
                    self._ensure_policy_row(conn, device_id)
                    if not enabled:
                        record_pause(conn, device_id, source=source)
                    else:
                        pause = pause_state(conn, device_id)
                        if pause['operator_paused']:
                            if not expected_pause_token or expected_pause_token != pause['operator_pause_token']:
                                raise SelectionWriteError('Scheduling pause changed; refresh before Resume.')
                            token = uuid4().hex
                            conn.execute("UPDATE operator_schedule_authorizations SET reason='resume requested',approval_token=? WHERE radio_id=? AND reason='operator disabled' AND approval_token=?", (token, device_id, expected_pause_token))
                            audit_change(conn, source=source, property='operator pause', old=True, new=False,
                                         radio_id=device_id, model_id=self._effective_operating_row(conn, device_id).get('id'), correlation=token)
                    row = self._resolved_policy_row(conn, device_id)
                transaction.complete()
            return _policy_from_row(row)
        return self._set_policy_value(device_profile_id_from_radio_id(radio_profile_id), _POLICY_COLUMNS[capability], bool(enabled))

    def set_operator_suppressed(
        self,
        radio_profile_id: str,
        suppressed: bool,
        *,
        source: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> RuntimePolicy:
        _check_source("set_operator_suppressed", source, _POLICY_WRITE_SOURCES)
        self._ensure_ready(runtime_status, write=True)
        device_id = device_profile_id_from_radio_id(radio_profile_id)
        policy = self._set_policy_value(device_id, "operator_suppressed", bool(suppressed))
        if suppressed:
            DurableRuntimeSelectionService(self.store, policy_store=self).remove_from_active_runtime(
                radio_profile_id,
                source=SOURCE_RUNTIME_POLICY,
                runtime_status=runtime_status,
            )
        return policy

    def discover_runtime_candidates(
        self,
        flrig_health: Mapping[str, bool],
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> tuple[str, ...]:
        """Return eligible FLRig radios for runtime activation.

        If `flrig_health` has no entry for a radio, current persisted
        `runtime_active` is used as a conservative fallback. This preserves an
        already-active radio unless FLRig explicitly reports it unhealthy.
        """
        status = self._ensure_ready(runtime_status)
        if not _runtime_ready(status):
            return ()
        health = dict(flrig_health or {})
        candidates: list[str] = []
        policies = {policy.radio_profile_id: policy for policy in self.list_policies(runtime_status=status)}
        with self.store.connect_readonly() as conn:
            rows = conn.execute("SELECT * FROM device_profiles ORDER BY display_order ASC, id ASC").fetchall()
            for raw in rows:
                row = dict(raw)
                radio_id = radio_shared_state_id(row.get("id"))
                policy = policies.get(radio_id)
                if policy is None:
                    continue
                if policy.operator_suppressed or not _bool(row.get("enabled"), True):
                    continue
                if str(row.get("control_backend", "") or "").strip().lower() != "flrig":
                    continue
                healthy = health.get(radio_id)
                if healthy is None:
                    healthy = _bool(row.get("runtime_active"), False)
                if healthy:
                    candidates.append(radio_id)
        return tuple(candidates)


class DurableRuntimeSelectionService:
    def __init__(
        self,
        store: Optional[MultiRadioStore] = None,
        *,
        policy_store: Optional[DurableRuntimePolicyStore] = None,
    ) -> None:
        self.store = store or MultiRadioStore()
        self.policy_store = policy_store or DurableRuntimePolicyStore(self.store)
        self._settings_radio_id: Optional[str] = None
        self._tab_radio_ids: Dict[str, str] = {}

    def _status(self, runtime_status: Optional[MultiRigRuntimeStatus]) -> MultiRigRuntimeStatus:
        return runtime_status or build_multi_rig_runtime_status(self.store)

    def _ensure_ready(self, runtime_status: Optional[MultiRigRuntimeStatus], *, write: bool = False) -> MultiRigRuntimeStatus:
        status = self._status(runtime_status)
        if write and not _runtime_ready(status):
            raise SelectionWriteError(f"Runtime selection write is not available during startup mode {status.startup_mode!r}.")
        return status

    def state(self, runtime_status: Optional[MultiRigRuntimeStatus] = None) -> RuntimeSelectionState:
        status = self._ensure_ready(runtime_status)
        return RuntimeSelectionState(
            settings_radio_id=self._settings_radio_id,
            tab_radio_ids=dict(self._tab_radio_ids),
            primary_runtime_radio_id=status.primary_radio_id if _runtime_ready(status) else None,
            active_runtime_radio_ids=status.active_radio_ids if _runtime_ready(status) else (),
        )

    def settings_radio_id(self) -> Optional[str]:
        return self._settings_radio_id

    def tab_radio_id(self, tab_id: str) -> Optional[str]:
        return self._tab_radio_ids.get(str(tab_id))

    def primary_runtime_radio_id(self, runtime_status: Optional[MultiRigRuntimeStatus] = None) -> Optional[str]:
        return self.state(runtime_status).primary_runtime_radio_id

    def active_runtime_radio_ids(self, runtime_status: Optional[MultiRigRuntimeStatus] = None) -> tuple[str, ...]:
        return self.state(runtime_status).active_runtime_radio_ids

    def set_settings_radio(self, radio_profile_id: str, *, source: str) -> RuntimeSelectionState:
        _check_source("set_settings_radio", source, {SOURCE_SETTINGS})
        device_profile_id_from_radio_id(radio_profile_id)
        self._settings_radio_id = str(radio_profile_id)
        return self.state()

    def set_tab_radio(self, tab_id: str, radio_profile_id: str, *, source_tab_id: str) -> RuntimeSelectionState:
        tab_id = str(tab_id)
        if source_tab_id != tab_id:
            raise SelectionWriteError(
                f"set_tab_radio({tab_id}) requires source_tab_id={tab_id!r}; got {source_tab_id!r}."
            )
        device_profile_id_from_radio_id(radio_profile_id)
        self._tab_radio_ids[tab_id] = str(radio_profile_id)
        return self.state()

    def _eligible_primary_id(self, conn) -> Optional[int]:
        row = conn.execute(
            """
            SELECT d.id
              FROM device_profiles d
              LEFT JOIN runtime_policies p ON p.radio_profile_id=d.id
             WHERE d.enabled=1
               AND d.runtime_active=1
               AND COALESCE(p.operator_suppressed, 0)=0
               AND LOWER(COALESCE(d.device_class, 'tx_rx')) <> 'observer'
          ORDER BY d.display_order ASC, d.id ASC
             LIMIT 1
            """
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _set_primary_id_direct(self, conn, device_id: Optional[int]) -> None:
        if device_id is None:
            conn.execute("UPDATE device_profiles SET runtime_primary=0")
            return
        conn.execute(
            "UPDATE device_profiles SET runtime_primary=CASE WHEN id=? THEN 1 ELSE 0 END WHERE runtime_active=1",
            (int(device_id),),
        )
        conn.execute("UPDATE device_profiles SET runtime_primary=0 WHERE runtime_active=0")

    def _refresh_primary_after_active_change(self, conn) -> Optional[int]:
        primary = conn.execute(
            """
            SELECT d.id
              FROM device_profiles d
              LEFT JOIN runtime_policies p ON p.radio_profile_id=d.id
             WHERE d.runtime_primary=1
               AND d.runtime_active=1
               AND d.enabled=1
               AND COALESCE(p.operator_suppressed, 0)=0
               AND LOWER(COALESCE(d.device_class, 'tx_rx')) <> 'observer'
             LIMIT 1
            """
        ).fetchone()
        if primary is not None:
            return int(primary[0])
        candidate = self._eligible_primary_id(conn)
        self._set_primary_id_direct(conn, candidate)
        return candidate

    def set_primary_runtime_radio(
        self,
        radio_profile_id: Optional[str],
        *,
        source: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> RuntimeSelectionState:
        _check_source("set_primary_runtime_radio", source, _PRIMARY_WRITE_SOURCES)
        self._ensure_ready(runtime_status, write=True)
        if radio_profile_id is None:
            with self.store.connect() as conn:
                self._set_primary_id_direct(conn, None)
                conn.commit()
            return self.state(build_multi_rig_runtime_status(self.store))

        device_id = device_profile_id_from_radio_id(radio_profile_id)
        with self.store.connect() as conn:
            row = conn.execute(
                """
                SELECT d.*, COALESCE(p.operator_suppressed, 0) AS operator_suppressed
                  FROM device_profiles d
                  LEFT JOIN runtime_policies p ON p.radio_profile_id=d.id
                 WHERE d.id=?
                """,
                (int(device_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown device profile id: {device_id}")
            data = dict(row)
            if not _bool(data.get("enabled"), True):
                raise SelectionWriteError("Primary runtime radio must be enabled.")
            if not _bool(data.get("runtime_active"), False):
                raise SelectionWriteError("Primary runtime radio must be runtime active.")
            if _bool(data.get("operator_suppressed"), False):
                raise SelectionWriteError("Primary runtime radio cannot be operator-suppressed.")
            if str(data.get("device_class", "") or "").strip().lower() == "observer":
                raise SelectionWriteError("Observer / SDR radios cannot be primary runtime radios.")
            self._set_primary_id_direct(conn, device_id)
            conn.commit()
            project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(device_id))
        return self.state(build_multi_rig_runtime_status(self.store))

    def set_active_runtime_radios(
        self,
        radio_profile_ids: Sequence[str],
        *,
        source: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> RuntimeSelectionState:
        """Replace the entire active runtime radio set.

        Callers that want to add one radio while preserving others must read the
        current active IDs first and pass the full desired set.
        """
        _check_source("set_active_runtime_radios", source, _ACTIVE_WRITE_SOURCES)
        self._ensure_ready(runtime_status, write=True)
        device_ids = tuple(dict.fromkeys(device_profile_id_from_radio_id(radio_id) for radio_id in radio_profile_ids))
        with self.store.connect() as conn:
            for device_id in device_ids:
                self.policy_store._ensure_policy_row(conn, int(device_id))
                row = conn.execute(
                    """
                    SELECT d.enabled,
                           d.device_class,
                           COALESCE(p.operator_suppressed, 0) AS operator_suppressed
                      FROM device_profiles d
                      LEFT JOIN runtime_policies p ON p.radio_profile_id=d.id
                     WHERE d.id=?
                    """,
                    (int(device_id),),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown device profile id: {device_id}")
                row_data = dict(row)
                if not _bool(row_data.get("enabled"), True):
                    raise SelectionWriteError(f"Cannot activate disabled radio {radio_shared_state_id(device_id)}.")
                if _bool(row_data.get("operator_suppressed"), False):
                    raise SelectionWriteError(f"Cannot activate operator-suppressed radio {radio_shared_state_id(device_id)}.")
                if str(row_data.get("device_class", "") or "").strip().lower() == "observer":
                    operating = self.policy_store._effective_operating_row(conn, int(device_id))
                    if not operating or not _bool(operating.get("receive_only"), False):
                        raise SelectionWriteError(
                            "Observer / SDR radios require a receive-only assigned Frequency Plan before activation."
                        )
            id_set = set(device_ids)
            rows = conn.execute("SELECT id FROM device_profiles").fetchall()
            for row in rows:
                conn.execute("UPDATE device_profiles SET runtime_active=? WHERE id=?", (1 if int(row[0]) in id_set else 0, int(row[0])))
            primary_id = self._refresh_primary_after_active_change(conn)
            conn.commit()
            if primary_id is not None:
                project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(primary_id))
        return self.state(build_multi_rig_runtime_status(self.store))

    def remove_from_active_runtime(
        self,
        radio_profile_id: str,
        *,
        source: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> RuntimeSelectionState:
        _check_source("remove_from_active_runtime", source, {SOURCE_RUNTIME_POLICY, SOURCE_SCHEDULER, SOURCE_LAUNCH_ORCHESTRATOR})
        self._ensure_ready(runtime_status, write=True)
        device_id = device_profile_id_from_radio_id(radio_profile_id)
        with self.store.connect() as conn:
            row = conn.execute("SELECT id FROM device_profiles WHERE id=?", (int(device_id),)).fetchone()
            if row is None:
                raise KeyError(f"Unknown device profile id: {device_id}")
            conn.execute("UPDATE device_profiles SET runtime_active=0, runtime_primary=0 WHERE id=?", (int(device_id),))
            primary_id = self._refresh_primary_after_active_change(conn)
            conn.commit()
            if primary_id is not None:
                project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(primary_id))
        return self.state(build_multi_rig_runtime_status(self.store))

    def activate_discovered_radios(
        self,
        flrig_health: Mapping[str, bool],
        *,
        source: str,
        runtime_status: Optional[MultiRigRuntimeStatus] = None,
    ) -> RuntimeSelectionState:
        _check_source("activate_discovered_radios", source, _ACTIVE_WRITE_SOURCES)
        status = self._ensure_ready(runtime_status, write=True)
        candidates = self.policy_store.discover_runtime_candidates(flrig_health, runtime_status=status)
        return self.set_active_runtime_radios(candidates, source=source, runtime_status=status)
