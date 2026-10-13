from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Sequence

from freqinout.core.message_file_scanner import FileRecord
from freqinout.core.message_row_identity import message_row_identity
from freqinout.core.message_row_presentation import relative_age_label


@dataclass(frozen=True)
class MessageDeleteCapability:
    source_label: str
    effect_label: str
    audit_action: str
    deletable: bool = True
    requires_source_identity: bool = True


@dataclass(frozen=True)
class MessageDeleteExecutionResult:
    result: str
    detail_key: str
    deleted_row: bool = False
    hidden: bool = False
    warning: str = ""


def _class_name(value: Any) -> str:
    return value.__class__.__name__ if value is not None else ""


def _source_label_for_origin(origin: object, msg_type: object = "") -> str:
    key = str(origin or "").strip().lower()
    labels = {
        "js8": "JS8Call",
        "spotter": "CommsPlan Spotter",
        "varac": "VarAC",
        "commstat": "CommStat",
        "sitrep": "SitRep",
        "flmsg": "FLMSG",
        "flamp": "FLAMP",
        "bbs": "BBS",
        "bbs_archive": "BBS Archive",
    }
    if key in labels:
        return labels[key]
    text = str(msg_type or origin or "Message").strip()
    if text.upper() in {"FLMSG", "FLAMP"}:
        return text.upper()
    return text or "Message"


def message_delete_capability(payload: Any, *, origin: object = "", msg_type: object = "") -> MessageDeleteCapability:
    source_label = _source_label_for_origin(origin, msg_type)
    cls = _class_name(payload)
    if isinstance(payload, FileRecord):
        return MessageDeleteCapability(
            source_label=source_label,
            effect_label="Move files to Recycle Bin",
            audit_action="Move files to Recycle Bin",
        )
    if cls == "CommStatArtifact":
        return MessageDeleteCapability(
            source_label="CommStat",
            effect_label="Delete source row when safe; otherwise hide from CommsPlan Messages",
            audit_action="CommStat source delete or CommsPlan hide",
        )
    if cls == "ProjectedMessagePayload":
        return MessageDeleteCapability(
            source_label=source_label,
            effect_label="Hide from CommsPlan projection",
            audit_action="Hide from CommsPlan projection",
            requires_source_identity=False,
        )
    if cls == "VarACMessage":
        return MessageDeleteCapability(
            source_label="VarAC",
            effect_label="Mark deleted in VarAC",
            audit_action="Mark deleted in VarAC",
        )
    if cls in {"JS8Message", "SpotterMessage", "SitrepMessage"}:
        return MessageDeleteCapability(
            source_label={
                "JS8Message": "JS8Call",
                "SpotterMessage": "CommsPlan Spotter",
                "SitrepMessage": "SitRep",
            }.get(cls, source_label),
            effect_label="Delete from CommsPlan message store",
            audit_action="Delete from CommsPlan message store",
        )
    return MessageDeleteCapability(
        source_label=source_label,
        effect_label="Delete",
        audit_action="Delete",
        deletable=False,
        requires_source_identity=False,
    )


def message_delete_result_detail(payload: Any, result: str) -> str:
    cls = _class_name(payload)
    result_key = str(result or "").strip().lower()
    if cls == "CommStatArtifact":
        if result_key == "deleted_source":
            return "CommStat source row deleted and CommsPlan projection removed"
        if result_key == "deleted_projection":
            return "CommStat source row was already absent; stale CommsPlan projection removed"
        if result_key == "hidden":
            return "CommStat artifact hidden from CommsPlan Messages; source row was not safely identifiable"
        if result_key == "skipped":
            return "CommStat artifact has no stable identity"
        return "CommStat item not deleted"
    if cls == "ProjectedMessagePayload":
        if result_key == "hidden":
            return "projected message hidden from CommsPlan views"
        if result_key == "skipped":
            return "missing projected message id"
        return "projected message not hidden"
    if isinstance(payload, FileRecord):
        if result_key == "deleted":
            return "file moved to Recycle Bin"
        if result_key == "skipped":
            return "file no longer exists"
        return "file not moved to Recycle Bin"
    if cls == "JS8Message":
        if result_key == "deleted":
            return "JS8Call inbox/local rows deleted"
        if result_key == "skipped":
            return "missing JS8 id"
        return "JS8Call inbox row not deleted"
    if cls == "VarACMessage":
        if result_key == "deleted":
            return "VarAC source row marked deleted and local row removed"
        if result_key == "skipped":
            return "missing VarAC id"
        return "VarAC source row not marked deleted"
    if cls == "SpotterMessage":
        if result_key == "deleted":
            return "Spotter row deleted from CommsPlan store"
        if result_key == "skipped":
            return "missing Spotter id"
        return "Spotter row not deleted"
    if cls == "SitrepMessage":
        if result_key == "deleted":
            return "SitRep row deleted from CommsPlan store"
        return "SitRep row not deleted"
    if result_key == "skipped":
        return "unsupported message payload"
    return "message not deleted"


def delete_success_result(detail_key: str = "deleted", *, hidden: bool = False) -> MessageDeleteExecutionResult:
    return MessageDeleteExecutionResult("deleted", str(detail_key or "deleted"), deleted_row=True, hidden=bool(hidden))


def missing_identity_delete_result(detail_key: str = "skipped", *, warning: str = "") -> MessageDeleteExecutionResult:
    return MessageDeleteExecutionResult("skipped", str(detail_key or "skipped"), warning=str(warning or ""))


def failed_source_delete_result(
    payload: Any,
    *,
    detail_key: str = "failed",
    fallback_warning: str = "",
) -> MessageDeleteExecutionResult:
    warning = str(fallback_warning or "").strip()
    if not warning:
        cls = _class_name(payload)
        if cls in {"JS8Message", "SpotterMessage", "VarACMessage"}:
            msg_id = int(getattr(payload, "spotter_id", 0) or getattr(payload, "msg_id", 0) or 0)
            warning = f"Failed to delete Message {msg_id}." if msg_id > 0 else "Failed to delete message."
        elif cls == "CommStatArtifact":
            warning = (
                "CommsPlan could not delete this CommStat item. The message database was busy or unavailable. "
                "Try Refresh Now and delete it again."
            )
        elif isinstance(payload, FileRecord):
            warning = "Failed to move file to the Recycle Bin."
    return MessageDeleteExecutionResult("failed", str(detail_key or "failed"), warning=warning)


def commstat_delete_execution_result(result: object) -> MessageDeleteExecutionResult:
    key = str(result or "").strip()
    if key == "skipped":
        return missing_identity_delete_result(
            "skipped",
            warning="CommsPlan could not hide this CommStat item because it does not have a stable message identity.",
        )
    if key not in {"deleted_source", "deleted_projection", "hidden"}:
        return MessageDeleteExecutionResult(
            "failed",
            "failed",
            warning=(
                "CommsPlan could not delete this CommStat item. The message database was busy or unavailable. "
                "Try Refresh Now and delete it again."
            ),
        )
    return delete_success_result(key, hidden=(key == "hidden"))


def delete_source_label_for_row(row: object) -> str:
    capability = message_delete_capability(
        getattr(row, "payload", None),
        origin=getattr(row, "origin", ""),
        msg_type=getattr(row, "msg_type", ""),
    )
    return capability.source_label


def delete_effect_label_for_row(row: object) -> str:
    capability = message_delete_capability(
        getattr(row, "payload", None),
        origin=getattr(row, "origin", ""),
        msg_type=getattr(row, "msg_type", ""),
    )
    return capability.effect_label


def delete_audit_action_for_row(row: object) -> str:
    capability = message_delete_capability(
        getattr(row, "payload", None),
        origin=getattr(row, "origin", ""),
        msg_type=getattr(row, "msg_type", ""),
    )
    return capability.audit_action


def summarize_delete_sources(rows: Sequence[object]) -> str:
    counts: Dict[str, int] = {}
    for row in rows:
        label = delete_source_label_for_row(row)
        counts[label] = counts.get(label, 0) + 1
    return ", ".join(f"{label}: {counts[label]}" for label in sorted(counts))


def summarize_delete_effects(rows: Sequence[object]) -> str:
    counts: Dict[str, int] = {}
    for row in rows:
        label = delete_effect_label_for_row(row)
        counts[label] = counts.get(label, 0) + 1
    return "\n".join(f"- {label}: {counts[label]}" for label in sorted(counts))


def collect_deletable_message_rows(rows: Sequence[object]) -> list[object]:
    out: list[object] = []
    for row in rows or ():
        capability = message_delete_capability(
            getattr(row, "payload", None),
            origin=getattr(row, "origin", ""),
            msg_type=getattr(row, "msg_type", ""),
        )
        if capability.deletable and message_row_identity(row) is not None:
            out.append(row)
    return out


def delete_effect_tooltip(rows: Sequence[object]) -> str:
    clean_rows = [row for row in rows if row is not None]
    if not clean_rows:
        return "Select messages to see delete actions."
    lines = [
        f"Selected: {len(clean_rows)}",
        f"Sources: {summarize_delete_sources(clean_rows) or 'Unknown'}",
        "Delete actions:",
    ]
    effects = summarize_delete_effects(clean_rows)
    lines.extend(effects.splitlines() if effects else ["- Delete"])
    return "\n".join(lines)


def message_row_summary_line(row: object) -> str:
    route = " -> ".join(
        part
        for part in (
            str(getattr(row, "from_call", "") or "").strip(),
            str(getattr(row, "to_call", "") or "").strip(),
        )
        if part
    )
    topics = ", ".join(str(topic) for topic in (getattr(row, "topics", ()) or ()) if str(topic or "").strip())
    parts = [
        str(getattr(row, "msg_type", "") or getattr(row, "origin", "") or "Message").strip(),
        str(getattr(row, "status", "") or "").strip(),
        relative_age_label(getattr(row, "rcv_ts", 0.0)),
        route,
        str(getattr(row, "title", "") or "").strip(),
        f"Topics: {topics}" if topics else "",
    ]
    return " | ".join(part for part in parts if part)


def bulk_delete_sample_lines(rows: Sequence[object], *, limit: int = 8) -> list[str]:
    out: list[str] = []
    capped = max(0, int(limit or 0))
    for row in list(rows)[:capped]:
        out.append(f"- {message_row_summary_line(row)}")
    remaining = max(0, len(rows) - capped)
    if remaining:
        out.append(f"- +{remaining} more")
    return out


def bulk_delete_confirmation_text(rows: Sequence[object], prompt: str = "Delete selected messages?") -> str:
    clean_rows = [row for row in rows if row is not None]
    source_summary = summarize_delete_sources(clean_rows)
    lines = [
        str(prompt or "Delete selected messages?").strip(),
        "",
        f"Selected: {len(clean_rows)} message(s)",
        f"Sources: {source_summary or 'Unknown'}",
        "",
        "Delete action:",
        summarize_delete_effects(clean_rows) or "- Delete",
        "",
        "First selected messages:",
    ]
    lines.extend(bulk_delete_sample_lines(clean_rows))
    lines.extend(
        [
            "",
            "This action updates the source store where CommsPlan can safely do so and removes the rows from the current view.",
        ]
    )
    return "\n".join(line for line in lines if line is not None).strip()


def bulk_delete_completion_text(
    *,
    deleted: int,
    skipped: int,
    failed: int,
    source_summary: str,
    detail_counts: Dict[str, int],
) -> str:
    result_labels = {
        "deleted_source": "Deleted from source",
        "deleted_projection": "Cleaned stale CommsPlan rows",
        "hidden": "Hidden in CommsPlan",
        "deleted": "Deleted",
    }
    lines = [f"Completed delete for {int(deleted)} message(s)."]
    if source_summary:
        lines.append(source_summary)
    for key in ("deleted_source", "deleted_projection", "hidden", "deleted"):
        count = int(detail_counts.get(key, 0) or 0)
        if count:
            lines.append(f"{result_labels[key]}: {count}")
    if skipped:
        lines.append(f"Skipped: {int(skipped)}")
    if failed:
        lines.append(f"Failed: {int(failed)}")
    return "\n".join(lines)


def single_delete_confirmation_text(row: object, prompt: str = "Delete this message?") -> str:
    capability = message_delete_capability(
        getattr(row, "payload", None),
        origin=getattr(row, "origin", ""),
        msg_type=getattr(row, "msg_type", ""),
    )
    lines = [
        str(prompt or "Delete this message?").strip(),
        "",
        f"Source: {capability.source_label}",
        f"Delete action: {capability.effect_label}",
        "",
        message_row_summary_line(row),
        "",
        "This action updates the source store where CommsPlan can safely do so and removes the row from the current view.",
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def single_delete_success_text(row: object, *, hidden: bool = False) -> str:
    capability = message_delete_capability(
        getattr(row, "payload", None),
        origin=getattr(row, "origin", ""),
        msg_type=getattr(row, "msg_type", ""),
    )
    if hidden:
        return f"{capability.source_label} message hidden from CommsPlan Messages."
    return f"{capability.source_label} message deleted."


def single_delete_failure_warning(payload: Any, outcome: MessageDeleteExecutionResult, *, fallback: str = "") -> str:
    if outcome.warning:
        return outcome.warning
    text = str(fallback or "").strip()
    if text:
        return text
    return message_delete_result_detail(payload, outcome.detail_key)
