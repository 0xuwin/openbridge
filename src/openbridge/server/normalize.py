"""Request normalization: adapt OpenAI-compatible payloads into Codex backend shape."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Responses API normalization
# ---------------------------------------------------------------------------


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


def normalize_responses_body(body: dict[str, Any]) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Chat Completions normalization
# ---------------------------------------------------------------------------


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


def _convert_chat_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a single chat completion message into zero or more Codex input items.

    Returns a list because an assistant message with tool_calls expands into
    multiple items: one ``function_call`` per call, plus an optional ``message``
    item when the assistant also produced text content.
    """
    role = message.get("role")
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        raise HTTPException(status_code=400, detail="Invalid chat message role")

    if role in {"system", "developer"}:
        return []

    content = message.get("content")
    if isinstance(content, str):
        normalized_content = [{"type": "input_text", "text": content}]
    elif isinstance(content, list):
        normalized_content = [_normalize_chat_content_item(item) for item in content]
    else:
        normalized_content = []

    if role == "tool":
        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise HTTPException(
                status_code=400,
                detail="Tool message is missing 'tool_call_id'",
            )
        return [
            {
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": content if isinstance(content, str) else normalized_content,
            }
        ]

    if role == "assistant":
        items: list[dict[str, Any]] = []
        # Expand each tool_call into a function_call item.
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for idx, call in enumerate(tool_calls):
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    continue
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = "{}"
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    call_id = f"chat_call_{idx}"
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                    }
                )
        # Only emit a message item when there is actual text content.
        if normalized_content:
            items.append({"type": "message", "role": "assistant", "content": normalized_content})
        return items

    # user role
    return [{"type": "message", "role": role, "content": normalized_content}]


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


def normalize_chat_completions_body(body: dict[str, Any]) -> dict[str, Any]:
    """Adapt Chat Completions requests into the Codex responses shape."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="Missing or invalid 'messages' field")

    input_items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="Invalid message in 'messages'")
        input_items.extend(_convert_chat_message(message))

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
