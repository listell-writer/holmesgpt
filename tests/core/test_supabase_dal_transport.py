"""ROB-4017: SupabaseDal must hand postgrest a thread-safe HTTP/1.1 client.

`postgrest.SyncPostgrestClient` builds its own ``httpx.Client(http2=True)`` when
no client is supplied, and httpcore's *sync* HTTP/2 connection is not
thread-safe. The conversation worker, realtime callbacks and request threads all
share one ``SupabaseDal`` client, so under concurrency the HTTP/2 framing
corrupts and calls fail with ``RemoteProtocolError: Server disconnected``
(intermittent ``HolmesStatus`` upserts, dropped conversation claims). The DAL
therefore constructs an explicit ``http2=False`` client and passes it via
``ClientOptions(httpx_client=...)`` so postgrest does NOT build its own HTTP/2
one.

These tests are deterministic (no network) and pin that wiring so it can't
silently regress. The behavioural proof — a 32-thread x 60-req x 3-round stress
against a live Supabase endpoint, where ``http2=True`` produced ~41%
``RemoteProtocolError`` and ``http2=False`` produced zero — is documented in the
PR; it isn't run here because it needs network + credentials.
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from holmes.core.supabase_dal import SUPABASE_TIMEOUT_SECONDS, SupabaseDal


def _ui_token() -> str:
    bundle = {
        "store_url": "https://example.supabase.co",
        "api_key": "anon-key",
        "account_id": "acc-1",
        "email": "svc@example.com",
        "password": "pw",
    }
    return base64.b64encode(json.dumps(bundle).encode()).decode()


def _build_dal(monkeypatch, ca_env=None):
    """Construct a SupabaseDal with network mocked, capturing the kwargs passed
    to ``httpx.Client`` and the ``ClientOptions`` handed to ``create_client``."""
    monkeypatch.setenv("ROBUSTA_UI_TOKEN", _ui_token())
    # Start from a clean CA-env slate; the test harness/sandbox may set these.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    for k, v in (ca_env or {}).items():
        monkeypatch.setenv(k, v)

    captured: dict = {}

    def fake_httpx_client(*args, **kwargs):
        captured["httpx_kwargs"] = kwargs
        return MagicMock(name="httpx_client")

    with (
        patch(
            "holmes.core.supabase_dal.httpx.Client", side_effect=fake_httpx_client
        ),
        patch("holmes.core.supabase_dal.create_client") as mock_create,
        patch.object(SupabaseDal, "sign_in", return_value="user-1"),
        patch.object(SupabaseDal, "patch_postgrest_execute"),
    ):
        dal = SupabaseDal(cluster="test-cluster")
        # create_client(self.url, self.api_key, options) -> options is args[2]
        captured["options"] = mock_create.call_args.args[2]
        captured["httpx_client_obj"] = captured["options"].httpx_client
    return dal, captured


def test_dal_disables_http2(monkeypatch):
    dal, cap = _build_dal(monkeypatch)
    assert dal.enabled is True
    kw = cap["httpx_kwargs"]
    assert kw["http2"] is False
    assert kw["follow_redirects"] is True
    assert kw["timeout"] == SUPABASE_TIMEOUT_SECONDS


def test_dal_passes_our_client_to_postgrest(monkeypatch):
    # The same explicit client must be handed to postgrest via ClientOptions, so
    # postgrest reuses it instead of building its own http2=True client.
    _, cap = _build_dal(monkeypatch)
    assert cap["httpx_client_obj"] is not None
    assert cap["options"].httpx_client is cap["httpx_client_obj"]


def test_dal_verify_defaults_to_true_without_ca_env(monkeypatch):
    _, cap = _build_dal(monkeypatch)
    assert cap["httpx_kwargs"]["verify"] is True


def test_dal_honors_ssl_cert_file(monkeypatch):
    _, cap = _build_dal(monkeypatch, ca_env={"SSL_CERT_FILE": "/etc/ssl/custom-ca.pem"})
    assert cap["httpx_kwargs"]["verify"] == "/etc/ssl/custom-ca.pem"


def test_dal_honors_requests_ca_bundle(monkeypatch):
    _, cap = _build_dal(
        monkeypatch, ca_env={"REQUESTS_CA_BUNDLE": "/etc/ssl/proxy-ca.pem"}
    )
    assert cap["httpx_kwargs"]["verify"] == "/etc/ssl/proxy-ca.pem"


def test_dal_ssl_cert_file_takes_precedence_over_requests_ca_bundle(monkeypatch):
    # Mirrors the code: SSL_CERT_FILE is checked before REQUESTS_CA_BUNDLE.
    _, cap = _build_dal(
        monkeypatch,
        ca_env={
            "SSL_CERT_FILE": "/etc/ssl/custom-ca.pem",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/proxy-ca.pem",
        },
    )
    assert cap["httpx_kwargs"]["verify"] == "/etc/ssl/custom-ca.pem"


@pytest.mark.parametrize("ca_env", [None, {"SSL_CERT_FILE": "/etc/ssl/custom-ca.pem"}])
def test_dal_always_disables_http2_regardless_of_ca(monkeypatch, ca_env):
    # http2 must stay disabled no matter the CA configuration.
    _, cap = _build_dal(monkeypatch, ca_env=ca_env)
    assert cap["httpx_kwargs"]["http2"] is False
