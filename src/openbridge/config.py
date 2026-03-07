"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    # Server settings
    host: str = "127.0.0.1"
    port: int = 8899
    oauth_port: int = 1455

    # Data directory
    data_dir: Path = field(default_factory=lambda: Path.home() / ".openbridge")

    # OpenAI OAuth constants (public client ID, not a secret)
    oauth_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    oauth_issuer: str = "https://auth.openai.com"
    codex_api_endpoint: str = (
        "https://chatgpt.com/backend-api/codex/responses"
    )

    @classmethod
    def from_env(cls) -> Config:
        """Build configuration from environment variables with defaults."""
        return cls(
            host=os.getenv("OPENBRIDGE_HOST", "127.0.0.1"),
            port=int(os.getenv("OPENBRIDGE_PORT", "8899")),
            oauth_port=int(os.getenv("OPENBRIDGE_OAUTH_PORT", "1455")),
            data_dir=Path(
                os.path.expanduser(
                    os.getenv("OPENBRIDGE_DATA_DIR", "~/.openbridge")
                )
            ),
        )

    @property
    def store_path(self) -> Path:
        return self.data_dir / "store.json"

    @property
    def oauth_redirect_uri(self) -> str:
        return f"http://localhost:{self.oauth_port}/auth/callback"

    def ensure_data_dir(self) -> None:
        """Create data directory with restricted permissions if needed."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
