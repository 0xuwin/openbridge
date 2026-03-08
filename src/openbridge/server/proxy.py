"""Forward requests to the ChatGPT backend-api, handling token refresh."""

from __future__ import annotations

import logging
import json
import time
from typing import Any, AsyncIterator

import httpx

from openbridge.config import Config
from openbridge.oauth.tokens import extract_account_id, refresh_access_token
from openbridge.store import OAuthTokens, Store

logger = logging.getLogger(__name__)


class UpstreamHTTPError(RuntimeError):
    """Raised when the upstream ChatGPT backend returns an HTTP error."""

    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


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


def _extract_error_detail(resp: httpx.Response) -> Any:
    """Return the most useful error payload from an upstream response."""
    try:
        data = resp.json()
    except ValueError:
        text = resp.text.strip()
        return text or f"Upstream request failed with status {resp.status_code}"

    if isinstance(data, dict):
        return data.get("error") or data.get("detail") or data
    return data


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
    if body.get("stream") is True:
        stream_headers = dict(headers)
        stream_headers["Accept"] = "text/event-stream"
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=body, headers=stream_headers) as resp:
                if not resp.is_success:
                    await resp.aread()
                    raise UpstreamHTTPError(resp.status_code, _extract_error_detail(resp))
                return await _collect_sse_response(resp)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=body, headers=headers)
        if not resp.is_success:
            raise UpstreamHTTPError(resp.status_code, _extract_error_detail(resp))
        return resp.json()  # type: ignore[no-any-return]


def _finalize_sse_event(
    event_name: str | None,
    data_lines: list[str],
    *,
    text_deltas: list[str],
) -> dict[str, Any] | None:
    if not event_name or not data_lines:
        return None

    payload_text = "\n".join(data_lines)
    if payload_text == "[DONE]":
        return None

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None

    if event_name == "response.output_text.delta" and isinstance(payload, dict):
        delta = payload.get("delta")
        if isinstance(delta, str):
            text_deltas.append(delta)
        return None

    if event_name == "response.completed":
        if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
            response = payload["response"]
        elif isinstance(payload, dict):
            response = payload
        else:
            return None

        if text_deltas and "output_text" not in response:
            response["output_text"] = "".join(text_deltas)
        return response

    if event_name in {"response.failed", "response.incomplete"}:
        detail = payload
        if isinstance(payload, dict):
            detail = payload.get("response", {}).get("error") or payload.get("error") or payload
        raise UpstreamHTTPError(502, detail)

    return None


async def _collect_sse_response(resp: httpx.Response) -> dict[str, Any]:
    """Consume an upstream SSE response and return the final response payload."""
    buffer = ""
    event_name: str | None = None
    data_lines: list[str] = []
    text_deltas: list[str] = []

    async for chunk in resp.aiter_text():
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")

            if not line:
                completed = _finalize_sse_event(
                    event_name,
                    data_lines,
                    text_deltas=text_deltas,
                )
                if completed is not None:
                    return completed
                event_name = None
                data_lines = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

    completed = _finalize_sse_event(event_name, data_lines, text_deltas=text_deltas)
    if completed is not None:
        return completed

    raise UpstreamHTTPError(502, "Upstream stream ended before sending response.completed")


async def _stream_response(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> AsyncIterator[bytes]:
    """Stream the upstream SSE response, yielding raw bytes."""

    stream_headers = dict(headers)
    stream_headers["Accept"] = "text/event-stream"

    async def _generate() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            async with client.stream("POST", url, json=body, headers=stream_headers) as resp:
                if not resp.is_success:
                    await resp.aread()
                    raise UpstreamHTTPError(resp.status_code, _extract_error_detail(resp))
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return _generate()
