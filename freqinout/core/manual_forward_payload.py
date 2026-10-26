"""Pure, bounded construction of a manually reviewed Mesh/JS8 payload.

This module deliberately has no transport, persistence, or UI dependencies.  A
caller can use the returned preview to decide whether the existing guarded
send path may be offered to an operator.  ``text`` is always the exact FIO
relay envelope; ``wire_text`` is the transport-facing preview.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


MESHCORE_MAX_BYTES = 160
MESHTASTIC_MAX_BYTES = 220
JS8_CP_MAX_BYTES = 512
MAX_BODY_BYTES = 64 * 1024
MAX_SENDER_LENGTH = 80
MAX_RELAY_ID_LENGTH = 40

_TRANSPORT_LIMITS = {
    "meshcore": MESHCORE_MAX_BYTES,
    "meshtastic": MESHTASTIC_MAX_BYTES,
    "js8": JS8_CP_MAX_BYTES,
}
_JS8_GROUP_RE = re.compile(r"^@[A-Z0-9]{1,8}$")
_JS8_CALL_RE = re.compile(r"^[A-Z0-9]+(?:/[A-Z0-9]+){0,2}$")
_JS8_ALNUM_ADJACENCY_RE = re.compile(r"[A-Z][0-9]|[0-9][A-Z]")


def _valid_js8_recipient(value: str) -> bool:
    """Conservative syntax gate matching JS8's compound-call guardrails."""

    if _JS8_GROUP_RE.fullmatch(value):
        return True
    if not _JS8_CALL_RE.fullmatch(value):
        return False
    compact = value.replace("/", "")
    return 3 <= len(compact) <= 9 and bool(_JS8_ALNUM_ADJACENCY_RE.search(compact))


@dataclass(frozen=True)
class ForwardPayload:
    """A complete, immutable preview for one manually reviewed send."""

    text: str
    wire_text: str
    payload_bytes: int
    wire_bytes: int
    max_bytes: int
    issues: tuple[str, ...]
    notices: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the preview has no blocking validation issue."""

        return not self.issues


def _as_text(value: object) -> str:
    # None is the only non-string value that has the useful meaning "absent".
    return "" if value is None else str(value)


def _utf8_bytes(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _has_forbidden_control(value: str, *, allow_lf_tab: bool = False) -> bool:
    for char in value:
        codepoint = ord(char)
        if codepoint < 32 or codepoint == 127:
            if allow_lf_tab and char in {"\n", "\t"}:
                continue
            return True
        # Catch Unicode control/format characters as well as ASCII controls,
        # while leaving ordinary non-ASCII text usable on mesh transports.
        if unicodedata.category(char).startswith("C"):
            return True
    return False


def _js8_clean_text(value: str) -> tuple[str, int, int]:
    """Return printable ASCII source text and observable removal/spacing counts."""

    cleaned: list[str] = []
    removed = normalized = 0
    for char in value:
        if 32 <= ord(char) <= 126:
            cleaned.append(char)
        elif char.isspace():
            cleaned.append(" ")
            normalized += 1
        else:
            removed += 1
    return "".join(cleaned), removed, normalized


def _effective_limit(
    baseline: int,
    requested: int | None,
    issues: list[str],
    notices: list[str],
) -> int:
    if requested is None:
        return baseline
    try:
        requested_int = int(requested)
    except (TypeError, ValueError, OverflowError):
        issues.append("invalid max_bytes")
        return 0
    if requested_int < 0:
        issues.append("invalid max_bytes")
        return 0
    if requested_int > baseline:
        notices.append(f"max_bytes capped at qualified {baseline} byte limit")
        return baseline
    return requested_int


def prepare_forward_payload(
    *,
    transport: str,
    sender: str,
    body: str,
    relay_id: str,
    recipient: str = "",
    max_bytes: int | None = None,
) -> ForwardPayload:
    """Build a bounded manual-forwarding preview.

    ``text`` retains the body exactly as supplied inside the ``CP`` envelope.
    No body trimming or truncation is performed.  JS8's ``wire_text`` is an
    cleaned uppercase ASCII preview addressed with the recipient prefix; the
    original source envelope remains in ``text`` for attribution and audit.
    Cleanup never applies to recipients or forwarding references.
    """

    transport_name = _as_text(transport).strip().casefold()
    sender_text = _as_text(sender)
    body_text = _as_text(body)
    relay_text = _as_text(relay_id)
    recipient_text = _as_text(recipient)

    baseline = _TRANSPORT_LIMITS.get(transport_name, 0)
    issues: list[str] = []
    notices: list[str] = []
    max_limit = _effective_limit(baseline, max_bytes, issues, notices)

    if baseline == 0:
        issues.append("unsupported transport")

    if not sender_text or not sender_text.strip():
        issues.append("empty sender")
    if len(sender_text) > MAX_SENDER_LENGTH:
        issues.append("sender exceeds 80 characters")
    if (transport_name != "js8" and _has_forbidden_control(sender_text)) or ":" in sender_text:
        issues.append("sender contains a control character or colon")

    if not relay_text or not relay_text.strip():
        issues.append("empty relay_id")
    if len(relay_text) > MAX_RELAY_ID_LENGTH:
        issues.append("relay_id exceeds 40 characters")
    if _has_forbidden_control(relay_text) or ":" in relay_text:
        issues.append("relay_id contains a control character or colon")

    if not body_text:
        issues.append("empty body")
    elif not body_text.strip():
        issues.append("body is whitespace only")

    envelope = f"CP {relay_text} FROM {sender_text}: {body_text}"
    payload_bytes = _utf8_bytes(envelope)
    if payload_bytes is None:
        # The envelope is retained for review, but cannot be represented as a
        # UTF-8 wire payload without silently replacing source content.
        payload_bytes = 0
        issues.append("payload is not valid UTF-8")
    elif _utf8_bytes(body_text) is not None and _utf8_bytes(body_text) > MAX_BODY_BYTES:
        issues.append("body exceeds 64 KiB")

    if transport_name == "js8":
        target = recipient_text.upper()
        if not recipient_text:
            issues.append("missing JS8 recipient")
        elif _has_forbidden_control(recipient_text) or any(char.isspace() for char in recipient_text):
            issues.append("JS8 recipient contains whitespace or control characters")
        elif any(ord(char) > 126 for char in recipient_text) or not _valid_js8_recipient(target):
            issues.append("invalid JS8 recipient")

        clean_sender, removed_sender, spaced_sender = _js8_clean_text(sender_text)
        clean_body, removed_body, spaced_body = _js8_clean_text(body_text)
        if removed_sender or removed_body:
            notices.append(f"Removed for JS8: {removed_sender} sender / {removed_body} message character(s)")
        if spaced_sender or spaced_body:
            notices.append("JS8 whitespace converted to spaces")
        if sender_text.strip() and not clean_sender.strip():
            clean_sender = "MESH SENDER"
            notices.append("No printable sender name; using MESH SENDER")
        if body_text.strip() and not clean_body.strip():
            issues.append("No printable message text remains after JS8 cleanup")
        preview = f"CP {relay_text} FROM {clean_sender}: {clean_body}".upper()
        wire_text = f"{target} {preview}"
        # References and recipients are not sanitizable source prose. Any
        # unsupported content left in the complete frame must still be blocked.
        if any(ord(char) < 32 or ord(char) > 126 for char in wire_text):
            issues.append("JS8 payload contains non-printable or non-ASCII text")
        if preview != f"CP {relay_text} FROM {clean_sender}: {clean_body}" or target != recipient_text:
            notices.append("JS8 wire preview is uppercase")
    else:
        if transport_name in {"meshcore", "meshtastic"} and _has_forbidden_control(
            body_text, allow_lf_tab=True
        ):
            issues.append("mesh body contains a forbidden control character")
        wire_text = envelope

    wire_bytes = _utf8_bytes(wire_text)
    if wire_bytes is None:
        wire_bytes = 0
        issues.append("wire payload is not valid UTF-8")
    elif wire_bytes > max_limit:
        issues.append(f"wire payload exceeds {max_limit} byte limit")

    return ForwardPayload(
        text=envelope,
        wire_text=wire_text,
        payload_bytes=payload_bytes,
        wire_bytes=wire_bytes,
        max_bytes=max_limit,
        issues=tuple(dict.fromkeys(issues)),
        notices=tuple(dict.fromkeys(notices)),
    )
