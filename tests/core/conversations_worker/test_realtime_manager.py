"""Unit tests for RealtimeManager's testable (non-async) surface."""
from unittest.mock import MagicMock

import realtime._async.client as rt_client

from holmes.core.conversations_worker.realtime_manager import (
    RealtimeManager,
    _install_proxy_patch_if_needed,
    broadcast_submit_topic,
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


def test_broadcast_submit_topic():
    assert (
        broadcast_submit_topic("acc-1", "cluster-1")
        == "holmes:submit:acc-1:cluster-1"
    )


def test_broadcast_submit_topic_preserves_colons_in_cluster_id():
    """Cluster id may itself contain colons — the entire suffix after the
    account_id segment is the cluster_id."""
    topic = broadcast_submit_topic("acc-1", "prod:us-east:1")
    assert topic == "holmes:submit:acc-1:prod:us-east:1"
    # Parsing contract: split into max 4 parts so cluster_id retains its colons.
    parts = topic.split(":", 3)
    assert parts[0] == "holmes"
    assert parts[1] == "submit"
    assert parts[2] == "acc-1"
    assert parts[3] == "prod:us-east:1"


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
    """Calling install twice should not double-patch.

    Monkeypatches ``_SocksProxy`` so the patching logic actually runs even
    when python-socks is not installed.
    """
    import holmes.core.conversations_worker.realtime_manager as _rm

    monkeypatch.setenv(
        "https_proxy", "http://user:pass@proxy.internal:8888"
    )
    # Ensure the patcher doesn't bail on _SocksProxy being None
    monkeypatch.setattr(_rm, "_SocksProxy", MagicMock())

    rt_client._holmes_proxy_patched = False
    original_connect = rt_client.connect
    _install_proxy_patch_if_needed()
    # After first call: connect must be replaced and flag set
    first_patched = rt_client.connect
    assert first_patched is not original_connect, "patch was not applied"
    assert getattr(rt_client, "_holmes_proxy_patched", False) is True

    _install_proxy_patch_if_needed()
    # After second call: connect must be unchanged (idempotent)
    second_patched = rt_client.connect
    assert first_patched is second_patched, "patch was reinstalled unexpectedly"
    assert getattr(rt_client, "_holmes_proxy_patched", False) is True

    # Cleanup: restore the original connect fn
    rt_client.connect = original_connect
    rt_client._holmes_proxy_patched = False
