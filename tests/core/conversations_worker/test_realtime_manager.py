"""Unit tests for RealtimeManager's testable (non-async) surface."""
import asyncio
import logging
import os
import ssl as _ssl
from unittest.mock import MagicMock

import certifi
import pytest
import realtime._async.client as rt_client
import websockets.exceptions as ws_exc
from realtime._async.channel import ChannelStates
from websockets.frames import Close

from holmes.core.conversations_worker.realtime_manager import (
    RealtimeManager,
    _benign_ws_close_code,
    _build_ssl_context,
    _install_ssl_patch_if_needed,
    broadcast_submit_topic,
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


def test_topic_helpers():
    assert pg_changes_topic("acc-1") == "holmes:pgchanges:acc-1"
    assert (
        broadcast_submit_topic("acc-1", "cluster-1")
        == "holmes:submit:acc-1:cluster-1"
    )


# ---- SSL / custom CA patching ----


def test_install_ssl_patch_does_nothing_without_ca_bundle(monkeypatch):
    """No CA env var → no patch."""
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("WEBSOCKET_CLIENT_CA_BUNDLE", raising=False)

    rt_client._holmes_ssl_patched = False
    original_connect = rt_client.connect
    _install_ssl_patch_if_needed()
    assert rt_client.connect is original_connect
    assert not getattr(rt_client, "_holmes_ssl_patched", False)


def test_install_ssl_patch_does_nothing_when_ca_bundle_missing(
    monkeypatch, tmp_path
):
    """CA env var pointing at a non-existent path is a no-op (don't crash)."""
    monkeypatch.setenv(
        "REQUESTS_CA_BUNDLE", str(tmp_path / "does-not-exist.pem")
    )
    monkeypatch.delenv("WEBSOCKET_CLIENT_CA_BUNDLE", raising=False)

    rt_client._holmes_ssl_patched = False
    original_connect = rt_client.connect
    _install_ssl_patch_if_needed()
    assert rt_client.connect is original_connect
    assert not getattr(rt_client, "_holmes_ssl_patched", False)


def test_install_ssl_patch_injects_ssl_for_wss(monkeypatch, tmp_path):
    """When a CA bundle is configured, wss:// connects must get ssl kwarg."""
    # Use the system certifi bundle as our "custom CA" — it's a real,
    # parseable PEM file, which is all create_default_context(cafile=...) needs.
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())
    monkeypatch.delenv("WEBSOCKET_CLIENT_CA_BUNDLE", raising=False)

    rt_client._holmes_ssl_patched = False
    original_connect = rt_client.connect

    captured_kwargs = {}

    async def fake_connect(url, *args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    rt_client.connect = fake_connect

    try:
        _install_ssl_patch_if_needed()
        assert getattr(rt_client, "_holmes_ssl_patched", False) is True

        asyncio.run(rt_client.connect("wss://realtime.example/realtime/v1"))
        assert "ssl" in captured_kwargs, "wss:// must get an ssl context"
        assert isinstance(captured_kwargs["ssl"], _ssl.SSLContext)
    finally:
        rt_client.connect = original_connect
        rt_client._holmes_ssl_patched = False


def test_install_ssl_patch_does_not_clobber_existing_ssl(monkeypatch):
    """Caller-supplied ssl kwarg must win — don't overwrite proxy patch's ctx."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())

    rt_client._holmes_ssl_patched = False
    original_connect = rt_client.connect

    captured_kwargs = {}

    async def fake_connect(url, *args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    rt_client.connect = fake_connect
    sentinel_ctx = _ssl.create_default_context()

    try:
        _install_ssl_patch_if_needed()
        asyncio.run(
            rt_client.connect(
                "wss://realtime.example/realtime/v1", ssl=sentinel_ctx
            )
        )
        assert captured_kwargs["ssl"] is sentinel_ctx
    finally:
        rt_client.connect = original_connect
        rt_client._holmes_ssl_patched = False


def test_install_ssl_patch_skips_non_wss(monkeypatch):
    """Plain ws:// (or non-WS schemes) must not get a forced ssl kwarg."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())

    rt_client._holmes_ssl_patched = False
    original_connect = rt_client.connect

    captured_kwargs = {}

    async def fake_connect(url, *args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    rt_client.connect = fake_connect

    try:
        _install_ssl_patch_if_needed()
        asyncio.run(rt_client.connect("ws://localhost:54321/realtime/v1"))
        assert "ssl" not in captured_kwargs
    finally:
        rt_client.connect = original_connect
        rt_client._holmes_ssl_patched = False


def test_install_ssl_patch_is_idempotent(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())

    rt_client._holmes_ssl_patched = False
    original_connect = rt_client.connect
    try:
        _install_ssl_patch_if_needed()
        first_patched = rt_client.connect
        assert first_patched is not original_connect

        _install_ssl_patch_if_needed()
        assert rt_client.connect is first_patched
    finally:
        rt_client.connect = original_connect
        rt_client._holmes_ssl_patched = False


def test_build_ssl_context_uses_custom_ca(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())
    ctx = _build_ssl_context()
    assert isinstance(ctx, _ssl.SSLContext)
    # Default context verifies the cert chain — if the cafile didn't load,
    # SSLContext construction wouldn't have raised, but we'd be back on the
    # OS store. Sanity-check via verify_mode.
    assert ctx.verify_mode == _ssl.CERT_REQUIRED


def test_build_ssl_context_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("WEBSOCKET_CLIENT_CA_BUNDLE", raising=False)
    ctx = _build_ssl_context()
    assert isinstance(ctx, _ssl.SSLContext)


# ---- _channel_unhealthy ----


def _alive_task():
    """Return a not-done asyncio.Task whose .done() == False."""
    t = MagicMock()
    t.done.return_value = False
    return t


def _done_task():
    t = MagicMock()
    t.done.return_value = True
    return t


def _make_healthy_manager():
    m = _make_manager()
    m._channel = MagicMock()
    m._channel.state = ChannelStates.JOINED
    m._client = MagicMock()
    m._client.is_connected = True
    m._client._listen_task = _alive_task()
    m._client._heartbeat_task = _alive_task()
    return m


def test_unhealthy_when_channel_none():
    m = _make_manager()
    assert m._channel_unhealthy() == "channel_none"


def test_unhealthy_when_channel_not_joined():
    m = _make_healthy_manager()
    m._channel.state = ChannelStates.CLOSED
    reason = m._channel_unhealthy()
    assert reason is not None and reason.startswith("channel_state=")


def test_unhealthy_when_client_none():
    m = _make_healthy_manager()
    m._client = None
    assert m._channel_unhealthy() == "client_none"


def test_unhealthy_when_ws_disconnected():
    m = _make_healthy_manager()
    m._client.is_connected = False
    assert m._channel_unhealthy() == "ws_disconnected"


def test_unhealthy_when_listen_task_done():
    """Silent-death case: listen task exited cleanly on ConnectionClosedOK.

    is_connected stays True, channel state stays JOINED, but the listen task
    is done — the read loop is gone and no notifications will arrive. This
    is the production failure mode the in-loop health check exists to catch.
    """
    m = _make_healthy_manager()
    m._client._listen_task = _done_task()
    assert m._channel_unhealthy() == "listen_task_done"


def test_unhealthy_when_listen_task_missing():
    m = _make_healthy_manager()
    m._client._listen_task = None
    assert m._channel_unhealthy() == "listen_task_done"


def test_unhealthy_when_heartbeat_task_done():
    m = _make_healthy_manager()
    m._client._heartbeat_task = _done_task()
    assert m._channel_unhealthy() == "heartbeat_task_done"


def test_unhealthy_when_heartbeat_task_missing():
    m = _make_healthy_manager()
    m._client._heartbeat_task = None
    assert m._channel_unhealthy() == "heartbeat_task_done"


def test_healthy_when_all_signals_good():
    m = _make_healthy_manager()
    assert m._channel_unhealthy() is None


def test_unhealthy_degrades_gracefully_when_internals_renamed():
    """If a future realtime version renames _listen_task / _heartbeat_task,
    getattr returns None and the check still flags unhealthy rather than
    crashing the worker thread."""
    m = _make_manager()
    m._channel = MagicMock()
    m._channel.state = ChannelStates.JOINED
    # Bare object — no _listen_task / _heartbeat_task attributes at all.
    class _StubClient:
        is_connected = True
    m._client = _StubClient()
    # Should return a reason string, never raise.
    reason = m._channel_unhealthy()
    assert reason == "listen_task_done"


def test_run_loop_triggers_reconnect_on_dead_listen_task():
    """When the listen task is done, _run must call _full_reconnect on the
    next health-tick wake instead of waiting for the auth-refresh interval.
    """
    async def _scenario():
        m = _make_healthy_manager()
        m._async_stop = asyncio.Event()
        m._loop = asyncio.get_running_loop()

        # First _full_reconnect call (initial connect): succeeds, sets up the
        # healthy mock client. The loop then enters the steady-state while.
        # Second call (after we kill the listen task): records the call and
        # signals async_stop so the loop exits.
        reconnect_calls = []

        async def fake_reconnect():
            reconnect_calls.append(asyncio.get_running_loop().time())
            if len(reconnect_calls) == 1:
                # Initial connect — keep the healthy mock client/channel.
                return True
            # Reconnect after detecting the dead listen task — signal stop.
            m._async_stop.set()
            m._stop_event.set()
            return True

        m._full_reconnect = fake_reconnect  # type: ignore[method-assign]

        async def fake_refresh_auth():
            return None

        m._maybe_refresh_auth = fake_refresh_auth  # type: ignore[method-assign]

        # Kill the listen task immediately so the first health check trips.
        m._client._listen_task = _done_task()

        # Force a short health tick so the test runs quickly.
        import holmes.core.conversations_worker.realtime_manager as _rm
        original_tick = _rm.CONVERSATION_WORKER_REALTIME_HEALTH_TICK_SECONDS
        _rm.CONVERSATION_WORKER_REALTIME_HEALTH_TICK_SECONDS = 0.05
        try:
            await asyncio.wait_for(m._run(), timeout=2.0)
        finally:
            _rm.CONVERSATION_WORKER_REALTIME_HEALTH_TICK_SECONDS = original_tick

        # Must have reconnected at least twice (initial + recovery).
        assert len(reconnect_calls) >= 2

    asyncio.run(_scenario())


# ---- _benign_ws_close_code -------------------------------------------------
#
# The worker already recovers from socket-went-away via _channel_unhealthy +
# _full_reconnect. Surfacing those close events to Sentry as errors is noise.
# The helper identifies the codes we treat as benign so call sites can log at
# WARNING instead of ERROR (Sentry only auto-captures ERROR+).


def _cc_error(code: int) -> ws_exc.ConnectionClosedError:
    """ConnectionClosedError with a received Close frame for ``code``."""
    frame = Close(code, "")
    # rcvd_then_sent must be set when both rcvd and sent are non-None.
    return ws_exc.ConnectionClosedError(frame, frame, True)


def _cc_1006() -> ws_exc.ConnectionClosedError:
    """1006 abnormal close — synthesized locally, no Close frame received."""
    return ws_exc.ConnectionClosedError(None, None)


@pytest.mark.parametrize("code", [1000, 1001, 1006])
def test_benign_ws_close_code_recognizes_benign_codes(code):
    exc = _cc_1006() if code == 1006 else _cc_error(code)
    assert _benign_ws_close_code(exc) == code


def test_benign_ws_close_code_rejects_policy_violation():
    # 1008 is policy violation — typically auth/RLS misconfig, must reach Sentry.
    assert _benign_ws_close_code(_cc_error(1008)) is None


def test_benign_ws_close_code_rejects_non_ws_exception():
    assert _benign_ws_close_code(ValueError("oops")) is None
    # A non-WS exception that happens to have a .code attribute must not match.
    class _Fake(Exception):
        code = 1006

    assert _benign_ws_close_code(_Fake()) is None


def test_benign_ws_close_code_walks_cause_chain():
    inner = _cc_1006()
    try:
        try:
            raise inner
        except Exception as e:
            raise RuntimeError("wrapper") from e
    except RuntimeError as wrapper:
        assert _benign_ws_close_code(wrapper) == 1006


def test_benign_ws_close_code_walks_context_chain():
    # Implicit chaining via __context__ (no `from`) must also be followed.
    try:
        try:
            raise _cc_1006()
        except Exception:
            raise RuntimeError("oops")
    except RuntimeError as wrapper:
        assert _benign_ws_close_code(wrapper) == 1006


def test_benign_ws_close_code_handles_none_input():
    assert _benign_ws_close_code(None) is None


# ---- end-to-end: log level at call sites ----------------------------------
#
# Verifies the wiring: a benign WS close raised by the realtime client during
# shutdown must be logged at WARNING, not ERROR. ERROR records become Sentry
# events via sentry_sdk's logging integration.


def test_shutdown_logs_warning_on_abnormal_close(caplog):
    async def _scenario():
        m = _make_manager()
        m._client = MagicMock()

        async def _raise_1006():
            raise _cc_1006()

        m._client.close = _raise_1006
        with caplog.at_level(logging.WARNING, logger="root"):
            await m._shutdown_async()

    asyncio.run(_scenario())

    ws_records = [
        r for r in caplog.records
        if "realtime" in r.getMessage().lower() or "ws" in r.getMessage().lower()
    ]
    assert ws_records, f"expected a log record, got {[r.getMessage() for r in caplog.records]}"
    assert all(r.levelno == logging.WARNING for r in ws_records), (
        f"benign WS close must not log at ERROR; got levels "
        f"{[r.levelname for r in ws_records]}"
    )


def test_shutdown_still_logs_exception_on_real_error(caplog):
    async def _scenario():
        m = _make_manager()
        m._client = MagicMock()

        async def _raise_value_error():
            raise ValueError("something actually wrong")

        m._client.close = _raise_value_error
        with caplog.at_level(logging.WARNING, logger="root"):
            await m._shutdown_async()

    asyncio.run(_scenario())

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "non-WS exception must still be logged at ERROR"
