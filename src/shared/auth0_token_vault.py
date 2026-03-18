"""
Auth0 Token Vault — secure token exchange for AI agents.

Based on RFC 8693 (OAuth 2.0 Token Exchange).
Docs: https://auth0.com/ai/docs/intro/token-vault

Flow:
  1. User logs in → gets Auth0 access_token + refresh_token
  2. User connects external account (GitHub) → Auth0 stores provider tokens in Vault
  3. Agent calls get_github_token(refresh_token) → receives scoped GitHub access token
  4. Agent uses token for one request, then discards it (never stored)
"""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger("auth0_token_vault")

# ── Sensitive fields that must NEVER appear in logs ──────────────────────────
_SENSITIVE = frozenset({
    "access_token", "refresh_token", "github_token",
    "subject_token", "client_secret", "id_token",
})


def _safe(data: dict) -> dict:
    """Return a copy of *data* with all sensitive values redacted."""
    return {k: "***REDACTED***" if k in _SENSITIVE else v for k, v in data.items()}


# ── Token Exchange constants (RFC 8693) ──────────────────────────────────────
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
_REFRESH_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:refresh_token"
_FEDERATED_TOKEN_TYPE = (
    "http://auth0.com/oauth/token-type/federated-connection-access-token"
)


class GitHubScope(str, Enum):
    """Minimal GitHub OAuth scopes — always request least privilege."""
    READ_ONLY = "repo"            # read repo content (Planner / Risks agents)
    WRITE_PR = "repo"             # create branches + PRs  (Code Execution agent)
    READ_USER = "read:user"       # read user profile


@dataclass(frozen=True)
class VaultToken:
    """
    Short-lived GitHub access token returned by Token Vault.
    Never persisted — lives only for the duration of a single agent request.
    """
    access_token: str
    token_type: str = "bearer"
    scope: str = ""

    def auth_header(self) -> str:
        return f"Bearer {self.access_token}"

    def __repr__(self) -> str:
        # Safety: never expose the token in repr / logs
        return f"VaultToken(token_type={self.token_type!r}, scope={self.scope!r}, access_token=***)"


class TokenVaultError(Exception):
    """Raised when Auth0 Token Vault exchange fails."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class Auth0TokenVault:
    """
    Thin async wrapper around the Auth0 Token Exchange endpoint.

    Usage::

        vault = Auth0TokenVault()

        # Inside an async request handler — token lives only here:
        github_token = await vault.get_github_token(
            subject_token=user.refresh_token,
            scopes=["repo"],
            use_refresh_token=True,
        )
        headers = {"Authorization": github_token.auth_header()}
        # ... call GitHub API ...
        # token goes out of scope — never stored

    Environment variables (loaded automatically)::

        AUTH0_DOMAIN          — e.g. "your-tenant.auth0.com"
        AUTH0_CLIENT_ID       — confidential client ID
        AUTH0_CLIENT_SECRET   — confidential client secret
    """

    def __init__(
        self,
        domain: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.domain = (domain or os.getenv("AUTH0_DOMAIN", "")).rstrip("/")
        self.client_id = client_id or os.getenv("AUTH0_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("AUTH0_CLIENT_SECRET", "")
        self.timeout = timeout

        if not all([self.domain, self.client_id, self.client_secret]):
            raise ValueError(
                "Auth0TokenVault requires AUTH0_DOMAIN, AUTH0_CLIENT_ID, "
                "and AUTH0_CLIENT_SECRET to be set."
            )

        self._token_url = f"https://{self.domain}/oauth/token"

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_github_token(
        self,
        subject_token: str,
        scopes: Optional[list[str]] = None,
        use_refresh_token: bool = True,
    ) -> VaultToken:
        """
        Exchange an Auth0 token for a scoped GitHub access token via Token Vault.

        :param subject_token:     User's Auth0 refresh_token (preferred) or access_token.
        :param scopes:            GitHub OAuth scopes to request. Default: ["repo"].
        :param use_refresh_token: True → subject_token is a refresh token (recommended).
                                  False → subject_token is an access token.
        :returns:                 VaultToken ready to use in Authorization header.
        :raises TokenVaultError:  On HTTP errors or unexpected Auth0 responses.
        """
        return await self._exchange(
            connection="github",
            subject_token=subject_token,
            scopes=scopes or ["repo"],
            use_refresh_token=use_refresh_token,
        )

    async def get_slack_token(
        self,
        subject_token: str,
        scopes: Optional[list[str]] = None,
        use_refresh_token: bool = True,
    ) -> VaultToken:
        """Exchange for a Slack access token (optional Slack notification support)."""
        return await self._exchange(
            connection="slack",
            subject_token=subject_token,
            scopes=scopes or ["chat:write"],
            use_refresh_token=use_refresh_token,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _exchange(
        self,
        connection: str,
        subject_token: str,
        scopes: list[str],
        use_refresh_token: bool,
    ) -> VaultToken:
        """
        Perform the actual RFC 8693 token exchange against Auth0's /oauth/token.

        Auth0-specific parameters:
          - requested_token_type: federated-connection-access-token
          - connection: the Auth0 social connection name (e.g. "github")
          - scope: space-separated list of provider-specific scopes
        """
        subject_token_type = (
            _REFRESH_TOKEN_TYPE if use_refresh_token else _ACCESS_TOKEN_TYPE
        )

        payload = {
            "grant_type": _GRANT_TYPE,
            "client_id": self.client_id,
            "client_secret": self.client_secret,         # kept out of logs below
            "subject_token": subject_token,               # kept out of logs below
            "subject_token_type": subject_token_type,
            "requested_token_type": _FEDERATED_TOKEN_TYPE,
            "connection": connection,
            "scope": " ".join(scopes),
        }

        logger.debug(
            "Token Vault exchange → connection=%s scopes=%s",
            connection, scopes
            # payload intentionally NOT logged (contains secrets)
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self._token_url, data=payload)
        except httpx.TimeoutException as exc:
            raise TokenVaultError(
                f"Auth0 Token Vault request timed out after {self.timeout}s"
            ) from exc
        except httpx.RequestError as exc:
            raise TokenVaultError(f"Auth0 Token Vault network error: {exc}") from exc

        if response.status_code != 200:
            # Try to extract Auth0 error description without leaking tokens
            try:
                err_body = response.json()
                err_msg = err_body.get("error_description") or err_body.get("error", "unknown")
            except Exception:
                err_msg = response.text[:200]

            logger.error(
                "Token Vault exchange failed: status=%d connection=%s error=%s",
                response.status_code, connection, err_msg,
            )
            raise TokenVaultError(
                f"Token Vault exchange failed ({response.status_code}): {err_msg}",
                status_code=response.status_code,
            )

        body = response.json()

        if "access_token" not in body:
            raise TokenVaultError(
                f"Token Vault response missing 'access_token'. "
                f"Keys received: {list(body.keys())}"
            )

        logger.info(
            "Token Vault exchange succeeded → connection=%s scope=%s",
            connection, body.get("scope", ""),
        )

        return VaultToken(
            access_token=body["access_token"],
            token_type=body.get("token_type", "bearer"),
            scope=body.get("scope", " ".join(scopes)),
        )

    # ── Auth0 Connected Accounts helpers ─────────────────────────────────────

    def get_connect_url(self, connection: str, redirect_uri: str, state: str = "") -> str:
        """
        Build the URL to initiate the Connected Accounts flow for a given connection.
        The frontend redirects the user here to link their external account.

        :param connection:    Auth0 connection name, e.g. "github"
        :param redirect_uri:  Callback URL (must be in Auth0 Allowed Callbacks)
        :param state:         Optional opaque state for CSRF protection
        :returns:             Full redirect URL
        """
        params = (
            f"response_type=code"
            f"&client_id={self.client_id}"
            f"&connection={connection}"
            f"&redirect_uri={redirect_uri}"
            f"&scope=openid profile email offline_access"
            f"&prompt=consent"
        )
        if state:
            params += f"&state={state}"
        return f"https://{self.domain}/authorize?{params}"
