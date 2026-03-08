"""Forward requests to the ChatGPT backend-api, handling token refresh."""

from __future__ import annotations

import codecs
import logging
import json
import time
from typing import Any, AsyncGenerator, AsyncIterator

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


async def _prepare(
    cfg: Config, store: Store, client: httpx.AsyncClient
) -> tuple[str, dict[str, str]]:
    """Resolve the upstream URL and auth headers, refreshing the token if needed."""
    tokens = await _ensure_valid_token(cfg, store)
    return cfg.codex_api_endpoint, _build_upstream_headers(tokens)


async def proxy_collect(
    cfg: Config,
    store: Store,
    body: dict[str, Any],
    *,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """POST to the upstream endpoint and return the aggregated response payload."""
    url, headers = await _prepare(cfg, store, client)
    return await _non_stream_response(client, url, headers, body)


async def proxy_stream(
    cfg: Config,
    store: Store,
    body: dict[str, Any],
    *,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    """Return an async iterator of raw SSE bytes from the upstream endpoint."""
    url, headers = await _prepare(cfg, store, client)
    return await _stream_response(client, url, headers, body)


async def _non_stream_response(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    """POST to the upstream SSE endpoint and aggregate the response.completed payload."""
    stream_headers = dict(headers)
    stream_headers["Accept"] = "text/event-stream"
    async with client.stream("POST", url, json=body, headers=stream_headers) as resp:
        if not resp.is_success:
            await resp.aread()
            raise UpstreamHTTPError(resp.status_code, _extract_error_detail(resp))
        return await _collect_sse_response(resp)


async def iter_sse_events(
    source: AsyncIterator[bytes],
) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    """Parse a raw SSE byte stream and yield (event_name, payload) pairs.

    Skips comment lines, malformed events, and non-dict payloads.  The
    ``[DONE]`` sentinel is consumed silently.  Callers receive only well-formed
    events so they can focus on business logic.
    """
    buffer = ""
    event_name: str | None = None
    data_lines: list[str] = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")

    async for chunk in source:
        if isinstance(chunk, str):
            text = chunk
        else:
            text = decoder.decode(chunk)
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")

            if not line:
                # blank line → dispatch accumulated event
                if event_name and data_lines:
                    payload_text = "\n".join(data_lines)
                    if payload_text != "[DONE]":
                        try:
                            payload = json.loads(payload_text)
                        except json.JSONDecodeError:
                            payload = None
                        if isinstance(payload, dict):
                            yield event_name, payload
                event_name = None
                data_lines = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

    tail = decoder.decode(b"", final=True)
    if tail:
        buffer += tail

    # flush any trailing event not terminated by a blank line
    if event_name and data_lines:
        payload_text = "\n".join(data_lines)
        if payload_text != "[DONE]":
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                yield event_name, payload


async def _collect_sse_response(resp: httpx.Response) -> dict[str, Any]:
    """Consume an upstream SSE response and return the final response payload."""
    text_deltas: list[str] = []

    async for event_name, payload in iter_sse_events(resp.aiter_bytes()):
        if event_name == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                text_deltas.append(delta)

        elif event_name == "response.completed":
            inner = payload.get("response")
            response: dict[str, Any] = inner if isinstance(inner, dict) else payload
            if text_deltas and "output_text" not in response:  # type: ignore[operator]
                response["output_text"] = "".join(text_deltas)
            return response  # type: ignore[return-value]

        elif event_name in {"response.failed", "response.incomplete"}:
            detail = payload.get("response", {}).get("error") or payload.get("error") or payload
            raise UpstreamHTTPError(502, detail)

    raise UpstreamHTTPError(502, "Upstream stream ended before sending response.completed")


async def _stream_response(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
) -> AsyncIterator[bytes]:
    """Return an async iterator that streams raw SSE bytes from upstream."""
    stream_headers = {**headers, "Accept": "text/event-stream"}
    stream = client.stream("POST", url, json=body, headers=stream_headers)
    resp = await stream.__aenter__()
    if not resp.is_success:
        await resp.aread()
        await stream.__aexit__(None, None, None)
        raise UpstreamHTTPError(resp.status_code, _extract_error_detail(resp))

    async def _generate() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await stream.__aexit__(None, None, None)

    return _generate()
