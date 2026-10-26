"""Selective, detached reads of message-signature verification policy."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from freqinout.core.sqlite_utils import connect_sqlite_readonly


SIGNATURE_POLICY_KEYS = (
    "gpg_verify_flamp_k2s_enabled",
    "hash_verify_flamp_k2s_enabled",
    "trusted_file_hashes",
    "gpg_trusted_signers",
    "gpg_inline_signed_filename_suffixes",
    "gpg_executable_path",
    "message_signature_verify_batch_limit",
)


@dataclass(frozen=True)
class SignaturePolicySnapshot:
    """Parsed values and freshness token for one read-only policy snapshot."""

    values: dict[str, Any]
    token: tuple[int, int, str]


def _token_atom(value: Any) -> bytes:
    """Encode SQLite scalar values with type and length preserved."""
    if value is None:
        return b"N"
    if isinstance(value, str):
        return b"T" + value.encode("utf-8")
    if isinstance(value, bytes):
        return b"B" + value
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"F" + value.hex().encode("ascii")
    raise TypeError(f"Unsupported SQLite value type: {type(value).__name__}")


def _rows_digest(rows: list[tuple[Any, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            atom = _token_atom(value)
            digest.update(len(atom).to_bytes(8, "big"))
            digest.update(atom)
    return digest.hexdigest()


def load_signature_policy_snapshot(db_path: str | Path) -> SignaturePolicySnapshot:
    """Read only the signature-policy keys and return detached parsed values.

    Missing keys remain absent so consumers can apply their established defaults.
    Database, filesystem, and JSON errors deliberately propagate to the caller.
    """
    path = Path(db_path).expanduser().resolve()
    before = path.stat()
    placeholders = ",".join("?" for _ in SIGNATURE_POLICY_KEYS)
    with closing(connect_sqlite_readonly(path, timeout=0.25, busy_timeout_ms=250)) as conn:
        rows = [tuple(row) for row in conn.execute(
            f"SELECT key, value FROM kv WHERE key IN ({placeholders}) ORDER BY key",
            SIGNATURE_POLICY_KEYS,
        )]
    after = path.stat()
    identity_before = (before.st_dev, before.st_ino)
    identity_after = (after.st_dev, after.st_ino)
    if identity_before != identity_after:
        raise OSError(f"Settings database was replaced while reading signature policy: {path}")

    values = {key: json.loads(raw_value) for key, raw_value in rows}
    return SignaturePolicySnapshot(
        values=values,
        token=(after.st_dev, after.st_ino, _rows_digest(rows)),
    )


def signature_policy_current(db_path: str | Path, token: tuple[int, int, str]) -> bool:
    """Return whether current policy still matches ``token``; errors fail closed."""
    try:
        return load_signature_policy_snapshot(db_path).token == token
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False
