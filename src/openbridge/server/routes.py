"""OpenAI-compatible API routes.

Supports both the Chat Completions API (/v1/chat/completions) and the
newer Responses API (/v1/responses).  Both are proxied to the same
ChatGPT codex backend endpoint.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from openbridge.server.auth import verify_api_key
from openbridge.server.proxy import proxy_request

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
# Shared proxy helper
# ---------------------------------------------------------------------------


async def _proxy_or_stream(
    request: Request, body: dict[str, Any]
) -> JSONResponse | StreamingResponse:
    """Validate model, then forward *body* upstream."""
    _validate_model(body)

    cfg = request.app.state.config
    store = request.app.state.store
    stream = body.get("stream", False)

    try:
        if stream:
            body["stream"] = True
            upstream_iter = await proxy_request(cfg, store, body, stream=True)
            return StreamingResponse(
                upstream_iter,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            body["stream"] = False
            result = await proxy_request(cfg, store, body, stream=False)
            return JSONResponse(content=result)

    except RuntimeError as exc:
        logger.error("Proxy error: %s", exc)
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

    return await _proxy_or_stream(request, body)


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

    return await _proxy_or_stream(request, body)


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
