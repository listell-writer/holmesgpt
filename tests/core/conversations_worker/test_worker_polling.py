"""Unit tests for the ConversationWorker's realtime-gated polling logic."""
from unittest.mock import MagicMock

from holmes.core.conversations_worker.worker import (
    ConversationWorker,
    _REALTIME_CONNECTED_IDLE_SECONDS,
)


def _make_worker_with_rt(connected: bool):
    worker = ConversationWorker.__new__(ConversationWorker)
    rt = MagicMock()
    rt.is_connected.return_value = connected
    worker._realtime_manager = rt
    return worker


def test_realtime_connected_returns_true_when_manager_connected():
    worker = _make_worker_with_rt(True)
    assert worker._realtime_connected() is True


def test_realtime_connected_false_when_no_manager():
    worker = ConversationWorker.__new__(ConversationWorker)
    worker._realtime_manager = None
    assert worker._realtime_connected() is False


def test_realtime_connected_false_when_manager_disconnected():
    worker = _make_worker_with_rt(False)
    assert worker._realtime_connected() is False


def test_realtime_connected_false_when_is_connected_raises():
    worker = ConversationWorker.__new__(ConversationWorker)
    rt = MagicMock()
    rt.is_connected.side_effect = RuntimeError("boom")
    worker._realtime_manager = rt
    assert worker._realtime_connected() is False


def test_idle_seconds_is_large_enough_to_avoid_polling():
    # Sanity check: the idle timeout when realtime is connected should be much
    # larger than a reasonable polling interval so the loop relies on events.
    assert _REALTIME_CONNECTED_IDLE_SECONDS >= 600
