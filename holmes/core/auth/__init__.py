"""
MCP OAuth 2.1 authentication and elicitation support for HolmesGPT.

Implements the client-side of the MCP Authorization spec (2025-06-18),
providing automatic OAuth flows for remote MCP servers that require
per-user authentication.

The MCP Python SDK's OAuthClientProvider handles the full OAuth flow
(discovery, PKCE, dynamic client registration, token refresh). This
module provides the pluggable pieces Holmes needs:

- TokenStorage: File-based storage for OAuth tokens and client info
- OAuth handlers: Browser redirect + localhost callback for CLI mode
- Elicitation handler: Terminal prompts for MCP server requests
"""

from holmes.core.auth.elicitation import cli_elicitation_callback
from holmes.core.auth.oauth_handlers import CLIOAuthCallbackHandler
from holmes.core.auth.token_storage import FileTokenStorage

__all__ = [
    "CLIOAuthCallbackHandler",
    "FileTokenStorage",
    "cli_elicitation_callback",
]
