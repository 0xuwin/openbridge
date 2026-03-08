"""OpenAI-compatible API routes.

Supports both the Chat Completions API (/v1/chat/completions) and the
newer Responses API (/v1/responses).  Both are proxied to the same
ChatGPT codex backend endpoint.
"""

from __future__ import annotations

import logging
import json
import time
from typing import Any, AsyncIterator, Callable, cast
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from openbridge.server.auth import verify_api_key
from openbridge.server.proxy import UpstreamHTTPError, proxy_request

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


def _normalize_response_content_item(item: Any) -> dict[str, Any]:
    """Normalize shorthand Responses API content items for the Codex backend."""
    if isinstance(item, str):
        return {"type": "input_text", "text": item}

    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail="Invalid response content item")

    normalized = dict(item)
    item_type = normalized.get("type")

    if item_type in {"input_text", "input_image", "input_file"}:
        return normalized

    if item_type == "text" and "text" in normalized:
        normalized["type"] = "input_text"
        return normalized

    return normalized


def _normalize_response_input_item(item: Any) -> dict[str, Any]:
    """Normalize shorthand Responses API input items for the Codex backend."""
    if isinstance(item, str):
        return {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": item}],
        }

    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail="Invalid response input item")

    normalized = dict(item)
    if "role" in normalized and normalized.get("type") in {None, "message"}:
        normalized["type"] = "message"
        content = normalized.get("content", [])
        if isinstance(content, str):
            content = [{"type": "input_text", "text": content}]
        elif isinstance(content, list):
            content = [_normalize_response_content_item(entry) for entry in content]
        else:
            raise HTTPException(status_code=400, detail="Invalid message content")
        normalized["content"] = content
        return normalized

    return normalized


def _normalize_responses_body(body: dict[str, Any]) -> dict[str, Any]:
    """Adapt public Responses API shorthand into Codex backend shape."""
    normalized = dict(body)
    normalized.setdefault("instructions", "")
    normalized.setdefault("tools", [])
    normalized.setdefault("tool_choice", "auto")
    normalized.setdefault("parallel_tool_calls", False)
    normalized.setdefault("store", False)
    normalized.setdefault("include", [])

    raw_input = normalized.get("input", [])
    if isinstance(raw_input, str):
        normalized["input"] = [_normalize_response_input_item(raw_input)]
    elif isinstance(raw_input, dict):
        normalized["input"] = [_normalize_response_input_item(raw_input)]
    elif isinstance(raw_input, list):
        normalized["input"] = [_normalize_response_input_item(item) for item in raw_input]
    else:
        raise HTTPException(status_code=400, detail="Invalid 'input' field")

    return normalized


def _normalize_chat_content_item(item: Any) -> dict[str, Any]:
    """Normalize chat content items into Codex input content items."""
    if isinstance(item, str):
        return {"type": "input_text", "text": item}

    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail="Invalid chat content item")

    normalized = dict(item)
    item_type = normalized.get("type")
    if item_type == "text" and "text" in normalized:
        return {"type": "input_text", "text": normalized["text"]}
    if item_type == "image_url" and "image_url" in normalized:
        image = normalized["image_url"]
        if isinstance(image, dict):
            image = image.get("url")
        return {"type": "input_image", "image_url": image}
    return normalized


def _normalize_chat_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a chat completion message into a Codex input item."""
    role = message.get("role")
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        raise HTTPException(status_code=400, detail="Invalid chat message role")

    if role in {"system", "developer"}:
        return None

    content = message.get("content")
    if isinstance(content, str):
        normalized_content = [{"type": "input_text", "text": content}]
    elif isinstance(content, list):
        normalized_content = [_normalize_chat_content_item(item) for item in content]
    else:
        normalized_content = []

    normalized_message: dict[str, Any] = {
        "type": "message",
        "role": role,
        "content": normalized_content,
    }

    tool_calls = message.get("tool_calls")
    if role == "assistant" and isinstance(tool_calls, list) and tool_calls and not normalized_content:
        return None

    tool_call_id = message.get("tool_call_id")
    if role == "tool":
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise HTTPException(
                status_code=400,
                detail="Tool message is missing 'tool_call_id'",
            )
        return {
            "type": "function_call_output",
            "call_id": tool_call_id,
            "output": content if isinstance(content, str) else normalized_content,
        }

    return normalized_message


def _normalize_assistant_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert assistant tool_calls into Codex function_call items."""
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []

    normalized: list[dict[str, Any]] = []
    for idx, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(arguments, str):
            arguments = "{}"
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"chat_call_{idx}"
        normalized.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return normalized


def _extract_chat_instructions(messages: list[dict[str, Any]]) -> str:
    """Collapse system/developer messages into Codex instructions."""
    instruction_parts: list[str] = []
    for message in messages:
        if message.get("role") not in {"system", "developer"}:
            continue
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            instruction_parts.append(content)
        elif isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    texts.append(str(item["text"]))
            if texts:
                instruction_parts.append("\n".join(texts))
    return "\n\n".join(instruction_parts) or "You are a helpful assistant."


def _normalize_chat_completions_body(body: dict[str, Any]) -> dict[str, Any]:
    """Adapt Chat Completions requests into the Codex responses shape."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="Missing or invalid 'messages' field")

    input_items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="Invalid message in 'messages'")
        if message.get("role") == "assistant":
            input_items.extend(_normalize_assistant_tool_calls(message))
        normalized_item = _normalize_chat_message(message)
        if normalized_item is not None:
            input_items.append(normalized_item)

    normalized: dict[str, Any] = {
        "model": body.get("model"),
        "instructions": _extract_chat_instructions(messages),
        "input": input_items,
        "stream": body.get("stream", False),
        "tools": body.get("tools", []),
        "tool_choice": body.get("tool_choice", "auto"),
        "parallel_tool_calls": body.get("parallel_tool_calls", False),
        "include": body.get("include", []),
        "store": body.get("store", False),
    }

    if "max_completion_tokens" in body and "max_output_tokens" not in body:
        normalized["max_output_tokens"] = body["max_completion_tokens"]

    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        json_schema = response_format.get("json_schema", {})
        if isinstance(json_schema, dict):
            normalized["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": json_schema.get("name", "response_format"),
                    "schema": json_schema.get("schema", {}),
                    "strict": bool(json_schema.get("strict", False)),
                }
            }

    for key in (
        "max_output_tokens",
        "temperature",
        "top_p",
        "reasoning",
        "service_tier",
        "metadata",
    ):
        if key in body:
            normalized[key] = body[key]

    return normalized


def _extract_response_text(result: dict[str, Any]) -> str:
    """Extract assistant text from an upstream responses payload."""
    output_text = result.get("output_text")
    if isinstance(output_text, str):
        return output_text

    parts: list[str] = []
    for item in result.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


def _extract_response_tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract function calls from a responses payload as chat tool_calls."""
    tool_calls: list[dict[str, Any]] = []
    call_index = 0
    for item in result.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue

        name = item.get("name")
        arguments = item.get("arguments")
        if not isinstance(name, str):
            continue
        if not isinstance(arguments, str):
            arguments = "{}"

        call_id = item.get("call_id") or item.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_{call_index}"

        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        )
        call_index += 1
    return tool_calls


def _map_chat_usage(usage: Any) -> dict[str, Any]:
    """Map responses usage fields into chat completion usage fields."""
    if not isinstance(usage, dict):
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    if "prompt_tokens" in usage and "completion_tokens" in usage:
        return usage

    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)

    mapped: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }

    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        mapped["prompt_tokens_details"] = {
            "cached_tokens": int(input_details.get("cached_tokens", 0) or 0)
        }

    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict):
        mapped["completion_tokens_details"] = {
            "reasoning_tokens": int(output_details.get("reasoning_tokens", 0) or 0)
        }

    return mapped


def _chat_finish_reason(result: dict[str, Any], has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"

    incomplete = result.get("incomplete_details")
    if isinstance(incomplete, dict):
        reason = incomplete.get("reason")
        if reason in {"max_output_tokens", "length"}:
            return "length"

    return "stop"


def _response_to_chat_completion(result: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a responses payload into chat completions format."""
    response_id = result.get("id")
    if not isinstance(response_id, str):
        response_id = f"chatcmpl-{uuid4().hex}"

    tool_calls = _extract_response_tool_calls(result)
    content = _extract_response_text(result)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not content:
            message["content"] = None

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.get("model", model),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _chat_finish_reason(result, bool(tool_calls)),
            }
        ],
        "usage": _map_chat_usage(result.get("usage")),
    }


def _chat_chunk_sse(
    *,
    chunk_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


def _stream_done_sse() -> bytes:
    return b"data: [DONE]\n\n"


def _parse_sse_event(
    event_name: str | None,
    data_lines: list[str],
) -> tuple[str | None, dict[str, Any] | None]:
    if not event_name or not data_lines:
        return None, None
    payload_text = "\n".join(data_lines)
    if payload_text == "[DONE]":
        return event_name, None
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return event_name, None
    if isinstance(payload, dict):
        return event_name, payload
    return event_name, None


def _extract_message_item_text(item: dict[str, Any]) -> str:
    """Extract assistant text from a single output message item."""
    parts: list[str] = []
    content = item.get("content", [])
    if not isinstance(content, list):
        return ""
    for entry in content:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") in {"output_text", "text"} and isinstance(
            entry.get("text"), str
        ):
            parts.append(entry["text"])
    return "".join(parts)


async def _responses_stream_to_chat_chunks(
    upstream: AsyncIterator[bytes],
    *,
    requested_model: str,
) -> AsyncIterator[bytes]:
    """Convert upstream responses SSE into OpenAI chat chunk SSE."""
    buffer = ""
    event_name: str | None = None
    data_lines: list[str] = []
    created = int(time.time())
    chunk_id = f"chatcmpl-{uuid4().hex}"
    model = requested_model
    role_emitted = False
    tool_calls_emitted = False
    saw_text_delta = False
    tool_call_indices: dict[str, int] = {}
    next_tool_call_index = 0

    async for chunk in upstream:
        text = chunk.decode("utf-8", errors="ignore")
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")

            if line:
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                continue

            parsed_event, payload = _parse_sse_event(event_name, data_lines)
            event_name = None
            data_lines = []
            if parsed_event is None or payload is None:
                continue

            if parsed_event == "response.created":
                response = payload.get("response")
                if isinstance(response, dict):
                    rid = response.get("id")
                    if isinstance(rid, str) and rid:
                        chunk_id = rid
                    upstream_model = response.get("model")
                    if isinstance(upstream_model, str) and upstream_model:
                        model = upstream_model
                continue

            if parsed_event == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    saw_text_delta = True
                    if not role_emitted:
                        role_emitted = True
                        yield _chat_chunk_sse(
                            chunk_id=chunk_id,
                            model=model,
                            created=created,
                            delta={"role": "assistant"},
                            finish_reason=None,
                        )
                    yield _chat_chunk_sse(
                        chunk_id=chunk_id,
                        model=model,
                        created=created,
                        delta={"content": delta},
                        finish_reason=None,
                    )
                continue

            if parsed_event == "response.output_item.done":
                item = payload.get("item")
                if (
                    isinstance(item, dict)
                    and item.get("type") == "message"
                    and not saw_text_delta
                ):
                    text = _extract_message_item_text(item)
                    if text:
                        if not role_emitted:
                            role_emitted = True
                            yield _chat_chunk_sse(
                                chunk_id=chunk_id,
                                model=model,
                                created=created,
                                delta={"role": "assistant"},
                                finish_reason=None,
                            )
                        yield _chat_chunk_sse(
                            chunk_id=chunk_id,
                            model=model,
                            created=created,
                            delta={"content": text},
                            finish_reason=None,
                        )

                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call"
                    and isinstance(item.get("name"), str)
                ):
                    tool_calls_emitted = True
                    if not role_emitted:
                        role_emitted = True
                        yield _chat_chunk_sse(
                            chunk_id=chunk_id,
                            model=model,
                            created=created,
                            delta={"role": "assistant"},
                            finish_reason=None,
                        )
                    arguments = item.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = "{}"
                    call_id = item.get("call_id") or item.get("id") or f"call_{uuid4().hex}"
                    if not isinstance(call_id, str):
                        call_id = f"call_{uuid4().hex}"
                    tool_index = tool_call_indices.get(call_id)
                    if tool_index is None:
                        tool_index = next_tool_call_index
                        tool_call_indices[call_id] = tool_index
                        next_tool_call_index += 1
                    yield _chat_chunk_sse(
                        chunk_id=chunk_id,
                        model=model,
                        created=created,
                        delta={
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": item["name"],
                                        "arguments": arguments,
                                    },
                                }
                            ]
                        },
                        finish_reason=None,
                    )
                continue

            if parsed_event in {"response.failed", "response.incomplete"}:
                detail: Any = payload
                detail = payload.get("response", {}).get("error") or payload.get("error") or payload
                raise UpstreamHTTPError(502, detail)

            if parsed_event == "response.completed":
                response = payload.get("response")
                finish_reason = "stop"
                if isinstance(response, dict):
                    finish_reason = _chat_finish_reason(response, tool_calls_emitted)
                if not role_emitted:
                    yield _chat_chunk_sse(
                        chunk_id=chunk_id,
                        model=model,
                        created=created,
                        delta={"role": "assistant"},
                        finish_reason=None,
                    )
                yield _chat_chunk_sse(
                    chunk_id=chunk_id,
                    model=model,
                    created=created,
                    delta={},
                    finish_reason=finish_reason,
                )
                yield _stream_done_sse()
                return

    raise UpstreamHTTPError(502, "Upstream stream ended before response.completed")


# ---------------------------------------------------------------------------
# Shared proxy helper
# ---------------------------------------------------------------------------


async def _proxy_or_stream(
    request: Request,
    body: dict[str, Any],
    *,
    force_upstream_stream: bool = False,
    stream_transform: Callable[[AsyncIterator[bytes]], AsyncIterator[bytes]] | None = None,
) -> JSONResponse | StreamingResponse:
    """Validate model, then forward *body* upstream."""
    _validate_model(body)

    cfg = request.app.state.config
    store = request.app.state.store
    stream = body.get("stream", False)

    try:
        if stream:
            body["stream"] = True
            upstream_iter = cast(
                AsyncIterator[bytes],
                await proxy_request(cfg, store, body, stream=True),
            )
            if stream_transform is not None:
                upstream_iter = stream_transform(upstream_iter)
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
            body["stream"] = force_upstream_stream
            result = await proxy_request(cfg, store, body, stream=False)
            return JSONResponse(content=result)

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

    normalized = _normalize_chat_completions_body(body)
    stream_transform: Callable[[AsyncIterator[bytes]], AsyncIterator[bytes]] | None = None
    if bool(normalized.get("stream")):
        stream_transform = lambda upstream: _responses_stream_to_chat_chunks(
            upstream,
            requested_model=body.get("model", ""),
        )

    result = await _proxy_or_stream(
        request,
        normalized,
        force_upstream_stream=True,
        stream_transform=stream_transform,
    )
    if isinstance(result, JSONResponse):
        payload = json.loads(bytes(result.body))
        return JSONResponse(
            content=_response_to_chat_completion(payload, body.get("model", ""))
        )
    return result


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

    return await _proxy_or_stream(
        request,
        _normalize_responses_body(body),
        force_upstream_stream=True,
    )


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
