"""Unit tests for the M2 ConversationWorker task hydration logic."""
from holmes.core.conversations_worker.models import ConversationTask
from holmes.core.conversations_worker.worker import ConversationWorker


def _make_worker():
    """Build a ConversationWorker without calling its __init__ so we can test
    _hydrate_task_from_events in isolation."""
    w = ConversationWorker.__new__(ConversationWorker)
    return w


def test_hydrate_first_turn_ask_only():
    worker = _make_worker()
    task = ConversationTask(
        conversation_id="c1",
        account_id="a1",
        cluster_id="cl1",
        origin="chat",
        request_sequence=1,
    )
    events = [
        {
            "request_sequence": 1,
            "seq": 1,
            "events": [
                {"event": "user_message", "data": {"ask": "hello"}, "ts": "2026-04-13T00:00:00Z"}
            ],
        }
    ]
    worker._hydrate_task_from_events(task, events)
    assert task.ask == "hello"
    assert task.conversation_history is None


def test_hydrate_followup_reconstructs_history():
    worker = _make_worker()
    task = ConversationTask(
        conversation_id="c1",
        account_id="a1",
        cluster_id="cl1",
        origin="chat",
        request_sequence=2,
    )
    prev_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    events = [
        {
            "request_sequence": 1,
            "seq": 1,
            "events": [
                {"event": "user_message", "data": {"ask": "hello"}, "ts": "1"}
            ],
        },
        {
            "request_sequence": 1,
            "seq": 2,
            "events": [
                {
                    "event": "ai_answer_end",
                    "data": {"content": "hi there", "messages": prev_messages},
                    "ts": "2",
                }
            ],
        },
        {
            "request_sequence": 2,
            "seq": 1,
            "events": [
                {"event": "user_message", "data": {"ask": "how are you?"}, "ts": "3"}
            ],
        },
    ]
    worker._hydrate_task_from_events(task, events)
    assert task.ask == "how are you?"
    assert task.conversation_history == prev_messages


def test_hydrate_picks_approval_required_history():
    worker = _make_worker()
    task = ConversationTask(
        conversation_id="c1",
        account_id="a1",
        cluster_id="cl1",
        origin="chat",
        request_sequence=2,
    )
    prev_messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    events = [
        {
            "request_sequence": 1,
            "seq": 1,
            "events": [
                {
                    "event": "approval_required",
                    "data": {"messages": prev_messages, "pending_approvals": []},
                    "ts": "1",
                }
            ],
        },
        {
            "request_sequence": 2,
            "seq": 1,
            "events": [
                {
                    "event": "user_message",
                    "data": {
                        "ask": "continue",
                        "tool_decisions": [{"tool_call_id": "x", "decision": "allow", "prefix_approved": False, "arguments": {}}],
                    },
                    "ts": "2",
                }
            ],
        },
    ]
    worker._hydrate_task_from_events(task, events)
    assert task.ask == "continue"
    assert task.conversation_history == prev_messages
    assert task.tool_decisions is not None
    assert task.enable_tool_approval is True


def test_hydrate_extracts_model_override():
    worker = _make_worker()
    task = ConversationTask(
        conversation_id="c1",
        account_id="a1",
        cluster_id="cl1",
        origin="chat",
        request_sequence=1,
    )
    events = [
        {
            "request_sequence": 1,
            "seq": 1,
            "events": [
                {
                    "event": "user_message",
                    "data": {
                        "ask": "hi",
                        "model": "Robusta/Sonnet 4.5",
                        "additional_system_prompt": "be concise",
                    },
                    "ts": "1",
                }
            ],
        }
    ]
    worker._hydrate_task_from_events(task, events)
    assert task.ask == "hi"
    assert task.model == "Robusta/Sonnet 4.5"
    assert task.additional_system_prompt == "be concise"
