"""Forward requests to the ChatGPT backend-api, handling token refresh."""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

import httpx

from openbridge.config import Config
from openbridge.oauth.tokens import extract_account_id, refresh_access_token
from openbridge.store import OAuthTokens, Store

logger = logging.getLogger(__name__)


async def _ensure_valid_token(cfg: Config, store: Store) -> OAuthTokens:
    """Return a valid access token, refreshing if necessary."""
    tokens = store.get_oauth()
    if tokens is None:
        raise RuntimeError("Not authenticated. Run `openbridge login` first.")

    # Refresh if expired or about to expire (30 s margin)
    if tokens.expires_at < time.time() + 30:
        logger.info("Access token expired or expiring, refreshing...")
        new = await refresh_access_token(
            issuer=cfg.oauth_issuer,
            client_id=cfg.oauth_client_id,
            refresh_token=tokens.refresh_token,
        )
        account_id = extract_account_id(new) or tokens.account_id
        tokens = OAuthTokens(
            access_token=new.access_token,
            refresh_token=new.refresh_token,
            expires_at=time.time() + new.expires_in,
            account_id=account_id,
        )
        store.set_oauth(tokens)
        logger.info("Token refreshed successfully.")

    return tokens


def _build_upstream_headers(tokens: OAuthTokens) -> dict[str, str]:
    """Build headers for the upstream ChatGPT request."""
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "Content-Type": "application/json",
        "User-Agent": "openbridge/0.1.0",
        "originator": "openbridge",
    }
    if tokens.account_id:
        headers["ChatGPT-Account-Id"] = tokens.account_id
    return headers


async def proxy_request(
    cfg: Config,
    store: Store,
    body: dict[str, Any],
    *,
    stream: bool = False,
) -> dict[str, Any] | AsyncIterator[bytes]:
    """Proxy an API request to the ChatGPT codex endpoint.

    Works for both chat/completions and responses API requests.
    When ``stream=True`` returns an async iterator of raw SSE bytes.
    Otherwise returns the parsed JSON response.
    """
    tokens = await _ensure_valid_token(cfg, store)
    headers = _build_upstream_headers(tokens)
    url = cfg.codex_api_endpoint

    if stream:
        return await _stream_response(url, headers, body)
    else:
        return await _non_stream_response(url, headers, body)


async def _non_stream_response(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


async def _stream_response(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> AsyncIterator[bytes]:
    """Stream the upstream SSE response, yielding raw bytes."""

    async def _generate() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return _generate()
