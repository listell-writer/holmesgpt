"""Shared OAuth utilities for authorization code exchange."""

import logging
from typing import Any, List, Optional

import httpx

from holmes.core.models import OAuthCallbackRequest, OAuthCallbackResponse

logger = logging.getLogger(__name__)


class OAuthTokenExchangeError(Exception):
    """Raised when an OAuth authorization code exchange fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Token exchange failed (HTTP {status_code}): {detail}")


def exchange_code_for_tokens(
    token_url: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: Optional[str] = None,
) -> dict:
    """Exchange an OAuth authorization code for tokens at the IdP's token endpoint.

    Returns the parsed JSON token response (containing at least ``access_token``).
    Raises :class:`OAuthTokenExchangeError` on HTTP failure or missing ``access_token``.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    resp = httpx.post(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )

    if resp.status_code != 200:
        detail = resp.text[:300] if resp.text else "Unknown error"
        raise OAuthTokenExchangeError(resp.status_code, detail)

    token_data = resp.json()
    if "access_token" not in token_data:
        raise OAuthTokenExchangeError(200, f"Response missing 'access_token'. Keys: {list(token_data.keys())}")

    return token_data


class OAuthConfigLookupError(Exception):
    """Raised when a toolset's OAuth config cannot be found or is invalid."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def get_toolset_oauth_config(
    toolsets: List[Any],
    toolset_name: str,
    token_manager: Any,
    client_id_override: Optional[str] = None,
) -> tuple:
    """Look up a toolset's OAuth config from a list of toolsets.

    Returns ``(oauth_config, client_id, token_manager)``.
    Raises :class:`OAuthConfigLookupError` on failure.
    """
    toolset = None
    for ts in toolsets:
        if ts.name == toolset_name:
            toolset = ts
            break

    if not toolset:
        raise OAuthConfigLookupError(f"Toolset '{toolset_name}' not found")

    mcp_config = getattr(toolset, "_mcp_config", None)
    oauth = getattr(mcp_config, "oauth", None) if mcp_config else None
    if not oauth or not oauth.enabled:
        raise OAuthConfigLookupError(f"Toolset '{toolset_name}' does not have OAuth enabled")

    if not oauth.token_url:
        raise OAuthConfigLookupError(f"OAuth config for '{toolset_name}' missing token_url")

    client_id = client_id_override or oauth.client_id
    if not client_id:
        raise OAuthConfigLookupError(f"No client_id available for '{toolset_name}'")

    return oauth, client_id, token_manager


def process_oauth_callback(
    request: OAuthCallbackRequest,
    toolsets: List[Any],
    token_manager: Any,
) -> OAuthCallbackResponse:
    """Process an OAuth callback: look up config, exchange code, store tokens.

    Shared by both the HTTP endpoint and the in-flight tool-approval path.
    """
    oauth, client_id, mgr = get_toolset_oauth_config(
        toolsets, request.toolset_name, token_manager, request.client_id,
    )

    token_data = exchange_code_for_tokens(
        token_url=oauth.token_url,
        code=request.code,
        redirect_uri=request.redirect_uri,
        client_id=client_id,
        code_verifier=request.code_verifier,
    )

    mgr.store_token(oauth, token_data)
    logger.info("OAuth tokens stored for toolset '%s'", request.toolset_name)
    return OAuthCallbackResponse(success=True)
