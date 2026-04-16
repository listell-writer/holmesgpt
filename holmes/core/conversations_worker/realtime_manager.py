"""
Realtime manager for the ConversationWorker.

Runs an asyncio event loop in a background daemon thread. Manages:
 - Cluster-level Presence channel: advertises this Holmes instance
 - Postgres Changes subscription on Conversations table: triggers a claim
   when new pending rows appear for this cluster
 - Per-conversation Presence channels (optional, for heartbeat)

Communication with the sync ConversationWorker is via a callback that is
invoked when a pending-conversation notification arrives. The callback MUST
be thread-safe (the worker passes a threading.Event.set).
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

import realtime._async.client as rt_client
from realtime._async.client import AsyncRealtimeClient
from websockets.asyncio.client import connect as ws_connect

try:
    # python-socks is only required when an HTTP CONNECT proxy is in use
    # (sandboxed environments, enterprise egress). Normal deployments don't
    # need it.
    from python_socks.async_.asyncio import Proxy as _SocksProxy
except ImportError:  # pragma: no cover — optional dependency
    _SocksProxy = None  # type: ignore[assignment]

from holmes import get_version
from holmes.common.env_vars import (
    CONVERSATION_WORKER_AUTH_REFRESH_INTERVAL_SECONDS,
)
from holmes.core.supabase_dal import CONVERSATIONS_TABLE

if TYPE_CHECKING:
    from holmes.core.supabase_dal import SupabaseDal


# ---- channel topic helpers ----


# Supabase Realtime rate-limits presence broadcasts per client.  We coalesce
# all conversation state changes into one track() call per interval, and only
# flush when the payload actually changes — staying well under any
# reasonable rate limit even during storms of short-lived conversations.
_PRESENCE_FLUSH_INTERVAL_SECONDS = 2.0


def account_presence_topic(account_id: str) -> str:
    """Single per-account channel carrying cluster + per-conversation presence.

    Separate from the pg-changes channel so that a presence rate-limit
    disconnect does not kill the Postgres Changes subscription used for
    claiming new conversations.
    """
    return f"holmes:presence:{account_id}"


def pg_changes_topic(account_id: str) -> str:
    """Dedicated per-account channel for Conversations Postgres Changes.

    Filtered server-side by ``account_id``; the callback does NOT further
    filter on ``cluster_id`` because ``claim_conversations`` already does
    that in the RPC.
    """
    return f"holmes:pgchanges:{account_id}"


def conversation_presence_key(conversation_id: str, holmes_id: str) -> str:
    """Presence key for a per-conversation entry on the account channel."""
    return f"conversation:{conversation_id}:{holmes_id}"


def _install_proxy_patch_if_needed() -> None:
    """
    If an ``https_proxy`` env var is set, monkey-patch ``realtime._async.client.connect``
    so the WebSocket connection is tunneled through the HTTP CONNECT proxy. This
    is needed in sandboxed environments that require all egress to go through a
    proxy (and direct DNS/TCP are blocked).

    Idempotent — only patches once.
    """
    proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if not proxy_url:
        return

    if getattr(rt_client, "_holmes_proxy_patched", False):
        return

    if _SocksProxy is None:
        logging.warning(
            "https_proxy is set but python-socks is not installed; "
            "Realtime WebSocket will attempt direct connection and likely fail. "
            "Install python-socks to tunnel WS through the proxy."
        )
        return

    p = urllib.parse.urlparse(proxy_url)
    if not p.hostname:
        logging.warning("https_proxy has no hostname; skipping proxy patch")
        return
    proxy_connect_url = f"http://{p.hostname}"
    if p.username and p.password:
        proxy_connect_url = f"http://{p.username}:{p.password}@{p.hostname}"
    elif p.username:
        proxy_connect_url = f"http://{p.username}@{p.hostname}"
    if p.port is not None:
        proxy_connect_url += f":{p.port}"

    async def _proxied_connect(url: str, *args: Any, **kwargs: Any) -> Any:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            return await ws_connect(url, *args, **kwargs)

        # skip proxy for localhost targets
        if parsed.hostname in ("localhost", "127.0.0.1"):
            return await ws_connect(url, *args, **kwargs)

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        proxy = _SocksProxy.from_url(proxy_connect_url)
        sock = await proxy.connect(dest_host=parsed.hostname, dest_port=port)
        kwargs.setdefault("server_hostname", parsed.hostname)
        if parsed.scheme == "wss" and "ssl" not in kwargs:
            kwargs["ssl"] = ssl.create_default_context()
        return await ws_connect(url, *args, sock=sock, **kwargs)

    rt_client.connect = _proxied_connect  # type: ignore[attr-defined]
    rt_client._holmes_proxy_patched = True  # type: ignore[attr-defined]
    logging.info(
        "Installed WebSocket proxy patch for realtime client (proxy=%s:%s)",
        p.hostname,
        p.port,
    )


class RealtimeManager:
    def __init__(
        self,
        dal: "SupabaseDal",
        holmes_id: str,
        on_new_pending: Callable[[], None],
    ) -> None:
        self.dal = dal
        self.holmes_id = holmes_id
        self.on_new_pending = on_new_pending
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = threading.Event()
        self._client = None
        self._account_channel = None
        # Separate channel for Postgres Changes subscriptions. Kept distinct
        # from the presence channel so that a presence rate-limit disconnect
        # on one doesn't also kill the pg-changes subscription that drives
        # conversation claiming.
        self._pg_channel = None
        # True once the pg-changes channel is SUBSCRIBED (drives
        # is_connected() and the claim-loop's realtime-vs-poll decision).
        # Presence-channel state is tracked separately but doesn't gate
        # claiming.
        self._connected = False
        # Last JWT we pushed to the realtime client via set_auth. Used to skip
        # the network call on the common case where the token hasn't rotated.
        self._last_auth_jwt: Optional[str] = None
        # Per-conversation highest request_sequence that has joined presence.
        # Older-sequence join / leave / update calls are ignored so a stale
        # worker can't disrupt the presence owned by the newest worker.
        self._presence_sequences: Dict[str, int] = {}
        self._presence_sequences_lock = threading.Lock()
        # Per-conversation entries packed into the cluster presence payload
        # under the "conversations" map.  Keys are full presence keys of the
        # form ``conversation:{conversation_id}:{holmes_id}`` so observers can
        # distinguish cluster (``cluster:*``) and conversation (``conversation:*``)
        # signals by prefix, per the design spec.
        self._conversations: Dict[str, Dict[str, Any]] = {}
        self._conversations_lock = threading.Lock()
        # Debounced presence retrack.  Supabase Realtime rate-limits presence
        # broadcasts (currently 10/sec per client); tracking on every
        # conversation state change triggers the limiter and gets us
        # disconnected.  Instead we set a dirty flag, and the main loop
        # flushes one track() call per ``_PRESENCE_FLUSH_INTERVAL_SECONDS``
        # with the latest payload.
        self._presence_dirty = False
        self._presence_dirty_lock = threading.Lock()
        # Last payload successfully sent via track() — used to short-circuit
        # the flusher when nothing actually changed.
        self._last_flushed_payload: Optional[Dict[str, Any]] = None

    # ---- public ----

    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Clear lifecycle events so a restart (after stop()) runs cleanly.
        self._stop_event.clear()
        self._started.clear()
        self._loop = None
        self._client = None
        self._account_channel = None
        self._pg_channel = None
        self._connected = False
        self._last_auth_jwt = None
        self._last_flushed_payload = None
        with self._conversations_lock:
            self._conversations.clear()
        with self._presence_sequences_lock:
            self._presence_sequences.clear()
        self._thread = threading.Thread(
            target=self._thread_entry,
            daemon=True,
            name="realtime-manager",
        )
        self._thread.start()
        # Wait briefly for loop to start
        self._started.wait(timeout=5)

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
            except Exception:
                logging.exception("Error scheduling shutdown coro", exc_info=True)
        if self._thread:
            self._thread.join(timeout=5)

    def _is_newest_sequence(
        self, conversation_id: str, request_sequence: int, *, update: bool
    ) -> bool:
        """
        Check whether ``request_sequence`` is >= the highest we've seen for
        ``conversation_id``. When ``update=True``, advance the stored value
        on success.

        Returns True if this caller is the current or newer owner; False if
        a newer owner has already claimed the conversation.
        """
        with self._presence_sequences_lock:
            current = self._presence_sequences.get(conversation_id, -1)
            if request_sequence < current:
                return False
            if update:
                self._presence_sequences[conversation_id] = max(
                    current, request_sequence
                )
            return True

    def join_conversation_presence(
        self,
        conversation_id: str,
        request_sequence: int,
        status: str = "running",
    ) -> None:
        """Join a per-conversation presence channel to advertise heartbeat.

        ``request_sequence`` gates against stale workers: if a newer worker
        (higher ``request_sequence``) has already joined presence for this
        conversation, this call is ignored so the newer worker's presence
        isn't disrupted.

        ``status`` is included in the presence payload so Relay can distinguish
        queued (claimed but waiting for an executor slot) from running.
        """
        if not self._is_newest_sequence(
            conversation_id, request_sequence, update=True
        ):
            logging.info(
                "Skipping stale join_conversation_presence "
                "(conv=%s, request_sequence=%s < current)",
                conversation_id,
                request_sequence,
            )
            return
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._apply_conversation_entry(
                    conversation_id,
                    request_sequence=request_sequence,
                    status=status,
                ),
                self._loop,
            )

    def update_conversation_presence(
        self,
        conversation_id: str,
        request_sequence: int,
        status: str = "running",
    ) -> None:
        """Update the presence payload for an already-joined conversation channel."""
        if not self._is_newest_sequence(
            conversation_id, request_sequence, update=True
        ):
            logging.info(
                "Skipping stale update_conversation_presence "
                "(conv=%s, request_sequence=%s < current)",
                conversation_id,
                request_sequence,
            )
            return
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._apply_conversation_entry(
                    conversation_id,
                    request_sequence=request_sequence,
                    status=status,
                ),
                self._loop,
            )

    def leave_conversation_presence(
        self, conversation_id: str, request_sequence: int
    ) -> None:
        """Leave a per-conversation presence channel.

        Only leaves if ``request_sequence`` matches the highest we've seen —
        otherwise a stale worker cleaning up would tear down the newer
        worker's presence.
        """
        # Atomically check-and-pop under one lock acquisition to avoid
        # TOCTOU: a newer join_conversation_presence could land between a
        # separate check and pop, and we'd delete the newer entry.
        with self._presence_sequences_lock:
            current = self._presence_sequences.get(conversation_id, -1)
            if request_sequence < current:
                logging.info(
                    "Skipping stale leave_conversation_presence "
                    "(conv=%s, request_sequence=%s < current=%s)",
                    conversation_id,
                    request_sequence,
                    current,
                )
                return
            self._presence_sequences.pop(conversation_id, None)
        if not (self._loop and self._loop.is_running()):
            logging.warning(
                "leave_conversation_presence called but loop is not running (conv=%s)",
                conversation_id,
            )
            return
        logging.info(
            "Scheduling leave_conversation_presence for %s", conversation_id
        )
        asyncio.run_coroutine_threadsafe(
            self._remove_conversation_entry(conversation_id), self._loop
        )

    # ---- thread entry point ----

    def _thread_entry(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            logging.exception("Realtime manager thread crashed", exc_info=True)

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._started.set()
        try:
            await self._connect_and_subscribe()
            # Main loop drives periodic JWT refresh.  We sleep until the next
            # refresh is due rather than polling every second so CPU stays
            # idle between refreshes.
            refresh_interval = CONVERSATION_WORKER_AUTH_REFRESH_INTERVAL_SECONDS
            next_refresh_at = asyncio.get_running_loop().time() + refresh_interval
            while not self._stop_event.is_set():
                now = asyncio.get_running_loop().time()
                if now >= next_refresh_at:
                    await self._maybe_refresh_auth()
                    # Postpone the next refresh by the full interval to avoid
                    # a 0-sleep spin loop if _maybe_refresh_auth itself was
                    # slow enough to push ``now`` past the old target.
                    next_refresh_at = (
                        asyncio.get_running_loop().time() + refresh_interval
                    )
                # Coalesced presence retrack (debounced to avoid hitting the
                # Supabase Realtime presence rate limit).
                await self._flush_presence_if_dirty()
                # Wake up frequently enough to flush presence changes with
                # low latency, but cap to the refresh deadline.
                sleep_for = min(
                    _PRESENCE_FLUSH_INTERVAL_SECONDS,
                    max(0.01, next_refresh_at - asyncio.get_running_loop().time()),
                )
                await asyncio.sleep(sleep_for)
        except Exception:
            logging.exception("Error in realtime manager main loop", exc_info=True)
        finally:
            self._connected = False
            # Wake the worker so it falls back to polling
            try:
                self.on_new_pending()
            except Exception:
                logging.debug(
                    "on_new_pending callback failed during shutdown",
                    exc_info=True,
                )

    async def _maybe_refresh_auth(self) -> None:
        """
        Supabase access tokens expire (default 1h). The Supabase Python client
        auto-refreshes its stored session, but the realtime WebSocket doesn't
        pick up the new token automatically — we must re-call ``set_auth``
        with the current JWT. If we skip this, RLS-scoped Postgres Changes
        subscriptions silently stop delivering events once the original JWT
        expires.

        Called periodically from ``_run``. Cheap when the token is unchanged.
        """
        if not self._client:
            return
        try:
            session = self.dal.client.auth.get_session()  # type: ignore[attr-defined]
            if session is None:
                return
            new_jwt = session.access_token
            if not new_jwt or new_jwt == self._last_auth_jwt:
                return
            await self._client.set_auth(new_jwt)
            self._last_auth_jwt = new_jwt
            logging.debug("Refreshed realtime client auth token")
        except Exception:
            logging.exception("Failed to refresh realtime auth token", exc_info=True)

    async def _connect_and_subscribe(self) -> None:
        _install_proxy_patch_if_needed()

        # Supabase Realtime URL: sp.stg.robusta.dev -> wss://sp.stg.robusta.dev/realtime/v1/websocket
        store_url = self.dal.url.rstrip("/")
        if store_url.startswith("https://"):
            ws_url = "wss://" + store_url[len("https://"):]
        elif store_url.startswith("http://"):
            ws_url = "ws://" + store_url[len("http://"):]
        else:
            ws_url = store_url
        ws_url = f"{ws_url}/realtime/v1/websocket"

        # For Supabase Realtime, the initial connection is authenticated via the
        # anon apikey in the URL. RLS is then enforced by passing the user's JWT
        # via set_auth() after connecting.
        apikey = self.dal.api_key
        session = self.dal.client.auth.get_session()  # type: ignore[attr-defined]
        user_jwt = session.access_token if session else None
        if not user_jwt:
            logging.warning(
                "No Supabase session available during realtime connect; "
                "RLS-scoped subscriptions may not work until a token refresh"
            )

        self._client = AsyncRealtimeClient(
            url=ws_url,
            token=apikey,
            auto_reconnect=True,
        )
        await self._client.connect()
        if user_jwt:
            try:
                await self._client.set_auth(user_jwt)
                self._last_auth_jwt = user_jwt
            except Exception:
                logging.exception("Failed to set_auth on realtime client", exc_info=True)

        # Two separate channels so that a rate-limit or error on one
        # doesn't break the other:
        #   1. ``holmes:presence:{account_id}`` — per-conversation presence
        #      entries (liveness signal for Relay)
        #   2. ``holmes:pgchanges:{account_id}`` — Postgres Changes
        #      notifications that drive conversation claiming
        presence_topic = account_presence_topic(self.dal.account_id)
        pg_topic = pg_changes_topic(self.dal.account_id)
        self._account_channel = self._client.channel(
            presence_topic,
            {
                "config": {
                    "presence": {"enabled": True, "key": self.holmes_id},
                    "private": False,
                }
            },
        )
        self._pg_channel = self._client.channel(
            pg_topic,
            {"config": {"private": False}},
        )


        def _on_pg_change(payload: Dict[str, Any]) -> None:
            try:
                change = payload.get("data", {}) or {}
                logging.info(
                    "RealtimeManager: Postgres change notification: %s",
                    change.get("type"),
                )
                # Any change to Conversations for our account is a signal to
                # try claiming.  We intentionally don't pre-filter on
                # cluster_id or status here: Supabase Realtime payloads
                # occasionally omit columns (schema changes, partial
                # updates), and ``claim_conversations`` is a cheap RPC that
                # already filters server-side to pending rows for our
                # cluster.  False positives cost nothing.
                self.on_new_pending()
            except Exception:
                logging.exception("Error in realtime pg change callback", exc_info=True)

        # Postgres Changes subscription on its OWN channel so that presence
        # rate limits on the presence channel don't kill our claim pipeline.
        account_id_filter = f"account_id=eq.{self.dal.account_id}"
        self._pg_channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table=CONVERSATIONS_TABLE,
            filter=account_id_filter,
            callback=_on_pg_change,
        )
        self._pg_channel.on_postgres_changes(
            event="UPDATE",
            schema="public",
            table=CONVERSATIONS_TABLE,
            filter=account_id_filter,
            callback=_on_pg_change,
        )

        pg_subscribed = asyncio.Event()

        def _on_pg_subscribe_cb(status: Any, err: Optional[Exception] = None) -> None:
            """pg-changes subscribe callback — this is what drives
            ``self._connected`` because the claim-loop's realtime-vs-poll
            decision depends on whether we're getting pg-changes events."""
            logging.info(
                "PG changes subscribe status=%s err=%s",
                status,
                err,
            )
            status_str = str(status).upper()
            if "SUBSCRIBED" in status_str:
                self._connected = True
                pg_subscribed.set()
                # Trigger a claim to cover any missed events during subscription
                # setup or reconnects
                try:
                    self.on_new_pending()
                except Exception:
                    logging.debug(
                        "on_new_pending callback failed in pg subscribe",
                        exc_info=True,
                    )
            elif any(
                s in status_str for s in ("CHANNEL_ERROR", "CLOSED", "TIMED_OUT")
            ):
                self._connected = False
                # Wake up the claim loop so it falls back to polling
                try:
                    self.on_new_pending()
                except Exception:
                    logging.debug(
                        "on_new_pending callback failed in pg error handler",
                        exc_info=True,
                    )

        def _on_presence_subscribe_cb(
            status: Any, err: Optional[Exception] = None
        ) -> None:
            """Presence subscribe callback — drives track()-readiness.
            Does NOT modify ``self._connected``: the claim loop doesn't
            depend on presence."""
            logging.info(
                "Account presence subscribe status=%s err=%s",
                status,
                err,
            )

        await self._pg_channel.subscribe(_on_pg_subscribe_cb)
        await self._account_channel.subscribe(_on_presence_subscribe_cb)

        # Wait for pg-changes SUBSCRIBED so the first claim pass is covered
        # by realtime events rather than the slower polling fallback.
        try:
            await asyncio.wait_for(pg_subscribed.wait(), timeout=5)
        except asyncio.TimeoutError:
            logging.warning("Timed out waiting for pg-changes subscribe ack")

        # No initial track() call — presence is only advertised once there
        # are active conversations.  The debounced flusher in _run() will
        # call track() when the first conversation is claimed.

        logging.info(
            "RealtimeManager connected: presence=%s pg_changes=%s",
            presence_topic,
            pg_topic,
        )

    def _build_presence_payload(self) -> Dict[str, Any]:
        """
        Build the presence payload for the account channel.

        The Supabase Realtime presence protocol allows one entry per client
        per channel, so we pack all per-conversation entries into a single
        payload.  Observers (Relay/support) iterate the ``conversations``
        map whose keys use the ``conversation:{conversation_id}:{holmes_id}``
        form — matching the "presence key" vocabulary of the design spec.
        """
        with self._conversations_lock:
            conversations = {k: dict(v) for k, v in self._conversations.items()}
        return {
            "holmes_id": self.holmes_id,
            "cluster_id": self.dal.cluster,
            "version": get_version(),
            "active_conversations": len(conversations),
            "conversations": conversations,
        }

    def _mark_presence_dirty(self) -> None:
        """Flag that the presence payload has changed and needs to be
        re-broadcast by the periodic flusher in ``_run``."""
        with self._presence_dirty_lock:
            self._presence_dirty = True

    async def _flush_presence_if_dirty(self) -> None:
        """Called from the main event loop at most once per
        ``_PRESENCE_FLUSH_INTERVAL_SECONDS`` — broadcasts the current
        payload if anything changed since the last flush."""
        with self._presence_dirty_lock:
            if not self._presence_dirty:
                return
            self._presence_dirty = False
        if self._account_channel is None:
            return
        payload = self._build_presence_payload()
        # Skip tracking if the payload is functionally identical to the last
        # one we sent.  Prevents generating needless presence broadcasts when
        # rapid state flips average out (e.g. a conversation being added and
        # removed within the flush window).
        if payload == self._last_flushed_payload:
            return
        # Don't advertise an empty presence — only track when we have active
        # conversations.  When the last conversation leaves, untrack to remove
        # our entry from the channel entirely.
        try:
            if payload["active_conversations"] > 0:
                await self._account_channel.track(payload)
            elif self._last_flushed_payload is not None:
                # Had conversations before, now empty → untrack
                await self._account_channel.untrack()
            self._last_flushed_payload = payload
        except Exception:
            logging.exception(
                "Failed to re-track account presence state", exc_info=True
            )
            # Put the dirty flag back so we retry on the next tick
            with self._presence_dirty_lock:
                self._presence_dirty = True

    async def _apply_conversation_entry(
        self,
        conversation_id: str,
        request_sequence: int,
        status: str,
    ) -> None:
        """Add or update a conversation entry on the account presence payload."""
        key = conversation_presence_key(conversation_id, self.holmes_id)
        with self._conversations_lock:
            existing = self._conversations.get(key)
            self._conversations[key] = {
                "type": "conversation",
                "holmes_id": self.holmes_id,
                "conversation_id": conversation_id,
                "request_sequence": request_sequence,
                "status": status,
                "started_at": existing["started_at"]
                if existing and "started_at" in existing
                else datetime.now(timezone.utc).isoformat(),
                "version": get_version(),
            }
        self._mark_presence_dirty()
        logging.info(
            "Queued conversation presence entry (conv=%s, request_sequence=%s, status=%s)",
            conversation_id,
            request_sequence,
            status,
        )

    async def _remove_conversation_entry(self, conversation_id: str) -> None:
        """Remove a conversation entry from the account presence payload."""
        key = conversation_presence_key(conversation_id, self.holmes_id)
        with self._conversations_lock:
            self._conversations.pop(key, None)
        self._mark_presence_dirty()
        logging.info(
            "Queued removal of conversation presence entry (conv=%s)",
            conversation_id,
        )

    async def _shutdown_async(self) -> None:
        self._connected = False
        try:
            if self._client:
                await self._client.close()
        except Exception:
            logging.exception("Error shutting down realtime client", exc_info=True)
