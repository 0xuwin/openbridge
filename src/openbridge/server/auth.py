"""API key authentication middleware for the OpenAI-compatible server."""

from __future__ import annotations

from fastapi import HTTPException, Request

from openbridge.keys import hash_key
from openbridge.store import Store


async def verify_api_key(request: Request) -> None:
    """Validate the Bearer token in the Authorization header.

    Raises ``HTTPException(401)`` if the key is missing or unknown.
    """
    auth_header = request.headers.get("authorization", "")

    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Expected: Bearer <api-key>",
        )

    raw_key = auth_header[7:]  # strip "Bearer "
    key_hash = hash_key(raw_key)
    store: Store = request.app.state.store

    if store.find_key_hash(key_hash) is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
