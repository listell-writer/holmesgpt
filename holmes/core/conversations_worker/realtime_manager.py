"""
Realtime manager for the ConversationWorker.

Runs an asyncio event loop in a background daemon thread. Manages:
 - Postgres Changes subscription on Conversations table: triggers a claim
   when new pending rows appear for this cluster

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

from holmes.common.env_vars import (
    CONVERSATION_WORKER_AUTH_REFRESH_INTERVAL_SECONDS,
)
from holmes.core.supabase_dal import CONVERSATIONS_TABLE

if TYPE_CHECKING:
    from holmes.core.supabase_dal import SupabaseDal


# ---- channel topic helpers ----


def pg_changes_topic(account_id: str) -> str:
    """Per-account channel for Conversations Postgres Changes.

    Filtered server-side by ``account_id``; the callback does NOT further
    filter on ``cluster_id`` because ``claim_conversations`` already does
    that in the RPC.
    """
    return f"holmes:pgchanges:{account_id}"


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
        self._pg_channel = None
        # True once the pg-changes channel is SUBSCRIBED (drives
        # is_connected() and the claim-loop's realtime-vs-poll decision).
        self._connected = False
        # Last JWT we pushed to the realtime client via set_auth. Used to skip
        # the network call on the common case where the token hasn't rotated.
        self._last_auth_jwt: Optional[str] = None

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
        self._pg_channel = None
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
            # Main loop drives periodic JWT refresh.
            refresh_interval = CONVERSATION_WORKER_AUTH_REFRESH_INTERVAL_SECONDS
            next_refresh_at = asyncio.get_running_loop().time() + refresh_interval
            while not self._stop_event.is_set():
                now = asyncio.get_running_loop().time()
                if now >= next_refresh_at:
                    await self._maybe_refresh_auth()
                    next_refresh_at = (
                        asyncio.get_running_loop().time() + refresh_interval
                    )
                sleep_for = max(
                    0.01,
                    next_refresh_at - asyncio.get_running_loop().time(),
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

        # Supabase Realtime URL
        store_url = self.dal.url.rstrip("/")
        if store_url.startswith("https://"):
            ws_url = "wss://" + store_url[len("https://"):]
        elif store_url.startswith("http://"):
            ws_url = "ws://" + store_url[len("http://"):]
        else:
            ws_url = store_url
        ws_url = f"{ws_url}/realtime/v1/websocket"

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

        # Postgres Changes channel — listens for Conversations table changes
        # filtered by account_id. The callback triggers a claim attempt.
        pg_topic = pg_changes_topic(self.dal.account_id)
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
                # occasionally omit columns, and ``claim_conversations`` is a
                # cheap RPC that already filters server-side.
                self.on_new_pending()
            except Exception:
                logging.exception("Error in realtime pg change callback", exc_info=True)

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
            logging.info(
                "PG changes subscribe status=%s err=%s",
                status,
                err,
            )
            status_str = str(status).upper()
            if "SUBSCRIBED" in status_str:
                self._connected = True
                pg_subscribed.set()
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
                try:
                    self.on_new_pending()
                except Exception:
                    logging.debug(
                        "on_new_pending callback failed in pg error handler",
                        exc_info=True,
                    )

        await self._pg_channel.subscribe(_on_pg_subscribe_cb)
        try:
            await asyncio.wait_for(pg_subscribed.wait(), timeout=5)
        except asyncio.TimeoutError:
            logging.warning("Timed out waiting for pg-changes subscribe ack")

        logging.info(
            "RealtimeManager connected: pg_changes=%s", pg_topic
        )

    async def _shutdown_async(self) -> None:
        self._connected = False
        try:
            if self._client:
                await self._client.close()
        except Exception:
            logging.exception("Error shutting down realtime client", exc_info=True)
