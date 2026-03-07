"""API key generation and validation."""

from __future__ import annotations

import hashlib
import secrets
import time

from openbridge.store import ApiKeyRecord

_KEY_PREFIX = "ob-"
_KEY_BYTES = 32  # 256 bits of randomness


def generate_api_key(name: str = "default") -> tuple[str, ApiKeyRecord]:
    """Generate a new API key and its storage record.

    Returns:
        (raw_key, record) – the raw key is displayed to the user exactly
        once; only the SHA-256 hash is persisted.
    """
    raw = _KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    display_prefix = raw[:12] + "..."
    record = ApiKeyRecord(
        key_hash=key_hash,
        key_prefix=display_prefix,
        name=name,
        created_at=time.time(),
    )
    return raw, record


def hash_key(raw_key: str) -> str:
    """Compute the SHA-256 hex digest of a raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()
