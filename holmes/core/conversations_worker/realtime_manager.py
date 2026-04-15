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


def cluster_presence_topic(account_id: str, cluster_id: str) -> str:
    """Topic for the cluster-level presence / pg-changes channel."""
    return f"holmes:cluster:{account_id}:{cluster_id}"


def conversation_presence_topic(conversation_id: str) -> str:
    """Topic for the per-conversation presence channel."""
    return f"holmes:conversation:{conversation_id}"


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
    if p.username:
        proxy_connect_url = (
            f"http://{p.username}:{p.password}@{p.hostname}:{p.port}"
        )
    else:
        proxy_connect_url = f"http://{p.hostname}:{p.port}"

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
        self._cluster_channel = None
        # True once the cluster channel is SUBSCRIBED; flips to False if the
        # realtime thread crashes or the channel reports ERROR/CLOSED.
        self._connected = False
        # Last JWT we pushed to the realtime client via set_auth. Used to skip
        # the network call on the common case where the token hasn't rotated.
        self._last_auth_jwt: Optional[str] = None
        # Per-conversation highest request_sequence that has joined presence.
        # Older-sequence join / leave / update calls are ignored so a stale
        # worker can't disrupt the presence owned by the newest worker.
        self._presence_sequences: Dict[str, int] = {}
        self._presence_sequences_lock = threading.Lock()

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
        self._cluster_channel = None
        self._connected = False
        self._last_auth_jwt = None
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
                self._join_conversation_channel(
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
                self._update_conversation_channel(
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
        # Check without updating: we only care whether this caller is still
        # the current owner. A newer sequence having joined means the current
        # channel belongs to them, so don't disrupt it.
        if not self._is_newest_sequence(
            conversation_id, request_sequence, update=False
        ):
            logging.info(
                "Skipping stale leave_conversation_presence "
                "(conv=%s, request_sequence=%s < current)",
                conversation_id,
                request_sequence,
            )
            return
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
            self._leave_conversation_channel(conversation_id), self._loop
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
                sleep_for = max(0.01, next_refresh_at - asyncio.get_running_loop().time())
                await asyncio.sleep(sleep_for)
        except Exception:
            logging.exception("Error in realtime manager main loop", exc_info=True)
        finally:
            self._connected = False
            # Wake the worker so it falls back to polling
            try:
                self.on_new_pending()
            except Exception:
                pass

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

        # 1. Cluster-level Presence
        topic = cluster_presence_topic(self.dal.account_id, self.dal.cluster)
        self._cluster_channel = self._client.channel(
            topic,
            {
                "config": {
                    "presence": {"enabled": True, "key": self.holmes_id},
                    "private": False,
                }
            },
        )

        def _on_pg_change(payload: Dict[str, Any]) -> None:
            try:
                change = payload.get("data", {}) or {}
                logging.info(
                    "RealtimeManager: Postgres change notification: %s",
                    change.get("type"),
                )
                # Supabase Realtime only supports a single-column filter per
                # subscription, so we filter by account_id on the server and
                # narrow to our cluster + pending rows here. For an UPDATE
                # payload the new row is under "record"; for INSERT it's also
                # "record". Older realtime payload shapes used "new".
                row = change.get("record") or change.get("new") or {}
                if row.get("cluster_id") != self.dal.cluster:
                    return
                if row.get("status") != "pending":
                    return
                self.on_new_pending()
            except Exception:
                logging.exception("Error in realtime pg change callback", exc_info=True)

        # Subscribe to Postgres Changes on Conversations for this cluster.
        # Realtime filters only allow a single operation, so we filter on
        # account_id here and do the cluster/status check in _on_pg_change.
        account_id_filter = f"account_id=eq.{self.dal.account_id}"
        self._cluster_channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table=CONVERSATIONS_TABLE,
            filter=account_id_filter,
            callback=_on_pg_change,
        )
        self._cluster_channel.on_postgres_changes(
            event="UPDATE",
            schema="public",
            table=CONVERSATIONS_TABLE,
            filter=account_id_filter,
            callback=_on_pg_change,
        )

        subscribed = asyncio.Event()

        def _on_subscribe_cb(status: Any, err: Optional[Exception] = None) -> None:
            logging.info(
                "RealtimeManager subscribe status=%s err=%s",
                status,
                err,
            )
            status_str = str(status).upper()
            if "SUBSCRIBED" in status_str:
                self._connected = True
                subscribed.set()
                # Trigger a claim to cover any missed events during subscription
                # setup or reconnects
                try:
                    self.on_new_pending()
                except Exception:
                    pass
            elif any(
                s in status_str for s in ("CHANNEL_ERROR", "CLOSED", "TIMED_OUT")
            ):
                self._connected = False
                # Wake up the claim loop so it falls back to polling
                try:
                    self.on_new_pending()
                except Exception:
                    pass

        # Channel state changes (CHANNEL_ERROR, CLOSED, SUBSCRIBED) are surfaced
        # via the subscribe callback above. realtime-py's on_error / on_close
        # are internal lifecycle methods, not callback registration points.
        await self._cluster_channel.subscribe(_on_subscribe_cb)
        # Wait for SUBSCRIBED before tracking presence — otherwise the track
        # message is dropped by the server (silently).
        try:
            await asyncio.wait_for(subscribed.wait(), timeout=5)
        except asyncio.TimeoutError:
            logging.warning("Timed out waiting for cluster presence subscribe ack")

        # Advertise presence
        presence_state = {
            "holmes_id": self.holmes_id,
            "version": get_version(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "active_conversations": 0,
        }
        try:
            await self._cluster_channel.track(presence_state)
            logging.info(
                "Advertised cluster presence: holmes_id=%s on %s",
                self.holmes_id,
                topic,
            )
        except Exception:
            logging.exception("Failed to track presence state", exc_info=True)

        logging.info(
            "RealtimeManager connected and subscribed to topic=%s", topic
        )

    async def _join_conversation_channel(
        self,
        conversation_id: str,
        request_sequence: int,
        status: str = "running",
    ) -> None:
        if not self._client:
            return
        try:
            topic = conversation_presence_topic(conversation_id)
            ch = self._client.channel(
                topic,
                {
                    "config": {
                        "presence": {"enabled": True, "key": self.holmes_id},
                        "private": False,
                    }
                },
            )
            subscribed = asyncio.Event()

            def _on_sub(sub_status: Any, err: Optional[Exception] = None) -> None:
                logging.debug(
                    "Conversation presence subscribe status=%s err=%s conv=%s",
                    sub_status,
                    err,
                    conversation_id,
                )
                subscribed.set()

            await ch.subscribe(_on_sub)
            # Wait briefly for subscribe ack before tracking presence
            try:
                await asyncio.wait_for(subscribed.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            await ch.track(
                {
                    "holmes_id": self.holmes_id,
                    "version": get_version(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "conversation_id": conversation_id,
                    "request_sequence": request_sequence,
                    "status": status,
                }
            )
            logging.info(
                "Joined conversation presence channel for %s (request_sequence=%s, status=%s)",
                conversation_id,
                request_sequence,
                status,
            )
        except Exception:
            logging.exception(
                "Failed to join conversation presence %s",
                conversation_id,
                exc_info=True,
            )

    async def _update_conversation_channel(
        self,
        conversation_id: str,
        request_sequence: int,
        status: str = "running",
    ) -> None:
        """Re-track presence with an updated status on an existing channel."""
        if not self._client:
            return
        topic = conversation_presence_topic(conversation_id)
        ch = self._client.channels.get(topic) or self._client.channels.get(  # type: ignore[attr-defined]
            f"realtime:{topic}"
        )
        if ch is None:
            logging.warning(
                "No conversation presence channel to update for %s", conversation_id
            )
            return
        try:
            await ch.track(
                {
                    "holmes_id": self.holmes_id,
                    "version": get_version(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "conversation_id": conversation_id,
                    "request_sequence": request_sequence,
                    "status": status,
                }
            )
            logging.debug(
                "Updated conversation presence for %s to status=%s (request_sequence=%s)",
                conversation_id,
                status,
                request_sequence,
            )
        except Exception:
            logging.exception(
                "Failed to update conversation presence %s",
                conversation_id,
                exc_info=True,
            )

    async def _leave_conversation_channel(self, conversation_id: str) -> None:
        logging.info(
            "_leave_conversation_channel invoked for %s", conversation_id
        )
        if not self._client:
            return
        topic = conversation_presence_topic(conversation_id)
        # realtime-py prefixes the channel topic with "realtime:" internally
        ch = self._client.channels.get(topic) or self._client.channels.get(  # type: ignore[attr-defined]
            f"realtime:{topic}"
        )
        if ch is None:
            logging.warning(
                "No conversation presence channel to leave for %s (channels=%s)",
                conversation_id,
                list(self._client.channels.keys()),  # type: ignore[attr-defined]
            )
            return
        try:
            # untrack removes our presence entry; unsubscribe leaves the channel
            try:
                await ch.untrack()
            except Exception:
                logging.exception(
                    "Error calling untrack on conv presence %s", conversation_id,
                    exc_info=True,
                )
            await self._client.remove_channel(ch)
            logging.info(
                "Left conversation presence channel for %s", conversation_id
            )
        except Exception:
            logging.exception(
                "Failed to leave conversation presence %s",
                conversation_id,
                exc_info=True,
            )

    async def _shutdown_async(self) -> None:
        self._connected = False
        try:
            if self._client:
                await self._client.close()
        except Exception:
            logging.exception("Error shutting down realtime client", exc_info=True)
