"""Shared OAuth utilities for authorization code exchange."""

import logging
from typing import Optional

import httpx

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
