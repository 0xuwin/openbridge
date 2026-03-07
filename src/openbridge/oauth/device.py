"""Headless device-code OAuth login flow."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from openbridge.config import Config
from openbridge.oauth.tokens import (
    TokenResponse,
    extract_account_id,
)
from openbridge.store import OAuthTokens, Store

logger = logging.getLogger(__name__)

_POLLING_SAFETY_MARGIN = 3  # seconds added to the suggested interval


async def run_device_login(cfg: Config, store: Store) -> None:
    """Execute the headless device-code OAuth login flow.

    1. Request a device code and user code from the issuer.
    2. Display the user code and verification URL.
    3. Poll for authorization completion.
    4. Exchange the authorization code for tokens and persist them.
    """
    async with httpx.AsyncClient() as client:
        # Step 1: request device code
        resp = await client.post(
            f"{cfg.oauth_issuer}/api/accounts/deviceauth/usercode",
            json={"client_id": cfg.oauth_client_id},
            headers={"User-Agent": "openbridge/0.1.0"},
        )
        resp.raise_for_status()
        device_data = resp.json()

    device_auth_id: str = device_data["device_auth_id"]
    user_code: str = device_data["user_code"]
    interval = max(int(device_data.get("interval", 5)), 1)

    verification_url = f"{cfg.oauth_issuer}/codex/device"
    print(
        f"\nTo log in, visit:  {verification_url}\n"
        f"Enter this code:   {user_code}\n"
        f"\nWaiting for authorization...\n"
    )

    # Step 2: poll for completion
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(interval + _POLLING_SAFETY_MARGIN)

            resp = await client.post(
                f"{cfg.oauth_issuer}/api/accounts/deviceauth/token",
                json={
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                },
                headers={"User-Agent": "openbridge/0.1.0"},
            )

            if resp.status_code in (403, 404):
                # Authorization pending – keep polling
                continue

            if not resp.is_success:
                raise RuntimeError(
                    f"Device authorization failed: {resp.status_code} {resp.text}"
                )

            # Got an authorization code – exchange it for tokens
            data = resp.json()
            auth_code: str = data["authorization_code"]
            code_verifier: str = data["code_verifier"]

            token_resp = await client.post(
                f"{cfg.oauth_issuer}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": f"{cfg.oauth_issuer}/deviceauth/callback",
                    "client_id": cfg.oauth_client_id,
                    "code_verifier": code_verifier,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()

            tokens = TokenResponse(
                id_token=token_data.get("id_token", ""),
                access_token=token_data["access_token"],
                refresh_token=token_data["refresh_token"],
                expires_in=token_data.get("expires_in", 3600),
            )

            account_id = extract_account_id(tokens)
            store.set_oauth(
                OAuthTokens(
                    access_token=tokens.access_token,
                    refresh_token=tokens.refresh_token,
                    expires_at=time.time() + tokens.expires_in,
                    account_id=account_id,
                )
            )
            logger.info("Login successful, tokens saved.")
            print("Login successful! Tokens saved.")
            return
