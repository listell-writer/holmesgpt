import asyncio
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Set

from holmes import get_version
from holmes.common.env_vars import CONVERSATION_WORKER_REALTIME_RECONNECT_MAX_SECONDS

if TYPE_CHECKING:
    from realtime._async.client import AsyncRealtimeClient

logger = logging.getLogger(__name__)


class RealtimeManager:
    """Manages Supabase Realtime Presence and Postgres Changes subscriptions.

    Runs in a dedicated daemon thread with its own asyncio event loop.
    Bridges async Realtime events to the sync ConversationWorker via
    :class:`queue.Queue`.
    """

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        account_id: str,
        cluster_id: str,
        holmes_id: str,
        notification_queue: queue.Queue,
    ):
        self._supabase_url = supabase_url
        self._supabase_key = supabase_key
        self._account_id = account_id
        self._cluster_id = cluster_id
        self._holmes_id = holmes_id
        self._notification_queue = notification_queue

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional["AsyncRealtimeClient"] = None
        self._running = False
        self._connected = threading.Event()
        self._polling_fallback = threading.Event()

        # Track active per-conversation presence channels
        self._conversation_channels: Dict[str, Any] = {}
        self._active_conversations: Set[str] = set()

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background thread and connect to Supabase Realtime."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_event_loop, daemon=True, name="realtime-manager"
        )
        self._thread.start()
        logger.info("RealtimeManager thread started")

    def stop(self) -> None:
        """Disconnect from Supabase Realtime and stop the background thread."""
        self._running = False
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("RealtimeManager stopped")

    @property
    def is_polling_fallback(self) -> bool:
        """True when the Realtime connection is down and polling should be used."""
        return self._polling_fallback.is_set()

    # ── Per-conversation Presence ────────────────────────────────────

    def join_conversation_presence(self, conversation_id: str) -> None:
        """Join the per-conversation Presence channel (heartbeat signal)."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._join_conversation_presence(conversation_id), self._loop
            )

    def leave_conversation_presence(self, conversation_id: str) -> None:
        """Leave the per-conversation Presence channel."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._leave_conversation_presence(conversation_id), self._loop
            )

    def update_conversation_presence(self, conversation_id: str) -> None:
        """Update presence state on the per-conversation channel (heartbeat)."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._update_conversation_presence(conversation_id), self._loop
            )

    # ── Background thread entry point ────────────────────────────────

    def _run_event_loop(self) -> None:
        """Entry point for the background daemon thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            logger.exception("RealtimeManager event loop crashed")
        finally:
            self._loop.close()

    async def _main(self) -> None:
        """Main async loop: connect, subscribe, listen, reconnect."""
        backoff = 1.0
        max_backoff = CONVERSATION_WORKER_REALTIME_RECONNECT_MAX_SECONDS
        disconnect_start: Optional[float] = None

        while self._running:
            try:
                await self._connect_and_subscribe()
                self._connected.set()
                self._polling_fallback.clear()
                disconnect_start = None
                backoff = 1.0
                logger.info("RealtimeManager connected and subscribed")

                # Listen blocks until disconnection
                await self._client.listen()  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning("Realtime connection lost: %s", exc)
            finally:
                self._connected.clear()

            if not self._running:
                break

            # Track how long we've been disconnected
            if disconnect_start is None:
                disconnect_start = time.monotonic()
            elapsed = time.monotonic() - disconnect_start
            if elapsed > 60:
                self._polling_fallback.set()

            logger.info(
                "Reconnecting in %.1fs (disconnected %.0fs)", backoff, elapsed
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _connect_and_subscribe(self) -> None:
        """Create a fresh Realtime client, join cluster Presence, and subscribe."""
        from realtime._async.client import AsyncRealtimeClient

        # Build the Realtime WebSocket URL
        realtime_url = self._supabase_url.replace("http", "ws") + "/realtime/v1"

        self._client = AsyncRealtimeClient(
            url=realtime_url,
            token=self._supabase_key,
            auto_reconnect=False,  # We handle reconnection ourselves
        )
        await self._client.connect()

        # 1. Subscribe to cluster Presence channel
        await self._setup_cluster_presence()

        # 2. Subscribe to Postgres Changes on Conversations table
        await self._setup_pending_conversations_subscription()

    async def _setup_cluster_presence(self) -> None:
        """Join the per-cluster Presence channel to advertise this Holmes instance."""
        topic = f"holmes:cluster:{self._account_id}:{self._cluster_id}"
        channel = self._client.channel(topic)  # type: ignore[union-attr]
        await channel.subscribe()
        await channel.track(
            {
                "holmes_id": self._holmes_id,
                "version": get_version(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "active_conversations": len(self._active_conversations),
            }
        )
        logger.info("Joined cluster presence channel: %s", topic)

    async def _setup_pending_conversations_subscription(self) -> None:
        """Subscribe to Postgres Changes on Conversations table for new pending work."""
        topic = f"conversations:pending:{self._account_id}:{self._cluster_id}"
        channel = self._client.channel(topic)  # type: ignore[union-attr]

        pending_filter = (
            f"account_id=eq.{self._account_id}"
            f"&cluster_id=eq.{self._cluster_id}"
            f"&status=eq.pending"
        )

        def on_pending_conversation(payload: Any) -> None:
            """Called when a new or updated pending conversation appears."""
            logger.debug("Pending conversation notification received")
            try:
                self._notification_queue.put_nowait("pending_conversation")
            except queue.Full:
                pass  # Queue is bounded; notification is best-effort

        from realtime.types import RealtimePostgresChangesListenEvent

        channel.on_postgres_changes(
            event=RealtimePostgresChangesListenEvent.INSERT,
            callback=on_pending_conversation,
            schema="public",
            table="Conversations",
            filter=pending_filter,
        )
        channel.on_postgres_changes(
            event=RealtimePostgresChangesListenEvent.UPDATE,
            callback=on_pending_conversation,
            schema="public",
            table="Conversations",
            filter=pending_filter,
        )

        await channel.subscribe()
        logger.info("Subscribed to pending conversations changes")

    # ── Per-conversation Presence (async internals) ──────────────────

    async def _join_conversation_presence(self, conversation_id: str) -> None:
        if not self._client or conversation_id in self._conversation_channels:
            return
        topic = f"holmes:conversation:{conversation_id}"
        channel = self._client.channel(topic)
        await channel.subscribe()
        await channel.track(
            {
                "holmes_id": self._holmes_id,
                "version": get_version(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        self._conversation_channels[conversation_id] = channel
        self._active_conversations.add(conversation_id)
        logger.debug("Joined conversation presence: %s", conversation_id)

    async def _leave_conversation_presence(self, conversation_id: str) -> None:
        channel = self._conversation_channels.pop(conversation_id, None)
        self._active_conversations.discard(conversation_id)
        if channel:
            try:
                await channel.untrack()
                await self._client.remove_channel(channel)  # type: ignore[union-attr]
            except Exception:
                logger.debug(
                    "Error leaving conversation presence %s (may already be disconnected)",
                    conversation_id,
                )

    async def _update_conversation_presence(self, conversation_id: str) -> None:
        channel = self._conversation_channels.get(conversation_id)
        if channel:
            try:
                await channel.track(
                    {
                        "holmes_id": self._holmes_id,
                        "version": get_version(),
                        "started_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    }
                )
            except Exception:
                logger.debug(
                    "Error updating conversation presence %s", conversation_id
                )

    # ── Shutdown ─────────────────────────────────────────────────────

    async def _shutdown(self) -> None:
        """Clean up all channels and close the Realtime connection."""
        if self._client:
            try:
                await self._client.remove_all_channels()
                await self._client.close()
            except Exception:
                logger.debug("Error during RealtimeManager shutdown")
        self._conversation_channels.clear()
        self._active_conversations.clear()
