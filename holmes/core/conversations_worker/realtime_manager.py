import asyncio
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, Optional, Set

from holmes.common.env_vars import (
    CONVERSATION_WORKER_REALTIME_RECONNECT_MAX_SECONDS,
    STORE_API_KEY,
    STORE_URL,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class RealtimeManager:
    """Manages Supabase Realtime connections in a dedicated background asyncio thread.

    Responsibilities:
      - Cluster Presence channel: advertise this Holmes instance as available
      - Postgres Changes subscription on ``Conversations`` table: notify when
        new pending conversations appear
      - Per-conversation Presence channels: heartbeat while processing

    Communication with the sync ConversationWorker is via a thread-safe
    ``queue.Queue`` (notifications) and ``threading.Event`` (polling fallback).
    """

    def __init__(
        self,
        account_id: str,
        cluster_id: str,
        holmes_id: str,
        access_token: str,
        notification_queue: "queue.Queue[str]",
    ):
        self._account_id = account_id
        self._cluster_id = cluster_id
        self._holmes_id = holmes_id
        self._access_token = access_token
        self._notification_queue = notification_queue

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None  # AsyncRealtimeClient, set in _run()
        self._cluster_channel = None  # AsyncRealtimeChannel
        self._pending_channel = None  # AsyncRealtimeChannel for postgres changes
        self._conversation_channels: Dict[str, object] = {}  # conversation_id -> channel

        self._running = False
        self._connected = False
        self._polling_fallback = threading.Event()
        self._active_conversations: Set[str] = set()

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background asyncio thread for Realtime connections."""
        if self._running:
            logger.warning("RealtimeManager already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="realtime-manager"
        )
        self._thread.start()
        logger.info("RealtimeManager started")

    def stop(self) -> None:
        """Gracefully shut down the Realtime connection and background thread."""
        self._running = False
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("RealtimeManager stopped")

    @property
    def polling_fallback_active(self) -> bool:
        """True when the Realtime connection has been down long enough to warrant polling."""
        return self._polling_fallback.is_set()

    # ── Per-conversation Presence ─────────────────────────────────

    def join_conversation_presence(self, conversation_id: str) -> None:
        """Join a per-conversation Presence channel (heartbeat signal)."""
        self._active_conversations.add(conversation_id)
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._join_conversation_channel(conversation_id), self._loop
            )

    def leave_conversation_presence(self, conversation_id: str) -> None:
        """Leave a per-conversation Presence channel on completion."""
        self._active_conversations.discard(conversation_id)
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._leave_conversation_channel(conversation_id), self._loop
            )

    def update_conversation_presence(self, conversation_id: str) -> None:
        """Update presence state for a conversation (heartbeat tick)."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._update_conversation_channel(conversation_id), self._loop
            )

    # ── Background thread entry point ─────────────────────────────

    def _run(self) -> None:
        """Entry point for the background asyncio thread.  Handles
        connection, reconnection with exponential backoff, and teardown."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        except Exception:
            logger.exception("RealtimeManager background loop crashed")
        finally:
            self._loop.close()

    async def _connect_loop(self) -> None:
        """Connect to Supabase Realtime with exponential backoff on failure."""
        backoff = 1
        disconnect_since: Optional[float] = None

        while self._running:
            try:
                await self._connect()
                self._connected = True
                self._polling_fallback.clear()
                disconnect_since = None
                backoff = 1
                logger.info("Realtime connected, entering listen loop")

                # Notify the worker to claim any pending conversations
                self._notification_queue.put("reconnected")

                await self._listen()
            except Exception:
                logger.exception("Realtime connection error")
                self._connected = False

                if disconnect_since is None:
                    disconnect_since = time.monotonic()

                elapsed = time.monotonic() - disconnect_since
                if elapsed > 60:
                    if not self._polling_fallback.is_set():
                        logger.warning(
                            "Realtime disconnected for >60s, activating polling fallback"
                        )
                        self._polling_fallback.set()

                await asyncio.sleep(backoff)
                backoff = min(
                    backoff * 2,
                    CONVERSATION_WORKER_REALTIME_RECONNECT_MAX_SECONDS,
                )

    async def _connect(self) -> None:
        """Create and connect the AsyncRealtimeClient, set up channels."""
        from realtime import AsyncRealtimeClient

        url = STORE_URL.rstrip("/")
        # Convert REST URL to Realtime WebSocket URL
        ws_url = url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/realtime/v1"

        self._client = AsyncRealtimeClient(
            url=ws_url,
            token=self._access_token,
            params={"apikey": STORE_API_KEY},
        )
        self._client.set_auth(self._access_token)
        await self._client.connect()
        await self._setup_cluster_presence()
        await self._setup_pending_subscription()

        # Rejoin any active conversation presence channels after reconnect
        for conversation_id in list(self._active_conversations):
            await self._join_conversation_channel(conversation_id)

    async def _listen(self) -> None:
        """Block in the listen loop until disconnected or stopped."""
        try:
            await self._client.listen()
        except Exception:
            if self._running:
                raise

    async def _shutdown(self) -> None:
        """Clean up all channels and close the client."""
        try:
            if self._cluster_channel:
                await self._cluster_channel.untrack()
                await self._cluster_channel.unsubscribe()

            if self._pending_channel:
                await self._pending_channel.unsubscribe()

            for conv_id in list(self._conversation_channels):
                await self._leave_conversation_channel(conv_id)

            if self._client:
                await self._client.close()
        except Exception:
            logger.exception("Error during RealtimeManager shutdown")

    # ── Cluster Presence ──────────────────────────────────────────

    async def _setup_cluster_presence(self) -> None:
        """Join the per-cluster Presence channel to advertise this Holmes instance."""
        from holmes import get_version

        channel_name = f"holmes:cluster:{self._account_id}:{self._cluster_id}"
        self._cluster_channel = self._client.channel(channel_name)

        def on_subscribe(status, err):
            if err:
                logger.error("Cluster presence subscribe error: %s", err)
            else:
                logger.info("Cluster presence subscribed: %s", status)

        self._cluster_channel.subscribe(on_subscribe)
        await self._cluster_channel.track(
            {
                "holmes_id": self._holmes_id,
                "version": get_version(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "active_conversations": len(self._active_conversations),
            }
        )

    # ── Postgres Changes subscription ─────────────────────────────

    async def _setup_pending_subscription(self) -> None:
        """Subscribe to INSERT/UPDATE on Conversations for pending status."""
        from realtime.types import RealtimePostgresChangesListenEvent

        channel_name = f"holmes:pending:{self._account_id}:{self._cluster_id}"
        self._pending_channel = self._client.channel(channel_name)

        pending_filter = (
            f"account_id=eq.{self._account_id}"
            f"&cluster_id=eq.{self._cluster_id}"
            f"&status=eq.pending"
        )

        for event in (
            RealtimePostgresChangesListenEvent.Insert,
            RealtimePostgresChangesListenEvent.Update,
        ):
            self._pending_channel.on_postgres_changes(
                event=event,
                schema="public",
                table="Conversations",
                filter=pending_filter,
                callback=self._on_pending_conversation,
            )

        def on_subscribe(status, err):
            if err:
                logger.error("Pending subscription error: %s", err)
            else:
                logger.info("Pending subscription active: %s", status)

        self._pending_channel.subscribe(on_subscribe)

    def _on_pending_conversation(self, payload) -> None:
        """Callback when a pending conversation appears or is updated."""
        logger.debug("Received pending conversation notification: %s", payload)
        try:
            self._notification_queue.put_nowait("pending_conversation")
        except queue.Full:
            pass  # Worker will pick it up on next poll cycle

    # ── Per-conversation Presence channels ────────────────────────

    async def _join_conversation_channel(self, conversation_id: str) -> None:
        """Join the per-conversation Presence channel for heartbeat."""
        if not self._client or conversation_id in self._conversation_channels:
            return

        from holmes import get_version

        channel_name = f"holmes:conversation:{conversation_id}"
        channel = self._client.channel(channel_name)

        def on_subscribe(status, err):
            if err:
                logger.error(
                    "Conversation presence subscribe error for %s: %s",
                    conversation_id,
                    err,
                )

        channel.subscribe(on_subscribe)
        await channel.track(
            {
                "holmes_id": self._holmes_id,
                "version": get_version(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        self._conversation_channels[conversation_id] = channel

    async def _leave_conversation_channel(self, conversation_id: str) -> None:
        """Leave the per-conversation Presence channel."""
        channel = self._conversation_channels.pop(conversation_id, None)
        if channel:
            try:
                await channel.untrack()
                await channel.unsubscribe()
            except Exception:
                logger.warning(
                    "Error leaving conversation presence for %s",
                    conversation_id,
                    exc_info=True,
                )

    async def _update_conversation_channel(self, conversation_id: str) -> None:
        """Update presence state for heartbeat."""
        channel = self._conversation_channels.get(conversation_id)
        if channel:
            try:
                await channel.track(
                    {
                        "holmes_id": self._holmes_id,
                        "updated_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    }
                )
            except Exception:
                logger.warning(
                    "Error updating conversation presence for %s",
                    conversation_id,
                    exc_info=True,
                )
