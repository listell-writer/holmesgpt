"""OAuthTokenManager: single interface for OAuth token lifecycle management.

Manages the 3-tier token storage (cache → DB → disk), automatic background
refresh an hour before expiry, and token persistence across clusters.
"""

import base64
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from holmes.plugins.toolsets.mcp.oauth_token_store import (
    DiskTokenStore,
    OAuthTokenCache,
)

logger = logging.getLogger(__name__)

# How long before access token expiry to trigger a background refresh
_REFRESH_AHEAD_SECONDS = 3600  # 1 hour


@dataclass
class _ScheduledRefresh:
    """Metadata for a token that needs background refresh."""

    cache_key: str
    token_url: str
    client_id: Optional[str]
    authorization_url: Optional[str]
    user_id: Optional[str]
    refresh_at: float  # monotonic time when refresh should happen


class OAuthTokenManager:
    """Central manager for OAuth token lifecycle.

    Usage:
        manager = OAuthTokenManager()
        manager.set_dal(dal)  # optional, for DB storage

        # Store a token after initial OAuth flow
        manager.store_token(oauth_config, token_data, request_context)

        # Get a valid access token (checks cache → DB → disk, refreshes if needed)
        token = manager.get_access_token(oauth_config, request_context)

        # Check if a token is available (without triggering refresh)
        if manager.has_token(oauth_config, request_context):
            ...

        # Shutdown background refresh thread
        manager.shutdown()
    """

    def __init__(self) -> None:
        self._cache = OAuthTokenCache()
        self._disk_store = DiskTokenStore()
        self._dal: Optional[Any] = None
        self._signing_key_getter: Optional[Callable[[], Optional[str]]] = None

        # Background refresh state
        self._scheduled: Dict[str, _ScheduledRefresh] = {}
        self._schedule_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._refresh_thread = threading.Thread(
            target=self._background_refresh_loop,
            name="oauth-token-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    # ── Configuration ──────────────────────────────────────────────────

    def set_dal(self, dal: Any) -> None:
        """Set the DAL instance for DB token operations. Called during server startup."""
        self._dal = dal
        if dal and dal.enabled:
            logger.info("OAuthTokenManager: DAL initialized for cross-cluster token storage")

    def set_signing_key_getter(self, getter: Callable[[], Optional[str]]) -> None:
        """Set the function used to retrieve the signing key (lazy, avoids import cycles)."""
        self._signing_key_getter = getter

    # ── Public API ─────────────────────────────────────────────────────

    def get_access_token(
        self,
        oauth_config: Any,
        request_context: Optional[Dict[str, Any]] = None,
        disk_key: Optional[str] = None,
        provider_aliases: Optional[list[str]] = None,
    ) -> Optional[str]:
        """Return a valid access token, checking cache → refresh → DB → disk.

        Args:
            provider_aliases: Additional provider names to try when looking up
                tokens in the DB (e.g. toolset name). The authorization_url is
                always tried first.

        Returns None if no token is available anywhere (caller should initiate OAuth flow).
        """
        cache_key = self._get_cache_key(oauth_config, request_context)
        user_id = _get_user_id(request_context)

        # 1. Check in-memory cache
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        # 2. Access expired but refresh token available → refresh now
        if self._cache.has(cache_key):
            refreshed = self._refresh_token(cache_key, oauth_config, user_id=user_id)
            if refreshed:
                return refreshed

        # 3. Check DB for cross-cluster token
        db_token = self._load_from_db(oauth_config, user_id, provider_aliases=provider_aliases)
        if db_token:
            self._cache.set(
                cache_key,
                db_token["access_token"],
                expires_in=db_token.get("expires_in", 300),
                refresh_token=db_token.get("refresh_token"),
                refresh_expires_in=db_token.get("refresh_expires_in"),
            )
            self._schedule_background_refresh(cache_key, oauth_config, db_token.get("expires_in", 300), user_id)
            logger.info("OAuthTokenManager: loaded token from DB (provider=%s)", oauth_config.authorization_url)
            return db_token["access_token"]

        # 4. Check disk store (CLI persistence)
        dk = disk_key or self._default_disk_key(oauth_config)
        disk_token = self._disk_store.get(dk)
        if disk_token and disk_token.get("access_token"):
            expires_in = int(disk_token.get("expires_at", time.time() + 300) - time.time())
            self._cache.set(
                cache_key,
                disk_token["access_token"],
                expires_in=expires_in,
                refresh_token=disk_token.get("refresh_token"),
            )
            self._schedule_background_refresh(cache_key, oauth_config, expires_in, user_id)
            logger.info("OAuthTokenManager: loaded token from disk")
            return disk_token["access_token"]

        return None

    def has_token(
        self,
        oauth_config: Any,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Check if any token (access or refreshable) is available in cache."""
        cache_key = self._get_cache_key(oauth_config, request_context)
        return self._cache.has(cache_key)

    def store_token(
        self,
        oauth_config: Any,
        token_data: Dict[str, Any],
        request_context: Optional[Dict[str, Any]] = None,
        disk_key: Optional[str] = None,
        store_to_disk: bool = False,
    ) -> None:
        """Store a token to cache, DB, and optionally disk. Schedules background refresh."""
        cache_key = self._get_cache_key(oauth_config, request_context)
        user_id = _get_user_id(request_context)
        access_token = token_data.get("access_token")
        if not access_token:
            logger.warning("OAuthTokenManager: store_token called with no access_token")
            return

        expires_in = token_data.get("expires_in", 300)

        self._cache.set(
            cache_key,
            access_token,
            expires_in=expires_in,
            refresh_token=token_data.get("refresh_token"),
            refresh_expires_in=token_data.get("refresh_expires_in"),
        )

        self._store_to_db(oauth_config.authorization_url, token_data, user_id)

        if store_to_disk:
            dk = disk_key or self._default_disk_key(oauth_config)
            self._disk_store.set(dk, token_data)

        self._schedule_background_refresh(cache_key, oauth_config, expires_in, user_id)
        logger.info(
            "OAuthTokenManager: token stored (cache_key=%s, expires_in=%s, has_refresh=%s)",
            cache_key, expires_in, "refresh_token" in token_data,
        )

    def shutdown(self) -> None:
        """Stop the background refresh thread."""
        self._shutdown_event.set()
        self._refresh_thread.join(timeout=5)

    # ── Cache / key helpers (exposed for callers that need the key) ─────

    @property
    def cache(self) -> OAuthTokenCache:
        return self._cache

    @property
    def disk_store(self) -> DiskTokenStore:
        return self._disk_store

    def get_cache_key(self, oauth_config: Any, request_context: Optional[Dict[str, Any]] = None) -> str:
        """Public accessor for the cache key (needed by inject_oauth_token and requires_approval)."""
        return self._get_cache_key(oauth_config, request_context)

    # ── Background refresh ─────────────────────────────────────────────

    def _schedule_background_refresh(
        self,
        cache_key: str,
        oauth_config: Any,
        expires_in: int,
        user_id: Optional[str],
    ) -> None:
        """Schedule a background refresh for this token 1 hour before expiry."""
        if expires_in <= _REFRESH_AHEAD_SECONDS:
            return

        refresh_at = time.monotonic() + expires_in - _REFRESH_AHEAD_SECONDS
        with self._schedule_lock:
            self._scheduled[cache_key] = _ScheduledRefresh(
                cache_key=cache_key,
                token_url=oauth_config.token_url,
                client_id=oauth_config.client_id,
                authorization_url=oauth_config.authorization_url,
                user_id=user_id,
                refresh_at=refresh_at,
            )
        logger.info(
            "OAuthTokenManager: scheduled background refresh in %ds (cache_key=%s)",
            expires_in - _REFRESH_AHEAD_SECONDS, cache_key,
        )

    def _background_refresh_loop(self) -> None:
        """Daemon thread that checks for tokens needing refresh every 60s."""
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(timeout=60)
            if self._shutdown_event.is_set():
                break
            self._process_pending_refreshes()

    def _process_pending_refreshes(self) -> None:
        """Check all scheduled refreshes and execute any that are due."""
        now = time.monotonic()
        due: list[_ScheduledRefresh] = []

        with self._schedule_lock:
            for key, entry in list(self._scheduled.items()):
                if now >= entry.refresh_at:
                    due.append(entry)
                    del self._scheduled[key]

        for entry in due:
            try:
                self._execute_background_refresh(entry)
            except Exception:
                logger.warning(
                    "OAuthTokenManager: background refresh failed (cache_key=%s)",
                    entry.cache_key, exc_info=True,
                )

    def _execute_background_refresh(self, entry: _ScheduledRefresh) -> None:
        """Execute a single background token refresh."""
        refresh_token = self._cache.get_refresh_token(entry.cache_key)
        if not refresh_token:
            logger.warning("OAuthTokenManager: background refresh skipped, no refresh token (cache_key=%s)", entry.cache_key)
            return

        result = self._do_refresh_request(entry.token_url, entry.client_id, refresh_token, entry.cache_key)
        if not result:
            return

        token_data, _access_token, _expires_in = result
        self._store_to_db(entry.authorization_url, token_data, entry.user_id)

        # Re-schedule using a minimal oauth_config-like object
        _OAuthLike = type("_OAuthLike", (), {
            "token_url": entry.token_url,
            "client_id": entry.client_id,
            "authorization_url": entry.authorization_url,
        })()
        self._schedule_background_refresh(entry.cache_key, _OAuthLike, _expires_in, entry.user_id)

    # ── Synchronous (reactive) refresh ─────────────────────────────────

    def _refresh_token(self, cache_key: str, oauth_config: Any, user_id: Optional[str] = None) -> Optional[str]:
        """Attempt to refresh an expired access token using the cached refresh token."""
        refresh_token = self._cache.get_refresh_token(cache_key)
        if not refresh_token:
            return None

        try:
            result = self._do_refresh_request(oauth_config.token_url, oauth_config.client_id, refresh_token, cache_key)
            if not result:
                return None

            token_data, access_token, expires_in = result
            self._store_to_db(oauth_config.authorization_url, token_data, user_id)
            self._schedule_background_refresh(cache_key, oauth_config, expires_in, user_id)
            return access_token
        except Exception:
            logger.warning("OAuthTokenManager: refresh failed (cache_key=%s)", cache_key, exc_info=True)
            return None

    def _do_refresh_request(
        self, token_url: str, client_id: Optional[str], refresh_token: str, cache_key: str,
    ) -> Optional[Tuple[Dict[str, Any], str, int]]:
        """POST to token endpoint, validate response, update cache.

        Returns (token_data, access_token, expires_in) on success, None on failure.
        """
        logger.info("OAuthTokenManager: refreshing token at %s (cache_key=%s)", token_url, cache_key)
        response = httpx.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if response.status_code != 200:
            logger.warning("OAuthTokenManager: refresh HTTP %d (cache_key=%s)", response.status_code, cache_key)
            return None

        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.warning("OAuthTokenManager: refresh response missing access_token (cache_key=%s)", cache_key)
            return None

        expires_in = token_data.get("expires_in", 300)
        self._cache.set(
            cache_key,
            access_token,
            expires_in=expires_in,
            refresh_token=token_data.get("refresh_token", refresh_token),
            refresh_expires_in=token_data.get("refresh_expires_in"),
        )
        logger.info("OAuthTokenManager: token refreshed (cache_key=%s, expires_in=%s)", cache_key, expires_in)
        return token_data, access_token, expires_in

    # ── DB operations ──────────────────────────────────────────────────

    def _get_signing_key(self) -> Optional[str]:
        if self._signing_key_getter:
            return self._signing_key_getter()
        return None

    def _get_signing_key_hash(self) -> Optional[str]:
        key = self._get_signing_key()
        if not key:
            return None
        return hashlib.sha256(key.encode()).hexdigest()

    def _derive_fernet_key(self, signing_key: str) -> bytes:
        """Derive a Fernet encryption key from the signing key via HKDF."""
        return base64.urlsafe_b64encode(
            HKDF(algorithm=SHA256(), length=32, salt=b"holmesgpt-oauth-db-token", info=b"token-encryption")
            .derive(signing_key.encode())
        )

    def _encrypt_token(self, token_data: Dict[str, Any]) -> Optional[str]:
        """Encrypt token data with signing_key-derived Fernet key for DB storage."""
        signing_key = self._get_signing_key()
        if not signing_key:
            return None
        return Fernet(self._derive_fernet_key(signing_key)).encrypt(json.dumps(token_data).encode()).decode()

    def _decrypt_token(self, encrypted: str) -> Optional[Dict[str, Any]]:
        """Decrypt token data from DB using signing_key-derived Fernet key."""
        signing_key = self._get_signing_key()
        if not signing_key:
            return None

        try:
            decrypted = Fernet(self._derive_fernet_key(signing_key)).decrypt(encrypted.encode())
            return json.loads(decrypted)
        except Exception:
            logger.warning("OAuthTokenManager: failed to decrypt token from DB (signing_key mismatch?)")
            return None

    def _load_from_db(
        self,
        oauth_config: Any,
        user_id: Optional[str],
        provider_aliases: Optional[list[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Load and decrypt a token from the DB.

        Tries provider lookup by authorization_url first, then any provider_aliases
        (e.g. toolset name). Handles frontend-stored tokens (signing_key_hash='__frontend__',
        unencrypted JSON) as well as Holmes-encrypted tokens.
        """
        if not self._dal or not self._dal.enabled:
            return None

        signing_key_hash = self._get_signing_key_hash()

        providers_to_try = []
        if oauth_config.authorization_url:
            providers_to_try.append(oauth_config.authorization_url)
        if provider_aliases:
            providers_to_try.extend(provider_aliases)
        if not providers_to_try:
            providers_to_try.append("unknown")

        for provider in providers_to_try:
            db_record = self._dal.get_oauth_token(provider, user_id=user_id)
            if not db_record:
                continue

            stored_hash = db_record.get("signing_key_hash", "")

            if stored_hash == "__frontend__":
                try:
                    token_data = json.loads(db_record["encrypted_token"])
                    if token_data.get("access_token"):
                        logger.info("OAuthTokenManager: loaded frontend-stored token from DB (provider=%s)", provider)
                        return token_data
                except (json.JSONDecodeError, TypeError):
                    logger.warning("OAuthTokenManager: failed to parse frontend token from DB (provider=%s)", provider)
                continue

            if not signing_key_hash:
                continue
            if stored_hash != signing_key_hash:
                logger.warning(
                    "OAuthTokenManager: DB token signing_key_hash mismatch (stored=%s, current=%s)",
                    stored_hash[:12], signing_key_hash[:12],
                )
                continue

            return self._decrypt_token(db_record["encrypted_token"])

        return None

    def _store_to_db(self, authorization_url: Optional[str], token_data: Dict[str, Any], user_id: Optional[str]) -> None:
        """Store an encrypted token to the DB."""
        signing_key_hash = self._get_signing_key_hash()
        if not self._dal or not self._dal.enabled or not signing_key_hash:
            return

        try:
            provider_name = authorization_url or "unknown"
            encrypted = self._encrypt_token(token_data)
            if not encrypted:
                logger.warning("OAuthTokenManager: cannot encrypt token (no signing key)")
                return

            expiry = None
            if token_data.get("refresh_expires_in"):
                expiry = (datetime.now(timezone.utc) + timedelta(seconds=token_data["refresh_expires_in"])).isoformat()
            elif token_data.get("expires_in"):
                expiry = (datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])).isoformat()

            self._dal.upsert_oauth_token(
                provider_name=provider_name,
                encrypted_token=encrypted,
                signing_key_hash=signing_key_hash,
                token_expiry=expiry,
                user_id=user_id,
            )
            logger.info("OAuthTokenManager: stored token to DB (provider=%s, user_id=%s)", provider_name, user_id)
        except Exception:
            logger.warning("OAuthTokenManager: failed to store token to DB", exc_info=True)

    # ── Key helpers ────────────────────────────────────────────────────

    def _get_cache_key(self, oauth_config: Any, request_context: Optional[Dict[str, Any]]) -> str:
        conv_key = _get_conversation_key(request_context)
        user_id = _get_user_id(request_context) or "__no_user__"
        idp_key = hashlib.sha256(
            f"{oauth_config.authorization_url}:{oauth_config.client_id}".encode()
        ).hexdigest()[:12]
        return f"{user_id}:{conv_key}:{idp_key}"

    @staticmethod
    def _default_disk_key(oauth_config: Any) -> str:
        """Derive a disk store key from the oauth config."""
        return oauth_config.authorization_url or "unknown"


# ── Module-level helpers (used by the manager and shared with toolset_mcp) ──


def _get_conversation_key(request_context: Optional[Dict[str, Any]]) -> str:
    """Extract a conversation key from request context headers."""
    if request_context:
        headers = request_context.get("headers", {})
        for key in ("X-Conversation-Id", "x-conversation-id", "X-Session-Id", "x-session-id"):
            if key in headers:
                return str(headers[key])
    return "__default__"


def _get_user_id(request_context: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract user_id from request context."""
    if request_context:
        return request_context.get("user_id")
    return None
