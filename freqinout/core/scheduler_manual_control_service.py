from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.multi_rig_runtime_status import device_profile_id_from_radio_id, radio_shared_state_id
from freqinout.core.shared_state import SchedulerManualControlState, SchedulerManualTarget


MANUAL_CONTROL_STATES = frozenset(
    {
        "on_schedule",
        "manual_hold",
        "manual_suspend",
        "manual_qsy",
        "busy_hold",
        "unavailable",
    }
)
OPERATOR_SOURCES = frozenset(
    {
        "main_control_center",
        "controlfreq",
        "scheduler_prompt",
        "net_control",
        "scheduler",
        "api",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, default: str = "") -> str:
    try:
        text = str(value if value is not None else default).strip()
    except Exception:
        text = str(default or "").strip()
    return text or default


def _normalize_state(value: Any, default: str = "on_schedule") -> str:
    state = _clean_text(value, default).lower()
    return state if state in MANUAL_CONTROL_STATES else default


def _normalize_operator_source(value: Any, default: str = "scheduler") -> str:
    source = _clean_text(value, default).lower()
    return source if source in OPERATOR_SOURCES else default


def _manual_target_to_json(target: Optional[SchedulerManualTarget]) -> str:
    if target is None:
        return "{}"
    return json.dumps(
        {
            "frequency_hz": int(target.frequency_hz or 0),
            "mode": _clean_text(target.mode),
            "vfo": _clean_text(target.vfo),
            "offset_hz": target.offset_hz,
            "source_action": _clean_text(target.source_action),
            "set_at_utc": _clean_text(target.set_at_utc, _utc_now_iso()),
        },
        sort_keys=True,
    )


def _manual_target_is_valid(target: Optional[SchedulerManualTarget]) -> bool:
    return target is not None and int(target.frequency_hz or 0) > 0


def _manual_target_from_json(value: Any) -> Optional[SchedulerManualTarget]:
    if value in (None, "", "{}"):
        return None
    try:
        raw = json.loads(str(value))
    except Exception:
        return None
    if not isinstance(raw, Mapping):
        return None
    frequency_hz = int(raw.get("frequency_hz", 0) or 0)
    if frequency_hz <= 0:
        return None
    offset_raw = raw.get("offset_hz")
    try:
        offset_hz = int(offset_raw) if offset_raw not in (None, "") else None
    except Exception:
        offset_hz = None
    return SchedulerManualTarget(
        frequency_hz=frequency_hz,
        mode=_clean_text(raw.get("mode")),
        vfo=_clean_text(raw.get("vfo")),
        offset_hz=offset_hz,
        source_action=_clean_text(raw.get("source_action")),
        set_at_utc=_clean_text(raw.get("set_at_utc"), _utc_now_iso()),
    )


def _radio_device_id(radio_profile_id: str | int) -> int:
    if isinstance(radio_profile_id, int):
        return int(radio_profile_id)
    text = _clean_text(radio_profile_id)
    if text.startswith("radio_"):
        return device_profile_id_from_radio_id(text)
    return int(text)


class SchedulerManualControlService:
    """Durable per-radio manual scheduler control state.

    This service is a Phase 6 foundation. It records operator intent without
    changing scheduler execution behavior yet.
    """

    def __init__(self, store: Optional[MultiRadioStore] = None) -> None:
        self.store = store or MultiRadioStore()

    def get_state(self, radio_profile_id: str | int) -> SchedulerManualControlState:
        device_id = _radio_device_id(radio_profile_id)
        # This public compatibility service may be used before the startup
        # owner has created the database (including CLI/tests). Scheduler timer
        # paths consume its worker-published cache and never call this inline.
        with self.store.connect() as conn:
            self._ensure_radio_exists(conn, device_id)
            row = conn.execute(
                "SELECT * FROM scheduler_manual_control_states WHERE radio_profile_id=?",
                (device_id,),
            ).fetchone()
            if row is None:
                now = _utc_now_iso()
                return SchedulerManualControlState(
                    radio_profile_id=radio_shared_state_id(device_id),
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            return self._state_from_row(dict(row))

    def list_active_states(self) -> tuple[SchedulerManualControlState, ...]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM scheduler_manual_control_states
                 WHERE state<>'on_schedule'
              ORDER BY radio_profile_id ASC
                """
            ).fetchall()
            return tuple(self._state_from_row(dict(row)) for row in rows)

    def clear_manual_qsy_states(self) -> int:
        """Clear runtime-only manual QSY state left from a previous app run."""
        now = _utc_now_iso()
        with self.store.connect() as conn:
            cur = conn.execute(
                """
                UPDATE scheduler_manual_control_states
                   SET state='on_schedule',
                       manual_target_json='{}',
                       hold_until_utc=NULL,
                       reason_code='startup_clear_manual_qsy',
                       operator_source='scheduler',
                       latest_event_id=NULL,
                       updated_utc=?
                 WHERE state='manual_qsy'
                """,
                (now,),
            )
            conn.commit()
            try:
                return int(cur.rowcount or 0)
            except Exception:
                return 0

    def set_manual_qsy(
        self,
        radio_profile_id: str | int,
        target: SchedulerManualTarget,
        *,
        hold_until_utc: Optional[str] = None,
        reason_code: str = "",
        operator_source: str = "controlfreq",
        latest_event_id: Optional[str] = None,
    ) -> SchedulerManualControlState:
        if not _manual_target_is_valid(target):
            raise ValueError("Manual QSY requires a target frequency.")
        existing = self.get_state(radio_profile_id)
        preserved_hold = hold_until_utc
        if preserved_hold is None and existing.state in {"manual_hold", "manual_qsy"}:
            try:
                deadline = datetime.fromisoformat(str(existing.hold_until_utc).replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline > datetime.now(timezone.utc):
                    preserved_hold = existing.hold_until_utc
            except (ValueError, TypeError):
                pass
        return self.save_state(
            SchedulerManualControlState(
                radio_profile_id=existing.radio_profile_id,
                state="manual_qsy",
                manual_target=target,
                hold_until_utc=preserved_hold,
                reason_code=reason_code,
                operator_source=operator_source,
                latest_event_id=latest_event_id,
                created_at_utc=existing.created_at_utc,
            )
        )

    def hold(
        self,
        radio_profile_id: str | int,
        *,
        hold_until_utc: str,
        manual_target: Optional[SchedulerManualTarget] = None,
        reason_code: str = "",
        operator_source: str = "main_control_center",
        latest_event_id: Optional[str] = None,
    ) -> SchedulerManualControlState:
        existing = self.get_state(radio_profile_id)
        return self.save_state(
            SchedulerManualControlState(
                radio_profile_id=existing.radio_profile_id,
                state="manual_hold",
                manual_target=manual_target if manual_target is not None else existing.manual_target,
                hold_until_utc=hold_until_utc,
                reason_code=reason_code,
                operator_source=operator_source,
                latest_event_id=latest_event_id,
                created_at_utc=existing.created_at_utc,
            )
        )

    def suspend(
        self,
        radio_profile_id: str | int,
        *,
        reason_code: str = "",
        operator_source: str = "main_control_center",
        latest_event_id: Optional[str] = None,
    ) -> SchedulerManualControlState:
        existing = self.get_state(radio_profile_id)
        return self.save_state(
            SchedulerManualControlState(
                radio_profile_id=existing.radio_profile_id,
                state="manual_suspend",
                manual_target=existing.manual_target,
                hold_until_utc=existing.hold_until_utc,
                reason_code=reason_code,
                operator_source=operator_source,
                latest_event_id=latest_event_id,
                created_at_utc=existing.created_at_utc,
            )
        )

    def resume(self, radio_profile_id: str | int, *, latest_event_id: Optional[str] = None) -> SchedulerManualControlState:
        existing = self.get_state(radio_profile_id)
        return self.save_state(
            SchedulerManualControlState(
                radio_profile_id=existing.radio_profile_id,
                state="on_schedule",
                latest_event_id=latest_event_id,
                operator_source="scheduler",
                created_at_utc=existing.created_at_utc,
            )
        )

    def resume_expired_hold(self, expected: SchedulerManualControlState) -> bool:
        """Clear only the expired version; a newer operator intent wins atomically."""
        if expected.state not in {"manual_hold", "manual_qsy"} or not expected.hold_until_utc:
            return False
        deadline = datetime.fromisoformat(expected.hold_until_utc.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < deadline:
            return False
        with self.store.connect() as conn:
            cur = conn.execute(
                """UPDATE scheduler_manual_control_states
                      SET state='on_schedule', manual_target_json='{}', hold_until_utc=NULL,
                          reason_code='hold_expired', operator_source='scheduler',
                          latest_event_id=NULL, updated_utc=?
                    WHERE radio_profile_id=? AND state=? AND hold_until_utc=?
                      AND manual_target_json=? AND updated_utc=?
                      AND reason_code=? AND operator_source=? AND latest_event_id IS ?""",
                (_utc_now_iso(), _radio_device_id(expected.radio_profile_id), expected.state,
                 expected.hold_until_utc, _manual_target_to_json(expected.manual_target),
                 expected.updated_at_utc, expected.reason_code, expected.operator_source,
                 expected.latest_event_id),
            )
            conn.commit()
            return bool(cur.rowcount)

    def save_state(self, state: SchedulerManualControlState) -> SchedulerManualControlState:
        device_id = _radio_device_id(state.radio_profile_id)
        now = _utc_now_iso()
        normalized = SchedulerManualControlState(
            radio_profile_id=radio_shared_state_id(device_id),
            state=_normalize_state(state.state),
            manual_target=state.manual_target,
            hold_until_utc=state.hold_until_utc,
            reason_code=_clean_text(state.reason_code),
            operator_source=_normalize_operator_source(state.operator_source),
            latest_event_id=state.latest_event_id,
            created_at_utc=state.created_at_utc or now,
            updated_at_utc=now,
        )
        if normalized.state == "manual_qsy" and not _manual_target_is_valid(normalized.manual_target):
            raise ValueError("Manual QSY requires a target frequency.")
        with self.store.connect() as conn:
            self._ensure_radio_exists(conn, device_id)
            conn.execute(
                """
                INSERT INTO scheduler_manual_control_states (
                    radio_profile_id,
                    state,
                    manual_target_json,
                    hold_until_utc,
                    reason_code,
                    operator_source,
                    latest_event_id,
                    created_utc,
                    updated_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(radio_profile_id) DO UPDATE SET
                    state=excluded.state,
                    manual_target_json=excluded.manual_target_json,
                    hold_until_utc=excluded.hold_until_utc,
                    reason_code=excluded.reason_code,
                    operator_source=excluded.operator_source,
                    latest_event_id=excluded.latest_event_id,
                    updated_utc=excluded.updated_utc
                """,
                (
                    device_id,
                    normalized.state,
                    _manual_target_to_json(normalized.manual_target),
                    normalized.hold_until_utc,
                    normalized.reason_code,
                    normalized.operator_source,
                    normalized.latest_event_id,
                    normalized.created_at_utc,
                    normalized.updated_at_utc,
                ),
            )
            conn.commit()
        return self.get_state(device_id)

    @staticmethod
    def _ensure_radio_exists(conn: Any, device_id: int) -> None:
        row = conn.execute("SELECT id FROM device_profiles WHERE id=?", (int(device_id),)).fetchone()
        if row is None:
            raise KeyError(f"Unknown radio id: {radio_shared_state_id(device_id)}")

    @staticmethod
    def _state_from_row(row: Mapping[str, Any]) -> SchedulerManualControlState:
        return SchedulerManualControlState(
            radio_profile_id=radio_shared_state_id(row.get("radio_profile_id")),
            state=_normalize_state(row.get("state")),
            manual_target=_manual_target_from_json(row.get("manual_target_json")),
            hold_until_utc=_clean_text(row.get("hold_until_utc")) or None,
            reason_code=_clean_text(row.get("reason_code")),
            operator_source=_normalize_operator_source(row.get("operator_source")),
            latest_event_id=_clean_text(row.get("latest_event_id")) or None,
            created_at_utc=_clean_text(row.get("created_utc"), _utc_now_iso()),
            updated_at_utc=_clean_text(row.get("updated_utc"), _utc_now_iso()),
        )
