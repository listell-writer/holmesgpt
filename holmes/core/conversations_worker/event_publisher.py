import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, TYPE_CHECKING

from holmes.core.conversations_worker.models import ConversationReassignedError
from holmes.utils.stream import StreamEvents, StreamMessage

if TYPE_CHECKING:
    from holmes.core.supabase_dal import SupabaseDal


# Events that should cause an immediate flush
_FLUSH_IMMEDIATELY_EVENTS = {
    StreamEvents.ANSWER_END,
    StreamEvents.APPROVAL_REQUIRED,
    StreamEvents.ERROR,
}


class ConversationEventPublisher:
    """
    Consumes StreamMessage events from call_stream() and batches them
    into ConversationEvents rows in Supabase.
    """

    def __init__(
        self,
        dal: "SupabaseDal",
        conversation_id: str,
        assignee: str,
        request_sequence: int,
        batch_interval_seconds: float = 0.5,
    ):
        self.dal = dal
        self.conversation_id = conversation_id
        self.assignee = assignee
        self.request_sequence = request_sequence
        self.batch_interval_seconds = batch_interval_seconds

        self._pending_events: List[Dict[str, Any]] = []
        self._last_flush_time: float = time.monotonic()
        self._lock = threading.Lock()

        self._last_terminal_event: Optional[StreamEvents] = None

    def consume(
        self,
        stream: Generator[StreamMessage, None, None],
    ) -> StreamEvents:
        """
        Drain the stream generator, batching events and writing them to the DB.
        Returns the terminal StreamEvents value observed, or None if the stream ended
        without a terminal event.
        Raises ConversationReassignedError if the conversation was reassigned mid-stream.
        """
        try:
            for message in stream:
                self._append_event(message)
                # Flush on terminal events immediately, or when interval elapses
                if message.event in _FLUSH_IMMEDIATELY_EVENTS:
                    self._last_terminal_event = message.event
                    self._flush(compact=False)
                elif message.event == StreamEvents.CONVERSATION_HISTORY_COMPACTED:
                    # Flush with compact=True so previous events are marked compacted
                    self._flush(compact=True)
                elif (
                    time.monotonic() - self._last_flush_time
                    >= self.batch_interval_seconds
                ):
                    self._flush(compact=False)
        finally:
            # final drain of any remaining events
            self._flush(compact=False)

        return self._last_terminal_event  # type: ignore[return-value]

    def _append_event(self, message: StreamMessage) -> None:
        with self._lock:
            self._pending_events.append(
                {
                    "event": message.event.value,
                    "data": message.data,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )

    def _flush(self, compact: bool) -> None:
        with self._lock:
            events_to_flush = self._pending_events
            self._pending_events = []
        if not events_to_flush and not compact:
            return
        if not events_to_flush and compact:
            # Nothing new to post but compact requested — safe to ignore; compact is
            # only meaningful alongside a new event batch.
            return

        try:
            seq = self.dal.post_conversation_events(
                conversation_id=self.conversation_id,
                assignee=self.assignee,
                request_sequence=self.request_sequence,
                events=events_to_flush,
                compact=compact,
            )
            if seq is None:
                raise ConversationReassignedError(
                    f"Conversation {self.conversation_id} reassigned or stale: post_conversation_events returned None"
                )
            self._last_flush_time = time.monotonic()
            logging.debug(
                "Posted %d events to conversation %s (seq=%s, compact=%s)",
                len(events_to_flush),
                self.conversation_id,
                seq,
                compact,
            )
        except ConversationReassignedError:
            raise
        except Exception as e:
            # Detect mismatch errors from the RPC via message content
            msg = str(e).lower()
            if (
                "assignee mismatch" in msg
                or "request sequence mismatch" in msg
                or "is not running" in msg
            ):
                raise ConversationReassignedError(str(e)) from e
            raise
