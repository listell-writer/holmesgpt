"""Unit tests for RealtimeManager's testable (non-async) surface."""
import asyncio
import os
import random
import threading
from unittest.mock import MagicMock

import pytest
import realtime._async.client as rt_client

from holmes.core.conversations_worker.realtime_manager import (
    RealtimeManager,
    _install_proxy_patch_if_needed,
    account_presence_topic,
    conversation_presence_key,
    pg_changes_topic,
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


def test_topic_and_key_helpers():
    """Per-account channels with conversation presence keys."""
    assert account_presence_topic("acc-1") == "holmes:presence:acc-1"
    assert pg_changes_topic("acc-1") == "holmes:pgchanges:acc-1"
    assert (
        conversation_presence_key("conv-abc", "h-123")
        == "conversation:conv-abc:h-123"
    )


def test_presence_payload_starts_empty():
    """Before any conversation is claimed, the payload has
    active_conversations == 0 and conversations is an empty dict."""
    m = _make_manager()
    payload = m._build_presence_payload()
    assert payload["holmes_id"] == "h-test"
    assert payload["cluster_id"] == "cluster-1"
    assert payload["active_conversations"] == 0
    assert payload["conversations"] == {}


def test_presence_payload_includes_conversation_entries_by_key():
    """_apply_conversation_entry adds an entry keyed by the full presence
    key (conversation:{conversation_id}:{holmes_id}) — matching the design
    spec's "presence key" vocabulary."""

    m = _make_manager()
    # _apply_conversation_entry awaits _retrack_presence which calls
    # self._account_channel.track — stub the channel so it's a no-op.
    m._account_channel = MagicMock()

    async def _track_noop(payload):
        return None

    m._account_channel.track = _track_noop

    asyncio.run(
        m._apply_conversation_entry("conv-abc", request_sequence=1, status="queued")
    )
    asyncio.run(
        m._apply_conversation_entry("conv-xyz", request_sequence=2, status="running")
    )

    key_abc = "conversation:conv-abc:h-test"
    key_xyz = "conversation:conv-xyz:h-test"
    assert key_abc in m._conversations
    assert key_xyz in m._conversations
    assert m._conversations[key_abc]["type"] == "conversation"
    assert m._conversations[key_abc]["conversation_id"] == "conv-abc"
    assert m._conversations[key_abc]["status"] == "queued"
    assert m._conversations[key_xyz]["status"] == "running"

    payload = m._build_presence_payload()
    assert payload["active_conversations"] == 2
    assert key_abc in payload["conversations"]
    assert key_xyz in payload["conversations"]


def test_presence_flush_is_debounced():
    """Multiple rapid conversation state changes should only produce a single
    track() call when the flusher runs — this is what keeps us under the
    Supabase Realtime presence rate limit."""

    m = _make_manager()
    m._account_channel = MagicMock()
    track_calls = []

    async def _track_recording(payload):
        track_calls.append(payload)

    m._account_channel.track = _track_recording

    # Many rapid state changes — each sets the dirty flag but doesn't track
    for i in range(50):
        asyncio.run(
            m._apply_conversation_entry(
                f"conv-{i}", request_sequence=1, status="running"
            )
        )
    # At this point: 0 track calls yet (they're only buffered via dirty flag)
    assert len(track_calls) == 0
    assert m._presence_dirty is True

    # One flush call writes ONE track with all 50 entries in it
    asyncio.run(m._flush_presence_if_dirty())
    assert len(track_calls) == 1
    assert track_calls[0]["active_conversations"] == 50
    assert m._presence_dirty is False

    # A second flush with no new changes is a no-op
    asyncio.run(m._flush_presence_if_dirty())
    assert len(track_calls) == 1


def test_presence_flush_retries_on_failure():
    """If track() throws (e.g., transient ws error), the dirty flag is put
    back so the next tick retries."""

    m = _make_manager()
    m._account_channel = MagicMock()

    async def _track_raise(payload):
        raise RuntimeError("boom")

    m._account_channel.track = _track_raise

    asyncio.run(
        m._apply_conversation_entry("conv-1", request_sequence=1, status="running")
    )
    assert m._presence_dirty is True
    asyncio.run(m._flush_presence_if_dirty())
    # Still dirty because the track() call failed
    assert m._presence_dirty is True


def test_presence_payload_removed_on_leave():
    """_remove_conversation_entry removes the entry from the map so the
    next track() call advertises the absence."""

    m = _make_manager()
    m._account_channel = MagicMock()

    async def _track_noop(payload):
        return None

    m._account_channel.track = _track_noop

    asyncio.run(
        m._apply_conversation_entry("conv-abc", request_sequence=1, status="running")
    )
    key = "conversation:conv-abc:h-test"
    assert key in m._conversations
    asyncio.run(m._remove_conversation_entry("conv-abc"))
    assert key not in m._conversations
    assert m._build_presence_payload()["active_conversations"] == 0


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

    m = _make_manager()
    m._loop = None

    sequences = list(range(1, 21))  # 20 "workers"
    random.shuffle(sequences)

    barrier = threading.Barrier(len(sequences))

    def _join(seq: int):
        barrier.wait()
        m.join_conversation_presence("c1", request_sequence=seq, status="queued")

    threads = [threading.Thread(target=_join, args=(s,)) for s in sequences]
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


    # Reset patch state
    rt_client._holmes_proxy_patched = False
    original_connect = rt_client.connect
    _install_proxy_patch_if_needed()
    assert rt_client.connect is original_connect
    assert not getattr(rt_client, "_holmes_proxy_patched", False)


def test_install_proxy_patch_is_idempotent(monkeypatch):
    """Calling install twice should not double-patch."""
    monkeypatch.setenv(
        "https_proxy", "http://user:pass@proxy.internal:8888"
    )


    rt_client._holmes_proxy_patched = False
    original_connect = rt_client.connect
    _install_proxy_patch_if_needed()
    first_patched = rt_client.connect
    _install_proxy_patch_if_needed()
    second_patched = rt_client.connect
    assert first_patched is second_patched, "patch was reinstalled unexpectedly"

    # Cleanup: restore the original connect fn
    rt_client.connect = original_connect
    rt_client._holmes_proxy_patched = False
