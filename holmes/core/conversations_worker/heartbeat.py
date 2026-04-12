import logging
import time
from typing import TYPE_CHECKING, Optional

from holmes.common.env_vars import CONVERSATION_WORKER_HEARTBEAT_INTERVAL_SECONDS
from holmes.core.tracing import DummySpan

if TYPE_CHECKING:
    from holmes.core.conversations_worker.realtime_manager import RealtimeManager

logger = logging.getLogger(__name__)


class ConversationHeartbeat(DummySpan):
    """A span that sends heartbeats for conversation processing via
    Supabase Realtime Presence.

    Mirrors :class:`ScheduledPromptsHeartbeatSpan` but uses Presence
    channel updates instead of DB writes for liveness signalling.
    """

    def __init__(
        self,
        conversation_id: str,
        realtime_manager: "RealtimeManager",
        heartbeat_interval_seconds: int = CONVERSATION_WORKER_HEARTBEAT_INTERVAL_SECONDS,
    ):
        self._conversation_id = conversation_id
        self._realtime_manager = realtime_manager
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._last_heartbeat_time = time.time()

    def start_span(self, name: Optional[str] = None, span_type=None, **kwargs):
        """Trigger heartbeat on activity (typically during tool calls)."""
        self._maybe_heartbeat()
        return ConversationHeartbeat(
            conversation_id=self._conversation_id,
            realtime_manager=self._realtime_manager,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )

    def log(self, *args, **kwargs):
        """Trigger heartbeat on logging activity."""
        self._maybe_heartbeat()

    def _maybe_heartbeat(self) -> None:
        """Send a presence update if enough time has elapsed."""
        current_time = time.time()
        if current_time - self._last_heartbeat_time >= self._heartbeat_interval_seconds:
            try:
                self._realtime_manager.update_conversation_presence(
                    self._conversation_id
                )
                self._last_heartbeat_time = current_time
                logger.debug(
                    "Heartbeat for conversation %s", self._conversation_id
                )
            except Exception as e:
                logger.warning(
                    "Heartbeat failed for conversation %s: %s",
                    self._conversation_id,
                    e,
                )
