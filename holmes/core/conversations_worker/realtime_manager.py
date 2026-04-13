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
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from holmes import get_version

if TYPE_CHECKING:
    from holmes.core.supabase_dal import SupabaseDal


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

    # ---- public ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
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

    def join_conversation_presence(self, conversation_id: str) -> None:
        """Join a per-conversation presence channel to advertise heartbeat."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._join_conversation_channel(conversation_id), self._loop
            )

    def leave_conversation_presence(self, conversation_id: str) -> None:
        if self._loop and self._loop.is_running():
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
            while not self._stop_event.is_set():
                await asyncio.sleep(1)
        except Exception:
            logging.exception("Error in realtime manager main loop", exc_info=True)

    async def _connect_and_subscribe(self) -> None:
        from realtime._async.client import AsyncRealtimeClient

        # Supabase Realtime URL: sp.stg.robusta.dev -> wss://sp.stg.robusta.dev/realtime/v1/websocket
        store_url = self.dal.url.rstrip("/")
        if store_url.startswith("https://"):
            ws_url = "wss://" + store_url[len("https://"):]
        elif store_url.startswith("http://"):
            ws_url = "ws://" + store_url[len("http://"):]
        else:
            ws_url = store_url
        ws_url = f"{ws_url}/realtime/v1/websocket"

        # Use the user's JWT for auth. SupabaseDal stored it via set_session.
        token = self.dal.client.auth.get_session().access_token  # type: ignore[attr-defined]

        self._client = AsyncRealtimeClient(
            url=ws_url,
            token=token,
            auto_reconnect=True,
        )
        await self._client.connect()

        # 1. Cluster-level Presence
        topic = f"holmes:cluster:{self.dal.account_id}:{self.dal.cluster}"
        self._cluster_channel = self._client.channel(topic)

        def _on_pg_change(payload: Dict[str, Any]) -> None:
            try:
                logging.info("RealtimeManager: Postgres change notification: %s", payload.get("data", {}).get("type"))
                self.on_new_pending()
            except Exception:
                logging.exception("Error in realtime pg change callback", exc_info=True)

        # Subscribe to Postgres Changes on Conversations for this cluster
        account_id_filter = f"account_id=eq.{self.dal.account_id}"
        self._cluster_channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table="Conversations",
            filter=account_id_filter,
            callback=_on_pg_change,
        )
        self._cluster_channel.on_postgres_changes(
            event="UPDATE",
            schema="public",
            table="Conversations",
            filter=account_id_filter,
            callback=_on_pg_change,
        )

        def _on_subscribe_cb(status, err=None) -> None:
            logging.info(
                "RealtimeManager subscribe status=%s err=%s",
                status,
                err,
            )
            # Trigger a claim to cover any missed events during subscription setup
            try:
                self.on_new_pending()
            except Exception:
                pass

        await self._cluster_channel.subscribe(_on_subscribe_cb)

        # Advertise presence
        presence_state = {
            "holmes_id": self.holmes_id,
            "version": get_version(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._cluster_channel.track(presence_state)
        except Exception:
            logging.exception("Failed to track presence state", exc_info=True)

        logging.info(
            "RealtimeManager connected and subscribed to topic=%s", topic
        )

    async def _join_conversation_channel(self, conversation_id: str) -> None:
        if not self._client:
            return
        try:
            topic = f"holmes:conversation:{conversation_id}"
            ch = self._client.channel(topic)
            await ch.subscribe()
            await ch.track(
                {
                    "holmes_id": self.holmes_id,
                    "version": get_version(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            logging.exception(
                "Failed to join conversation presence %s", conversation_id, exc_info=True
            )

    async def _leave_conversation_channel(self, conversation_id: str) -> None:
        if not self._client:
            return
        topic = f"holmes:conversation:{conversation_id}"
        for ch in list(self._client.channels):  # type: ignore[attr-defined]
            try:
                if getattr(ch, "topic", None) == topic:
                    await ch.unsubscribe()
                    break
            except Exception:
                pass

    async def _shutdown_async(self) -> None:
        try:
            if self._client:
                await self._client.close()
        except Exception:
            logging.exception("Error shutting down realtime client", exc_info=True)
