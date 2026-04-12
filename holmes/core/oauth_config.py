"""OAuth configuration types, exceptions, and exchange manager."""

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────


class OAuthTokenExchangeError(Exception):
    """Raised when an OAuth authorization code exchange fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Token exchange failed (HTTP {status_code}): {detail}")


class OAuthConfigLookupError(Exception):
    """Raised when a toolset's OAuth config cannot be found or is invalid."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


# ── Data types ────────────────────────────────────────────────────────────


@dataclass
class OAuthEndpoints:
    """Minimal OAuth endpoint config — decoupled from MCPOAuthConfig.

    Passed by toolset_mcp.py to the pure-OAuth functions so they
    don't depend on pydantic models or MCP-specific types.
    """

    authorization_url: Optional[str] = None
    token_url: Optional[str] = None
    client_id: Optional[str] = None
    scopes: Optional[List[str]] = None
    registration_endpoint: Optional[str] = None


class MCPOAuthConfig(BaseModel):
    """OAuth authorization_code config for MCP servers requiring user login.

    Set enabled=true with no other fields to auto-discover OAuth endpoints
    via the MCP OAuth flow (RFC 9728 Protected Resource Metadata + OIDC Discovery + DCR).

    If any of authorization_url, token_url, or client_id is set, enabled defaults to true.
    """

    enabled: bool = Field(default=False, description="Enable OAuth for this MCP server. Auto-set to true when other OAuth fields are provided.")
    authorization_url: Optional[str] = Field(default=None, description="IdP authorization endpoint URL. Auto-discovered if omitted.")
    token_url: Optional[str] = Field(default=None, description="IdP token endpoint URL. Auto-discovered if omitted.")
    client_id: Optional[str] = Field(default=None, description="OAuth public client ID. Auto-registered via DCR if omitted.")
    scopes: Optional[List[str]] = Field(default=None, description="OAuth scopes to request.")
    registration_endpoint: Optional[str] = Field(default=None, exclude=True, description="DCR endpoint (auto-populated during discovery, not user-facing).")

    @model_validator(mode="after")
    def auto_enable_when_configured(self):
        """Auto-enable OAuth when any endpoint or client_id is explicitly set."""
        if not self.enabled and (self.authorization_url or self.token_url or self.client_id):
            self.enabled = True
        return self


# ── Pending OAuth Exchange Manager ────────────────────────────────────────


class _PendingOAuthExchange:
    """State for a pending OAuth approval: PKCE verifier and config."""

    def __init__(self, code_verifier: str, oauth_config: MCPOAuthConfig, redirect_uri: str) -> None:
        self.code_verifier = code_verifier
        self.oauth_config = oauth_config
        self.redirect_uri = redirect_uri


class OAuthExchangeManager:
    """Manages pending OAuth authorization code exchanges.

    Bridges the gap between requires_approval() (which generates PKCE and registers
    a pending exchange) and complete_exchange() (which consumes the pending exchange
    and trades the auth code for tokens).
    """

    def __init__(self) -> None:
        self._pending: Dict[str, _PendingOAuthExchange] = {}
        self._lock = threading.Lock()

    def register_pending(
        self,
        tool_call_id: str,
        code_verifier: str,
        oauth_config: MCPOAuthConfig,
        redirect_uri: str = "",
    ) -> None:
        """Register a pending OAuth exchange for the given tool call."""
        with self._lock:
            self._pending[tool_call_id] = _PendingOAuthExchange(
                code_verifier=code_verifier,
                oauth_config=oauth_config,
                redirect_uri=redirect_uri,
            )

    def complete_exchange(
        self,
        tool_call_id: str,
        payload_json: str,
        request_context: Optional[Dict[str, Any]],
    ) -> None:
        """Exchange an OAuth authorization code for an access token.

        Called from tool_calling_llm when a tool approval decision includes an
        OAuth payload from the frontend browser flow.
        """
        from holmes.core.oauth_utils import _get_token_manager, exchange_code_for_tokens

        with self._lock:
            pending = self._pending.pop(tool_call_id, None)

        if pending is None:
            logger.error("OAuth exchange: no pending exchange for tool_call_id=%s", tool_call_id)
            return

        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            logger.warning("OAuth exchange: invalid JSON payload for tool_call_id=%s", tool_call_id)
            return

        # Frontend may include client_id and client_secret from DCR
        client_id = payload.get("client_id") or pending.oauth_config.client_id
        client_secret = payload.get("client_secret")
        if client_id and not pending.oauth_config.client_id:
            pending.oauth_config.client_id = client_id
            logger.info("OAuth: using client_id from frontend DCR: %s", client_id)

        try:
            token_data = exchange_code_for_tokens(
                token_url=pending.oauth_config.token_url,
                code=payload["code"],
                redirect_uri=payload.get("redirect_uri", ""),
                client_id=client_id,
                code_verifier=pending.code_verifier,
                client_secret=client_secret,
            )
        except (OAuthTokenExchangeError, KeyError, Exception):
            logger.exception("OAuth exchange failed (tool_call_id=%s, token_url=%s)", tool_call_id, pending.oauth_config.token_url)
            return

        _get_token_manager().store_token(pending.oauth_config, token_data, request_context)
        logger.info(
            "OAuth token stored (idp=%s, expires_in=%s, has_refresh=%s)",
            pending.oauth_config.token_url, token_data.get("expires_in"), "refresh_token" in token_data,
        )


# ── Singleton ─────────────────────────────────────────────────────────────

_exchange_manager = OAuthExchangeManager()


def _get_exchange_manager() -> OAuthExchangeManager:
    return _exchange_manager
