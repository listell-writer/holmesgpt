"""
OAuth redirect and callback handlers for the MCP SDK's OAuthClientProvider.

The OAuthClientProvider needs two async callables:
- redirect_handler(auth_url): Open the browser for user to authenticate
- callback_handler() -> (auth_code, state): Wait for the OAuth callback

For CLI mode, we open the system browser and start a temporary localhost
HTTP server to receive the OAuth redirect (similar to `gh auth login`).

For server (AG-UI) mode, the frontend handles the browser flow. Holmes
exposes an endpoint for the frontend to POST back the auth code.
"""

import asyncio
import logging
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find an available port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CLIOAuthCallbackHandler:
    """Handles the OAuth browser flow for CLI mode.

    Usage with OAuthClientProvider:
        handler = CLIOAuthCallbackHandler()
        provider = OAuthClientProvider(
            ...
            redirect_handler=handler.redirect,
            callback_handler=handler.callback,
        )
    """

    def __init__(self, timeout: float = 300.0) -> None:
        self._timeout = timeout
        self._port: Optional[int] = None
        self._auth_code: Optional[str] = None
        self._state: Optional[str] = None
        self._event = asyncio.Event()
        self._server: Optional[HTTPServer] = None

    @property
    def redirect_uri(self) -> str:
        if self._port is None:
            self._port = _find_free_port()
        return f"http://localhost:{self._port}/callback"

    async def redirect(self, authorization_url: str) -> None:
        """Open the authorization URL in the user's browser."""
        logger.info(f"Opening browser for OAuth authentication...")
        print(f"\n{'='*60}")
        print("Authentication required by MCP server.")
        print("Opening your browser to complete sign-in...")
        print(f"{'='*60}")
        print(f"\nIf the browser doesn't open, visit:\n{authorization_url}\n")

        # Start the callback server before opening the browser
        self._start_callback_server()

        # Open browser
        try:
            webbrowser.open(authorization_url)
        except Exception:
            logger.warning("Could not open browser automatically")

    async def callback(self) -> Tuple[str, Optional[str]]:
        """Wait for the OAuth callback with auth code and state."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"OAuth callback not received within {self._timeout}s. "
                "Please try again."
            )
        finally:
            self._stop_callback_server()

        if not self._auth_code:
            raise ValueError("No authorization code received in callback")

        return (self._auth_code, self._state)

    def _start_callback_server(self) -> None:
        """Start a temporary HTTP server to receive the OAuth callback."""
        handler_ref = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)

                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                error = params.get("error", [None])[0]

                if error:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    error_desc = params.get("error_description", [error])[0]
                    self.wfile.write(
                        f"<html><body><h2>Authentication failed</h2>"
                        f"<p>{error_desc}</p>"
                        f"<p>You can close this tab.</p></body></html>".encode()
                    )
                elif code:
                    handler_ref._auth_code = code
                    handler_ref._state = state
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h2>Authentication successful!</h2>"
                        b"<p>You can close this tab and return to Holmes.</p>"
                        b"</body></html>"
                    )
                else:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h2>Missing authorization code</h2></body></html>"
                    )

                # Signal the waiting coroutine
                handler_ref._event.set()

            def log_message(self, format: str, *args: object) -> None:
                # Suppress default HTTP server logging
                pass

        if self._port is None:
            self._port = _find_free_port()

        self._server = HTTPServer(("127.0.0.1", self._port), CallbackHandler)
        thread = Thread(target=self._server.handle_request, daemon=True)
        thread.start()

    def _stop_callback_server(self) -> None:
        if self._server:
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
