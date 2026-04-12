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
    """Consumes a ``call_stream`` generator and publishes batched events
    to the ``ConversationEvents`` table via the DAL.

    Events are accumulated for up to ``batch_interval`` seconds, then
    flushed as a single ``post_conversation_events`` call.  Terminal
    events (``ANSWER_END``, ``APPROVAL_REQUIRED``, ``ERROR``) trigger an
    immediate flush.
    """

    # Events that must flush immediately (they mark the end of a stream phase)
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

        self._pending: List[dict] = []
        self._last_flush_time = time.monotonic()

    def consume_stream(
        self,
        stream: Generator[StreamMessage, None, None],
    ) -> None:
        """Iterate through the full ``call_stream`` generator, batching and
        publishing events to Supabase.

        Raises :class:`ConversationReassignedError` if the conversation
        has been reassigned (the DAL returns ``None`` for a write).
        """
        try:
            for message in stream:
                self._accumulate(message)

                # Flush immediately on terminal events
                if message.event in self.FLUSH_EVENTS:
                    compact = message.event == StreamEvents.CONVERSATION_HISTORY_COMPACTED
                    self._flush(compact=compact)
                elif self._should_flush():
                    self._flush()
        finally:
            # Flush any remaining events
            if self._pending:
                self._flush()

    # ── Internal helpers ─────────────────────────────────────────────

    def _accumulate(self, message: StreamMessage) -> None:
        """Add a StreamMessage to the pending batch."""
        event_dict = {
            "event": message.event.value,
            "data": message.data,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._pending.append(event_dict)

    def _should_flush(self) -> bool:
        """Check whether enough time has elapsed to flush the batch."""
        if not self._pending:
            return False
        return (time.monotonic() - self._last_flush_time) >= self._batch_interval

    def _flush(self, compact: bool = False) -> None:
        """Write the accumulated events to Supabase via the DAL.

        If the DAL returns ``None`` (mismatch), the conversation was
        reassigned — raise :class:`ConversationReassignedError`.
        """
        if not self._pending:
            return

        events_batch = self._pending
        self._pending = []
        self._last_flush_time = time.monotonic()

        seq = self._dal.post_conversation_events(
            conversation_id=self._conversation_id,
            holmes_id=self._holmes_id,
            request_sequence=self._request_sequence,
            events=events_batch,
            compact=compact,
        )

        if seq is None:
            raise ConversationReassignedError(
                f"Conversation {self._conversation_id} was reassigned or stopped"
            )

        logger.debug(
            "Published %d events (seq=%d) for conversation %s",
            len(events_batch),
            seq,
            self._conversation_id,
        )
