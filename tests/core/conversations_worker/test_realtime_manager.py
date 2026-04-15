"""Unit tests for RealtimeManager's testable (non-async) surface."""
import os
from unittest.mock import MagicMock

import pytest

from holmes.core.conversations_worker.realtime_manager import (
    RealtimeManager,
    _install_proxy_patch_if_needed,
)


def _make_manager():
    dal = MagicMock()
    dal.url = "https://sp.stg.example"
    dal.account_id = "acc-1"
    dal.cluster = "cluster-1"
    return RealtimeManager(dal=dal, holmes_id="h-test", on_new_pending=MagicMock())


def test_initial_state_is_disconnected():
    m = _make_manager()
    assert m.is_connected() is False


def test_is_connected_reflects_connection_flag():
    m = _make_manager()
    m._connected = True
    assert m.is_connected() is True
    m._connected = False
    assert m.is_connected() is False


def test_leave_conversation_presence_noop_without_loop():
    """Calling leave when the loop hasn't started should be a safe no-op."""
    m = _make_manager()
    m._loop = None
    # Must not raise
    m.leave_conversation_presence("some-id", request_sequence=1)


def test_join_conversation_presence_noop_without_loop():
    m = _make_manager()
    m._loop = None
    m.join_conversation_presence("some-id", request_sequence=1)


def test_presence_sequence_gate_skips_older_join():
    """A newer request_sequence's join should block a later older-sequence join."""
    m = _make_manager()
    m._loop = None  # asyncio schedule is a no-op without a loop
    # Newer worker joins first
    m.join_conversation_presence("c1", request_sequence=5, status="running")
    assert m._presence_sequences["c1"] == 5
    # Older worker tries to join — gate rejects it
    assert m._is_newest_sequence("c1", 3, update=True) is False
    # Stored sequence is unchanged
    assert m._presence_sequences["c1"] == 5


def test_presence_sequence_gate_skips_older_leave():
    """A newer worker's join must prevent an older worker's leave
    from tearing down the newer presence."""
    m = _make_manager()
    m._loop = None
    m.join_conversation_presence("c1", request_sequence=7, status="running")
    # Older worker calling leave must not disrupt the newer presence
    assert m._is_newest_sequence("c1", 4, update=False) is False
    # Same-sequence leave is allowed
    assert m._is_newest_sequence("c1", 7, update=False) is True


def test_presence_sequence_gate_advances_on_equal_or_newer():
    m = _make_manager()
    m._loop = None
    m.join_conversation_presence("c1", request_sequence=2, status="queued")
    assert m._presence_sequences["c1"] == 2
    # Same sequence update is fine
    m.update_conversation_presence("c1", request_sequence=2, status="running")
    assert m._presence_sequences["c1"] == 2
    # Newer sequence advances
    m.join_conversation_presence("c1", request_sequence=10, status="queued")
    assert m._presence_sequences["c1"] == 10


def test_leave_prunes_presence_sequence_entry():
    """A successful leave must remove the conversation's sequence entry so
    the map doesn't grow unbounded across many conversations."""
    m = _make_manager()
    m._loop = None  # presence channel coroutines are no-ops without loop
    m.join_conversation_presence("c1", request_sequence=3, status="running")
    assert "c1" in m._presence_sequences
    # Current owner's leave prunes the entry
    m.leave_conversation_presence("c1", request_sequence=3)
    assert "c1" not in m._presence_sequences


def test_stale_leave_does_not_prune_entry():
    """A stale leave (older request_sequence) is ignored AND must not
    remove the newer owner's entry from the map."""
    m = _make_manager()
    m._loop = None
    m.join_conversation_presence("c1", request_sequence=7, status="running")
    assert m._presence_sequences["c1"] == 7
    # Older sequence trying to leave: rejected by the gate, must NOT prune.
    m.leave_conversation_presence("c1", request_sequence=2)
    assert m._presence_sequences["c1"] == 7


def test_many_workers_race_newest_wins():
    """Simulate many concurrent calls for different sequences — only the
    highest is remembered and allowed to operate."""
    import threading as _threading
    import random as _random

    m = _make_manager()
    m._loop = None

    sequences = list(range(1, 21))  # 20 "workers"
    _random.shuffle(sequences)

    barrier = _threading.Barrier(len(sequences))

    def _join(seq: int):
        barrier.wait()
        m.join_conversation_presence("c1", request_sequence=seq, status="queued")

    threads = [_threading.Thread(target=_join, args=(s,)) for s in sequences]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The stored sequence must be the highest (20) after all races finish
    assert m._presence_sequences["c1"] == max(sequences)

    # Now any older-sequence leave is rejected
    for seq in range(1, 20):
        assert m._is_newest_sequence("c1", seq, update=False) is False
    # Only sequence 20 is accepted as current
    assert m._is_newest_sequence("c1", 20, update=False) is True


def test_install_proxy_patch_does_nothing_without_env(monkeypatch):
    """Patch installer must be a no-op when https_proxy is unset."""
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    import realtime._async.client as rt

    # Reset patch state
    rt._holmes_proxy_patched = False
    original_connect = rt.connect
    _install_proxy_patch_if_needed()
    assert rt.connect is original_connect
    assert not getattr(rt, "_holmes_proxy_patched", False)


def test_install_proxy_patch_is_idempotent(monkeypatch):
    """Calling install twice should not double-patch."""
    monkeypatch.setenv(
        "https_proxy", "http://user:pass@proxy.internal:8888"
    )
    import realtime._async.client as rt

    rt._holmes_proxy_patched = False
    original_connect = rt.connect
    _install_proxy_patch_if_needed()
    first_patched = rt.connect
    _install_proxy_patch_if_needed()
    second_patched = rt.connect
    assert first_patched is second_patched, "patch was reinstalled unexpectedly"

    # Cleanup: restore the original connect fn
    rt.connect = original_connect
    rt._holmes_proxy_patched = False
