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

# Terminal events whose `messages` array carries the full conversation history
# snapshot — all prior events are superseded and should be marked compacted.
_COMPACT_ON_FLUSH_EVENTS = {
    StreamEvents.ANSWER_END,
    StreamEvents.APPROVAL_REQUIRED,
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
    ) -> Optional[StreamEvents]:
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
                    # ai_answer_end and approval_required carry a full
                    # conversation history snapshot in their messages array,
                    # so all prior events are superseded → compact them.
                    self._flush(
                        compact=message.event in _COMPACT_ON_FLUSH_EVENTS
                    )
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

        return self._last_terminal_event

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
            if not self._pending_events:
                return
            # Snapshot but don't clear yet — only clear after a successful post.
            events_to_flush = list(self._pending_events)

        try:
            seq = self.dal.post_conversation_events(
                conversation_id=self.conversation_id,
                assignee=self.assignee,
                request_sequence=self.request_sequence,
                events=events_to_flush,
                compact=compact,
            )
        except ConversationReassignedError:
            raise
        except Exception as e:
            # The RPCs prefix mismatch errors (status / assignee / request_sequence)
            # with "MISMATCH " — promote those to ConversationReassignedError so
            # the worker can exit the processing loop cleanly.
            if "mismatch" in str(e).lower():
                raise ConversationReassignedError(str(e)) from e
            raise

        if seq is None:
            # DAL returned None (disabled or unexpected empty response).
            # Keep events in memory so the next flush retries them.
            logging.warning(
                "post_conversation_events returned None for conversation %s — "
                "events retained for retry (%d events)",
                self.conversation_id,
                len(events_to_flush),
            )
            return

        # Success — remove the flushed events from the pending list.
        # New events may have been appended while the RPC was in flight,
        # so we remove only the count we just posted.
        with self._lock:
            del self._pending_events[: len(events_to_flush)]
        self._last_flush_time = time.monotonic()
        logging.debug(
            "Posted %d events to conversation %s (seq=%s, compact=%s)",
            len(events_to_flush),
            self.conversation_id,
            seq,
            compact,
        )
