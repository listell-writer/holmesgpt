"""Tests for MCP OAuth authorization_code support."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from holmes.plugins.toolsets.mcp.toolset_mcp import (
    MCPConfig,
    MCPMode,
    MCPOAuthConfig,
    OAuthKeyExchange,
    OAuthTokenCache,
    RemoteMCPTool,
    RemoteMCPToolset,
    _cli_oauth_flow,
    _generate_pkce,
    _get_conversation_key,
    _get_oauth_cache_key,
    _oauth_token_cache,
    _pending_exchanges,
    _exchanges_lock,
    _PendingOAuthExchange,
    decrypt_code_and_exchange_for_token,
)


class TestMCPOAuthConfig:
    def test_oauth_config_parsing(self):
        config = MCPConfig(
            url="http://example.com:8000",
            mode=MCPMode.STREAMABLE_HTTP,
            oauth=MCPOAuthConfig(
                enabled=True,
                authorization_url="http://auth.example.com/authorize",
                token_url="http://auth.example.com/token",
                client_id="my-client",
                scopes=["mcp:tools", "read"],
            ),
        )
        assert config.oauth is not None
        assert config.oauth.authorization_url == "http://auth.example.com/authorize"
        assert config.oauth.token_url == "http://auth.example.com/token"
        assert config.oauth.client_id == "my-client"
        assert config.oauth.scopes == ["mcp:tools", "read"]

    def test_oauth_config_default_none(self):
        config = MCPConfig(url="http://example.com:8000")
        assert config.oauth is None

    def test_oauth_config_defaults(self):
        oauth = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://auth/authorize",
            token_url="http://auth/token",
            client_id="cid",
        )
        assert oauth.scopes is None


class TestOAuthKeyExchange:
    def test_encrypt_decrypt_roundtrip(self):
        kx = OAuthKeyExchange()
        public_key_pem = kx.get_public_key_pem()

        # Simulate frontend: encrypt with public key
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        plaintext = '{"code": "auth-code-123", "redirect_uri": "http://localhost/callback"}'
        ciphertext = public_key.encrypt(
            plaintext.encode(),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        encrypted_b64 = base64.b64encode(ciphertext).decode()

        # Holmes side: decrypt
        decrypted = kx.decrypt(encrypted_b64)
        assert decrypted == plaintext

    def test_public_key_is_valid_pem(self):
        kx = OAuthKeyExchange()
        pem = kx.get_public_key_pem()
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert pem.strip().endswith("-----END PUBLIC KEY-----")

    def test_cli_instances_share_persisted_key(self, tmp_path):
        """CLI mode: two instances with the same store dir produce the same key (persisted)."""
        key_dir = str(tmp_path / "cli_keys")
        kx1 = OAuthKeyExchange(key_store_dir=key_dir)
        kx2 = OAuthKeyExchange(key_store_dir=key_dir)
        assert kx1.get_public_key_pem() == kx2.get_public_key_pem(), "CLI instances should share persisted key"

    def test_different_store_dirs_have_different_keys(self, tmp_path):
        """Different store dirs produce different keys (no sharing)."""
        kx1 = OAuthKeyExchange(key_store_dir=str(tmp_path / "a"))
        kx2 = OAuthKeyExchange(key_store_dir=str(tmp_path / "b"))
        assert kx1.get_public_key_pem() != kx2.get_public_key_pem()

    def test_same_signing_key_produces_same_keypair(self, tmp_path):
        """Same signing_key generates identical public+private keys across 5 instances."""
        signing_key = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        key_dir = str(tmp_path / "keys")
        keys = []
        for _ in range(5):
            kx = OAuthKeyExchange(signing_key=signing_key, key_store_dir=key_dir)
            keys.append(kx.get_public_key_pem())
            # Verify private key works too (decrypt roundtrip)
            pub = serialization.load_pem_public_key(kx.get_public_key_pem().encode())
            ct = pub.encrypt(
                b"test-payload",
                padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
            )
            assert kx.decrypt(base64.b64encode(ct).decode()) == "test-payload"

        # All 5 should be identical (loaded from persisted file after first generation)
        assert all(k == keys[0] for k in keys), "Same signing_key must produce identical keys every time"

    def test_different_signing_keys_produce_different_keypairs(self, tmp_path):
        """Different signing_keys generate different keypairs."""
        kx1 = OAuthKeyExchange(signing_key="uuid-aaaa-1111-bbbb-2222", key_store_dir=str(tmp_path / "a"))
        kx2 = OAuthKeyExchange(signing_key="uuid-cccc-3333-dddd-4444", key_store_dir=str(tmp_path / "b"))
        assert kx1.get_public_key_pem() != kx2.get_public_key_pem(), "Different signing_keys must produce different keys"


class TestPKCE:
    def test_generate_pkce(self):
        verifier, challenge = _generate_pkce()
        assert len(verifier) <= 128
        assert len(verifier) >= 43
        assert len(challenge) > 0
        # Challenge should be base64url-encoded (no padding)
        assert "=" not in challenge
        assert "+" not in challenge
        assert "/" not in challenge

    def test_pkce_different_each_time(self):
        v1, c1 = _generate_pkce()
        v2, c2 = _generate_pkce()
        assert v1 != v2
        assert c1 != c2


class TestOAuthTokenCache:
    def test_set_and_get(self):
        cache = OAuthTokenCache()
        cache.set("conv-1", "token-abc", expires_in=60)
        assert cache.get("conv-1") == "token-abc"

    def test_has(self):
        cache = OAuthTokenCache()
        assert not cache.has("conv-1")
        cache.set("conv-1", "token-abc", expires_in=60)
        assert cache.has("conv-1")

    def test_expired_entry(self):
        cache = OAuthTokenCache()
        # Set with 0 expires_in — the code does max(expires_in - 30, 10) so minimum is 10s
        # Instead, directly manipulate the cache entry to test expiry
        cache.set("conv-exp", "token-abc", expires_in=31)  # will be 1 second after buffer
        # Manually expire it
        cache._cache["conv-exp"].expires_at = time.monotonic() - 1
        cache._cache["conv-exp"].refresh_expires_at = time.monotonic() - 1
        assert cache.get("conv-exp") is None
        assert not cache.has("conv-exp")

    def test_different_conversations(self):
        cache = OAuthTokenCache()
        cache.set("conv-1", "token-1", expires_in=60)
        cache.set("conv-2", "token-2", expires_in=60)
        assert cache.get("conv-1") == "token-1"
        assert cache.get("conv-2") == "token-2"


class TestGetConversationKey:
    def test_with_conversation_id_header(self):
        ctx = {"headers": {"X-Conversation-Id": "abc-123"}}
        assert _get_conversation_key(ctx) == "abc-123"

    def test_with_session_id_header(self):
        ctx = {"headers": {"X-Session-Id": "sess-456"}}
        assert _get_conversation_key(ctx) == "sess-456"

    def test_without_headers_returns_default(self):
        assert _get_conversation_key(None) == "__default__"
        assert _get_conversation_key({}) == "__default__"


class TestRequiresApproval:
    def _make_tool(self, oauth_config=None):
        toolset = RemoteMCPToolset(name="test-oauth", enabled=True)
        toolset._mcp_config = MCPConfig(
            url="http://mcp-server:8000",
            mode=MCPMode.STREAMABLE_HTTP,
            oauth=oauth_config,
        )
        return RemoteMCPTool(
            name="test_tool",
            description="test",
            parameters={},
            toolset=toolset,
        )

    def _make_context(self, conv_id="test-conv", tool_call_id="tc-123"):
        ctx = MagicMock()
        ctx.user_approved = False
        ctx.tool_call_id = tool_call_id
        ctx.request_context = {"headers": {"X-Conversation-Id": conv_id}}
        return ctx

    def test_requires_approval_with_oauth_metadata(self):
        oauth = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak/authorize",
            token_url="http://keycloak/token",
            client_id="cid",
            scopes=["mcp:tools"],
        )
        tool = self._make_tool(oauth)
        context = self._make_context(conv_id="approval-test-1")
        _oauth_token_cache._cache.pop("approval-test-1", None)

        params = {"a": 1}
        result = tool.requires_approval(params, context)

        assert result is not None
        assert result.needs_approval is True
        assert "OAuth authentication required" in result.reason

        meta = params["__oauth_metadata"]
        assert meta["authorization_url"] == "http://keycloak/authorize"
        assert meta["client_id"] == "cid"
        assert meta["scopes"] == ["mcp:tools"]
        assert meta["code_challenge_method"] == "S256"
        assert len(meta["code_challenge"]) > 0
        assert "BEGIN PUBLIC KEY" in meta["encryption_public_key"]
        # token_url should NOT be sent to frontend
        assert "token_url" not in meta

    def test_no_approval_when_token_cached(self):
        oauth = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak/authorize",
            token_url="http://keycloak/token",
            client_id="cid",
        )
        tool = self._make_tool(oauth)
        context = self._make_context(conv_id="cached-conv-2")
        # Cache using the real cache key (conv + idp hash)
        cache_key = _get_oauth_cache_key(oauth, context.request_context)
        _oauth_token_cache.set(cache_key, "some-token")

        result = tool.requires_approval({}, context)
        assert result is None

    def test_no_approval_without_oauth(self):
        tool = self._make_tool(oauth_config=None)
        context = self._make_context()
        result = tool.requires_approval({}, context)
        assert result is None


class TestDecryptCodeAndExchangeForToken:
    def test_full_flow(self):
        """Simulate: Holmes generates keypair+PKCE → frontend encrypts auth code → Holmes exchanges for token."""
        kx = OAuthKeyExchange()
        code_verifier = "test-verifier-12345"
        tool_call_id = "tc-full-flow"
        conv_id = "conv-full-flow"

        oauth_config = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak/authorize",
            token_url="http://keycloak/token",
            client_id="holmes-client",
        )

        # Register the pending exchange
        with _exchanges_lock:
            _pending_exchanges[tool_call_id] = _PendingOAuthExchange(
                key_exchange=kx,
                code_verifier=code_verifier,
                oauth_config=oauth_config,
                redirect_uri="",
            )

        # Simulate frontend encrypting the auth code payload
        public_key = serialization.load_pem_public_key(kx.get_public_key_pem().encode())
        payload = json.dumps({"code": "auth-code-xyz", "redirect_uri": "http://frontend/callback"})
        ciphertext = public_key.encrypt(
            payload.encode(),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        encrypted_b64 = base64.b64encode(ciphertext).decode()

        # Mock the token endpoint response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "final-access-token-abc",
            "token_type": "Bearer",
            "expires_in": 300,
        }
        mock_response.raise_for_status = MagicMock()

        request_context = {"headers": {"X-Conversation-Id": conv_id}}

        with patch("holmes.plugins.toolsets.mcp.toolset_mcp.httpx.post", return_value=mock_response) as mock_post:
            decrypt_code_and_exchange_for_token(tool_call_id, encrypted_b64, request_context)

            # Verify token endpoint was called correctly
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert call_kwargs[0][0] == "http://keycloak/token"
            post_data = call_kwargs[1]["data"]
            assert post_data["grant_type"] == "authorization_code"
            assert post_data["code"] == "auth-code-xyz"
            assert post_data["client_id"] == "holmes-client"
            assert post_data["code_verifier"] == code_verifier
            assert post_data["redirect_uri"] == "http://frontend/callback"

        # Verify token was cached using the real cache key
        cache_key = _get_oauth_cache_key(oauth_config, request_context)
        assert _oauth_token_cache.get(cache_key) == "final-access-token-abc"

        # Pending exchange should be consumed
        assert tool_call_id not in _pending_exchanges

    def test_missing_exchange_does_not_crash(self):
        """Gracefully handle missing pending exchange."""
        decrypt_code_and_exchange_for_token("nonexistent-id", "garbage", None)
        # Should log error but not raise


class TestOAuthCacheKeySharedIdP:
    """Tests that MCP servers sharing the same IdP share the same token."""

    def test_same_idp_same_cache_key(self):
        """Two MCP servers using the same authorization_url + client_id get the same cache key."""
        oauth1 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak:8080/realms/mcp/protocol/openid-connect/auth",
            token_url="http://keycloak:8080/realms/mcp/protocol/openid-connect/token",
            client_id="holmes-client",
        )
        oauth2 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak:8080/realms/mcp/protocol/openid-connect/auth",
            token_url="http://internal-keycloak:8080/realms/mcp/protocol/openid-connect/token",  # different token_url
            client_id="holmes-client",
        )
        ctx = {"headers": {"X-Conversation-Id": "conv-shared"}}
        key1 = _get_oauth_cache_key(oauth1, ctx)
        key2 = _get_oauth_cache_key(oauth2, ctx)
        assert key1 == key2, "Same authorization_url + client_id should produce same cache key"

    def test_different_idp_different_cache_key(self):
        """Two MCP servers using different IdPs get different cache keys."""
        oauth1 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak-a:8080/auth",
            token_url="http://keycloak-a:8080/token",
            client_id="holmes-client",
        )
        oauth2 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak-b:8080/auth",
            token_url="http://keycloak-b:8080/token",
            client_id="holmes-client",
        )
        ctx = {"headers": {"X-Conversation-Id": "conv-diff"}}
        key1 = _get_oauth_cache_key(oauth1, ctx)
        key2 = _get_oauth_cache_key(oauth2, ctx)
        assert key1 != key2, "Different authorization_urls should produce different cache keys"

    def test_different_client_id_different_cache_key(self):
        """Same IdP but different client_id gets different cache key."""
        oauth1 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak:8080/auth",
            token_url="http://keycloak:8080/token",
            client_id="client-a",
        )
        oauth2 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak:8080/auth",
            token_url="http://keycloak:8080/token",
            client_id="client-b",
        )
        ctx = {"headers": {"X-Conversation-Id": "conv-cid"}}
        key1 = _get_oauth_cache_key(oauth1, ctx)
        key2 = _get_oauth_cache_key(oauth2, ctx)
        assert key1 != key2, "Different client_ids should produce different cache keys"

    def test_different_conversation_different_cache_key(self):
        """Same IdP + client but different conversation gets different cache key."""
        oauth = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak:8080/auth",
            token_url="http://keycloak:8080/token",
            client_id="holmes",
        )
        ctx1 = {"headers": {"X-Conversation-Id": "conv-1"}}
        ctx2 = {"headers": {"X-Conversation-Id": "conv-2"}}
        key1 = _get_oauth_cache_key(oauth, ctx1)
        key2 = _get_oauth_cache_key(oauth, ctx2)
        assert key1 != key2, "Different conversations should produce different cache keys"

    def test_shared_token_across_mcp_servers(self):
        """Token cached for one MCP server is reusable by another with same IdP."""
        oauth1 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak:8080/auth",
            token_url="http://keycloak:8080/token",
            client_id="shared-client",
        )
        oauth2 = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://keycloak:8080/auth",
            token_url="http://internal:8080/token",  # different token_url, same auth
            client_id="shared-client",
        )
        ctx = {"headers": {"X-Conversation-Id": "conv-share-test"}}

        # Cache token via first MCP server's config
        cache_key1 = _get_oauth_cache_key(oauth1, ctx)
        _oauth_token_cache.set(cache_key1, "shared-token-xyz", expires_in=300)

        # Second MCP server should find the same token
        cache_key2 = _get_oauth_cache_key(oauth2, ctx)
        assert _oauth_token_cache.get(cache_key2) == "shared-token-xyz"


class TestLiveAtlassianOAuthDiscovery:
    """Live tests against Atlassian's MCP server OAuth discovery.

    These tests hit real Atlassian endpoints to verify our discovery logic
    matches what the MCP SDK does. No authentication is needed — only discovery.

    Run with: poetry run pytest tests/test_mcp_oauth.py -k "LiveAtlassian" -v --no-cov
    """

    ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

    def test_atlassian_returns_401_on_unauthenticated_request(self):
        """Verify the MCP server returns 401 without a token."""
        response = httpx.post(
            self.ATLASSIAN_MCP_URL,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}, "id": 1},
            timeout=15,
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_atlassian_prm_not_available(self):
        """Atlassian doesn't serve RFC 9728 Protected Resource Metadata — verify graceful fallback."""
        # Root-based
        r1 = httpx.get("https://mcp.atlassian.com/.well-known/oauth-protected-resource", timeout=10)
        assert r1.status_code != 200, f"Unexpected PRM at root: {r1.status_code}"

        # Path-based
        r2 = httpx.get("https://mcp.atlassian.com/.well-known/oauth-protected-resource/v1/mcp", timeout=10)
        assert r2.status_code != 200, f"Unexpected PRM at path: {r2.status_code}"

    def test_atlassian_legacy_oauth_metadata_available(self):
        """Atlassian serves OAuth metadata at the legacy well-known path on the MCP server."""
        response = httpx.get(
            "https://mcp.atlassian.com/.well-known/oauth-authorization-server",
            timeout=10,
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()

        assert "authorization_endpoint" in data, f"Missing authorization_endpoint. Keys: {list(data.keys())}"
        assert "token_endpoint" in data, f"Missing token_endpoint. Keys: {list(data.keys())}"
        assert "registration_endpoint" in data, f"Missing registration_endpoint (DCR). Keys: {list(data.keys())}"
        assert "authorization_code" in data.get("grant_types_supported", []), f"authorization_code not in grant_types: {data.get('grant_types_supported')}"
        assert "refresh_token" in data.get("grant_types_supported", []), f"refresh_token not in grant_types: {data.get('grant_types_supported')}"

    def test_atlassian_dcr_succeeds(self):
        """Dynamic Client Registration works with Atlassian's auth server."""
        # First get the registration endpoint
        metadata = httpx.get(
            "https://mcp.atlassian.com/.well-known/oauth-authorization-server",
            timeout=10,
        ).json()
        registration_endpoint = metadata["registration_endpoint"]

        # Register a client
        dcr_response = httpx.post(
            registration_endpoint,
            json={
                "client_name": "HolmesGPT OAuth Test",
                "redirect_uris": ["http://127.0.0.1:0/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            timeout=15,
        )
        assert dcr_response.status_code in (200, 201), f"DCR failed: HTTP {dcr_response.status_code} - {dcr_response.text[:300]}"

        dcr_data = dcr_response.json()
        assert "client_id" in dcr_data, f"No client_id in DCR response. Keys: {list(dcr_data.keys())}"
        assert len(dcr_data["client_id"]) > 0, "Empty client_id"

    def test_full_discovery_flow_via_toolset(self):
        """End-to-end: RemoteMCPToolset auto-discovers all OAuth endpoints for Atlassian."""
        toolset = RemoteMCPToolset(name="atlassian-test", enabled=True)
        toolset._mcp_config = MCPConfig(
            url=self.ATLASSIAN_MCP_URL,
            mode=MCPMode.STREAMABLE_HTTP,
            oauth=MCPOAuthConfig(enabled=True),
        )

        # Simulate the initial 401 response
        initial_response = httpx.post(
            self.ATLASSIAN_MCP_URL,
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}, "id": 1},
            timeout=15,
        )
        assert initial_response.status_code == 401

        # Run discovery
        result = toolset._discover_oauth_endpoints(self.ATLASSIAN_MCP_URL, initial_response)
        assert result is True, "Discovery should succeed for Atlassian"

        oauth = toolset._mcp_config.oauth
        assert oauth.authorization_url is not None, "authorization_url should be discovered"
        assert oauth.token_url is not None, "token_url should be discovered"
        assert oauth.registration_endpoint is not None, "registration_endpoint should be discovered for deferred DCR"
        # client_id is None because DCR is deferred to runtime (CLI or frontend handles it)
        assert "atlassian" in oauth.authorization_url.lower() or "mcp" in oauth.authorization_url.lower(), f"Unexpected authorization_url: {oauth.authorization_url}"

    def test_full_oauth_flow_with_browser(self):
        """End-to-end: discover endpoints, register client, open browser for user login.

        Run with: poetry run pytest tests/test_mcp_oauth.py -k "test_full_oauth_flow_with_browser" -v --no-cov -s
        """
        import webbrowser
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from urllib.parse import urlparse, parse_qs, urlencode

        # Step 1: Discover OAuth endpoints
        toolset = RemoteMCPToolset(name="atlassian-live", enabled=True)
        toolset._mcp_config = MCPConfig(
            url=self.ATLASSIAN_MCP_URL,
            mode=MCPMode.STREAMABLE_HTTP,
            oauth=MCPOAuthConfig(enabled=True),
        )
        initial_response = httpx.post(
            self.ATLASSIAN_MCP_URL,
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}, "id": 1},
            timeout=15,
        )
        result = toolset._discover_oauth_endpoints(self.ATLASSIAN_MCP_URL, initial_response)
        assert result is True, "Discovery failed"

        oauth = toolset._mcp_config.oauth
        print(f"\n  authorization_url: {oauth.authorization_url}")
        print(f"  token_url: {oauth.token_url}")
        print(f"  client_id: {oauth.client_id}")

        # Step 2: Generate PKCE
        code_verifier, code_challenge = _generate_pkce()

        # Step 3: Start local callback server (port 0 = OS-assigned)
        auth_code_result = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                if "code" in params:
                    auth_code_result["code"] = params["code"][0]
                    auth_code_result["state"] = params.get("state", [None])[0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<h1>Authenticated! You can close this tab.</h1>")
                else:
                    self.send_response(400)
                    self.end_headers()
                    error = params.get("error", ["unknown"])[0]
                    desc = params.get("error_description", [""])[0]
                    auth_code_result["error"] = f"{error}: {desc}"
                    self.wfile.write(f"<h1>Error: {error} - {desc}</h1>".encode())

            def log_message(self, format, *args):
                pass  # Suppress HTTP logs

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        callback_port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{callback_port}/callback"
        print(f"  Callback server on port {callback_port}")

        # Step 3b: Re-register DCR with actual redirect_uri
        if oauth.registration_endpoint:
            dcr_resp = httpx.post(
                oauth.registration_endpoint,
                json={
                    "client_name": "HolmesGPT OAuth Test",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                timeout=15,
            )
            if dcr_resp.status_code in (200, 201):
                oauth.client_id = dcr_resp.json().get("client_id", oauth.client_id)
                print(f"  Re-registered client_id={oauth.client_id} with redirect_uri={redirect_uri}")

        # Step 4: Build authorization URL and open browser
        import secrets
        state = secrets.token_urlsafe(32)
        auth_params = {
            "response_type": "code",
            "client_id": oauth.client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if oauth.scopes:
            auth_params["scope"] = " ".join(oauth.scopes)

        auth_url = f"{oauth.authorization_url}?{urlencode(auth_params)}"
        print(f"\n  Opening browser for OAuth login: {auth_url[:100]}...")
        webbrowser.open(auth_url)

        # Step 5: Wait for callback
        print("  Waiting for OAuth callback (login in your browser)...")
        server.handle_request()  # blocks until one request
        server.server_close()

        assert "error" not in auth_code_result, f"OAuth error: {auth_code_result.get('error')}"
        assert "code" in auth_code_result, "No auth code received"
        print(f"  Auth code received: {auth_code_result['code'][:20]}...")

        # Step 6: Exchange code for token
        token_response = httpx.post(
            oauth.token_url,
            data={
                "grant_type": "authorization_code",
                "code": auth_code_result["code"],
                "client_id": oauth.client_id,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        assert token_response.status_code == 200, f"Token exchange failed: HTTP {token_response.status_code} - {token_response.text[:300]}"

        token_data = token_response.json()
        assert "access_token" in token_data, f"No access_token in response. Keys: {list(token_data.keys())}"
        print(f"  Access token obtained: {token_data['access_token'][:30]}...")
        print(f"  Token type: {token_data.get('token_type')}")
        print(f"  Expires in: {token_data.get('expires_in')}s")
        print(f"  Has refresh_token: {'refresh_token' in token_data}")

        # Step 7: Use token to list MCP tools
        import asyncio
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.session import ClientSession

        async def list_tools():
            headers = {"Authorization": f"Bearer {token_data['access_token']}"}
            async with streamablehttp_client(self.ATLASSIAN_MCP_URL, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return tools

        tools_result = asyncio.run(list_tools())
        print(f"\n  Discovered {len(tools_result.tools)} tools:")
        for t in tools_result.tools:
            print(f"    - {t.name}: {t.description[:80] if t.description else 'no description'}")

        assert len(tools_result.tools) > 0, "Expected at least one tool from Atlassian MCP server"


class TestCLIOAuthFlow:
    """Tests for the CLI OAuth browser flow with mocked browser/server/network."""

    def _make_oauth_config(self, **overrides):
        defaults = dict(
            enabled=True,
            authorization_url="http://idp.test/authorize",
            token_url="http://idp.test/token",
            client_id="test-client",
            scopes=["mcp:tools"],
        )
        defaults.update(overrides)
        return MCPOAuthConfig(**defaults)

    def test_cli_flow_full_roundtrip(self):
        """Mock browser + callback: DCR → auth URL → callback with code → token exchange."""
        import threading
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from urllib.parse import urlparse, parse_qs

        oauth = self._make_oauth_config()

        # Mock httpx.post for the token exchange
        mock_token_response = MagicMock()
        mock_token_response.status_code = 200
        mock_token_response.json.return_value = {
            "access_token": "cli-test-token-abc",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "cli-refresh-xyz",
        }

        def mock_post(url, **kwargs):
            if "token" in url:
                return mock_token_response
            # DCR
            dcr_resp = MagicMock()
            dcr_resp.status_code = 201
            dcr_resp.json.return_value = {"client_id": "dcr-client-123"}
            return dcr_resp

        # Mock webbrowser.open to simulate the callback instead
        def mock_browser_open(auth_url):
            """Parse the auth URL, extract state, and POST back to the callback server."""
            parsed = urlparse(auth_url)
            params = parse_qs(parsed.query)
            state = params["state"][0]
            redirect_uri = params["redirect_uri"][0]
            redirect_parsed = urlparse(redirect_uri)
            port = redirect_parsed.port

            # Simulate IdP redirecting back with an auth code
            def send_callback():
                import time as _time
                _time.sleep(0.3)  # Give server time to start
                import urllib.request
                callback_url = f"http://127.0.0.1:{port}/callback?code=mock-auth-code-999&state={state}"
                try:
                    urllib.request.urlopen(callback_url, timeout=5)
                except Exception:
                    pass  # Response doesn't matter

            threading.Thread(target=send_callback, daemon=True).start()

        with patch("holmes.plugins.toolsets.mcp.toolset_mcp.httpx.post", side_effect=mock_post), \
             patch("webbrowser.open", side_effect=mock_browser_open):
            result = _cli_oauth_flow(oauth, "test-server")

        assert result is not None, "CLI flow should return token data"
        assert result["access_token"] == "cli-test-token-abc"
        assert result["refresh_token"] == "cli-refresh-xyz"
        assert result["expires_in"] == 3600
        assert "expires_at" in result, "Should add expires_at for disk storage"

    def test_cli_flow_with_dcr(self):
        """CLI flow performs DCR when client_id is None."""
        import threading
        from urllib.parse import urlparse, parse_qs

        oauth = self._make_oauth_config(client_id=None, registration_endpoint="http://idp.test/register")

        def mock_post(url, **kwargs):
            if "register" in url:
                resp = MagicMock()
                resp.status_code = 201
                resp.json.return_value = {"client_id": "dcr-new-client"}
                return resp
            if "token" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"access_token": "dcr-token", "expires_in": 300}
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        def mock_browser_open(auth_url):
            parsed = urlparse(auth_url)
            params = parse_qs(parsed.query)
            state = params["state"][0]
            redirect_uri = params["redirect_uri"][0]
            port = urlparse(redirect_uri).port

            def send_callback():
                import time as _time
                _time.sleep(0.3)
                import urllib.request
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=dcr-code&state={state}", timeout=5)
                except Exception:
                    pass

            threading.Thread(target=send_callback, daemon=True).start()

        with patch("holmes.plugins.toolsets.mcp.toolset_mcp.httpx.post", side_effect=mock_post), \
             patch("webbrowser.open", side_effect=mock_browser_open):
            result = _cli_oauth_flow(oauth, "dcr-test")

        assert result is not None
        assert result["access_token"] == "dcr-token"
        assert oauth.client_id == "dcr-new-client", "DCR should set client_id on the config"

    def test_cli_flow_dcr_cache_key_consistency(self):
        """After DCR changes client_id, cache key should use the new client_id."""
        import threading
        from urllib.parse import urlparse, parse_qs

        oauth = self._make_oauth_config(client_id=None, registration_endpoint="http://idp.test/register")
        ctx = {"headers": {"X-Conversation-Id": "cli-conv"}}

        # Cache key before DCR (client_id=None)
        key_before = _get_oauth_cache_key(oauth, ctx)

        def mock_post(url, **kwargs):
            if "register" in url:
                resp = MagicMock()
                resp.status_code = 201
                resp.json.return_value = {"client_id": "new-dcr-id"}
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"access_token": "tok", "expires_in": 300}
            return resp

        def mock_browser_open(auth_url):
            parsed = urlparse(auth_url)
            params = parse_qs(parsed.query)
            state = params["state"][0]
            port = urlparse(params["redirect_uri"][0]).port

            def send_callback():
                import time as _time
                _time.sleep(0.3)
                import urllib.request
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=c&state={state}", timeout=5)
                except Exception:
                    pass

            threading.Thread(target=send_callback, daemon=True).start()

        with patch("holmes.plugins.toolsets.mcp.toolset_mcp.httpx.post", side_effect=mock_post), \
             patch("webbrowser.open", side_effect=mock_browser_open):
            _cli_oauth_flow(oauth, "key-test")

        # Cache key after DCR (client_id="new-dcr-id")
        key_after = _get_oauth_cache_key(oauth, ctx)

        assert key_before != key_after, "Cache key should change after DCR sets client_id"
        assert oauth.client_id == "new-dcr-id"

    def test_cli_flow_fails_without_endpoints(self):
        """CLI flow returns None when authorization_url or token_url is missing."""
        oauth = MCPOAuthConfig(enabled=True, authorization_url=None, token_url=None, client_id="x")
        result = _cli_oauth_flow(oauth, "no-endpoints")
        assert result is None

    def test_cli_flow_fails_without_client_id_and_no_dcr(self):
        """CLI flow returns None when client_id is None and no registration_endpoint."""
        oauth = MCPOAuthConfig(
            enabled=True,
            authorization_url="http://idp/auth",
            token_url="http://idp/token",
            client_id=None,
            registration_endpoint=None,
        )
        result = _cli_oauth_flow(oauth, "no-dcr")
        assert result is None

    def test_cli_flow_token_exchange_failure(self):
        """CLI flow returns None when token exchange fails."""
        import threading
        from urllib.parse import urlparse, parse_qs

        oauth = self._make_oauth_config()

        def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 401
            resp.text = "invalid_grant"
            return resp

        def mock_browser_open(auth_url):
            parsed = urlparse(auth_url)
            params = parse_qs(parsed.query)
            state = params["state"][0]
            port = urlparse(params["redirect_uri"][0]).port

            def send_callback():
                import time as _time
                _time.sleep(0.3)
                import urllib.request
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=bad&state={state}", timeout=5)
                except Exception:
                    pass

            threading.Thread(target=send_callback, daemon=True).start()

        with patch("holmes.plugins.toolsets.mcp.toolset_mcp.httpx.post", side_effect=mock_post), \
             patch("webbrowser.open", side_effect=mock_browser_open):
            result = _cli_oauth_flow(oauth, "fail-test")

        assert result is None
