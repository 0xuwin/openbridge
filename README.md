# OpenBridge

Bridge your ChatGPT Pro/Plus subscription into an OpenAI-compatible API server.

OpenBridge authenticates with your ChatGPT account via OAuth, generates local API keys, and exposes a proxy server that accepts standard OpenAI SDK requests — forwarding them to the ChatGPT backend on your behalf.

## Requirements

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) (recommended package manager)

## Installation

```bash
# Clone the repository
git clone https://github.com/your-username/openbridge.git
cd openbridge

# Install dependencies
uv sync
```

## Quick Start

### 1. Login

Authenticate with your ChatGPT account. Two methods are available:

```bash
# Browser-based login (default) — opens a browser window
uv run openbridge login

# Headless login — displays a code to enter at auth.openai.com
uv run openbridge login --method device
```

### 2. Create an API Key

Generate a key that clients will use to authenticate with your local server:

```bash
uv run openbridge key create --name "my-app"
```

The raw key (e.g. `ob-aBcDeFgH...`) is displayed **once**. Save it somewhere safe.

### 3. Start the Server

```bash
uv run openbridge serve
```

The server starts on `http://127.0.0.1:8899` by default. You can override the address:

```bash
uv run openbridge serve --host 0.0.0.0 --port 9000
```

### 4. Use with OpenAI SDK

Point any OpenAI-compatible client at your local server:

**Environment variables** (works with most OpenAI-compatible tools):

```bash
export OPENAI_API_KEY="ob-your-key-here"
export OPENAI_BASE_URL="http://127.0.0.1:8899/v1"
```

**Chat Completions API**

```python
from openai import OpenAI

client = OpenAI(
    api_key="ob-your-key-here",
    base_url="http://127.0.0.1:8899/v1",
)

response = client.chat.completions.create(
    model="gpt-5",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

**Responses API** (recommended for new projects)

```python
from openai import OpenAI

client = OpenAI(
    api_key="ob-your-key-here",
    base_url="http://127.0.0.1:8899/v1",
)

response = client.responses.create(
    model="gpt-5",
    input="Hello!",
)
print(response.output_text)
```

**curl**

```bash
# Chat Completions
curl http://127.0.0.1:8899/v1/chat/completions \
  -H "Authorization: Bearer ob-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Responses API
curl http://127.0.0.1:8899/v1/responses \
  -H "Authorization: Bearer ob-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5",
    "input": "Hello!"
  }'
```

## CLI Reference

| Command | Description |
|---|---|
| `openbridge login` | Authenticate with ChatGPT (options: `--method browser\|device`) |
| `openbridge logout` | Remove stored OAuth tokens |
| `openbridge key create` | Generate a new API key (option: `--name NAME`) |
| `openbridge key list` | List all API keys |
| `openbridge key revoke PREFIX` | Revoke an API key by its display prefix |
| `openbridge serve` | Start the API server (options: `--host`, `--port`) |
| `openbridge status` | Show authentication and key status |

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | Chat completions (streaming & non-streaming) |
| `/v1/responses` | POST | Responses API (streaming & non-streaming) |
| `/v1/models` | GET | List available models |
| `/v1/models/{id}` | GET | Retrieve a single model |
| `/health` | GET | Health check (no auth required) |

## Configuration

Settings can be overridden via environment variables or a `.env` file (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `OPENBRIDGE_HOST` | `127.0.0.1` | Server bind address |
| `OPENBRIDGE_PORT` | `8899` | Server bind port |
| `OPENBRIDGE_OAUTH_PORT` | `1455` | Port for OAuth callback during login |
| `OPENBRIDGE_DATA_DIR` | `~/.openbridge` | Directory for persistent data |

## Data Storage

All data is stored in `~/.openbridge/` (configurable):

- `store.json` — API key hashes and encrypted OAuth tokens
- Directory permissions: `0700`; file permissions: `0600`
- `encryption.key` — Fernet encryption key for token encryption (auto-generated)
- OAuth tokens are encrypted at rest using Fernet
- API keys are stored as SHA-256 hashes only; raw keys are never persisted

## Project Structure

```
src/openbridge/
├── cli.py               # Click CLI commands
├── config.py            # Configuration from environment
├── keys.py              # API key generation and hashing
├── store.py             # Encrypted JSON persistence
├── oauth/
│   ├── pkce.py          # PKCE code verifier/challenge
│   ├── tokens.py        # Token exchange, refresh, JWT parsing
│   ├── browser.py       # Browser-based OAuth flow
│   └── device.py        # Device-code OAuth flow
└── server/
    ├── app.py            # FastAPI application factory + shared httpx client lifespan
    ├── auth.py           # API key validation middleware
    ├── proxy.py          # Upstream request forwarding (proxy_collect / proxy_stream)
    ├── normalize.py      # Request normalization (Chat Completions → Codex shape)
    ├── convert.py        # Response conversion (Codex → Chat Completion / SSE chunks)
    └── routes.py         # Route handlers and model validation
```

## License

MIT
