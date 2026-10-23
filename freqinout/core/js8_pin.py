"""Pure parser for the receive-only SpotterX PIN wire grammar.

The parser deliberately knows nothing about settings, databases, maps, or
transport APIs.  A valid result is a bounded description of what the packet
claimed; it does not establish that the target accepted the report or that the
reported location is verified.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


PIN_CATEGORIES: tuple[str, ...] = (
    "HAZARD",
    "SUPPLY",
    "CHECKPOINT",
    "HQ",
    "MEDICAL",
    "SHELTER",
    "COMMS",
    "BLOCKED",
    "WATER",
    "WILDFIRE",
    "RADIATION",
    "STAGING",
    "CAMPGROUND",
    "FLOOD",
    "BRIDGE",
    "FUEL",
)
PIN_CATEGORY_SET = frozenset(PIN_CATEGORIES)
MAX_PIN_RAW_BYTES = 1024
MAX_PIN_DESCRIPTION_LENGTH = 120

_SENDER_RE = r"[A-Z0-9/._-]+"
_TARGET_RE = r"@?[A-Z0-9][A-Z0-9/-]*"
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}[A-X]{2}$", re.IGNORECASE)
_TARGET_FULL_RE = re.compile(rf"^{_TARGET_RE}$", re.IGNORECASE)
_SENDER_FULL_RE = re.compile(rf"^{_SENDER_RE}$", re.IGNORECASE)
_PIN_PREFIX_RE = re.compile(r"^PIN(?:\s|$)", re.IGNORECASE)
_ENVELOPE_RE = re.compile(
    rf"^(?P<sender>{_SENDER_RE}):\s+(?P<target>\S+)\s+(?P<body>PIN(?:\s|$).*)$",
    re.IGNORECASE | re.DOTALL,
)
_TERMINAL_MARKERS = ("♢", "◇", "◆", "<>")


@dataclass(frozen=True)
class PinParseResult:
    """The bounded result of one PIN parse attempt.

    ``recognized`` distinguishes a non-PIN message from a PIN-shaped packet
    that failed validation.  ``valid`` is true only when all fields satisfy
    the wire grammar.  ``raw`` is always the original input representation;
    parsing never truncates or rewrites it.
    """

    raw: str
    recognized: bool
    valid: bool
    reason: str
    sender: str = ""
    target: str = ""
    category: str = ""
    grid: str = ""
    description: str = ""
    envelope: bool = False

def _result(raw: str, *, recognized: bool, reason: str, **fields: object) -> PinParseResult:
    return PinParseResult(raw=raw, recognized=recognized, valid=not reason, reason=reason, **fields)


def _looks_like_pin(raw: str) -> bool:
    """Bound the recognition probe so oversized input remains cheap."""

    probe = raw[:512].strip()
    if _PIN_PREFIX_RE.match(probe):
        return True
    return bool(
        re.match(rf"^{_SENDER_RE}:\s+\S+\s+PIN(?:\s|$)", probe, re.IGNORECASE)
        or re.match(r"^\S+\s+PIN(?:\s|$)", probe, re.IGNORECASE)
    )


def _strip_terminal_marker(text: str) -> str:
    """Remove one known native JS8 completion marker, and nothing else."""

    cleaned = text.strip()
    for marker in _TERMINAL_MARKERS:
        if cleaned.endswith(marker):
            return cleaned[: -len(marker)].rstrip()
    return cleaned


def _valid_target(value: str) -> bool:
    return bool(_TARGET_FULL_RE.fullmatch(value))


def _valid_sender(value: str) -> bool:
    return bool(_SENDER_FULL_RE.fullmatch(value))


def parse_pin(
    text: object,
    *,
    target_hint: object = "",
    sender_hint: object = "",
) -> PinParseResult:
    """Parse a native or canonical directed SpotterX PIN payload.

    Native text has the form ``SENDER: TARGET PIN TYPE GRID DESCRIPTION``.
    A directed API payload may instead be just ``PIN TYPE GRID DESCRIPTION``;
    that form requires ``target_hint`` and may use ``sender_hint`` for the
    observed transport sender.  A sender hint supplied with an envelope is a
    strict comparison, because it is transport evidence rather than a claim
    inside the packet.
    """

    raw = "" if text is None else str(text)
    if len(raw) > MAX_PIN_RAW_BYTES:
        return _result(raw, recognized=_looks_like_pin(raw), reason="raw_oversize")
    try:
        raw_bytes = raw.encode("utf-8")
    except UnicodeEncodeError:
        return _result(raw, recognized=_looks_like_pin(raw), reason="invalid_utf8")
    if len(raw_bytes) > MAX_PIN_RAW_BYTES:
        return _result(raw, recognized=_looks_like_pin(raw), reason="raw_oversize")

    normalized = _strip_terminal_marker(raw)
    sender = str(sender_hint or "").strip().upper()
    target = str(target_hint or "").strip().upper()
    envelope = False
    supplied_target = bool(str(target_hint or "").strip())

    envelope_match = _ENVELOPE_RE.fullmatch(normalized)
    if envelope_match:
        envelope = True
        sender = envelope_match.group("sender").upper()
        target = envelope_match.group("target").upper()
        body = envelope_match.group("body")
        supplied_sender = str(sender_hint or "").strip().upper()
        if supplied_target and not _valid_target(str(target_hint or "").strip().upper()):
            return _result(raw, recognized=True, reason="invalid_target", sender=sender, target=target, envelope=True)
        if supplied_target and target != str(target_hint or "").strip().upper():
            return _result(
                raw,
                recognized=True,
                reason="target_hint_mismatch",
                sender=sender,
                target=target,
                envelope=True,
            )
        if supplied_sender and supplied_sender != sender:
            return _result(
                raw,
                recognized=True,
                reason="sender_hint_mismatch",
                sender=sender,
                target=target,
                envelope=True,
            )
    elif _PIN_PREFIX_RE.match(normalized):
        body = normalized
    else:
        targeted_match = re.fullmatch(r"(?P<target>\S+)\s+(?P<body>PIN(?:\s|$).*)", normalized, re.IGNORECASE | re.DOTALL)
        if targeted_match:
            target = targeted_match.group("target").upper()
            body = targeted_match.group("body")
            if supplied_target and not _valid_target(str(target_hint or "").strip().upper()):
                return _result(raw, recognized=True, reason="invalid_target", target=target)
            if supplied_target and target != str(target_hint or "").strip().upper():
                return _result(raw, recognized=True, reason="target_hint_mismatch", target=target)
        else:
            return _result(raw, recognized=False, reason="not_pin")

    if "\n" in normalized or "\r" in normalized:
        return _result(raw, recognized=True, reason="multiline", sender=sender, target=target, envelope=envelope)
    if not target:
        return _result(raw, recognized=True, reason="missing_target", sender=sender, envelope=envelope)
    if not _valid_target(target):
        return _result(raw, recognized=True, reason="invalid_target", sender=sender, target=target, envelope=envelope)
    if sender and not _valid_sender(sender):
        return _result(raw, recognized=True, reason="invalid_sender", sender=sender, target=target, envelope=envelope)

    pin_match = re.fullmatch(r"PIN(?:\s+)(\S+)(?:\s+)(\S+)(?:\s+)(.+)", body, re.IGNORECASE | re.DOTALL)
    if not pin_match:
        return _result(raw, recognized=True, reason="missing_fields", sender=sender, target=target, envelope=envelope)
    category, grid, description = (part.strip() for part in pin_match.groups())
    category = category.upper()
    grid = grid.upper()
    if category not in PIN_CATEGORY_SET:
        return _result(
            raw, recognized=True, reason="unknown_category", sender=sender, target=target,
            category=category, grid=grid, description=description, envelope=envelope,
        )
    if not _GRID_RE.fullmatch(grid):
        return _result(
            raw, recognized=True, reason="invalid_grid", sender=sender, target=target,
            category=category, grid=grid, description=description, envelope=envelope,
        )
    if not description:
        return _result(
            raw, recognized=True, reason="missing_description", sender=sender, target=target,
            category=category, grid=grid, envelope=envelope,
        )
    if len(description) > MAX_PIN_DESCRIPTION_LENGTH:
        return _result(
            raw, recognized=True, reason="description_oversize", sender=sender, target=target,
            category=category, grid=grid, description=description, envelope=envelope,
        )
    if any(unicodedata.category(character) == "Cc" for character in description):
        return _result(
            raw, recognized=True, reason="description_control_character", sender=sender, target=target,
            category=category, grid=grid, description=description, envelope=envelope,
        )
    return PinParseResult(
        raw=raw,
        recognized=True,
        valid=True,
        reason="",
        sender=sender,
        target=target,
        category=category,
        grid=grid,
        description=description,
        envelope=envelope,
    )


__all__ = [
    "MAX_PIN_DESCRIPTION_LENGTH",
    "MAX_PIN_RAW_BYTES",
    "PIN_CATEGORIES",
    "PIN_CATEGORY_SET",
    "PinParseResult",
    "parse_pin",
]
