"""Unit tests for worker lifecycle / claim-loop / error handling."""
import threading
from unittest.mock import MagicMock, patch

import pytest

from holmes.core.conversations_worker.models import (
    ConversationReassignedError,
    ConversationTask,
)
from holmes.core.conversations_worker.worker import ConversationWorker


def _bare_worker():
    w = ConversationWorker.__new__(ConversationWorker)
    w.dal = MagicMock()
    w.dal.enabled = True
    w.config = MagicMock()
    w.chat_function = MagicMock()
    w.holmes_id = "h-test"
    w._running = True
    w._claim_thread = None
    w._notify_event = threading.Event()
    w._executor = MagicMock()
    w._active_conversation_ids = set()
    w._active_lock = threading.Lock()
    w._realtime_manager = None
    return w


def test_build_task_from_conversation_row_parses_required_fields():
    w = _bare_worker()
    row = {
        "conversation_id": "c1",
        "account_id": "a1",
        "cluster_id": "cl1",
        "origin": "chat",
        "request_sequence": 3,
        "metadata": {"foo": "bar"},
        "title": "hello",
    }
    task = w._build_task_from_conversation_row(row)
    assert task is not None
    assert task.conversation_id == "c1"
    assert task.request_sequence == 3
    assert task.metadata == {"foo": "bar"}
    assert task.title == "hello"


def test_build_task_from_conversation_row_tolerates_missing_fields():
    w = _bare_worker()
    row = {"conversation_id": "c1", "account_id": "a1", "cluster_id": "cl1"}
    task = w._build_task_from_conversation_row(row)
    assert task is not None
    assert task.request_sequence == 1
    assert task.origin == "chat"


def test_build_task_from_conversation_row_returns_none_on_bad_input():
    w = _bare_worker()
    task = w._build_task_from_conversation_row({})  # missing required fields
    assert task is None


def test_try_claim_and_dispatch_skips_when_at_capacity(monkeypatch):
    w = _bare_worker()
    monkeypatch.setattr(
        "holmes.core.conversations_worker.worker.CONVERSATION_WORKER_MAX_CONCURRENT",
        1,
    )
    w._active_conversation_ids = {"existing"}
    w._try_claim_and_dispatch()
    w.dal.claim_conversations.assert_not_called()


def test_try_claim_and_dispatch_submits_claimed_to_executor():
    w = _bare_worker()
    w.dal.claim_conversations.return_value = [
        {
            "conversation_id": "c1",
            "account_id": "a1",
            "cluster_id": "cl1",
            "origin": "chat",
            "request_sequence": 1,
            "metadata": {},
        }
    ]
    w._try_claim_and_dispatch()
    w._executor.submit.assert_called_once()
    # first submitted callable = _process_conversation_safe, second arg = the task
    args = w._executor.submit.call_args[0]
    assert args[0] == w._process_conversation_safe
    assert isinstance(args[1], ConversationTask)
    assert args[1].conversation_id == "c1"
    # active set should track the conversation
    assert "c1" in w._active_conversation_ids


def test_process_conversation_safe_marks_failed_on_exception():
    w = _bare_worker()
    task = ConversationTask(
        conversation_id="c1",
        account_id="a1",
        cluster_id="cl1",
        origin="chat",
        request_sequence=1,
    )

    def boom(*a, **kw):
        raise RuntimeError("synthetic failure")

    with patch.object(ConversationWorker, "_process_conversation", boom):
        w._process_conversation_safe(task)

    w.dal.complete_conversation.assert_called_once_with(
        conversation_id="c1",
        request_sequence=1,
        assignee="h-test",
        status="failed",
    )
    # active conversation cleared
    assert "c1" not in w._active_conversation_ids


def test_process_conversation_safe_joins_and_leaves_presence():
    w = _bare_worker()
    rt = MagicMock()
    w._realtime_manager = rt
    task = ConversationTask(
        conversation_id="c1",
        account_id="a1",
        cluster_id="cl1",
        origin="chat",
        request_sequence=1,
    )
    # _process_conversation is a no-op for this test
    with patch.object(ConversationWorker, "_process_conversation", lambda self, t: None):
        w._process_conversation_safe(task)

    rt.join_conversation_presence.assert_not_called()  # join happens inside _process_conversation (which we mocked out)
    rt.leave_conversation_presence.assert_called_once_with("c1")
    assert "c1" not in w._active_conversation_ids


def test_process_conversation_safe_always_leaves_presence_on_error():
    """On ConversationReassignedError the worker must:
     - leave the per-conversation presence channel (finally)
     - NOT call complete_conversation — the conversation's state is already
       being handled by whoever reassigned it (e.g. stop_conversation bumped
       request_sequence, or another Holmes took over). A stale
       complete_conversation call would either fail the RPC's status guard or
       race with the new owner.
    """
    w = _bare_worker()
    rt = MagicMock()
    w._realtime_manager = rt
    task = ConversationTask(
        conversation_id="c1",
        account_id="a1",
        cluster_id="cl1",
        origin="chat",
        request_sequence=1,
    )

    def boom(*a, **kw):
        raise ConversationReassignedError("x")

    with patch.object(ConversationWorker, "_process_conversation", boom):
        w._process_conversation_safe(task)

    # leave must run in the finally even after a reassignment
    rt.leave_conversation_presence.assert_called_once_with("c1")
    # Critically: we must NOT mark a reassigned conversation as failed
    w.dal.complete_conversation.assert_not_called()


def test_notify_event_wakes_claim_loop():
    """The claim loop should wake quickly when notify_event is set."""
    w = _bare_worker()
    w._realtime_manager = MagicMock()
    w._realtime_manager.is_connected.return_value = True  # long idle

    call_count = {"n": 0}

    def fake_claim():
        call_count["n"] += 1
        # after first call from startup, stop the loop
        if call_count["n"] >= 2:
            w._running = False

    w._try_claim_and_dispatch = fake_claim

    t = threading.Thread(target=w._claim_loop)
    t.start()
    # wake it up
    w._notify_event.set()
    t.join(timeout=3)
    assert not t.is_alive(), "claim loop did not exit after notify"
    assert call_count["n"] == 2
