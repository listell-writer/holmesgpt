import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Generator, List

from holmes.common.env_vars import CONVERSATION_WORKER_EVENT_BATCH_INTERVAL_SECONDS
from holmes.core.conversations_worker.models import ConversationReassignedError
from holmes.utils.stream import StreamEvents, StreamMessage

if TYPE_CHECKING:
    from holmes.core.supabase_dal import SupabaseDal

logger = logging.getLogger(__name__)


class ConversationEventPublisher:
    """Wraps a ``call_stream`` generator and batches ``StreamMessage``
    objects into ``ConversationEvents`` writes via the DAL.

    Events are accumulated for up to ``batch_interval`` seconds before
    flushing.  Terminal events (``ANSWER_END``, ``APPROVAL_REQUIRED``)
    flush immediately.

    If ``post_conversation_events`` returns ``None`` (holmes_id or
    request_sequence mismatch), a ``ConversationReassignedError`` is
    raised to abort processing.
    """

    # Events that trigger an immediate flush
    FLUSH_EVENTS = frozenset(
        {
            StreamEvents.ANSWER_END,
            StreamEvents.APPROVAL_REQUIRED,
            StreamEvents.ERROR,
        }
    )

    def __init__(
        self,
        dal: "SupabaseDal",
        conversation_id: str,
        holmes_id: str,
        request_sequence: int,
        batch_interval: float = CONVERSATION_WORKER_EVENT_BATCH_INTERVAL_SECONDS,
    ):
        self._dal = dal
        self._conversation_id = conversation_id
        self._holmes_id = holmes_id
        self._request_sequence = request_sequence
        self._batch_interval = batch_interval

        self._buffer: List[dict] = []
        self._last_flush: float = time.monotonic()

    def consume_stream(
        self, stream: Generator[StreamMessage, None, None]
    ) -> None:
        """Iterate over the full ``call_stream`` generator, batching and
        publishing events to Supabase.  Returns when the stream is
        exhausted."""
        try:
            for message in stream:
                self._buffer.append(self._message_to_event(message))

                compact = (
                    message.event
                    == StreamEvents.CONVERSATION_HISTORY_COMPACTED
                )

                if message.event in self.FLUSH_EVENTS or compact:
                    self._flush(compact=compact)
                elif self._should_flush():
                    self._flush()
        finally:
            # Flush any remaining buffered events on exit (normal or exception)
            if self._buffer:
                try:
                    self._flush()
                except Exception:
                    logger.warning(
                        "Failed to flush remaining events for %s",
                        self._conversation_id,
                        exc_info=True,
                    )

    # ── Internal helpers ──────────────────────────────────────────

    def _should_flush(self) -> bool:
        elapsed = time.monotonic() - self._last_flush
        return elapsed >= self._batch_interval

    def _flush(self, compact: bool = False) -> None:
        """Publish the buffered events to Supabase via the DAL."""
        if not self._buffer:
            return

        events_to_send = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.monotonic()

        seq = self._dal.post_conversation_events(
            conversation_id=self._conversation_id,
            holmes_id=self._holmes_id,
            request_sequence=self._request_sequence,
            events=events_to_send,
            compact=compact,
        )

        if seq is None:
            raise ConversationReassignedError(
                f"Conversation {self._conversation_id} was reassigned or "
                f"stopped (request_sequence={self._request_sequence})"
            )

        logger.debug(
            "Published %d events for conversation %s (seq=%s, compact=%s)",
            len(events_to_send),
            self._conversation_id,
            seq,
            compact,
        )

    @staticmethod
    def _message_to_event(message: StreamMessage) -> dict:
        """Convert a ``StreamMessage`` into the JSONB event format stored
        in ``ConversationEvents.events``."""
        return {
            "event": message.event.value,
            "data": message.data,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
