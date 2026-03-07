"""Persistent storage for OAuth tokens and API keys.

Data is stored as a JSON file with restricted file permissions (0o600).
Token values are encrypted at rest using Fernet symmetric encryption.
The Fernet key is randomly generated on first use and persisted to a
separate key file (0o600) in the same data directory.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _load_or_create_key(key_path: Path) -> bytes:
    """Load the Fernet key from disk, or generate and persist a new one."""
    if key_path.exists():
        return key_path.read_bytes().strip()

    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    return key


class _Encryption:
    """Lazy-initialised Fernet wrapper tied to a key file path."""

    def __init__(self, key_path: Path) -> None:
        self._key_path = key_path
        self._fernet: Fernet | None = None

    def _get(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(_load_or_create_key(self._key_path))
        return self._fernet

    def encrypt(self, value: str) -> str:
        return self._get().encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._get().decrypt(value.encode()).decode()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class OAuthTokens:
    """Stored OAuth token set."""

    access_token: str
    refresh_token: str
    expires_at: float  # epoch timestamp (seconds)
    account_id: str | None = None

    def to_dict(self, enc: _Encryption) -> dict[str, Any]:
        """Serialize with encrypted token values."""
        return {
            "access_token": enc.encrypt(self.access_token),
            "refresh_token": enc.encrypt(self.refresh_token),
            "expires_at": self.expires_at,
            "account_id": self.account_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], enc: _Encryption) -> OAuthTokens:
        """Deserialize, decrypting token values."""
        return cls(
            access_token=enc.decrypt(data["access_token"]),
            refresh_token=enc.decrypt(data["refresh_token"]),
            expires_at=data["expires_at"],
            account_id=data.get("account_id"),
        )


@dataclass
class ApiKeyRecord:
    """Metadata for a generated API key (the raw key is never stored)."""

    key_hash: str       # SHA-256 hex digest of the raw key
    key_prefix: str     # First few chars for display, e.g. "ob-aBcD..."
    name: str
    created_at: float   # epoch timestamp


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """Read / write the local JSON store file with encrypted token fields."""

    def __init__(self, path: Path) -> None:
        self._path = path
        key_path = path.parent / "encryption.key"
        self._enc = _Encryption(key_path)

    # -- low-level persistence ------------------------------------------------

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())  # type: ignore[no-any-return]

    def _write_raw(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))
        self._path.chmod(0o600)

    # -- OAuth tokens ---------------------------------------------------------

    def get_oauth(self) -> OAuthTokens | None:
        raw = self._read_raw()
        oauth_data = raw.get("oauth")
        if not oauth_data:
            return None
        try:
            return OAuthTokens.from_dict(oauth_data, self._enc)
        except InvalidToken:
            logger.warning(
                "Failed to decrypt stored OAuth tokens (encryption key "
                "mismatch). Clearing invalid data. Please run "
                "`openbridge login` again."
            )
            self.clear_oauth()
            return None

    def set_oauth(self, tokens: OAuthTokens) -> None:
        raw = self._read_raw()
        raw["oauth"] = tokens.to_dict(self._enc)
        self._write_raw(raw)

    def clear_oauth(self) -> None:
        raw = self._read_raw()
        raw.pop("oauth", None)
        self._write_raw(raw)

    # -- API keys -------------------------------------------------------------

    def list_api_keys(self) -> list[ApiKeyRecord]:
        raw = self._read_raw()
        return [ApiKeyRecord(**k) for k in raw.get("api_keys", [])]

    def add_api_key(self, record: ApiKeyRecord) -> None:
        raw = self._read_raw()
        keys: list[dict[str, Any]] = raw.get("api_keys", [])
        keys.append(asdict(record))
        raw["api_keys"] = keys
        self._write_raw(raw)

    def remove_api_key(self, key_prefix: str) -> bool:
        """Remove a key by its display prefix. Returns True if found."""
        raw = self._read_raw()
        keys: list[dict[str, Any]] = raw.get("api_keys", [])
        filtered = [k for k in keys if k["key_prefix"] != key_prefix]
        if len(filtered) == len(keys):
            return False
        raw["api_keys"] = filtered
        self._write_raw(raw)
        return True

    def find_key_hash(self, key_hash: str) -> ApiKeyRecord | None:
        """Look up a key record by its SHA-256 hash."""
        for record in self.list_api_keys():
            if record.key_hash == key_hash:
                return record
        return None
