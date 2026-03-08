"""Response conversion: transform Codex upstream payloads into OpenAI-compatible formats."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator
from uuid import uuid4

from openbridge.server.proxy import iter_sse_events

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Non-streaming: Responses payload -> Chat Completion object
# ---------------------------------------------------------------------------


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
    for call_index, item in enumerate(result.get("output", [])):
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
    """Derive the OpenAI chat finish_reason from a Codex response payload."""
    if has_tool_calls:
        return "tool_calls"

    incomplete = result.get("incomplete_details")
    if isinstance(incomplete, dict):
        reason = incomplete.get("reason")
        if reason in {"max_output_tokens", "length"}:
            return "length"

    return "stop"


def response_to_chat_completion(result: dict[str, Any], model: str) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Streaming: Responses SSE -> Chat Completion chunk SSE
# ---------------------------------------------------------------------------


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


def _error_sse(message: str, error_type: str = "upstream_error") -> bytes:
    """Emit an OpenAI-compatible error event for in-stream errors.

    Once HTTP headers are sent the only way to signal an error to the client
    is via the SSE body; raising an exception would just close the connection.
    """
    payload = {"error": {"message": message, "type": error_type}}
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


def _extract_message_item_text(item: dict[str, Any]) -> str:
    """Extract assistant text from a single output message item."""
    parts: list[str] = []
    content = item.get("content", [])
    if not isinstance(content, list):
        return ""
    for entry in content:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") in {"output_text", "text"} and isinstance(entry.get("text"), str):
            parts.append(entry["text"])
    return "".join(parts)


async def responses_stream_to_chat_chunks(
    upstream: AsyncIterator[bytes],
    *,
    requested_model: str,
) -> AsyncIterator[bytes]:
    """Convert upstream responses SSE into OpenAI chat chunk SSE."""
    created = int(time.time())
    chunk_id = f"chatcmpl-{uuid4().hex}"
    model = requested_model
    role_emitted = False
    tool_calls_emitted = False
    saw_text_delta = False
    tool_call_indices: dict[str, int] = {}
    next_tool_call_index = 0

    async for event_name, payload in iter_sse_events(upstream):
        if event_name == "response.created":
            response = payload.get("response")
            if isinstance(response, dict):
                rid = response.get("id")
                if isinstance(rid, str) and rid:
                    chunk_id = rid
                upstream_model = response.get("model")
                if isinstance(upstream_model, str) and upstream_model:
                    model = upstream_model

        elif event_name == "response.output_text.delta":
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

        elif event_name == "response.output_item.done":
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

        elif event_name in {"response.failed", "response.incomplete"}:
            detail = payload.get("response", {}).get("error") or payload.get("error") or payload
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            logger.warning("Upstream stream error event '%s': %s", event_name, message)
            yield _error_sse(str(message))
            yield _stream_done_sse()
            return

        elif event_name == "response.completed":
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

    logger.warning("Upstream stream ended before response.completed")
    yield _error_sse("Upstream stream ended before response.completed")
    yield _stream_done_sse()
