"""Shared OAuth utilities: token exchange, PKCE, DCR, CLI flow, and discovery."""

import logging
import secrets
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from mcp.client.auth.oauth2 import PKCEParameters
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
)

from holmes.core.oauth_config import (
    OAuthConfigLookupError,
    OAuthEndpoints,
    OAuthTokenExchangeError,
)

logger = logging.getLogger(__name__)


# ── Singleton token manager ──────────────────────────────────────────────
# Lazy-initialized to avoid circular import (oauth_utils → oauth_token_manager
# → toolsets/__init__ → toolset_mcp → oauth_utils).

_token_manager = None


def _get_token_manager():
    global _token_manager
    if _token_manager is None:
        from holmes.plugins.toolsets.mcp.oauth_token_manager import OAuthTokenManager
        _token_manager = OAuthTokenManager()
    return _token_manager


def set_oauth_dal(dal: Any) -> None:
    """Set the DAL instance for OAuth DB operations. Called during server startup."""
    _get_token_manager().set_dal(dal)


# ── Token exchange ────────────────────────────────────────────────────────


def exchange_code_for_tokens(
    token_url: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: Optional[str] = None,
    client_secret: Optional[str] = None,
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

    # Some IdPs (e.g. Notion) require client credentials via HTTP Basic Auth,
    # while others (e.g. Supabase) accept them in the POST body.
    # Try Basic Auth first when client_secret is present, fall back to POST body.
    auth = None
    if client_secret:
        auth = httpx.BasicAuth(client_id, client_secret)

    resp = httpx.post(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        auth=auth,
        timeout=30,
    )

    # If Basic Auth failed, retry with client_secret in POST body
    if client_secret and not resp.is_success:
        data["client_secret"] = client_secret
        resp = httpx.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

    if not resp.is_success:
        detail = resp.text[:300] if resp.text else "Unknown error"
        raise OAuthTokenExchangeError(resp.status_code, detail)

    token_data = resp.json()
    if "access_token" not in token_data:
        raise OAuthTokenExchangeError(resp.status_code, f"Response missing 'access_token'. Keys: {list(token_data.keys())}")

    return token_data


# ── PKCE ──────────────────────────────────────────────────────────────────


def generate_pkce() -> Tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Delegates to the MCP SDK's ``PKCEParameters.generate()``.
    Returns (code_verifier, code_challenge).
    """
    pkce = PKCEParameters.generate()
    return pkce.code_verifier, pkce.code_challenge


# ── CLI OAuth flow helpers ────────────────────────────────────────────────


def find_available_port(start: int = 18900, end: int = 18920) -> int:
    """Find an available TCP port in the given range. Returns 0 if none found."""
    for port in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except socket.error:
            continue
    return 0


def perform_dcr(
    registration_endpoint: str,
    redirect_uri: str,
    server_name: str,
) -> Optional[str]:
    """Perform Dynamic Client Registration at the given endpoint.

    Returns the registered client_id, or None on failure.
    """
    try:
        response = httpx.post(
            registration_endpoint,
            json={
                "client_name": f"HolmesGPT ({server_name})",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            timeout=15,
        )
        if response.status_code in (200, 201):
            client_id = response.json().get("client_id")
            logger.info("CLI OAuth %s: DCR registered client_id=%s", server_name, client_id)
            return client_id
        logger.warning("CLI OAuth %s: DCR failed HTTP %d", server_name, response.status_code)
    except Exception:
        logger.warning("CLI OAuth %s: DCR request failed", server_name, exc_info=True)
    return None


def wait_for_oauth_callback(port: int, timeout: int = 300) -> Dict[str, Any]:
    """Start a local HTTP server and wait for an OAuth callback.

    Returns a dict with 'code' on success, or 'error'/'error_description' on failure.
    Empty dict on timeout.
    """
    result: Dict[str, Any] = {}
    callback_event = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                result["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Authenticated! You can close this tab.</h1>")
            else:
                result["error"] = params.get("error", ["unknown"])[0]
                result["error_description"] = params.get("error_description", [""])[0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<h1>Error: {result['error']}</h1>".encode())
            callback_event.set()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        callback_event.wait(timeout=timeout)
    finally:
        server.shutdown()
    return result


def build_authorization_url(
    authorization_url: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: Optional[List[str]] = None,
) -> str:
    """Build the full authorization URL with PKCE and scope parameters."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    return f"{authorization_url}?{urlencode(params)}"


def cli_oauth_flow(oauth: OAuthEndpoints, server_name: str) -> Optional[Dict[str, Any]]:
    """Run OAuth authorization_code flow via local browser + callback server.

    Returns the token data dict or None on failure.
    """
    if not oauth.authorization_url or not oauth.token_url:
        logger.warning("CLI OAuth %s: missing authorization_url or token_url", server_name)
        return None

    if not oauth.client_id and not oauth.registration_endpoint:
        logger.warning("CLI OAuth %s: no client_id and no registration_endpoint", server_name)
        return None

    callback_port = find_available_port()
    if callback_port == 0:
        logger.warning("CLI OAuth %s: could not find available port for callback server", server_name)
        return None

    redirect_uri = f"http://127.0.0.1:{callback_port}/callback"

    # Perform DCR if needed (now that we know the redirect_uri)
    if oauth.registration_endpoint:
        dcr_client_id = perform_dcr(oauth.registration_endpoint, redirect_uri, server_name)
        if dcr_client_id:
            oauth.client_id = dcr_client_id
        elif not oauth.client_id:
            return None

    if not oauth.client_id:
        logger.warning("CLI OAuth %s: no client_id after DCR attempt", server_name)
        return None

    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    auth_url = build_authorization_url(
        oauth.authorization_url, oauth.client_id, redirect_uri, code_challenge, state, oauth.scopes,
    )

    logger.info("CLI OAuth %s: opening browser for authentication", server_name)
    print(f"\nOpening browser for OAuth authentication to {server_name}...")
    print(f"If browser doesn't open, visit: {auth_url}\n")
    webbrowser.open(auth_url)

    result = wait_for_oauth_callback(callback_port)

    if "error" in result:
        logger.warning("CLI OAuth %s: OAuth error: %s - %s", server_name, result["error"], result.get("error_description", ""))
        return None
    if "code" not in result:
        logger.warning("CLI OAuth %s: no auth code received (timeout?)", server_name)
        return None

    try:
        token_data = exchange_code_for_tokens(
            token_url=oauth.token_url,
            code=result["code"],
            redirect_uri=redirect_uri,
            client_id=oauth.client_id,
            code_verifier=code_verifier,
        )
    except OAuthTokenExchangeError as e:
        logger.warning("CLI OAuth %s: token exchange failed: %s", server_name, e)
        return None

    if "expires_in" in token_data and "expires_at" not in token_data:
        token_data["expires_at"] = time.time() + token_data["expires_in"]

    logger.info("CLI OAuth %s: authentication successful", server_name)
    return token_data


# ── OAuth discovery ───────────────────────────────────────────────────────


def discover_auth_server_from_prm(
    initial_response: httpx.Response,
    mcp_url: str,
    verify_ssl: bool,
    server_name: str,
) -> Tuple[Optional[str], Optional[List[str]]]:
    """Try Protected Resource Metadata (RFC 9728) to find the authorization server URL.

    Uses the MCP SDK's URL builder for discovery order.
    Returns (auth_server_url, scopes_supported) — either may be None.
    """
    www_auth_url = extract_resource_metadata_from_www_auth(initial_response)
    prm_urls = build_protected_resource_metadata_discovery_urls(www_auth_url, mcp_url)

    for prm_url in prm_urls:
        try:
            resp = httpx.get(prm_url, timeout=10, verify=verify_ssl)
            if resp.status_code != 200:
                continue
            prm = resp.json()
            auth_servers = prm.get("authorization_servers", [])
            if auth_servers:
                scopes = prm.get("scopes_supported")
                logging.info("OAuth discovery %s: found auth server via PRM %s: %s", server_name, prm_url, auth_servers[0])
                return str(auth_servers[0]).rstrip("/"), scopes
        except Exception:
            continue
    return None, None


def fetch_oauth_metadata(
    auth_server_url: Optional[str],
    mcp_url: str,
    verify_ssl: bool,
    server_name: str,
) -> Optional[Dict[str, Any]]:
    """Fetch OAuth/OIDC metadata from the auth server or legacy fallback.

    Uses the MCP SDK's URL builder for discovery order.
    Returns the metadata dict, or None if all attempts fail.
    """
    discovery_urls = build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, mcp_url)

    for url in discovery_urls:
        try:
            resp = httpx.get(url, timeout=10, verify=verify_ssl)
            if resp.status_code == 200:
                logging.info("OAuth discovery %s: fetched metadata from %s", server_name, url)
                return resp.json()
        except Exception:
            continue

    logging.warning("OAuth discovery %s: all metadata discovery attempts failed", server_name)
    return None
