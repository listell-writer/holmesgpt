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
        assignee: str,
        request_sequence: int,
        events: list,
        compact: bool = False,
    ) -> Optional[int]:
        self.calls.append(
            {
                "conversation_id": conversation_id,
                "assignee": assignee,
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
        assignee="h1",
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
        assignee="h1",
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
    """Only the CONVERSATION_HISTORY_COMPACTED event triggers compact=True."""
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        assignee="h1",
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
    # Exactly one flush with compact=True (the one containing the compacted event).
    compact_calls = [c for c in dal.calls if c["compact"] is True]
    assert len(compact_calls) == 1, f"expected 1 compact=True call, got {len(compact_calls)}"
    # The compact=True call must carry the compacted event.
    compact_event_types = [e["event"] for e in compact_calls[0]["events"]]
    assert "conversation_history_compacted" in compact_event_types

    # The ANSWER_END flush must NOT have compact=True.
    answer_calls = [
        c for c in dal.calls
        if any(e["event"] == "ai_answer_end" for e in c["events"])
    ]
    assert len(answer_calls) == 1
    assert answer_calls[0]["compact"] is False

    # Every event appears exactly once across all calls (no double-posting).
    all_event_types = [e["event"] for c in dal.calls for e in c["events"]]
    assert all_event_types.count("conversation_history_compacted") == 1
    assert all_event_types.count("conversation_history_compaction_start") == 1
    assert all_event_types.count("ai_answer_end") == 1


def test_publisher_compaction_start_does_not_trigger_compact_flag():
    """Only CONVERSATION_HISTORY_COMPACTED triggers compact=True, never START."""
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        assignee="h1",
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
                # Compaction aborted/unfinished — no COMPACTED event ever fires
                StreamMessage(event=StreamEvents.ANSWER_END, data={"content": "done"}),
            ]
        )
    )
    # No compact=True should have been set since COMPACTED never arrived
    assert not any(c["compact"] is True for c in dal.calls)


def test_publisher_no_compaction_events_never_sets_compact_flag():
    """Normal flows must never set compact=True."""
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        assignee="h1",
        request_sequence=1,
        batch_interval_seconds=60.0,
    )
    pub.consume(
        _stream(
            [
                StreamMessage(event=StreamEvents.START_TOOL, data={"tool_name": "t"}),
                StreamMessage(event=StreamEvents.TOOL_RESULT, data={"tool_call_id": "t"}),
                StreamMessage(event=StreamEvents.AI_MESSAGE, data={"content": "thinking"}),
                StreamMessage(event=StreamEvents.TOKEN_COUNT, data={}),
                StreamMessage(event=StreamEvents.ANSWER_END, data={"content": "final"}),
            ]
        )
    )
    assert all(c["compact"] is False for c in dal.calls), (
        f"unexpected compact=True: {dal.calls}"
    )


def test_publisher_raises_on_reassignment():
    dal = _FakeDal(raise_mismatch=True)
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        assignee="h1",
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


def test_publisher_flushes_on_error_event():
    """ERROR events are terminal and must be flushed immediately."""
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        assignee="h1",
        request_sequence=1,
        batch_interval_seconds=60.0,
    )
    terminal = pub.consume(
        _stream(
            [
                StreamMessage(event=StreamEvents.AI_MESSAGE, data={"content": "hi"}),
                StreamMessage(
                    event=StreamEvents.ERROR,
                    data={"description": "rate limit", "error_code": 5204},
                ),
            ]
        )
    )
    assert terminal == StreamEvents.ERROR
    # Both events in a single batch (flushed on ERROR)
    assert len(dal.calls) == 1
    assert dal.calls[0]["events"][-1]["event"] == "error"


def test_publisher_covers_all_stream_event_types():
    """Sanity check that every StreamEvents value is accepted by the publisher."""
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        assignee="h1",
        request_sequence=1,
        batch_interval_seconds=60.0,
    )
    # Build a message for each StreamEvents value
    all_events = [StreamMessage(event=e, data={}) for e in StreamEvents]
    # Consume — publisher should never crash
    pub.consume(_stream(all_events))
    # Collect all event type strings actually written
    written = {ev["event"] for call in dal.calls for ev in call["events"]}
    expected = {e.value for e in StreamEvents}
    assert expected == written, f"missing from writes: {expected - written}"


def test_publisher_batches_intermediate_events():
    """Events that don't trigger immediate flush should be batched together."""
    dal = _FakeDal()
    pub = ConversationEventPublisher(
        dal=dal,
        conversation_id="c1",
        assignee="h1",
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
