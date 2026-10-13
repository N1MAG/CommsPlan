from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from freqinout.core.js8_expect_store import (
    ExpectEvaluationResult,
    ExpectRequestClaimResult,
    claim_expect_request,
    complete_expect_request_claim,
    default_expect_db_path,
)
from freqinout.core.js8_msg_auth import sign_js8_text
from freqinout.core.js8_msg_auth_store import MSG_AUTH_SCOPE_SIGNING, load_msg_auth_keys
from freqinout.core.js8_send_service import JS8SendResult, send_js8_message_guarded
from freqinout.core.sqlite_utils import connect_sqlite_readonly, connect_sqlite_runtime_write, table_exists
from freqinout.radio_interface.js8_api_client import JS8ApiClient


@dataclass(frozen=True)
class ExpectDispatchResult:
    decision: str
    reason: str
    sent: bool = False
    transmitted_text: str = ""
    send_result: Optional[JS8SendResult] = None


def _record_dispatch_audit(
    *,
    evaluation: ExpectEvaluationResult,
    decision: str,
    reason: str,
    event_id: str = "",
    source_radio_id: object = "",
    source_js8_instance_id: object = "",
    requesting_callsign: object = "",
    target_group: object = "",
    transmitted_text: str = "",
    db_path: Optional[str | Path] = None,
) -> None:
    path = Path(db_path) if db_path is not None else default_expect_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite_runtime_write(path)
    try:
        conn.execute(
            """
            INSERT INTO js8_expect_dispatch_audit
                (event_id, expect_entry_id, expect_key, source_radio_id, source_js8_instance_id,
                 requesting_callsign, target_group, decision, reason, reply_radio_id,
                 reply_js8_instance_id, transmitted_text, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event_id or ""),
                int(evaluation.expect_entry_id or 0),
                str(evaluation.expect_key or "").strip().upper(),
                str(source_radio_id or ""),
                str(source_js8_instance_id or ""),
                str(requesting_callsign or "").strip().upper().lstrip("@"),
                str(target_group or "").strip().upper(),
                str(decision or ""),
                str(reason or ""),
                str(evaluation.reply_radio_id or ""),
                str(evaluation.reply_js8_instance_id or ""),
                str(transmitted_text or ""),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_expect_dispatch_audit(
    *,
    db_path: Optional[str | Path] = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    path = Path(db_path) if db_path is not None else default_expect_db_path()
    if not path.exists():
        return []
    conn = connect_sqlite_readonly(path)
    try:
        if not table_exists(conn, "js8_expect_dispatch_audit"):
            return []
        rows = conn.execute(
            """
            SELECT id, event_id, expect_entry_id, expect_key, source_radio_id, source_js8_instance_id,
                   requesting_callsign, target_group, decision, reason, reply_radio_id,
                   reply_js8_instance_id, transmitted_text, created_ts
            FROM js8_expect_dispatch_audit
            ORDER BY created_ts DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(500, int(limit or 50))),),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, object]] = []
    for row in rows:
        if hasattr(row, "keys"):
            out.append({key: row[key] for key in row.keys()})
        else:
            out.append(
                {
                    "id": row[0],
                    "event_id": row[1],
                    "expect_entry_id": row[2],
                    "expect_key": row[3],
                    "source_radio_id": row[4],
                    "source_js8_instance_id": row[5],
                    "requesting_callsign": row[6],
                    "target_group": row[7],
                    "decision": row[8],
                    "reason": row[9],
                    "reply_radio_id": row[10],
                    "reply_js8_instance_id": row[11],
                    "transmitted_text": row[12],
                    "created_ts": row[13],
                }
            )
    return out


def record_expect_dispatch_hold(
    *,
    evaluation: ExpectEvaluationResult,
    reason: str,
    event_id: str = "",
    source_radio_id: object = "",
    source_js8_instance_id: object = "",
    requesting_callsign: object = "",
    target_group: object = "",
    db_path: Optional[str | Path] = None,
) -> ExpectDispatchResult:
    result = ExpectDispatchResult("held", str(reason or "Expect auto-reply was held."))
    _record_dispatch_audit(
        evaluation=evaluation,
        decision=result.decision,
        reason=result.reason,
        event_id=event_id,
        source_radio_id=source_radio_id,
        source_js8_instance_id=source_js8_instance_id,
        requesting_callsign=requesting_callsign,
        target_group=target_group,
        db_path=db_path,
    )
    return result


def _reply_target(*, target_group: object = "", requesting_callsign: object = "") -> str:
    target = str(target_group or "").strip().upper()
    if target:
        return target
    return str(requesting_callsign or "").strip().upper().lstrip("@")


def _target_neutral_response_text(response_text: object, target: str) -> str:
    text = str(response_text or "").strip()
    target_text = str(target or "").strip()
    if not text or not target_text:
        return text
    upper = text.upper()
    target_upper = target_text.upper()
    if upper == target_upper:
        return ""
    if upper.startswith(target_upper + " "):
        return text[len(target_text):].strip()
    return text


def _build_expect_reply_text(
    evaluation: ExpectEvaluationResult,
    *,
    target_group: object = "",
    requesting_callsign: object = "",
    relay_path: object = "",
    db_path: Optional[str | Path] = None,
) -> tuple[str, str]:
    target = _reply_target(target_group=target_group, requesting_callsign=requesting_callsign)
    payload = _target_neutral_response_text(evaluation.response_text, target)
    route = str(relay_path or "").strip().upper().strip(">")
    if route and any(
        re.fullmatch(r"[A-Z0-9/]+", part) is None
        for part in route.split(">")
    ):
        return "", "invalid JS8 relay path"
    addressed_target = f"{route}>{target}" if route and target else target
    if not target:
        return payload, "no target available"
    if not payload:
        return addressed_target, "empty payload"
    sign_detail = ""
    body = payload
    if bool(evaluation.msg_auth_sign_enabled):
        from_call = str(evaluation.msg_auth_sign_callsign or "").strip().upper()
        if from_call:
            keys = load_msg_auth_keys(
                group_name=target,
                callsign=from_call,
                key_scope=MSG_AUTH_SCOPE_SIGNING,
                db_path=Path(db_path) if db_path is not None else None,
            )
            key = keys[0] if keys else None
            if key and str(key.key or "").strip():
                body = sign_js8_text(
                    from_call,
                    target,
                    payload,
                    key.key,
                    include_datecode=bool(evaluation.msg_auth_include_datecode),
                    datecode=str(evaluation.msg_auth_datecode or ""),
                ).signed_text
                sign_detail = f"signed MsgAuth for {target}"
            else:
                sign_detail = f"MsgAuth signing enabled, no key for {target}; sent unsigned"
        else:
            sign_detail = "MsgAuth signing enabled, no signing callsign; sent unsigned"
    return " ".join(part for part in (addressed_target, body) if part).strip(), sign_detail


def dispatch_expect_auto_reply(
    *,
    evaluation: ExpectEvaluationResult,
    client: JS8ApiClient,
    runtime_unattended_enabled: bool = False,
    event_id: str = "",
    source_radio_id: object = "",
    source_js8_instance_id: object = "",
    requesting_callsign: object = "",
    target_group: object = "",
    relay_path: object = "",
    db_path: Optional[str | Path] = None,
    timeout_s: float = 0.8,
    claim_event_key: str = "",
    claim_q_id: str = "",
    claim_already_acquired: bool = False,
) -> ExpectDispatchResult:
    if evaluation.decision != "reply-ready":
        result = ExpectDispatchResult("skipped", f"Expect decision is {evaluation.decision}; no auto-reply sent.")
        _record_dispatch_audit(
            evaluation=evaluation,
            decision=result.decision,
            reason=result.reason,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=source_js8_instance_id,
            requesting_callsign=requesting_callsign,
            target_group=target_group,
            db_path=db_path,
        )
        return result
    if not evaluation.auto_reply_enabled:
        result = ExpectDispatchResult("skipped", "Expect entry auto-reply is not enabled.")
        _record_dispatch_audit(
            evaluation=evaluation,
            decision=result.decision,
            reason=result.reason,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=source_js8_instance_id,
            requesting_callsign=requesting_callsign,
            target_group=target_group,
            db_path=db_path,
        )
        return result
    if not runtime_unattended_enabled:
        result = ExpectDispatchResult("held", "Runtime unattended Expect auto-reply is disabled.")
        _record_dispatch_audit(
            evaluation=evaluation,
            decision=result.decision,
            reason=result.reason,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=source_js8_instance_id,
            requesting_callsign=requesting_callsign,
            target_group=target_group,
            db_path=db_path,
        )
        return result
    if not evaluation.unattended_auto_reply_enabled:
        result = ExpectDispatchResult("held", "Expect entry is not approved for unattended auto-reply.")
        _record_dispatch_audit(
            evaluation=evaluation,
            decision=result.decision,
            reason=result.reason,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=source_js8_instance_id,
            requesting_callsign=requesting_callsign,
            target_group=target_group,
            db_path=db_path,
        )
        return result
    response_text, sign_detail = _build_expect_reply_text(
        evaluation,
        target_group=target_group,
        requesting_callsign=requesting_callsign,
        relay_path=relay_path,
        db_path=db_path,
    )
    if not response_text:
        result = ExpectDispatchResult("blocked", "Expect reply text is empty.")
        _record_dispatch_audit(
            evaluation=evaluation,
            decision=result.decision,
            reason=result.reason,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=source_js8_instance_id,
            requesting_callsign=requesting_callsign,
            target_group=target_group,
            db_path=db_path,
        )
        return result

    claim = None
    if str(claim_event_key or "").strip():
        if claim_already_acquired:
            claim = ExpectRequestClaimResult(True, "claimed", "Request claim was acquired by source dispatch.", str(claim_event_key))
        else:
            claim = claim_expect_request(
                event_key=str(claim_event_key),
                expect_entry_id=int(evaluation.expect_entry_id or 0),
                q_id=str(claim_q_id or evaluation.q_id or evaluation.expect_key or ""),
                source_radio_id=source_radio_id,
                source_js8_instance_id=source_js8_instance_id,
                requesting_callsign=requesting_callsign,
                target_group=target_group,
                max_replies=int(evaluation.max_replies or 1),
                cooldown_seconds=int(evaluation.cooldown_seconds or 0),
                db_path=Path(db_path) if db_path is not None else None,
            )
        if not claim.acquired:
            result = ExpectDispatchResult(claim.status, claim.reason)
            _record_dispatch_audit(
                evaluation=evaluation,
                decision=result.decision,
                reason=result.reason,
                event_id=event_id,
                source_radio_id=source_radio_id,
                source_js8_instance_id=source_js8_instance_id,
                requesting_callsign=requesting_callsign,
                target_group=target_group,
                db_path=db_path,
            )
            return result

    send_result = send_js8_message_guarded(
        client,
        response_text,
        timeout_s=timeout_s,
        clear_selected_target=True,
    )
    decision = "sent" if send_result.sent else "blocked"
    reason = send_result.detail
    if not send_result.sent:
        selected_target_issue = next(
            (issue for issue in send_result.preflight.issues if issue.code == "selected_target_present" and issue.blocking),
            None,
        )
        if selected_target_issue is not None:
            selected_target = str(send_result.preflight.selected_call or "").strip()
            reason = (
                f"JS8Call still has selected target '{selected_target}'. "
                "Deselect it in JS8Call before CommsPlan can send this automatic reply."
            )
    if sign_detail:
        reason = f"{reason} ({sign_detail})"
    result = ExpectDispatchResult(
        decision,
        reason,
        sent=bool(send_result.sent),
        transmitted_text=send_result.transmitted_text,
        send_result=send_result,
    )
    if claim is not None:
        complete_expect_request_claim(
            event_key=claim.event_key,
            status="sent" if result.sent else "failed",
            reason=reason,
            reply_text=result.transmitted_text,
            db_path=Path(db_path) if db_path is not None else None,
            retry_after_seconds=30 if not result.sent else 0,
        )
    _record_dispatch_audit(
        evaluation=evaluation,
        decision=result.decision,
        reason=result.reason,
        event_id=event_id,
        source_radio_id=source_radio_id,
        source_js8_instance_id=source_js8_instance_id,
        requesting_callsign=requesting_callsign,
        target_group=target_group,
        transmitted_text=result.transmitted_text,
        db_path=db_path,
    )
    return result
