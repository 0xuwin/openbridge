"""Token exchange, refresh, and JWT parsing utilities."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class TokenResponse:
    """Parsed token response from the OAuth server."""

    id_token: str
    access_token: str
    refresh_token: str
    expires_in: int = 3600


async def exchange_code_for_tokens(
    *,
    issuer: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> TokenResponse:
    """Exchange an authorization code for tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{issuer}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
    return TokenResponse(
        id_token=data.get("id_token", ""),
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=data.get("expires_in", 3600),
    )


async def refresh_access_token(
    *,
    issuer: str,
    client_id: str,
    refresh_token: str,
) -> TokenResponse:
    """Use a refresh token to obtain new tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{issuer}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
    return TokenResponse(
        id_token=data.get("id_token", ""),
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", refresh_token),
        expires_in=data.get("expires_in", 3600),
    )


# ---------------------------------------------------------------------------
# JWT claim helpers (no signature verification – we trust the issuer)
# ---------------------------------------------------------------------------


def parse_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode the payload of a JWT without verification."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        # Add padding if needed
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))  # type: ignore[no-any-return]
    except Exception:
        return None


def extract_account_id(tokens: TokenResponse) -> str | None:
    """Try to extract a ChatGPT account ID from id_token or access_token."""
    for token_str in (tokens.id_token, tokens.access_token):
        if not token_str:
            continue
        claims = parse_jwt_claims(token_str)
        if not claims:
            continue
        acct = claims.get("chatgpt_account_id") or (
            claims.get("https://api.openai.com/auth") or {}
        ).get("chatgpt_account_id")
        if acct:
            return acct  # type: ignore[no-any-return]
        orgs = claims.get("organizations")
        if isinstance(orgs, list) and orgs:
            return orgs[0].get("id")  # type: ignore[no-any-return]
    return None
