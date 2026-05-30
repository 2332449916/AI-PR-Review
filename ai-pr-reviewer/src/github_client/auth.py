"""
GitHub authentication helpers.

Supports two authentication modes:
1. **Personal Access Token (PAT)**: Simple token-based auth for personal use.
2. **GitHub App**: Installation-based auth suitable for multi-repo or
   organisational use.

Design rationale:
- We decouple auth from the fetcher so that the same fetcher can be used
  with different auth strategies
- GitHub App auth requires JWT generation and token exchange — handled here
  so the rest of the codebase never deals with those details
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class AuthProvider(Protocol):
    """Protocol for GitHub authentication strategies."""

    def get_token(self) -> str:
        """Return a valid GitHub API token."""
        ...

    @property
    def auth_type(self) -> str:
        """Human-readable auth type name (for logging/debugging)."""
        ...


# ---------------------------------------------------------------------------
# PAT authentication
# ---------------------------------------------------------------------------


@dataclass
class PATAuth:
    """Personal Access Token authentication."""

    token: str
    auth_type: str = field(default="pat", init=False)

    def get_token(self) -> str:
        return self.token


# ---------------------------------------------------------------------------
# GitHub App authentication
# ---------------------------------------------------------------------------

try:
    import jwt as _jwt
except ImportError:
    _jwt = None  # type: ignore[assignment]


@dataclass
class GitHubAppAuth:
    """GitHub App installation authentication.

    Uses a private key (PEM) to sign a JWT, then exchanges it for an
    installation access token.

    Args:
        app_id: GitHub App ID (numeric).
        private_key_pem: The full PEM-encoded private key string.
        installation_id: The installation ID (not the app ID).
    """

    app_id: str
    private_key_pem: str
    installation_id: str

    auth_type: str = field(default="github_app", init=False)
    _cached_token: str | None = field(default=None, init=False, repr=False)
    _token_expires_at: float = field(default=0.0, init=False, repr=False)

    def get_token(self) -> str:
        """Return a valid installation token, refreshing if expired."""
        if self._cached_token and time.time() < self._token_expires_at - 60:
            return self._cached_token

        if _jwt is None:
            raise ImportError(
                "PyJWT is required for GitHub App authentication. "
                "Install it with: pip install pyjwt"
            )

        # Create a JWT signed with the app's private key
        now = int(time.time())
        payload = {
            "iat": now - 60,  # issued 60s ago (clock drift tolerance)
            "exp": now + 600,  # expires in 10 minutes
            "iss": self.app_id,
        }
        app_token = _jwt.encode(payload, self.private_key_pem, algorithm="RS256")

        # Exchange the JWT for an installation access token
        import httpx

        headers = {
            "Authorization": f"Bearer {app_token}",
            "Accept": "application/vnd.github+json",
        }
        url = (
            f"https://api.github.com/app/installations/"
            f"{self.installation_id}/access_tokens"
        )

        try:
            resp = httpx.post(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self._cached_token = data["token"]
            self._token_expires_at = time.time() + (data.get("expires_in", 3600) - 60)
            logger.info("Obtained GitHub App installation token (expires in %d s)", data.get("expires_in", 3600))
            return self._cached_token
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to get installation token: %s %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            raise
