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
    m.leave_conversation_presence("some-id")


def test_join_conversation_presence_noop_without_loop():
    m = _make_manager()
    m._loop = None
    m.join_conversation_presence("some-id")


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
