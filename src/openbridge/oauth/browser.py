"""Browser-based OAuth login flow with local HTTP callback server."""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlencode, parse_qs, urlparse

from openbridge.config import Config
from openbridge.oauth.pkce import PkceCodes, generate_pkce, generate_state
from openbridge.oauth.tokens import exchange_code_for_tokens, extract_account_id
from openbridge.store import OAuthTokens, Store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML templates served by the callback server
# ---------------------------------------------------------------------------

_HTML_SUCCESS = """\
<!doctype html>
<html>
<head><title>OpenBridge - Authorization Successful</title>
<style>
  body { font-family: system-ui, sans-serif; display: flex;
         justify-content: center; align-items: center; height: 100vh;
         margin: 0; background: #131010; color: #f1ecec; }
  .c { text-align: center; padding: 2rem; }
  h1 { color: #f1ecec; } p { color: #b7b1b1; }
</style></head>
<body><div class="c">
  <h1>Authorization Successful</h1>
  <p>You can close this window and return to the terminal.</p>
</div>
<script>setTimeout(() => window.close(), 2000)</script>
</body></html>"""

_HTML_ERROR = """\
<!doctype html>
<html>
<head><title>OpenBridge - Authorization Failed</title>
<style>
  body { font-family: system-ui, sans-serif; display: flex;
         justify-content: center; align-items: center; height: 100vh;
         margin: 0; background: #131010; color: #f1ecec; }
  .c { text-align: center; padding: 2rem; }
  h1 { color: #fc533a; } p { color: #b7b1b1; }
  .err { color: #ff917b; font-family: monospace; margin-top: 1rem;
         padding: 1rem; background: #3c140d; border-radius: .5rem; }
</style></head>
<body><div class="c">
  <h1>Authorization Failed</h1>
  <p>An error occurred during authorization.</p>
  <div class="err">%(error)s</div>
</div></body></html>"""


def _build_authorize_url(cfg: Config, pkce: PkceCodes, state: str) -> str:
    params = urlencode({
        "response_type": "code",
        "client_id": cfg.oauth_client_id,
        "redirect_uri": cfg.oauth_redirect_uri,
        "scope": "openid profile email offline_access",
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "state": state,
    })
    return f"{cfg.oauth_issuer}/oauth/authorize?{params}"


async def run_browser_login(cfg: Config, store: Store) -> None:
    """Execute the full browser-based OAuth login flow.

    1. Start a local HTTP server to receive the callback.
    2. Open the authorize URL in the user's browser.
    3. Wait for the redirect with the authorization code.
    4. Exchange the code for tokens and persist them.
    """
    pkce = generate_pkce()
    state = generate_state()

    loop = asyncio.get_running_loop()
    code_future: asyncio.Future[str] = loop.create_future()

    # -- tiny HTTP handler using asyncio.Protocol ----------------------------

    class _CallbackProtocol(asyncio.Protocol):
        """Minimal HTTP/1.1 protocol that handles the OAuth callback."""

        def __init__(self) -> None:
            self.transport: asyncio.Transport | None = None

        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            self.transport = transport

        def data_received(self, data: bytes) -> None:
            try:
                request_line = data.split(b"\r\n")[0].decode()
                path = request_line.split(" ")[1]
                parsed = urlparse(path)

                if parsed.path == "/auth/callback":
                    qs = parse_qs(parsed.query)
                    self._handle_callback(qs)
                else:
                    self._respond(404, "Not found")
            except Exception:
                logger.exception("Error handling callback request")
                self._respond(500, "Internal server error")

        def _handle_callback(self, qs: dict[str, list[str]]) -> None:
            error = qs.get("error", [None])[0]  # type: ignore[list-item]
            if error:
                desc = qs.get("error_description", [error])[0]
                self._respond(200, _HTML_ERROR % {"error": desc}, "text/html")
                if not code_future.done():
                    code_future.set_exception(RuntimeError(desc))
                return

            code = qs.get("code", [None])[0]  # type: ignore[list-item]
            cb_state = qs.get("state", [None])[0]  # type: ignore[list-item]

            if not code:
                self._respond(400, _HTML_ERROR % {"error": "Missing authorization code"}, "text/html")
                if not code_future.done():
                    code_future.set_exception(RuntimeError("Missing code"))
                return

            if cb_state != state:
                self._respond(400, _HTML_ERROR % {"error": "Invalid state"}, "text/html")
                if not code_future.done():
                    code_future.set_exception(RuntimeError("State mismatch"))
                return

            self._respond(200, _HTML_SUCCESS, "text/html")
            if not code_future.done():
                code_future.set_result(code)

        def _respond(
            self, status: int, body: str, content_type: str = "text/plain"
        ) -> None:
            status_text = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
            header = (
                f"HTTP/1.1 {status} {status_text.get(status, 'Error')}\r\n"
                f"Content-Type: {content_type}; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode())}\r\n"
                f"Connection: close\r\n\r\n"
            )
            if self.transport:
                self.transport.write(header.encode() + body.encode())
                self.transport.close()

    server = await loop.create_server(
        _CallbackProtocol, "127.0.0.1", cfg.oauth_port
    )

    auth_url = _build_authorize_url(cfg, pkce, state)
    print(f"\nOpen this URL to log in:\n\n  {auth_url}\n")

    try:
        code = await asyncio.wait_for(code_future, timeout=300)
    except asyncio.TimeoutError:
        raise RuntimeError("Authorization timed out (5 minutes)")
    finally:
        server.close()
        await server.wait_closed()

    # Exchange code for tokens
    tokens = await exchange_code_for_tokens(
        issuer=cfg.oauth_issuer,
        client_id=cfg.oauth_client_id,
        code=code,
        redirect_uri=cfg.oauth_redirect_uri,
        code_verifier=pkce.verifier,
    )

    account_id = extract_account_id(tokens)
    store.set_oauth(OAuthTokens(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=time.time() + tokens.expires_in,
        account_id=account_id,
    ))
    logger.info("Login successful, tokens saved.")
