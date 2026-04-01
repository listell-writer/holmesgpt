import asyncio
import base64
import hashlib
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, TextIO, Tuple, Type, Union

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool as MCP_Tool
from pydantic import AnyUrl, BaseModel, Field, model_validator

from holmes.common.env_vars import SSE_READ_TIMEOUT
from holmes.core.config import config_path_dir
from holmes.core.tools import (
    ApprovalRequirement,
    CallablePrerequisite,
    StructuredToolResult,
    StructuredToolResultStatus,
    Tool,
    ToolInvokeContext,
    ToolParameter,
    Toolset,
)
from holmes.utils.header_rendering import render_header_templates
from holmes.utils.pydantic_utils import ToolsetConfig

logger = logging.getLogger(__name__)
display_logger = logging.getLogger("holmes.display.mcp_toolset")


def _extract_root_error_message(exc: Exception) -> str:
    """Extract the actual error message from an ExceptionGroup.

    When the MCP library's internal asyncio.TaskGroup encounters errors (e.g. auth
    failures, connection refused), the real exception gets wrapped in an
    ExceptionGroup with the unhelpful message "unhandled errors in a TaskGroup
    (1 sub-exception)".  This function unwraps the group to surface the actual
    root-cause error so that users see, for example, "401 Unauthorized" instead.
    """
    current: BaseException = exc
    while hasattr(current, "exceptions") and current.exceptions:
        current = current.exceptions[0]
    return str(current)


# Lock per MCP server URL to serialize calls to the same server
_server_locks: Dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def create_mcp_http_client_factory(verify_ssl: bool = True):
    """Create a factory function for httpx clients with configurable SSL verification."""

    def factory(
        headers: Dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        kwargs: Dict[str, Any] = {
            "follow_redirects": True,
            "verify": verify_ssl,
        }
        if timeout is None:
            kwargs["timeout"] = httpx.Timeout(SSE_READ_TIMEOUT)
        else:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


def get_server_lock(url: str) -> threading.Lock:
    """Get or create a lock for a specific MCP server URL."""
    with _locks_lock:
        if url not in _server_locks:
            _server_locks[url] = threading.Lock()
        return _server_locks[url]


class MCPMode(str, Enum):
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"
    STDIO = "stdio"


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


def _get_signing_key() -> Optional[str]:
    """Load the signing_key from Robusta's global_config. Returns None in CLI mode."""
    from holmes.utils.definitions import RobustaConfig

    config_file_path = os.environ.get("RUNNER_CONFIG_PATH", "/etc/robusta/config/active_playbooks.yaml")
    if not os.path.exists(config_file_path):
        return None
    try:
        import yaml as _yaml

        with open(config_file_path) as f:
            yaml_content = _yaml.safe_load(f)
            config = RobustaConfig(**yaml_content)
            return config.global_config.get("signing_key")
    except Exception:
        logger.warning("Failed to load signing_key from Robusta config", exc_info=True)
        return None


# Cached singleton — derived once from signing_key, reused for all OAuth flows
_deterministic_keypair: Optional["OAuthKeyExchange"] = None


class OAuthKeyExchange:
    """RSA keypair for secure auth code transit from frontend to Holmes.

    When a signing_key is available (in-cluster), derives a deterministic keypair
    from it — same key every time, survives Holmes restarts. In CLI mode (no
    signing_key), generates a random keypair per instance.

    The frontend encrypts the OAuth authorization code with the public key.
    Holmes decrypts with the private key, then exchanges the code for
    a token server-side (so the access token never leaves the cluster).
    """

    def __init__(self, signing_key: Optional[str] = None, key_store_dir: Optional[str] = None) -> None:
        if signing_key:
            # Server mode: persist encrypted with signing_key
            self._private_key = self._load_or_generate_persistent_key(signing_key, key_store_dir)
        else:
            # CLI mode: persist unencrypted in ~/.holmes/auth/
            self._private_key = self._load_or_generate_cli_key(key_store_dir)

    @staticmethod
    def _load_or_generate_persistent_key(signing_key: str, key_store_dir: Optional[str] = None) -> Any:
        """Load persisted RSA key or generate + persist a new one, encrypted with signing_key."""
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from pathlib import Path

        # Derive a Fernet key from signing_key for encrypting the RSA private key at rest
        fernet_key = base64.urlsafe_b64encode(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=b"holmesgpt-oauth-fernet", info=b"key-encryption")
            .derive(signing_key.encode())
        )
        fernet = Fernet(fernet_key)

        if key_store_dir:
            key_path = Path(key_store_dir) / "oauth_keypair.enc"
        else:
            key_path = Path(os.environ.get("RUNNER_CONFIG_PATH", "/etc/robusta/config")).parent / "oauth_keypair.enc"

        # Try to load existing key
        if key_path.exists():
            try:
                encrypted_pem = key_path.read_bytes()
                pem_bytes = fernet.decrypt(encrypted_pem)
                logger.warning("OAuth: loaded persisted keypair from %s", key_path)
                return serialization.load_pem_private_key(pem_bytes, password=None)
            except Exception:
                logger.warning("OAuth: failed to load persisted keypair, generating new one")

        # Generate new keypair
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Persist encrypted
        try:
            pem_bytes = private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(fernet.encrypt(pem_bytes))
            logger.warning("OAuth: generated and persisted new keypair at %s", key_path)
        except Exception:
            logger.warning("OAuth: could not persist keypair (read-only filesystem?), will regenerate on restart", exc_info=True)

        return private_key

    @staticmethod
    def _load_or_generate_cli_key(key_store_dir: Optional[str] = None) -> Any:
        """Load or generate a persisted RSA key for CLI mode.

        Encrypted at rest using a passphrase derived from machine identity
        (hostname + username). File permissions set to 600 (owner-only).
        Stored in ~/.holmes/auth/ by default.
        """
        import getpass
        import platform
        import stat
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.fernet import Fernet
        from pathlib import Path

        # Derive encryption key from machine identity (not secret, but prevents casual file copying)
        machine_id = f"{platform.node()}:{getpass.getuser()}:holmesgpt-oauth"
        fernet_key = base64.urlsafe_b64encode(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=b"holmesgpt-cli-keypair", info=b"cli-key-encryption")
            .derive(machine_id.encode())
        )
        fernet = Fernet(fernet_key)

        if key_store_dir:
            key_path = Path(key_store_dir) / "oauth_keypair.enc"
        else:
            from holmes.core.config import config_path_dir
            key_path = Path(config_path_dir) / "auth" / "oauth_keypair.enc"

        # Try to load existing key
        if key_path.exists():
            try:
                encrypted_pem = key_path.read_bytes()
                pem_bytes = fernet.decrypt(encrypted_pem)
                logger.warning("OAuth: loaded persisted CLI keypair from %s", key_path)
                return serialization.load_pem_private_key(pem_bytes, password=None)
            except Exception:
                logger.warning("OAuth: failed to load persisted CLI keypair, generating new one")

        # Generate new keypair
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Persist encrypted with restricted permissions
        try:
            pem_bytes = private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(fernet.encrypt(pem_bytes))
            key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600 — owner read/write only
            logger.warning("OAuth: generated and persisted CLI keypair at %s (mode 600)", key_path)
        except Exception:
            logger.warning("OAuth: could not persist CLI keypair, will regenerate on restart", exc_info=True)

        return private_key

    def get_public_key_pem(self) -> str:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def decrypt(self, encrypted_b64: str) -> str:
        """Decrypt a base64-encoded RSA-OAEP ciphertext."""
        ciphertext = base64.b64decode(encrypted_b64)
        plaintext = self._private_key.decrypt(
            ciphertext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return plaintext.decode()


def get_oauth_key_exchange() -> OAuthKeyExchange:
    """Get the singleton OAuthKeyExchange. Uses signing_key if available for persistence."""
    global _deterministic_keypair
    if _deterministic_keypair is None:
        signing_key = _get_signing_key()
        _deterministic_keypair = OAuthKeyExchange(signing_key=signing_key)
        if signing_key:
            logger.warning("OAuth: initialized deterministic keypair from signing_key (persistent across restarts)")
        else:
            logger.warning("OAuth: initialized random keypair (CLI mode, not persistent)")
    return _deterministic_keypair


def _generate_pkce() -> Tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Returns (code_verifier, code_challenge).
    """
    import hashlib
    import secrets

    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


class _CachedToken:
    """Holds an access token, its expiry, and an optional refresh token."""

    def __init__(self, access_token: str, expires_at: float, refresh_token: Optional[str] = None, refresh_expires_at: Optional[float] = None) -> None:
        self.access_token = access_token
        self.expires_at = expires_at
        self.refresh_token = refresh_token
        self.refresh_expires_at = refresh_expires_at

    @property
    def access_expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    @property
    def refresh_expired(self) -> bool:
        if self.refresh_token is None or self.refresh_expires_at is None:
            return True
        return time.monotonic() >= self.refresh_expires_at


class OAuthTokenCache:
    """TTL cache for OAuth tokens keyed by conversation ID, with refresh token support."""

    def __init__(self) -> None:
        self._cache: Dict[str, _CachedToken] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        """Return a valid access token, or None if expired and not refreshable."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if not entry.access_expired:
                return entry.access_token
            # Access token expired — caller must try refresh
            if not entry.refresh_expired:
                return None  # Has refresh token but access expired — caller should refresh
            # Both expired
            del self._cache[key]
            return None

    def get_refresh_token(self, key: str) -> Optional[str]:
        """Return the refresh token if it hasn't expired, even if the access token has."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if not entry.refresh_expired:
                return entry.refresh_token
            return None

    def set(self, key: str, access_token: str, expires_in: int = 300, refresh_token: Optional[str] = None, refresh_expires_in: Optional[int] = None) -> None:
        now = time.monotonic()
        # Subtract a small buffer so we refresh before actual expiry
        access_expires_at = now + max(expires_in - 30, 10)
        refresh_expires_at = None
        if refresh_token:
            # Default to 24 hours if IdP doesn't return refresh_expires_in (not all do)
            refresh_ttl = refresh_expires_in if refresh_expires_in else 86400
            refresh_expires_at = now + max(refresh_ttl - 30, 10)
        with self._lock:
            self._cache[key] = _CachedToken(access_token, access_expires_at, refresh_token, refresh_expires_at)

    def has(self, key: str) -> bool:
        """True if there is a valid access token OR a valid refresh token."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return False
            if not entry.access_expired:
                return True
            if not entry.refresh_expired:
                return True
            del self._cache[key]
            return False


class DiskTokenStore:
    """Persists OAuth tokens to ~/.holmes/auth/mcp_tokens.json for CLI usage."""

    def __init__(self) -> None:
        from holmes.core.config import config_path_dir
        from pathlib import Path

        self._path = Path(config_path_dir) / "auth" / "mcp_tokens.json"
        self._enabled = True
        self._lock = threading.Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Read-only filesystem (e.g. in-cluster container) — disk store disabled
            self._enabled = False
            logger.info("OAuth disk token store disabled (read-only filesystem)")

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self._enabled:
            return None
        with self._lock:
            data = self._load()
            token = data.get(key)
            if token and token.get("expires_at", float("inf")) > time.time():
                return token
            return None

    def set(self, key: str, token_data: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        with self._lock:
            data = self._load()
            data[key] = token_data
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path) as f:
                return json.load(f)
        except Exception:
            return {}


_disk_token_store = DiskTokenStore()


def _cli_oauth_flow(oauth_config: MCPOAuthConfig, server_name: str) -> Optional[Dict[str, Any]]:
    """Run OAuth authorization_code flow via local browser + callback server.

    Opens the user's browser to the IdP login page, starts a local HTTP
    server to receive the callback, exchanges the auth code for a token.
    Returns the token data dict or None on failure.
    """
    import secrets
    import socket
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlencode, urlparse

    if not oauth_config.authorization_url or not oauth_config.token_url:
        logger.warning("CLI OAuth %s: missing authorization_url or token_url", server_name)
        return None

    # DCR if no client_id (e.g. Atlassian auto-discovery)
    if not oauth_config.client_id and oauth_config.registration_endpoint:
        logger.warning("CLI OAuth %s: no client_id, performing DCR at %s", server_name, oauth_config.registration_endpoint)
        # We don't know the callback port yet — register with a placeholder, re-register after starting the server
    elif not oauth_config.client_id:
        logger.warning("CLI OAuth %s: no client_id and no registration_endpoint", server_name)
        return None

    # Generate PKCE
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    # Start callback server on an available port
    callback_server = None
    callback_port = 0
    for port in range(18900, 18920):
        try:
            callback_server = HTTPServer(("127.0.0.1", port), type("H", (BaseHTTPRequestHandler,), {"do_GET": lambda s: None, "log_message": lambda s, *a: None}))
            callback_port = port
            callback_server.server_close()
            break
        except socket.error:
            continue

    if callback_port == 0:
        logger.warning("CLI OAuth %s: could not find available port for callback server", server_name)
        return None

    redirect_uri = f"http://127.0.0.1:{callback_port}/callback"

    # Register or re-register via DCR with the actual redirect_uri
    if oauth_config.registration_endpoint:
        try:
            dcr_response = httpx.post(
                oauth_config.registration_endpoint,
                json={
                    "client_name": f"HolmesGPT ({server_name})",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                timeout=15,
            )
            if dcr_response.status_code in (200, 201):
                dcr_data = dcr_response.json()
                oauth_config.client_id = dcr_data.get("client_id", oauth_config.client_id)
                logger.warning("CLI OAuth %s: DCR registered client_id=%s with redirect_uri=%s", server_name, oauth_config.client_id, redirect_uri)
            elif not oauth_config.client_id:
                logger.warning("CLI OAuth %s: DCR failed HTTP %d and no client_id available", server_name, dcr_response.status_code)
                return None
        except Exception:
            if not oauth_config.client_id:
                logger.warning("CLI OAuth %s: DCR failed and no client_id available", server_name, exc_info=True)
                return None
            logger.warning("CLI OAuth %s: DCR re-registration failed, using existing client_id", server_name, exc_info=True)

    if not oauth_config.client_id:
        logger.warning("CLI OAuth %s: no client_id after DCR attempt", server_name)
        return None

    # Build authorization URL
    auth_params = {
        "response_type": "code",
        "client_id": oauth_config.client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if oauth_config.scopes:
        auth_params["scope"] = " ".join(oauth_config.scopes)

    auth_url = f"{oauth_config.authorization_url}?{urlencode(auth_params)}"

    # Set up callback handler
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

    server = HTTPServer(("127.0.0.1", callback_port), CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        logger.warning("CLI OAuth %s: opening browser for authentication", server_name)
        print(f"\nOpening browser for OAuth authentication to {server_name}...")
        print(f"If browser doesn't open, visit: {auth_url}\n")
        webbrowser.open(auth_url)

        logger.warning("CLI OAuth %s: waiting for callback on port %d", server_name, callback_port)
        callback_event.wait(timeout=300)

        if "error" in result:
            logger.warning("CLI OAuth %s: OAuth error: %s - %s", server_name, result["error"], result.get("error_description", ""))
            return None

        if "code" not in result:
            logger.warning("CLI OAuth %s: no auth code received (timeout?)", server_name)
            return None

        # Exchange code for token
        token_response = httpx.post(
            oauth_config.token_url,
            data={
                "grant_type": "authorization_code",
                "code": result["code"],
                "client_id": oauth_config.client_id,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if token_response.status_code != 200:
            logger.warning("CLI OAuth %s: token exchange failed HTTP %d: %s", server_name, token_response.status_code, token_response.text[:300])
            return None

        token_data = token_response.json()
        if "access_token" not in token_data:
            logger.warning("CLI OAuth %s: no access_token in response", server_name)
            return None

        # Add expires_at for disk storage
        if "expires_in" in token_data and "expires_at" not in token_data:
            token_data["expires_at"] = time.time() + token_data["expires_in"]

        logger.warning("CLI OAuth %s: authentication successful", server_name)
        return token_data

    finally:
        server.shutdown()


class _PendingOAuthExchange:
    """State for a pending OAuth approval: key exchange, PKCE verifier, and config."""

    def __init__(self, key_exchange: OAuthKeyExchange, code_verifier: str, oauth_config: MCPOAuthConfig, redirect_uri: str) -> None:
        self.key_exchange = key_exchange
        self.code_verifier = code_verifier
        self.oauth_config = oauth_config
        self.redirect_uri = redirect_uri


# Global caches
_oauth_token_cache = OAuthTokenCache()
_pending_exchanges: Dict[str, _PendingOAuthExchange] = {}
_exchanges_lock = threading.Lock()

# Module-level DAL reference for OAuth DB operations. Set via set_oauth_dal() during server startup.
_oauth_dal: Optional[Any] = None


def set_oauth_dal(dal: Any) -> None:
    """Set the DAL instance for OAuth DB operations. Called during server startup."""
    global _oauth_dal
    _oauth_dal = dal
    if dal and dal.enabled:
        logger.warning("OAuth: DAL initialized for cross-cluster token storage")


def _get_signing_key_hash() -> Optional[str]:
    """Get a SHA-256 hash of the signing_key for DB storage (never store the key itself)."""
    signing_key = _get_signing_key()
    if not signing_key:
        return None
    return hashlib.sha256(signing_key.encode()).hexdigest()


def _encrypt_token_for_db(token_data: Dict[str, Any], signing_key: str) -> str:
    """Encrypt token data with signing_key-derived Fernet key for DB storage."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    fernet_key = base64.urlsafe_b64encode(
        HKDF(algorithm=hashes.SHA256(), length=32, salt=b"holmesgpt-oauth-db-token", info=b"token-encryption")
        .derive(signing_key.encode())
    )
    return Fernet(fernet_key).encrypt(json.dumps(token_data).encode()).decode()


def _decrypt_token_from_db(encrypted: str, signing_key: str) -> Optional[Dict[str, Any]]:
    """Decrypt token data from DB using signing_key-derived Fernet key."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    try:
        fernet_key = base64.urlsafe_b64encode(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=b"holmesgpt-oauth-db-token", info=b"token-encryption")
            .derive(signing_key.encode())
        )
        decrypted = Fernet(fernet_key).decrypt(encrypted.encode())
        return json.loads(decrypted)
    except Exception:
        logger.warning("OAuth: failed to decrypt token from DB (signing_key mismatch?)")
        return None


def _store_token_to_db(oauth_config: MCPOAuthConfig, token_data: Dict[str, Any], context_id: str) -> None:
    """Store an OAuth token to the DB for cross-cluster reuse, encrypted with signing_key."""
    signing_key = _get_signing_key()
    signing_key_hash = _get_signing_key_hash()
    if not _oauth_dal or not _oauth_dal.enabled or not signing_key or not signing_key_hash:
        return

    try:
        # Use authorization_url as provider name since it uniquely identifies the IdP
        provider_name = oauth_config.authorization_url or "unknown"
        encrypted = _encrypt_token_for_db(token_data, signing_key)
        # Store refresh token expiry (what matters for cross-cluster reuse).
        # Fall back to access token expiry if no refresh_expires_in.
        from datetime import datetime, timezone, timedelta
        expiry = None
        if token_data.get("refresh_expires_in"):
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=token_data["refresh_expires_in"])).isoformat()
        elif token_data.get("expires_in"):
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])).isoformat()

        _oauth_dal.upsert_oauth_token(
            provider_name=provider_name,
            encrypted_token=encrypted,
            signing_key_hash=signing_key_hash,
            token_expiry=expiry,
        )
        logger.warning("OAuth: stored token to DB for provider %s", provider_name)
    except Exception:
        logger.warning("OAuth: failed to store token to DB", exc_info=True)


def _get_conversation_key(request_context: Optional[Dict[str, Any]]) -> str:
    """Extract a conversation key from request context headers."""
    if request_context:
        headers = request_context.get("headers", {})
        for key in ("X-Conversation-Id", "x-conversation-id", "X-Session-Id", "x-session-id"):
            if key in headers:
                return str(headers[key])
    return "__default__"


def _get_oauth_cache_key(oauth_config: MCPOAuthConfig, request_context: Optional[Dict[str, Any]]) -> str:
    """Build a cache key from IdP identity + conversation. All MCP servers sharing the same IdP share the same token."""
    conv_key = _get_conversation_key(request_context)
    idp_key = hashlib.sha256(f"{oauth_config.authorization_url}:{oauth_config.client_id}".encode()).hexdigest()[:12]
    return f"{conv_key}:{idp_key}"


class MCPConfig(ToolsetConfig):
    mode: MCPMode = Field(
        default=MCPMode.SSE,
        title="Mode",
        description="Connection mode to use when talking to the MCP server.",
        examples=[MCPMode.STREAMABLE_HTTP],
    )
    url: AnyUrl = Field(
        title="URL",
        description="MCP server URL (for SSE or Streamable HTTP modes).",
        examples=["http://example.com:8000/mcp/messages"],
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None,
        title="Headers",
        description="Optional HTTP headers to include in requests (e.g., Authorization).",
        examples=[{"Authorization": "Bearer YOUR_TOKEN"}],
    )
    verify_ssl: bool = Field(
        default=True,
        title="Verify SSL",
        description="Whether to verify SSL certificates (set to false for local/dev servers without valid SSL).",
        examples=[False],
    )
    extra_headers: Optional[Dict[str, str]] = Field(
        default=None,
        title="Extra Headers",
        description="Template headers that will be rendered with request context and environment variables.",
        examples=[
            {
                "X-Custom-Header": "{{ request_context.headers['X-Custom-Header'] }}",
                "X-Api-Key": "{{ env.API_KEY }}",
            }
        ],
    )
    icon_url: str = Field(
        default="https://registry.npmmirror.com/@lobehub/icons-static-png/1.46.0/files/light/mcp.png",
        description="Icon URL for this MCP server, displayed in the UI for tool calls.",
        examples=["https://cdn.simpleicons.org/github/181717"],
    )
    oauth: Optional[MCPOAuthConfig] = Field(
        default=None,
        title="OAuth",
        description="OAuth authorization_code configuration. When set, users authenticate via browser before tools can be used.",
    )

    def get_lock_string(self) -> str:
        return str(self.url)


class StdioMCPConfig(ToolsetConfig):
    mode: MCPMode = Field(
        default=MCPMode.STDIO,
        title="Mode",
        description="Stdio mode runs an MCP server as a local subprocess.",
        examples=[MCPMode.STDIO],
    )
    command: str = Field(
        title="Command",
        description="The command to start the MCP server (e.g., npx, uv, python).",
        examples=["npx"],
    )
    args: Optional[List[str]] = Field(
        default=None,
        title="Arguments",
        description="Arguments to pass to the MCP server command.",
        examples=[["-y", "@modelcontextprotocol/server-github"]],
    )
    env: Optional[Dict[str, str]] = Field(
        default=None,
        title="Environment Variables",
        description="Environment variables to set for the MCP server process.",
        examples=[{"GITHUB_PERSONAL_ACCESS_TOKEN": "{{ env.GITHUB_TOKEN }}"}],
    )
    icon_url: str = Field(
        default="https://registry.npmmirror.com/@lobehub/icons-static-png/1.46.0/files/light/mcp.png",
        description="Icon URL for this MCP server, displayed in the UI for tool calls.",
        examples=["https://cdn.simpleicons.org/github/181717"],
    )

    def get_lock_string(self) -> str:
        return str(self.command)


def _get_mcp_log_file(server_name: str) -> TextIO:
    """Get a file handle for MCP server stderr output.

    Redirects MCP subprocess stderr to ~/.holmes/logs/mcp/<server_name>.log
    so it doesn't pollute the CLI output.
    """
    log_dir = os.path.join(config_path_dir, "logs", "mcp")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{server_name}.log")
    display_logger.info(f"MCP server '{server_name}' logs: {log_path}")
    return open(log_path, "w")


def _inject_oauth_token(
    toolset: "RemoteMCPToolset",
    request_context: Optional[Dict[str, Any]],
    headers: Optional[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    """Inject cached OAuth Bearer token into headers if available."""
    if not isinstance(toolset._mcp_config, MCPConfig) or not toolset._mcp_config.oauth or not toolset._mcp_config.oauth.enabled:
        return headers

    oauth_config = toolset._mcp_config.oauth
    cache_key = _get_oauth_cache_key(oauth_config, request_context)
    cached_token = _oauth_token_cache.get(cache_key)
    if not cached_token:
        # Access token expired or missing — try refresh before giving up
        cached_token = _try_refresh_token(cache_key, oauth_config)
        if cached_token:
            logger.warning("OAuth token refreshed for MCP server %s (idp=%s)", toolset.name, oauth_config.authorization_url)
    if cached_token:
        headers = headers or {}
        headers["Authorization"] = f"Bearer {cached_token}"
        logger.warning("OAuth token injected for MCP server %s (cache_key=%s)", toolset.name, cache_key)
    else:
        logger.warning("OAuth MCP server %s: no cached token (cache_key=%s) — request will likely 401", toolset.name, cache_key)
    return headers


def decrypt_code_and_exchange_for_token(tool_call_id: str, encrypted_payload: str, request_context: Optional[Dict[str, Any]]) -> None:
    """Decrypt an OAuth authorization code and exchange it for an access token.

    The frontend encrypts a JSON payload: {"code": "...", "redirect_uri": "..."}.
    Holmes decrypts it, then exchanges the code at the IdP's token_url using the
    PKCE code_verifier (generated during requires_approval). The access token
    stays server-side and never transits through the frontend.

    Called from tool_calling_llm._execute_tool_decisions() when a decision
    includes an encrypted_token from the frontend OAuth flow.
    """
    with _exchanges_lock:
        pending = _pending_exchanges.pop(tool_call_id, None)

    if pending is None:
        logger.error("OAuth exchange failed: no pending key exchange for tool_call_id=%s (possible timeout or duplicate)", tool_call_id)
        return

    try:
        # Decrypt the payload from frontend
        logger.warning("OAuth: decrypting auth code payload for tool_call_id=%s", tool_call_id)
        decrypted = pending.key_exchange.decrypt(encrypted_payload)
        payload = json.loads(decrypted)
        auth_code = payload["code"]
        redirect_uri = payload.get("redirect_uri", "")
        # Frontend may include client_id from DCR (when Holmes didn't have one at discovery time)
        client_id = payload.get("client_id") or pending.oauth_config.client_id
        if client_id and not pending.oauth_config.client_id:
            pending.oauth_config.client_id = client_id
            logger.warning("OAuth: using client_id from frontend DCR: %s", client_id)
        logger.warning("OAuth: auth code decrypted, exchanging at token endpoint %s (client_id=%s)", pending.oauth_config.token_url, client_id)

        # Exchange auth code for access token at the IdP's token endpoint (server-side)
        token_response = httpx.post(
            pending.oauth_config.token_url,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": client_id,
                "code_verifier": pending.code_verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if token_response.status_code != 200:
            logger.error(
                "OAuth token exchange failed: HTTP %d from %s — response: %s",
                token_response.status_code, pending.oauth_config.token_url, token_response.text[:500],
            )
            token_response.raise_for_status()

        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error("OAuth token exchange: response missing 'access_token' field. Keys: %s", list(token_data.keys()))
            return

        cache_key = _get_oauth_cache_key(pending.oauth_config, request_context)
        _oauth_token_cache.set(
            cache_key,
            access_token,
            expires_in=token_data.get("expires_in", 300),
            refresh_token=token_data.get("refresh_token"),
            refresh_expires_in=token_data.get("refresh_expires_in"),
        )
        logger.warning(
            "OAuth token cached (cache_key=%s, idp=%s, expires_in=%s, has_refresh=%s)",
            cache_key, pending.oauth_config.token_url, token_data.get("expires_in"), "refresh_token" in token_data,
        )

        # Store to DB for cross-cluster reuse
        _store_token_to_db(pending.oauth_config, token_data, tool_call_id)
    except json.JSONDecodeError:
        logger.exception("OAuth token exchange: failed to parse JSON response from %s", pending.oauth_config.token_url)
    except httpx.HTTPStatusError:
        pass  # Already logged above
    except Exception:
        logger.exception("OAuth token exchange failed (tool_call_id=%s, token_url=%s)", tool_call_id, pending.oauth_config.token_url)


def _try_refresh_token(cache_key: str, oauth_config: MCPOAuthConfig) -> Optional[str]:
    """Attempt to refresh an expired access token using the cached refresh token. Returns new access token or None."""
    refresh_token = _oauth_token_cache.get_refresh_token(cache_key)
    if not refresh_token:
        return None

    try:
        logger.warning("OAuth: attempting token refresh at %s (cache_key=%s)", oauth_config.token_url, cache_key)
        response = httpx.post(
            oauth_config.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": oauth_config.client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if response.status_code != 200:
            logger.warning("OAuth refresh failed: HTTP %d from %s (cache_key=%s)", response.status_code, oauth_config.token_url, cache_key)
            return None

        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return None

        _oauth_token_cache.set(
            cache_key,
            access_token,
            expires_in=token_data.get("expires_in", 300),
            refresh_token=token_data.get("refresh_token", refresh_token),
            refresh_expires_in=token_data.get("refresh_expires_in"),
        )
        logger.warning("OAuth token refreshed (cache_key=%s, expires_in=%s)", cache_key, token_data.get("expires_in"))
        # Update DB with refreshed token
        _store_token_to_db(oauth_config, token_data, "refresh")
        return access_token
    except Exception:
        logger.warning("OAuth refresh failed (cache_key=%s)", cache_key, exc_info=True)
        return None


@asynccontextmanager
async def get_initialized_mcp_session(
    toolset: "RemoteMCPToolset", request_context: Optional[Dict[str, Any]] = None
):
    if toolset._mcp_config is None:
        raise ValueError("MCP config is not initialized")

    if isinstance(toolset._mcp_config, StdioMCPConfig):
        server_params = StdioServerParameters(
            command=toolset._mcp_config.command,
            args=toolset._mcp_config.args or [],
            env=toolset._mcp_config.env,
        )
        errlog = _get_mcp_log_file(toolset.name)
        try:
            async with stdio_client(server_params, errlog=errlog) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    _ = await session.initialize()
                    yield session
        finally:
            errlog.close()
    elif toolset._mcp_config.mode == MCPMode.SSE:
        url = str(toolset._mcp_config.url)
        httpx_factory = create_mcp_http_client_factory(toolset._mcp_config.verify_ssl)
        rendered_headers = _inject_oauth_token(toolset, request_context, toolset._render_headers(request_context))
        async with sse_client(
            url,
            rendered_headers,
            sse_read_timeout=SSE_READ_TIMEOUT,
            httpx_client_factory=httpx_factory,
        ) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                _ = await session.initialize()
                yield session
    else:
        url = str(toolset._mcp_config.url)
        httpx_factory = create_mcp_http_client_factory(toolset._mcp_config.verify_ssl)
        rendered_headers = _inject_oauth_token(toolset, request_context, toolset._render_headers(request_context))
        async with streamablehttp_client(
            url,
            headers=rendered_headers,
            sse_read_timeout=SSE_READ_TIMEOUT,
            httpx_client_factory=httpx_factory,
        ) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                _ = await session.initialize()
                yield session


class RemoteMCPTool(Tool):
    toolset: "RemoteMCPToolset" = Field(exclude=True)

    def requires_approval(
        self, params: Dict, context: ToolInvokeContext
    ) -> Optional[ApprovalRequirement]:
        """Prompt user for OAuth browser login when no cached token exists."""
        if not isinstance(self.toolset._mcp_config, MCPConfig) or not self.toolset._mcp_config.oauth or not self.toolset._mcp_config.oauth.enabled:
            return None

        oauth_config = self.toolset._mcp_config.oauth
        cache_key = _get_oauth_cache_key(oauth_config, context.request_context)

        if _oauth_token_cache.has(cache_key):
            # Try refresh if access token expired but refresh token is still valid
            if _oauth_token_cache.get(cache_key) is None:
                refreshed = _try_refresh_token(cache_key, oauth_config)
                if refreshed:
                    logger.warning("OAuth token refreshed for MCP %s, skipping approval (cache_key=%s)", self.toolset.name, cache_key)
                    return None
            else:
                logger.warning("OAuth token reused for MCP %s from shared IdP cache (cache_key=%s)", self.toolset.name, cache_key)
                return None

        logger.warning("OAuth MCP %s: no cached token (cache_key=%s), checking DB and disk", self.toolset.name, cache_key)

        # Check DB for cross-cluster token (server mode with DAL)
        signing_key = _get_signing_key()
        if _oauth_dal and _oauth_dal.enabled and signing_key:
            signing_key_hash = _get_signing_key_hash()
            # provider_name in DB is the authorization_url (uniquely identifies the IdP)
            db_provider = oauth_config.authorization_url or self.toolset.name
            db_record = _oauth_dal.get_oauth_token(db_provider)
            if not db_record:
                logger.warning("OAuth MCP %s: no DB token found for provider=%s", self.toolset.name, db_provider)
            if db_record:
                if db_record.get("signing_key_hash") == signing_key_hash:
                    db_token_data = _decrypt_token_from_db(db_record["encrypted_token"], signing_key)
                    if db_token_data and db_token_data.get("access_token"):
                        _oauth_token_cache.set(
                            cache_key,
                            db_token_data["access_token"],
                            expires_in=db_token_data.get("expires_in", 300),
                            refresh_token=db_token_data.get("refresh_token"),
                        )
                        logger.warning("OAuth MCP %s: loaded token from DB (cross-cluster)", self.toolset.name)
                        return None
                else:
                    logger.warning(
                        "OAuth MCP %s: found DB token but signing_key_hash mismatch (stored=%s, current=%s). "
                        "Token from a different cluster/config — will prompt for re-authentication.",
                        self.toolset.name, db_record.get("signing_key_hash", "")[:12], (signing_key_hash or "")[:12],
                    )

        # Check disk store for CLI-persisted tokens
        disk_key = str(self.toolset._mcp_config.url) if isinstance(self.toolset._mcp_config, MCPConfig) else cache_key
        disk_token = _disk_token_store.get(disk_key)
        if disk_token:
            _oauth_token_cache.set(
                cache_key,
                disk_token["access_token"],
                expires_in=int(disk_token.get("expires_at", time.time() + 300) - time.time()),
                refresh_token=disk_token.get("refresh_token"),
            )
            logger.warning("OAuth MCP %s: loaded token from disk store", self.toolset.name)
            return None

        # Detect CLI vs frontend mode: if request_context exists, the request came
        # through the API server (frontend). CLI calls have request_context=None.
        is_frontend = context.request_context is not None

        if not is_frontend:
            # CLI mode: run browser OAuth flow synchronously
            logger.warning("OAuth MCP %s: CLI mode detected, running browser OAuth flow", self.toolset.name)
            token_data = _cli_oauth_flow(oauth_config, self.toolset.name)
            if token_data:
                # Recompute cache key — DCR may have changed client_id
                cache_key = _get_oauth_cache_key(oauth_config, context.request_context)
                _oauth_token_cache.set(
                    cache_key,
                    token_data["access_token"],
                    expires_in=token_data.get("expires_in", 300),
                    refresh_token=token_data.get("refresh_token"),
                    refresh_expires_in=token_data.get("refresh_expires_in"),
                )
                _disk_token_store.set(disk_key, token_data)
                _store_token_to_db(oauth_config, token_data, "cli-flow")
                logger.warning("OAuth MCP %s: CLI auth successful, token cached (cache_key=%s)", self.toolset.name, cache_key)
                return None  # Token obtained, no approval needed
            else:
                logger.warning("OAuth MCP %s: CLI OAuth flow failed", self.toolset.name)
                # Fall through to frontend flow as fallback

        # Frontend mode: use RSA key exchange + approval mechanism
        key_exchange = get_oauth_key_exchange()
        code_verifier, code_challenge = _generate_pkce()

        with _exchanges_lock:
            _pending_exchanges[context.tool_call_id] = _PendingOAuthExchange(
                key_exchange=key_exchange,
                code_verifier=code_verifier,
                oauth_config=oauth_config,
                redirect_uri="",  # Set by frontend in the encrypted payload
            )

        metadata: Dict[str, Any] = {
            "authorization_url": oauth_config.authorization_url,
            "client_id": oauth_config.client_id,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "encryption_public_key": key_exchange.get_public_key_pem(),
        }
        if oauth_config.scopes:
            metadata["scopes"] = oauth_config.scopes
        if oauth_config.registration_endpoint:
            metadata["registration_endpoint"] = oauth_config.registration_endpoint
        params["__oauth_metadata"] = metadata

        return ApprovalRequirement(
            needs_approval=True,
            reason=f"OAuth authentication required for MCP server '{self.toolset.name}'",
        )

    def _is_placeholder_connect_tool(self) -> bool:
        return self.name.endswith("_connect") and "requires OAuth" in self.description

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        try:
            # For OAuth placeholder tools: load real tools after authentication
            if self._is_placeholder_connect_tool():
                return self._invoke_oauth_connect(params, context)

            # Serialize calls to the same MCP server to prevent SSE conflicts
            # Different servers can still run in parallel
            if not self.toolset._mcp_config:
                raise ValueError("MCP config not initialized")

            lock = get_server_lock(str(self.toolset._mcp_config.get_lock_string()))
            with lock:
                return asyncio.run(self._invoke_async(params, context.request_context))
        except Exception as e:
            error_detail = _extract_root_error_message(e)
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=error_detail,
                params=params,
                invocation=f"MCPtool {self.name} with params {params}",
            )

    def _invoke_oauth_connect(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        """Handle the OAuth placeholder tool: load real tools from the MCP server after authentication."""
        try:
            if not self.toolset._mcp_config:
                raise ValueError("MCP config not initialized")

            lock = get_server_lock(str(self.toolset._mcp_config.get_lock_string()))
            with lock:
                tools_result = asyncio.run(self.toolset._get_server_tools_with_context(context.request_context))

            real_tools = [RemoteMCPTool.create(tool, self.toolset) for tool in tools_result.tools]

            if real_tools:
                # Replace the placeholder with real tools on the toolset
                self.toolset.tools = real_tools

                # Register new tools in the tool executor so the LLM can call them
                tool_executor = getattr(context.llm, "tool_executor", None)
                if tool_executor:
                    # Remove the placeholder
                    tool_executor.tools_by_name.pop(self.name, None)
                    tool_executor._tool_to_toolset.pop(self.name, None)
                    # Register real tools
                    for tool in real_tools:
                        tool_executor.tools_by_name[tool.name] = tool
                        tool_executor._tool_to_toolset[tool.name] = self.toolset

                tool_names = [t.name for t in real_tools]
                logger.warning("OAuth MCP %s: loaded %d tools after authentication: %s", self.toolset.name, len(real_tools), tool_names)
                return StructuredToolResult(
                    status=StructuredToolResultStatus.SUCCESS,
                    data=f"Successfully authenticated and discovered {len(real_tools)} tools: {', '.join(tool_names)}. You can now call these tools directly.",
                    params=params,
                    invocation=f"OAuth connect to {self.toolset.name}",
                )
            else:
                logger.warning("OAuth MCP %s: authenticated but no tools found", self.toolset.name)
                return StructuredToolResult(
                    status=StructuredToolResultStatus.ERROR,
                    error=f"Authenticated but no tools found on MCP server {self.toolset.name}",
                    params=params,
                    invocation=f"OAuth connect to {self.toolset.name}",
                )
        except Exception as e:
            error_detail = _extract_root_error_message(e)
            logger.warning("OAuth MCP %s: connect failed: %s", self.toolset.name, error_detail)
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=f"OAuth connect failed: {error_detail}",
                params=params,
                invocation=f"OAuth connect to {self.toolset.name}",
            )

    @staticmethod
    def _is_content_error(content: str) -> bool:
        try:  # aws mcp sometimes returns an error in content - status code != 200
            json_content: dict = json.loads(content)
            status_code = json_content.get("response", {}).get("status_code", 200)
            return status_code >= 300
        except Exception:
            return False

    async def _invoke_async(
        self, params: Dict, request_context: Optional[Dict[str, Any]]
    ) -> StructuredToolResult:
        async with get_initialized_mcp_session(
            self.toolset, request_context
        ) as session:
            tool_result = await session.call_tool(self.name, params)

        merged_text = " ".join(c.text for c in tool_result.content if c.type == "text")

        is_error = tool_result.isError or self._is_content_error(merged_text)

        images = None
        if not is_error:
            images = [
                {"data": c.data, "mimeType": c.mimeType}
                for c in tool_result.content
                if c.type == "image"
            ] or None

        return StructuredToolResult(
            status=(
                StructuredToolResultStatus.ERROR if is_error
                else StructuredToolResultStatus.SUCCESS
            ),
            data=merged_text,
            images=images,
            params=params,
            invocation=f"MCPtool {self.name} with params {params}",
        )

    @classmethod
    def create(
        cls,
        tool: MCP_Tool,
        toolset: "RemoteMCPToolset",
    ):
        parameters = cls.parse_input_schema(tool.inputSchema)
        return cls(
            name=tool.name,
            description=tool.description or "",
            parameters=parameters,
            toolset=toolset,
        )

    @classmethod
    def parse_input_schema(
        cls, input_schema: dict[str, Any]
    ) -> Dict[str, ToolParameter]:
        required_list = input_schema.get("required", [])
        schema_params = input_schema.get("properties", {})
        parameters = {}
        for key, val in schema_params.items():
            parameters[key] = cls._parse_tool_parameter(
                val, root_schema=input_schema, required=key in required_list
            )

        return parameters

    @classmethod
    def _resolve_schema(
        cls, schema: dict[str, Any], root_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolves $ref and extracts the first non-null type from anyOf/oneOf/allOf."""
        if not isinstance(schema, dict):
            return schema

        # 1. Resolve $ref
        if "$ref" in schema:
            ref_path = str(schema["$ref"])
            if ref_path.startswith("#/"):
                parts = ref_path[2:].split("/")
                resolved = root_schema
                for part in parts:
                    if isinstance(resolved, dict):
                        resolved = resolved.get(part, {})
                    else:
                        resolved = {}
                        break

                # Recursively resolve the matched definition in case it contains more refs/anyOf
                resolved_schema = dict(schema)
                resolved_schema.pop("$ref")
                resolved_schema.update(cls._resolve_schema(resolved, root_schema))
                return resolved_schema

        # 2. Handle anyOf / oneOf / allOf for nullable or union types
        for compound_key in ["anyOf", "oneOf", "allOf"]:
            if compound_key in schema and isinstance(schema[compound_key], list):
                if compound_key == "allOf":
                    merged = dict(schema)
                    merged.pop(compound_key)
                    for sub_schema in schema[compound_key]:
                        if isinstance(sub_schema, dict):
                            resolved_sub = cls._resolve_schema(sub_schema, root_schema)
                            if resolved_sub.get("type") != "null":
                                for k, v in resolved_sub.items():
                                    if k == "properties" and isinstance(v, dict):
                                        merged.setdefault("properties", {}).update(v)
                                    elif k == "required" and isinstance(v, list):
                                        reqs = merged.setdefault("required", [])
                                        for req in v:
                                            if req not in reqs:
                                                reqs.append(req)
                                    elif k == "type":
                                        if "type" not in merged or merged["type"] == "null":
                                            merged["type"] = v
                                    else:
                                        merged[k] = v
                    return merged
                else:
                    for sub_schema in schema[compound_key]:
                        if isinstance(sub_schema, dict):
                            resolved_sub = cls._resolve_schema(sub_schema, root_schema)
                            # Skip null types, pick the first valid underlying schema type
                            if resolved_sub.get("type") != "null":
                                merged = dict(schema)
                                merged.pop(compound_key)
                                merged.update(resolved_sub)
                                return merged

        return schema

    @classmethod
    def _parse_tool_parameter(
        cls, schema: dict[str, Any], root_schema: dict[str, Any], required: bool = True
    ) -> ToolParameter:
        """Recursively parse a JSON Schema property into a ToolParameter.

        This preserves nested items, properties, and enum from MCP tool schemas
        so that the OpenAI-formatted schema sent to the LLM accurately describes
        complex parameter types (arrays, objects).
        """
        schema = cls._resolve_schema(schema, root_schema)

        param_type = schema.get("type", "string")

        items = None
        if "items" in schema and isinstance(schema["items"], dict):
            items = cls._parse_tool_parameter(
                schema["items"], root_schema, required=True
            )

        properties = None
        if "properties" in schema and isinstance(schema["properties"], dict):
            nested_required = schema.get("required", [])
            properties = {
                name: cls._parse_tool_parameter(
                    prop, root_schema, required=name in nested_required
                )
                for name, prop in schema["properties"].items()
            }

        enum = schema.get("enum")

        additional_properties = None
        raw_ap = schema.get("additionalProperties")
        if raw_ap is not None:
            if isinstance(raw_ap, bool):
                additional_properties = raw_ap
            elif isinstance(raw_ap, dict):
                # Resolve $ref pointers so the LLM sees concrete types, but
                # preserve compound keywords (anyOf/oneOf) intact — _resolve_schema
                # collapses those to a single branch which loses type information
                # (e.g. string|array becomes just string).
                if "$ref" in raw_ap:
                    additional_properties = cls._resolve_schema(raw_ap, root_schema)
                else:
                    additional_properties = raw_ap

        # Capture JSON Schema validation keywords that aren't modeled as
        # dedicated ToolParameter fields.  These are passed through to the
        # OpenAI-formatted schema so the LLM sees constraints like array
        # length limits, numeric ranges, and string patterns.
        _PASSTHROUGH_KEYWORDS = {
            "minItems", "maxItems",
            "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
            "minLength", "maxLength",
            "pattern",
            "default",
        }
        json_schema_extra = {k: v for k, v in schema.items() if k in _PASSTHROUGH_KEYWORDS}

        return ToolParameter(
            description=schema.get("description"),
            type=param_type,
            required=required,
            items=items,
            properties=properties,
            enum=enum,
            additional_properties=additional_properties,
            json_schema_extra=json_schema_extra or None,
        )

    def get_parameterized_one_liner(self, params: Dict) -> str:
        # AWS MCP cli_command
        if params and params.get("cli_command"):
            return f"{params.get('cli_command')}"

        # gcloud MCP run_gcloud_command
        if self.name == "run_gcloud_command" and params and "args" in params:
            args = params.get("args", [])
            if isinstance(args, list):
                return f"gcloud {' '.join(str(arg) for arg in args)}"

        if self.name and params and "args" in params:
            args = params.get("args", [])
            if isinstance(args, list):
                return f"{self.name} {' '.join(str(arg) for arg in args)}"

        return f"{self.toolset.name}: {self.name} {params}"


class RemoteMCPToolset(Toolset):
    config_classes: ClassVar[list[Type[Union[MCPConfig, StdioMCPConfig]]]] = [
        MCPConfig,
        StdioMCPConfig,
    ]
    description: str = "MCP server toolset"
    tools: List[RemoteMCPTool] = Field(default_factory=list)  # type: ignore
    _mcp_config: Optional[Union[MCPConfig, StdioMCPConfig]] = None

    def _render_headers(
        self, request_context: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, str]]:
        """
        Merge and render headers for MCP connection.

        Process:
        1. Start with 'headers' field (backward compatibility, passed as-is)
        2. Render 'extra_headers' via Jinja2 templates
        3. Merge them (later layers take precedence)

        Returns:
            Merged headers dictionary or None
        """
        if not isinstance(self._mcp_config, MCPConfig):
            return None

        # Start with direct headers (no rendering, backward compatibility)
        final_headers: Dict[str, str] = {}
        if self._mcp_config.headers:
            final_headers.update(self._mcp_config.headers)

        # Render and merge config-level extra_headers
        if self._mcp_config.extra_headers:
            rendered = render_header_templates(
                extra_headers=self._mcp_config.extra_headers,
                request_context=request_context,
                source_name=self.name,
            )
            if rendered:
                final_headers.update(rendered)

        return final_headers if final_headers else None

    def model_post_init(self, __context: Any) -> None:
        self.prerequisites = [
            CallablePrerequisite(callable=self.prerequisites_callable)
        ]
        # Set icon from config if specified
        if self.icon_url is None and self.config:
            self.icon_url = self.config.get("icon_url")

    @model_validator(mode="before")
    @classmethod
    def migrate_url_to_config(cls, values: dict[str, Any]) -> dict[str, Any]:
        """
        Migrates url from field parameter to config object.
        If url is passed as a parameter, it's moved to config (or config is created if it doesn't exist).
        """
        if not isinstance(values, dict) or "url" not in values:
            return values

        url_value = values.pop("url")
        if url_value is None:
            return values

        config = values.get("config")
        if config is None:
            config = {}
            values["config"] = config

        toolset_name = values.get("name", "unknown")
        if "url" in config:
            logging.warning(
                f"Toolset {toolset_name}: has two urls defined, remove the 'url' field from the toolset configuration and keep the 'url' in the config section."
            )
            return values

        logging.warning(
            f"Toolset {toolset_name}: 'url' field has been migrated to config. "
            "Please move 'url' to the config section."
        )
        config["url"] = url_value
        return values

    def prerequisites_callable(self, config) -> Tuple[bool, str]:
        try:
            if not config:
                return (False, f"Config is required for {self.name}")

            mode_value = config.get("mode", MCPMode.SSE.value)
            allowed_modes = [e.value for e in MCPMode]
            if mode_value not in allowed_modes:
                return (
                    False,
                    f'Invalid mode "{mode_value}", allowed modes are {", ".join(allowed_modes)}',
                )

            if mode_value == MCPMode.STDIO.value:
                self._mcp_config = StdioMCPConfig(**config)
            else:
                self._mcp_config = MCPConfig(**config)
                clean_url_str = str(self._mcp_config.url).rstrip("/")

                if self._mcp_config.mode == MCPMode.SSE and not clean_url_str.endswith(
                    "/sse"
                ):
                    self._mcp_config.url = AnyUrl(clean_url_str + "/sse")

            # For OAuth-protected servers, skip full MCP session init (it will 401).
            # Just verify the server is reachable and register a placeholder tool
            # that triggers the OAuth flow on first use. Tools are loaded after auth.
            if isinstance(self._mcp_config, MCPConfig) and self._mcp_config.oauth and self._mcp_config.oauth.enabled:
                return self._check_oauth_server_reachable()

            tools_result = asyncio.run(self._get_server_tools())

            self.tools = [
                RemoteMCPTool.create(tool, self) for tool in tools_result.tools
            ]

            if not self.tools:
                logging.warning(f"mcp server {self.name} loaded 0 tools.")

            return (True, "")
        except Exception as e:
            error_detail = _extract_root_error_message(e)
            return (
                False,
                f"Failed to load mcp server {self.name}: {error_detail}"
                ". If the server is still starting up, Holmes will retry automatically",
            )

    def _check_oauth_server_reachable(self) -> Tuple[bool, str]:
        """For OAuth MCP servers, verify reachability without authenticating.

        If a cached token exists (from a previous request in the same conversation),
        load the real tools directly. Otherwise, auto-discover OAuth endpoints if needed,
        then register a placeholder tool that triggers the OAuth flow on first use.
        """
        assert isinstance(self._mcp_config, MCPConfig)
        assert self._mcp_config.oauth is not None
        url = str(self._mcp_config.url).rstrip("/")

        # If we already have a cached token (in-memory or disk), try to load real tools directly
        oauth_config = self._mcp_config.oauth
        disk_key = str(self._mcp_config.url)

        # Load disk token into in-memory cache if available
        if oauth_config.authorization_url and oauth_config.client_id:
            startup_cache_key = _get_oauth_cache_key(oauth_config, None)
            if not _oauth_token_cache.has(startup_cache_key):
                disk_token = _disk_token_store.get(disk_key)
                if disk_token:
                    _oauth_token_cache.set(
                        startup_cache_key,
                        disk_token["access_token"],
                        expires_in=int(disk_token.get("expires_at", time.time() + 300) - time.time()),
                        refresh_token=disk_token.get("refresh_token"),
                    )
                    logging.warning(f"OAuth MCP server {self.name}: loaded token from disk store into cache")

            if _oauth_token_cache.has(startup_cache_key):
                try:
                    tools_result = asyncio.run(self._get_server_tools())
                    self.tools = [RemoteMCPTool.create(tool, self) for tool in tools_result.tools]
                    if self.tools:
                        logging.warning(f"OAuth MCP server {self.name}: loaded {len(self.tools)} tools using cached token")
                        return (True, "")
                except Exception as e:
                    logging.warning(f"OAuth MCP server {self.name}: cached token failed, falling back to placeholder: {_extract_root_error_message(e)}")

        try:
            # Try the well-known endpoint first (no auth needed)
            response = httpx.get(
                f"{url}/.well-known/oauth-protected-resource",
                timeout=10,
                verify=self._mcp_config.verify_ssl,
            )
            if response.status_code not in (200, 401):
                # Also try the root — a 401 means server is up but needs auth
                response = httpx.post(url, timeout=10, verify=self._mcp_config.verify_ssl)

            if response.status_code not in (200, 401):
                return (False, f"MCP server {self.name} returned HTTP {response.status_code}")

            # Auto-discover OAuth endpoints if not configured
            if not oauth_config.authorization_url or not oauth_config.token_url or not oauth_config.client_id:
                discovered = self._discover_oauth_endpoints(url, response)
                if not discovered:
                    return (False, f"MCP server {self.name}: OAuth enabled but auto-discovery failed. Configure authorization_url, token_url, and client_id manually.")

        except Exception as e:
            return (False, f"MCP server {self.name} unreachable: {_extract_root_error_message(e)}")

        # Register a placeholder tool that will trigger OAuth on first call.
        # After auth succeeds, _invoke will load the real tools dynamically.
        from mcp.types import Tool as MCP_Tool
        placeholder = MCP_Tool(
            name=f"{self.name}_connect",
            description=f"Connect to {self.name} (requires OAuth authentication). Call this tool to authenticate and discover available tools.",
            inputSchema={"type": "object", "properties": {}},
        )
        self.tools = [RemoteMCPTool.create(placeholder, self)]
        logging.info(f"OAuth MCP server {self.name} is reachable, registered placeholder tool (auth required)")
        return (True, "")

    def _discover_oauth_endpoints(self, mcp_url: str, initial_response: httpx.Response) -> bool:
        """Auto-discover OAuth endpoints following the MCP SDK's discovery flow.

        Discovery order (matching mcp.client.auth):
        1. Try Protected Resource Metadata (RFC 9728) — path-based, then root-based
        2. If PRM found auth server → fetch its OIDC/OAuth metadata
        3. If PRM not found → legacy fallback: /.well-known/oauth-authorization-server on MCP server
        4. Dynamic Client Registration if no client_id configured

        Returns True if discovery succeeded and oauth config is fully populated.
        """
        import re
        from urllib.parse import urlparse

        assert isinstance(self._mcp_config, MCPConfig) and self._mcp_config.oauth is not None
        oauth_config = self._mcp_config.oauth
        verify_ssl = self._mcp_config.verify_ssl
        parsed = urlparse(mcp_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        # Step 1: Try to find auth server via Protected Resource Metadata (RFC 9728)
        auth_server_url = None
        prm_urls = []

        # From WWW-Authenticate header
        www_auth = initial_response.headers.get("www-authenticate", "")
        rm_match = re.search(r'resource_metadata="([^"]+)"', www_auth)
        if rm_match:
            prm_urls.append(rm_match.group(1))

        # Path-based well-known (e.g. /.well-known/oauth-protected-resource/v1/mcp)
        if parsed.path and parsed.path != "/":
            prm_urls.append(f"{base_url}/.well-known/oauth-protected-resource{parsed.path}")

        # Root-based well-known
        prm_urls.append(f"{base_url}/.well-known/oauth-protected-resource")

        for prm_url in prm_urls:
            try:
                prm_response = httpx.get(prm_url, timeout=10, verify=verify_ssl)
                if prm_response.status_code == 200:
                    prm = prm_response.json()
                    auth_servers = prm.get("authorization_servers", [])
                    if auth_servers:
                        auth_server_url = auth_servers[0].rstrip("/")
                        if not oauth_config.scopes and prm.get("scopes_supported"):
                            oauth_config.scopes = prm["scopes_supported"]
                        logging.warning("OAuth discovery %s: found auth server from PRM %s: %s", self.name, prm_url, auth_server_url)
                        break
            except Exception:
                continue

        # Step 2: Fetch OAuth/OIDC metadata from auth server (or legacy fallback)
        oidc_config = None

        if auth_server_url:
            # Auth server found via PRM — try its OIDC/OAuth metadata
            auth_parsed = urlparse(auth_server_url)
            auth_base = f"{auth_parsed.scheme}://{auth_parsed.netloc}"
            discovery_urls = []
            if auth_parsed.path and auth_parsed.path != "/":
                discovery_urls.append(f"{auth_base}/.well-known/oauth-authorization-server{auth_parsed.path.rstrip('/')}")
                discovery_urls.append(f"{auth_base}/.well-known/openid-configuration{auth_parsed.path.rstrip('/')}")
            discovery_urls.append(f"{auth_base}/.well-known/oauth-authorization-server")
            discovery_urls.append(f"{auth_base}/.well-known/openid-configuration")
        else:
            # Legacy fallback (MCP spec 2025-03-26): try on the MCP server itself
            discovery_urls = [
                f"{base_url}/.well-known/oauth-authorization-server",
                f"{base_url}/.well-known/openid-configuration",
            ]

        for disc_url in discovery_urls:
            try:
                disc_response = httpx.get(disc_url, timeout=10, verify=verify_ssl)
                if disc_response.status_code == 200:
                    oidc_config = disc_response.json()
                    logging.warning("OAuth discovery %s: fetched OAuth metadata from %s", self.name, disc_url)
                    break
            except Exception:
                continue

        if not oidc_config:
            logging.warning("OAuth discovery %s: all OAuth metadata discovery attempts failed", self.name)
            return False

        if not oauth_config.authorization_url:
            oauth_config.authorization_url = oidc_config.get("authorization_endpoint")
        if not oauth_config.token_url:
            oauth_config.token_url = oidc_config.get("token_endpoint")

        if not oauth_config.authorization_url or not oauth_config.token_url:
            logging.warning("OAuth discovery %s: missing authorization or token endpoint in OAuth metadata", self.name)
            return False

        # Save registration endpoint for potential CLI re-registration later
        registration_endpoint = oidc_config.get("registration_endpoint")
        if registration_endpoint:
            oauth_config.registration_endpoint = registration_endpoint

        # Step 3: Dynamic Client Registration if no client_id
        # At startup, we don't know the redirect_uri (it depends on CLI port or frontend URL).
        # Skip DCR here — it will be done at runtime by the CLI flow (with actual port)
        # or by the frontend (with its own callback URL).
        if not oauth_config.client_id:
            if not registration_endpoint:
                logging.warning("OAuth discovery %s: no client_id and no DCR endpoint — frontend or CLI must provide client_id", self.name)
                # Don't fail — the frontend may handle DCR itself
            else:
                logging.warning("OAuth discovery %s: no client_id, DCR deferred to runtime (registration_endpoint=%s)", self.name, registration_endpoint)

        logging.warning(
            "OAuth discovery %s complete: authorization_url=%s, token_url=%s, client_id=%s",
            self.name, oauth_config.authorization_url, oauth_config.token_url, oauth_config.client_id,
        )
        return True

    async def _get_server_tools(self):
        async with get_initialized_mcp_session(self, None) as session:
            return await session.list_tools()

    async def _get_server_tools_with_context(self, request_context: Optional[Dict[str, Any]]):
        async with get_initialized_mcp_session(self, request_context) as session:
            return await session.list_tools()
