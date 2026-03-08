"""OpenAI-compatible API routes.

Supports both the Chat Completions API (/v1/chat/completions) and the
newer Responses API (/v1/responses).  Both are proxied to the same
ChatGPT codex backend endpoint.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from openbridge.server.auth import verify_api_key
from openbridge.server.convert import response_to_chat_completion, responses_stream_to_chat_chunks
from openbridge.server.normalize import normalize_chat_completions_body, normalize_responses_body
from openbridge.server.proxy import UpstreamHTTPError, proxy_collect, proxy_stream

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_api_key)])


# ---------------------------------------------------------------------------
# Allowed models
# ---------------------------------------------------------------------------

# Models confirmed available through the ChatGPT codex backend via OAuth.
# Source: https://developers.openai.com/codex/models
#
# Requests for models not in this set are rejected immediately.
_CODEX_MODELS: set[str] = {
    # Recommended
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    # Alternative
    "gpt-5.2-codex",
    "gpt-5.2",
    "gpt-5.1-codex-max",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5-codex",
    "gpt-5-codex-mini",
    "gpt-5",
}


def _validate_model(body: dict[str, Any]) -> None:
    """Raise 400 if the requested model is not in the allowed set."""
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    if model not in _CODEX_MODELS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model}' is not supported. "
                f"Supported models: {', '.join(sorted(_CODEX_MODELS))}"
            ),
        )


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------


def _sse_response(upstream_iter: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(
        upstream_iter,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _get_proxy_deps(request: Request) -> tuple[Any, Any, httpx.AsyncClient]:
    return request.app.state.config, request.app.state.store, request.app.state.http_client


def _proxy_stream(request: Request, body: dict[str, Any]) -> AsyncIterator[bytes]:
    """Validate model, force upstream stream, and return a raw SSE byte iterator."""
    _validate_model(body)
    cfg, store, http_client = _get_proxy_deps(request)
    body["stream"] = True
    return proxy_stream(cfg, store, body, client=http_client)


async def _proxy_collect(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Validate model, force upstream stream, and return the aggregated response dict."""
    _validate_model(body)
    cfg, store, http_client = _get_proxy_deps(request)
    body["stream"] = True
    try:
        return await proxy_collect(cfg, store, body, client=http_client)
    except UpstreamHTTPError as exc:
        logger.warning("Upstream request failed with status %s", exc.status_code)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except RuntimeError as exc:
        logger.error("Proxy error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    except httpx.HTTPError as exc:
        logger.exception("HTTP error proxying request")
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error proxying request")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    """OpenAI-compatible chat completions endpoint.

    Supports both streaming (``stream: true``) and non-streaming requests.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    normalized = normalize_chat_completions_body(body)
    if bool(normalized.get("stream")):
        chunks = responses_stream_to_chat_chunks(
            _proxy_stream(request, normalized),
            requested_model=body.get("model", ""),
        )
        return _sse_response(chunks)

    result = await _proxy_collect(request, normalized)
    return JSONResponse(
        content=response_to_chat_completion(result, body.get("model", ""))
    )


# ---------------------------------------------------------------------------
# POST /v1/responses
# ---------------------------------------------------------------------------


@router.post("/v1/responses")
async def responses(request: Request) -> Any:
    """OpenAI Responses API endpoint.

    Accepts the same request format as the official Responses API and
    proxies it to the ChatGPT codex backend.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    normalized = normalize_responses_body(body)
    if bool(normalized.get("stream")):
        return _sse_response(_proxy_stream(request, normalized))

    result = await _proxy_collect(request, normalized)
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


@router.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    """Return a list of available models in OpenAI format."""
    now = int(time.time())
    models = [
        {
            "id": model_id,
            "object": "model",
            "created": now,
            "owned_by": "openai",
        }
        for model_id in sorted(_CODEX_MODELS)
    ]
    return JSONResponse(content={"object": "list", "data": models})


@router.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str, request: Request) -> JSONResponse:
    """Return details for a single model."""
    if model_id not in _CODEX_MODELS:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return JSONResponse(
        content={
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "openai",
        }
    )
