from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from freqinout.core.mesh.channel_policy import MeshChannelPolicy

MESHCORE_COMPANION_DECODER_WARNING = (
    "MeshCore is connected, but message ingest is not active yet. "
    "CommsPlan needs the MeshCore Companion decoder before Inbox traffic can appear."
)


@dataclass(frozen=True)
class MeshIngestReadiness:
    state: str
    headline: str
    guidance: str
    accepted_count: int = 0
    pending_count: int = 0
    ignored_count: int = 0
    key_needed_count: int = 0
    ingest_warning_count: int = 0

    @property
    def is_ready(self) -> bool:
        return self.state == "ready"

    def summary(self) -> str:
        parts = [self.headline]
        if self.guidance:
            parts.append(self.guidance)
        return " ".join(part for part in parts if part).strip()


def mesh_ingest_readiness(
    *,
    policies: Sequence[MeshChannelPolicy],
    health_rows: Sequence[Mapping[str, object]] = (),
) -> MeshIngestReadiness:
    accepted = [policy for policy in policies if policy.review_state == "accepted"]
    pending = [policy for policy in policies if policy.review_state == "pending"]
    ignored = [policy for policy in policies if policy.review_state == "ignored"]
    key_needed = [policy for policy in policies if policy.requires_key and not policy.key_available]
    ingest_warning_count = sum(1 for row in health_rows if _row_has_ingest_warning(row))

    base = {
        "accepted_count": len(accepted),
        "pending_count": len(pending),
        "ignored_count": len(ignored),
        "key_needed_count": len(key_needed),
        "ingest_warning_count": ingest_warning_count,
    }
    if not policies:
        return MeshIngestReadiness(
            state="needs_channels",
            headline="No mesh feeds are reviewed yet.",
            guidance="Connect the device to discover feeds, or stage its supported default channel.",
            **base,
        )
    if key_needed:
        names = ", ".join(policy.display_name for policy in key_needed[:3])
        if len(key_needed) > 3:
            names = f"{names}, +{len(key_needed) - 3} more"
        return MeshIngestReadiness(
            state="needs_key",
            headline=f"Private key needed for {names}.",
            guidance="Join that channel on the mesh device, then Mark Joined in CommsPlan.",
            **base,
        )
    if not accepted:
        return MeshIngestReadiness(
            state="needs_accept",
            headline="No mesh feeds are accepted yet.",
            guidance="Accept the feeds you want in Inbox, Ops Center, Map, and topics.",
            **base,
        )
    if ingest_warning_count:
        return MeshIngestReadiness(
            state="decoder_needed",
            headline=MESHCORE_COMPANION_DECODER_WARNING,
            guidance="Channel policy is ready; live MeshCore BLE receive still needs the decoder bridge.",
            **base,
        )
    return MeshIngestReadiness(
        state="ready",
        headline=f"{len(accepted)} mesh feed{'s' if len(accepted) != 1 else ''} ready for CommsPlan.",
        guidance="Accepted feeds can populate Inbox, Ops Center, Map, and topic scanning.",
        **base,
    )


def _row_has_ingest_warning(row: Mapping[str, object]) -> bool:
    warnings = row.get("warnings")
    if isinstance(warnings, str):
        candidates = (warnings,)
    elif isinstance(warnings, Sequence):
        candidates = tuple(str(item) for item in warnings)
    else:
        candidates = ()
    return any("message ingest is not active" in warning or "receive bridge" in warning for warning in candidates)
