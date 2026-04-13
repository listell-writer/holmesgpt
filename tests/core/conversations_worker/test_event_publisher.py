"""Unit tests for the M2 ConversationEventPublisher."""
from typing import Any, List, Optional

import pytest

from holmes.core.conversations_worker.event_publisher import (
    ConversationEventPublisher,
)
from holmes.core.conversations_worker.models import ConversationReassignedError
from holmes.utils.stream import StreamEvents, StreamMessage


class _FakeDal:
    """Minimal fake of SupabaseDal for unit tests."""

    def __init__(self, seq_start: int = 0, raise_mismatch: bool = False):
        self.calls: List[dict] = []
        self._next_seq = seq_start
        self._raise_mismatch = raise_mismatch

    def post_conversation_events(
        self,
        conversation_id: str,
        holmes_id: str,
        request_sequence: int,
        events: list,
        compact: bool = False,
    ) -> Optional[int]:
        self.calls.append(
            {
                "conversation_id": conversation_id,
                "holmes_id": holmes_id,
                "request_sequence": request_sequence,
                "events": events,
                "compact": compact,
            }
        )
        if self._raise_mismatch:
            raise Exception("Assignee mismatch: expected X, got Y")
        self._next_seq += 1
        return self._next_seq


def _stream(events: List[StreamMessage]):
    for e in events:
        yield e


def test_publisher_flushes_on_terminal_answer_end():
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        holmes_id="h1",
        request_sequence=1,
        batch_interval_seconds=60.0,  # long — no interval flush
    )
    terminal = pub.consume(
        _stream(
            [
                StreamMessage(event=StreamEvents.AI_MESSAGE, data={"content": "thinking"}),
                StreamMessage(event=StreamEvents.ANSWER_END, data={"content": "done"}),
            ]
        )
    )
    assert terminal == StreamEvents.ANSWER_END
    # Both events should be in a single batch (flushed on ANSWER_END)
    assert len(dal.calls) == 1
    assert len(dal.calls[0]["events"]) == 2
    assert dal.calls[0]["compact"] is False
    assert dal.calls[0]["events"][0]["event"] == "ai_message"
    assert dal.calls[0]["events"][1]["event"] == "ai_answer_end"


def test_publisher_flushes_on_approval_required():
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        holmes_id="h1",
        request_sequence=1,
        batch_interval_seconds=60.0,
    )
    terminal = pub.consume(
        _stream(
            [
                StreamMessage(event=StreamEvents.START_TOOL, data={"tool_name": "bash"}),
                StreamMessage(
                    event=StreamEvents.APPROVAL_REQUIRED,
                    data={"pending_approvals": [{"tool_call_id": "1"}]},
                ),
            ]
        )
    )
    assert terminal == StreamEvents.APPROVAL_REQUIRED
    assert len(dal.calls) == 1


def test_publisher_compact_flag_on_compacted_event():
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        holmes_id="h1",
        request_sequence=1,
        batch_interval_seconds=60.0,
    )
    pub.consume(
        _stream(
            [
                StreamMessage(
                    event=StreamEvents.CONVERSATION_HISTORY_COMPACTION_START,
                    data={"content": "compacting"},
                ),
                StreamMessage(
                    event=StreamEvents.CONVERSATION_HISTORY_COMPACTED,
                    data={"content": "done"},
                ),
                StreamMessage(event=StreamEvents.ANSWER_END, data={"content": "final"}),
            ]
        )
    )
    # The compacted event triggers a flush with compact=True. Then the ANSWER_END
    # event is flushed separately.
    assert any(c["compact"] is True for c in dal.calls)
    # Each event appears exactly once
    all_event_types = [
        e["event"] for c in dal.calls for e in c["events"]
    ]
    assert all_event_types.count("conversation_history_compacted") == 1
    assert all_event_types.count("conversation_history_compaction_start") == 1
    assert all_event_types.count("ai_answer_end") == 1


def test_publisher_raises_on_reassignment():
    dal = _FakeDal(raise_mismatch=True)
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        holmes_id="h1",
        request_sequence=1,
    )
    with pytest.raises(ConversationReassignedError):
        pub.consume(
            _stream(
                [
                    StreamMessage(event=StreamEvents.ANSWER_END, data={"content": "x"}),
                ]
            )
        )


def test_publisher_batches_intermediate_events():
    """Events that don't trigger immediate flush should be batched together."""
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        holmes_id="h1",
        request_sequence=1,
        batch_interval_seconds=60.0,  # very large — no interval flush
    )
    pub.consume(
        _stream(
            [
                StreamMessage(event=StreamEvents.START_TOOL, data={"tool_name": "t1"}),
                StreamMessage(event=StreamEvents.TOOL_RESULT, data={"tool_call_id": "1"}),
                StreamMessage(event=StreamEvents.TOKEN_COUNT, data={}),
                StreamMessage(event=StreamEvents.ANSWER_END, data={"content": "ok"}),
            ]
        )
    )
    # All 4 should end up in a single flush (final ANSWER_END)
    assert len(dal.calls) == 1
    assert len(dal.calls[0]["events"]) == 4
