"""
OAuth-protected MCP server using FastMCP with Keycloak token introspection.

Serves two simple tools (add_numbers, multiply_numbers) behind OAuth 2.1 auth.
Validates tokens via Keycloak's introspection endpoint.

Environment variables:
  KEYCLOAK_URL     - Keycloak base URL (default: http://keycloak:8080)
  KEYCLOAK_REALM   - Keycloak realm name (default: mcp-oauth)
  MCP_CLIENT_ID    - Client ID for token introspection (default: mcp-server)
  MCP_CLIENT_SECRET - Client secret for introspection (default: mcp-server-secret)
  MCP_HOST         - Server bind host (default: 0.0.0.0)
  MCP_PORT         - Server bind port (default: 8000)
"""

import datetime
import logging
import os
from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import AnyHttpUrl

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp.server import FastMCP
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

logger = logging.getLogger(__name__)

# Configuration from environment
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://keycloak:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "mcp-oauth")
MCP_CLIENT_ID = os.getenv("MCP_CLIENT_ID", "mcp-server")
MCP_CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET", "mcp-server-secret")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", f"http://localhost:{MCP_PORT}")

AUTH_BASE_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/"
SERVER_URL = MCP_SERVER_URL


def create_oauth_urls() -> dict[str, str]:
    """Create Keycloak OIDC endpoint URLs."""
    return {
        "issuer": AUTH_BASE_URL,
        "introspection_endpoint": urljoin(AUTH_BASE_URL, "protocol/openid-connect/token/introspect"),
        "authorization_endpoint": urljoin(AUTH_BASE_URL, "protocol/openid-connect/auth"),
        "token_endpoint": urljoin(AUTH_BASE_URL, "protocol/openid-connect/token"),
    }


class KeycloakTokenVerifier(TokenVerifier):
    """Validates tokens via Keycloak's OAuth 2.0 Token Introspection (RFC 7662)."""

    def __init__(
        self,
        introspection_endpoint: str,
        server_url: str,
        client_id: str,
        client_secret: str,
    ):
        self.introspection_endpoint = introspection_endpoint
        self.server_url = server_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.resource_url = resource_url_from_server_url(server_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify token via Keycloak introspection endpoint."""
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    self.introspection_endpoint,
                    data={
                        "token": token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

                if response.status_code != 200:
                    logger.warning("Introspection returned %d", response.status_code)
                    return None

                data = response.json()
                if not data.get("active", False):
                    logger.info("Token is inactive")
                    return None

                if not self._validate_audience(data):
                    logger.warning("Token audience mismatch")
                    return None

                return AccessToken(
                    token=token,
                    client_id=data.get("client_id", "unknown"),
                    scopes=data.get("scope", "").split() if data.get("scope") else [],
                    expires_at=data.get("exp"),
                    resource=data.get("aud"),
                )

            except Exception:
                logger.exception("Token introspection failed")
                return None

    def _validate_audience(self, token_data: dict[str, Any]) -> bool:
        """Validate the token was issued for this resource server."""
        aud = token_data.get("aud")
        if aud is None:
            return False

        audiences = aud if isinstance(aud, list) else [aud]
        return any(
            check_resource_allowed(self.resource_url, a) for a in audiences
        )


def create_server() -> FastMCP:
    """Create and configure the OAuth-protected MCP server."""
    oauth_urls = create_oauth_urls()

    token_verifier = KeycloakTokenVerifier(
        introspection_endpoint=oauth_urls["introspection_endpoint"],
        server_url=SERVER_URL,
        client_id=MCP_CLIENT_ID,
        client_secret=MCP_CLIENT_SECRET,
    )

    app = FastMCP(
        name="MCP OAuth Demo Server",
        instructions="OAuth-protected MCP server with Keycloak authorization",
        host=MCP_HOST,
        port=MCP_PORT,
        debug=True,
        streamable_http_path="/",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(oauth_urls["issuer"]),
            required_scopes=["mcp:tools"],
            resource_server_url=AnyHttpUrl(SERVER_URL),
        ),
    )

    @app.tool()
    async def add_numbers(a: float, b: float) -> dict[str, Any]:
        """Add two numbers together.

        Args:
            a: The first number to add
            b: The second number to add
        """
        return {
            "operation": "addition",
            "a": a,
            "b": b,
            "result": a + b,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    @app.tool()
    async def multiply_numbers(x: float, y: float) -> dict[str, Any]:
        """Multiply two numbers together.

        Args:
            x: The first number to multiply
            y: The second number to multiply
        """
        return {
            "operation": "multiplication",
            "x": x,
            "y": y,
            "result": x * y,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    return app


def main() -> int:
    """Run the MCP OAuth server."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    oauth_urls = create_oauth_urls()
    logger.info("Starting MCP OAuth Server on %s:%s", MCP_HOST, MCP_PORT)
    logger.info("Keycloak issuer: %s", oauth_urls["issuer"])

    server = create_server()
    server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
