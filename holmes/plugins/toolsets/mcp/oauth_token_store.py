"""OAuth token storage primitives: in-memory cache and disk store."""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from holmes.core.config import config_path_dir

logger = logging.getLogger(__name__)


class _CachedToken:
    """Holds an access token, its expiry, and an optional refresh token."""

    def __init__(
        self,
        access_token: str,
        expires_at: float,
        refresh_token: Optional[str] = None,
        refresh_expires_at: Optional[float] = None,
        token_url: Optional[str] = None,
        client_id: Optional[str] = None,
        authorization_url: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self.access_token = access_token
        self.expires_at = expires_at
        self.refresh_token = refresh_token
        self.refresh_expires_at = refresh_expires_at
        # Metadata for background refresh sweep
        self.token_url = token_url
        self.client_id = client_id
        self.authorization_url = authorization_url
        self.user_id = user_id

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

    def get_valid_access_token(self, key: str) -> Optional[str]:
        """Return a valid (non-expired) access token, or None if expired or missing."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if not entry.access_expired:
                return entry.access_token
            # Access expired — keep entry if refresh token exists (caller should try refresh)
            if entry.refresh_token:
                return None
            # No refresh token, evict
            del self._cache[key]
            return None

    def get_refresh_token(self, key: str) -> Optional[str]:
        """Return the refresh token even if expired — some IdPs accept expired refresh tokens to issue new ones."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            return entry.refresh_token

    def set(
        self,
        key: str,
        access_token: str,
        expires_in: int = 300,
        refresh_token: Optional[str] = None,
        refresh_expires_in: Optional[int] = None,
        token_url: Optional[str] = None,
        client_id: Optional[str] = None,
        authorization_url: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        now = time.monotonic()
        # Subtract a small buffer so we refresh before actual expiry
        access_expires_at = now + max(expires_in - 30, 10)
        refresh_expires_at = None
        if refresh_token:
            # Default to 24 hours if IdP doesn't return refresh_expires_in (not all do)
            refresh_ttl = refresh_expires_in if refresh_expires_in else 86400
            refresh_expires_at = now + max(refresh_ttl - 30, 10)
        with self._lock:
            self._cache[key] = _CachedToken(
                access_token, access_expires_at, refresh_token, refresh_expires_at,
                token_url=token_url, client_id=client_id,
                authorization_url=authorization_url, user_id=user_id,
            )

    def evict(self, key: str) -> None:
        """Remove an entry from the cache (e.g. after a failed refresh)."""
        with self._lock:
            self._cache.pop(key, None)

    def get_expiring_entries(self, within_seconds: int) -> list[tuple[str, "_CachedToken"]]:
        """Return (key, entry) pairs for tokens whose access expires within the given window."""
        threshold = time.monotonic() + within_seconds
        with self._lock:
            return [
                (key, entry) for key, entry in self._cache.items()
                if entry.expires_at <= threshold
            ]

    def has_token_or_refresh(self, key: str) -> bool:
        """True if there is a valid access token or any refresh token (even expired — IdP decides validity)."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return False
            if not entry.access_expired:
                return True
            if entry.refresh_token:
                return True
            del self._cache[key]
            return False


class DiskTokenStore:
    """Persists OAuth tokens to ~/.holmes/auth/mcp_tokens.json for CLI usage."""

    def __init__(self) -> None:
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

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path) as f:
                return json.load(f)
        except Exception:
            return {}
