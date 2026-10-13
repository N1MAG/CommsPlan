"""Widget-independent drafts used by the SOP Builder.

The builder intentionally keeps its transient editing state here rather than in
Qt cell widgets.  Persistence remains owned by :mod:`sop_manager`; this module
only normalizes the payload exchanged by the card and bulk editors.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Iterable


@dataclass
class SopActionDraft:
    id: int = 0
    group_name: str = ""
    condition_levels: str = "ALL"
    band: str = ""
    frequency: str = ""
    operating_group_frequency_key: str = ""
    software: str = ""
    mode: str = ""
    action_key: str = ""
    action_label: str = ""
    enabled: bool = True
    daily_start_utc: str = "00:00"
    daily_end_utc: str = "01:00"
    duration_minutes: int = 60
    interval_minutes: int = 180
    interval_phase_minutes: int = 0
    interval_hours: int = 3
    # Keep this literal here so the model remains independent of SOPManager
    # (and its database/bootstrap side effects).
    conflict_policy: str = "SOP_ALL"
    daily_conflict_summary: str = ""
    net_conflict_summary: str = ""
    schedule_applied: bool = True
    description: str = ""
    contact_rule: str = "none"
    contact_target: str = ""
    sort_order: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None, *, sort_order: int = 0) -> "SopActionDraft":
        source = dict(payload or {})
        values = {field.name: source[field.name] for field in fields(cls) if field.name in source}
        values.setdefault("sort_order", sort_order)
        return cls(**values)

    def to_payload(self, *, sort_order: int | None = None) -> dict[str, Any]:
        payload = asdict(self)
        if sort_order is not None:
            payload["sort_order"] = int(sort_order)
        return payload


class SopActionDraftCollection:
    """Small ordered collection shared by card and advanced editors."""

    def __init__(self, rows: Iterable[dict[str, Any]] = ()) -> None:
        self._rows: list[SopActionDraft] = []
        self.replace(rows)

    def replace(self, rows: Iterable[dict[str, Any]]) -> None:
        self._rows = [SopActionDraft.from_payload(row, sort_order=index) for index, row in enumerate(rows)]

    def payloads(self) -> list[dict[str, Any]]:
        return [row.to_payload(sort_order=index) for index, row in enumerate(self._rows)]

    def rows(self) -> tuple[SopActionDraft, ...]:
        return tuple(self._rows)

    def append(self, payload: dict[str, Any] | None = None) -> SopActionDraft:
        draft = SopActionDraft.from_payload(payload, sort_order=len(self._rows))
        self._rows.append(draft)
        return draft

    def duplicate(self, index: int) -> SopActionDraft | None:
        if index < 0 or index >= len(self._rows):
            return None
        copy = self._rows[index].to_payload(sort_order=len(self._rows))
        copy["id"] = 0
        return self.append(copy)

    def remove(self, index: int) -> bool:
        if index < 0 or index >= len(self._rows):
            return False
        self._rows.pop(index)
        return True

    def update(self, index: int, field_name: str, value: Any) -> bool:
        if index < 0 or index >= len(self._rows) or not hasattr(self._rows[index], field_name):
            return False
        setattr(self._rows[index], field_name, value)
        return True
