"""Tests for MCP OAuth authorization_code support."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

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
    _generate_pkce,
    _get_conversation_key,
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

    def test_different_instances_have_different_keys(self):
        kx1 = OAuthKeyExchange()
        kx2 = OAuthKeyExchange()
        assert kx1.get_public_key_pem() != kx2.get_public_key_pem()


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
        cache = OAuthTokenCache(ttl_seconds=60)
        cache.set("conv-1", "token-abc")
        assert cache.get("conv-1") == "token-abc"

    def test_has(self):
        cache = OAuthTokenCache(ttl_seconds=60)
        assert not cache.has("conv-1")
        cache.set("conv-1", "token-abc")
        assert cache.has("conv-1")

    def test_expired_entry(self):
        cache = OAuthTokenCache(ttl_seconds=0)
        cache.set("conv-1", "token-abc")
        time.sleep(0.01)
        assert cache.get("conv-1") is None
        assert not cache.has("conv-1")

    def test_different_conversations(self):
        cache = OAuthTokenCache(ttl_seconds=60)
        cache.set("conv-1", "token-1")
        cache.set("conv-2", "token-2")
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
            authorization_url="http://keycloak/authorize",
            token_url="http://keycloak/token",
            client_id="cid",
        )
        tool = self._make_tool(oauth)
        _oauth_token_cache.set("cached-conv", "some-token")
        context = self._make_context(conv_id="cached-conv")

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

        # Verify token was cached
        assert _oauth_token_cache.get(conv_id) == "final-access-token-abc"

        # Pending exchange should be consumed
        assert tool_call_id not in _pending_exchanges

    def test_missing_exchange_does_not_crash(self):
        """Gracefully handle missing pending exchange."""
        decrypt_code_and_exchange_for_token("nonexistent-id", "garbage", None)
        # Should log error but not raise
