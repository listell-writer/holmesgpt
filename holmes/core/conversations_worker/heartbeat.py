import logging
import time
from typing import TYPE_CHECKING, Optional

from holmes.common.env_vars import CONVERSATION_WORKER_HEARTBEAT_INTERVAL_SECONDS
from holmes.core.tracing import DummySpan

if TYPE_CHECKING:
    from holmes.core.conversations_worker.realtime_manager import RealtimeManager

logger = logging.getLogger(__name__)


class ConversationHeartbeat(DummySpan):
    """A tracing span that sends Presence heartbeats for a running conversation.

    Injected as ``trace_span`` into :class:`ChatRequest` so that the LLM
    tool-calling loop triggers periodic heartbeats through
    :meth:`start_span` and :meth:`log` callbacks — exactly the same
    pattern used by :class:`ScheduledPromptsHeartbeatSpan`.
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

    def start_span(
        self, name: Optional[str] = None, span_type=None, **kwargs
    ) -> "ConversationHeartbeat":
        """Called during tool calls — trigger a heartbeat if interval has elapsed."""
        self._maybe_heartbeat()
        return ConversationHeartbeat(
            conversation_id=self._conversation_id,
            realtime_manager=self._realtime_manager,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )

    def log(self, *args, **kwargs) -> None:
        """Called during logging — trigger a heartbeat if interval has elapsed."""
        self._maybe_heartbeat()

    def _maybe_heartbeat(self) -> None:
        """Update Presence state if enough time has elapsed since last heartbeat."""
        now = time.time()
        if now - self._last_heartbeat_time >= self._heartbeat_interval_seconds:
            try:
                self._realtime_manager.update_conversation_presence(
                    self._conversation_id
                )
                self._last_heartbeat_time = now
                logger.debug(
                    "Heartbeat for conversation %s", self._conversation_id
                )
            except Exception as exc:
                logger.warning(
                    "Heartbeat failed for conversation %s: %s",
                    self._conversation_id,
                    exc,
                )
