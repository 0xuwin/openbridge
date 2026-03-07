"""PKCE (Proof Key for Code Exchange) utilities for OAuth 2.0."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass

_CHARSET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


@dataclass(frozen=True)
class PkceCodes:
    verifier: str
    challenge: str


def _base64url_encode(data: bytes) -> str:
    """Base64url-encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _random_string(length: int) -> str:
    """Generate a random string from the unreserved character set."""
    rand_bytes = secrets.token_bytes(length)
    return "".join(_CHARSET[b % len(_CHARSET)] for b in rand_bytes)


def generate_pkce(verifier_length: int = 43) -> PkceCodes:
    """Generate a PKCE verifier/challenge pair using S256."""
    verifier = _random_string(verifier_length)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = _base64url_encode(digest)
    return PkceCodes(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """Generate a random state parameter for CSRF protection."""
    return _base64url_encode(secrets.token_bytes(32))
