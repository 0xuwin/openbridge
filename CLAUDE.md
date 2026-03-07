# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working on the OpenBridge codebase.

## Project Overview

OpenBridge is a Python CLI tool and local API server that bridges ChatGPT Pro/Plus subscriptions to an OpenAI-compatible API. It authenticates via OpenAI OAuth, generates local API keys, and proxies standard OpenAI SDK requests to the ChatGPT backend.

## Development Setup

```bash
uv sync          # Install all dependencies
uv run openbridge --help   # Verify CLI works
```

Python >= 3.11 is required. The project uses `uv` as its package manager.

## Common Commands

```bash
# Run the CLI
uv run openbridge <command>

# Run a specific module directly
uv run python -m openbridge

# Verify all imports resolve
uv run python -c "from openbridge.cli import main"

# Re-sync after changing pyproject.toml
uv sync
```

## Architecture

```
src/openbridge/
├── cli.py           # Click CLI entry point; all user-facing commands
├── config.py        # Immutable Config dataclass, loaded from env vars
├── keys.py          # API key generation (ob-xxx) and SHA-256 hashing
├── store.py         # Fernet-encrypted JSON persistence (~/.openbridge/store.json)
├── oauth/           # OpenAI OAuth flows
│   ├── pkce.py      # PKCE S256 verifier/challenge generation
│   ├── tokens.py    # Token exchange, refresh, JWT claim parsing
│   ├── browser.py   # Browser OAuth flow (localhost callback server)
│   └── device.py    # Headless device-code OAuth flow
└── server/          # FastAPI-based OpenAI-compatible API server
    ├── app.py       # Application factory; attaches Config + Store to app.state
    ├── auth.py      # Bearer token validation (hashes incoming key, checks store)
    ├── proxy.py     # Forwards requests to ChatGPT codex endpoint; handles token refresh
    └── routes.py    # /v1/chat/completions, /v1/responses, /v1/models
```

**Key data flow:** Client request (with `ob-` API key) -> `auth.py` validates key -> `routes.py` parses request -> `proxy.py` adds OAuth token + rewrites URL -> ChatGPT backend -> response streamed back to client.

## Code Conventions

- **Language:** Python 3.11+, use `from __future__ import annotations` in all modules
- **Type hints:** All function signatures should have type annotations
- **Imports:** Use absolute imports (`from openbridge.config import Config`), not relative
- **Dataclasses:** Prefer `@dataclass` for data structures; use `frozen=True` for immutable ones
- **Async:** OAuth flows and proxy use `async/await` with `httpx.AsyncClient`; the server is FastAPI (async)
- **CLI:** Built with Click; lazy-import heavy modules inside command functions to keep startup fast
- **Formatting:** Standard Python conventions — 4-space indent, double quotes for user-facing strings, no trailing whitespace
- **No hardcoded secrets:** OAuth tokens are encrypted at rest; API keys stored as SHA-256 hashes only; file permissions are `0600`/`0700`

## Security Considerations

- The OAuth client ID (`app_EMoamEEZ73f0CkXaXp7hrann`) is a **public** client ID, not a secret — it is safe to have in source code
- OAuth tokens in `store.json` are Fernet-encrypted; the key is stored in `~/.openbridge/encryption.key` (generated once, `0600` permissions)
- Raw API keys are never persisted; only their SHA-256 hashes are stored
- The store file has `0600` permissions; the data directory has `0700`
- The server binds to `127.0.0.1` by default — binding to `0.0.0.0` exposes it to the network

## Adding New Models

Edit the `_CODEX_MODELS` set in `src/openbridge/server/routes.py`.

## Adding New API Endpoints

1. Add the route handler in `src/openbridge/server/routes.py` on the existing `router`
2. If it needs upstream proxying, add a corresponding function in `src/openbridge/server/proxy.py`
3. All routes on `router` automatically require API key auth via the `verify_api_key` dependency
