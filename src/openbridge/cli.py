"""CLI entry point – ``openbridge`` command."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import click

from openbridge import __version__
from openbridge.config import Config

# Configure logging once at module level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _cfg() -> Config:
    return Config.from_env()


def _store(cfg: Config | None = None):
    from openbridge.store import Store
    c = cfg or _cfg()
    c.ensure_data_dir()
    return Store(c.store_path)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__, prog_name="openbridge")
def main() -> None:
    """OpenBridge – Bridge ChatGPT Pro/Plus subscriptions to an OpenAI-compatible API."""


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

@main.command()
@click.option(
    "--method",
    type=click.Choice(["browser", "device"]),
    default="browser",
    help="OAuth login method.",
)
def login(method: str) -> None:
    """Authenticate with your ChatGPT account."""
    cfg = _cfg()
    store = _store(cfg)

    if method == "browser":
        from openbridge.oauth.browser import run_browser_login
        asyncio.run(run_browser_login(cfg, store))
    else:
        from openbridge.oauth.device import run_device_login
        asyncio.run(run_device_login(cfg, store))

    click.echo("Login complete.")


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------

@main.command()
def logout() -> None:
    """Remove stored OAuth tokens."""
    store = _store()
    store.clear_oauth()
    click.echo("Logged out. OAuth tokens removed.")


# ---------------------------------------------------------------------------
# key management
# ---------------------------------------------------------------------------

@main.group(name="key")
def key_group() -> None:
    """Manage API keys."""


@key_group.command(name="create")
@click.option("--name", default="default", help="Human-readable name for the key.")
def key_create(name: str) -> None:
    """Generate a new API key."""
    from openbridge.keys import generate_api_key

    store = _store()
    raw_key, record = generate_api_key(name)
    store.add_api_key(record)

    click.echo(f"\nAPI key created: {raw_key}")
    click.echo(
        "\nSave this key – it will not be shown again.\n"
        "Use it as a Bearer token when calling the OpenBridge server.\n"
    )


@key_group.command(name="list")
def key_list() -> None:
    """List all API keys."""
    store = _store()
    keys = store.list_api_keys()
    if not keys:
        click.echo("No API keys found. Create one with: openbridge key create")
        return
    click.echo(f"{'PREFIX':<20} {'NAME':<20} {'CREATED'}")
    click.echo("-" * 60)
    for k in keys:
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(k.created_at))
        click.echo(f"{k.key_prefix:<20} {k.name:<20} {created}")


@key_group.command(name="revoke")
@click.argument("prefix")
def key_revoke(prefix: str) -> None:
    """Revoke an API key by its prefix."""
    store = _store()
    if store.remove_api_key(prefix):
        click.echo(f"Key {prefix} revoked.")
    else:
        click.echo(f"No key found with prefix '{prefix}'.", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@main.command()
@click.option("--host", default=None, help="Bind address (default from env or 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Bind port (default from env or 8899).")
def serve(host: str | None, port: int | None) -> None:
    """Start the OpenAI-compatible API server."""
    import uvicorn

    from openbridge.server.app import create_app

    cfg = _cfg()
    store = _store(cfg)

    # Check auth state
    if store.get_oauth() is None:
        click.echo("Not authenticated. Run `openbridge login` first.", err=True)
        sys.exit(1)

    if not store.list_api_keys():
        click.echo("No API keys found. Run `openbridge key create` first.", err=True)
        sys.exit(1)

    bind_host = host or cfg.host
    bind_port = port or cfg.port

    app = create_app(cfg, store)

    click.echo(f"\nOpenBridge server starting on http://{bind_host}:{bind_port}")
    click.echo("Use your API key as a Bearer token.\n")

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@main.command()
def status() -> None:
    """Show current authentication and key status."""
    cfg = _cfg()
    store = _store(cfg)

    tokens = store.get_oauth()
    if tokens is None:
        click.echo("Auth:  Not logged in")
    else:
        remaining = tokens.expires_at - time.time()
        if remaining > 0:
            mins = int(remaining // 60)
            click.echo(f"Auth:  Logged in (token valid for ~{mins}m)")
        else:
            click.echo("Auth:  Logged in (token expired, will auto-refresh)")
        if tokens.account_id:
            click.echo(f"  Account ID: {tokens.account_id}")

    keys = store.list_api_keys()
    click.echo(f"Keys:  {len(keys)} API key(s)")
    for k in keys:
        click.echo(f"  - {k.key_prefix}  ({k.name})")
