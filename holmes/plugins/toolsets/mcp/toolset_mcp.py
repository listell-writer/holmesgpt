import asyncio
import base64
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, TextIO, Tuple, Type, Union

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool as MCP_Tool
from pydantic import AnyUrl, BaseModel, Field, model_validator

from holmes.common.env_vars import SSE_READ_TIMEOUT
from holmes.core.config import config_path_dir
from holmes.core.tools import (
    ApprovalRequirement,
    CallablePrerequisite,
    StructuredToolResult,
    StructuredToolResultStatus,
    Tool,
    ToolInvokeContext,
    ToolParameter,
    Toolset,
)
from holmes.utils.header_rendering import render_header_templates
from holmes.utils.pydantic_utils import ToolsetConfig

logger = logging.getLogger(__name__)
display_logger = logging.getLogger("holmes.display.mcp_toolset")


def _extract_root_error_message(exc: Exception) -> str:
    """Extract the actual error message from an ExceptionGroup.

    When the MCP library's internal asyncio.TaskGroup encounters errors (e.g. auth
    failures, connection refused), the real exception gets wrapped in an
    ExceptionGroup with the unhelpful message "unhandled errors in a TaskGroup
    (1 sub-exception)".  This function unwraps the group to surface the actual
    root-cause error so that users see, for example, "401 Unauthorized" instead.
    """
    current: BaseException = exc
    while hasattr(current, "exceptions") and current.exceptions:
        current = current.exceptions[0]
    return str(current)


# Lock per MCP server URL to serialize calls to the same server
_server_locks: Dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def create_mcp_http_client_factory(verify_ssl: bool = True):
    """Create a factory function for httpx clients with configurable SSL verification."""

    def factory(
        headers: Dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        kwargs: Dict[str, Any] = {
            "follow_redirects": True,
            "verify": verify_ssl,
        }
        if timeout is None:
            kwargs["timeout"] = httpx.Timeout(SSE_READ_TIMEOUT)
        else:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


def get_server_lock(url: str) -> threading.Lock:
    """Get or create a lock for a specific MCP server URL."""
    with _locks_lock:
        if url not in _server_locks:
            _server_locks[url] = threading.Lock()
        return _server_locks[url]


class MCPMode(str, Enum):
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"
    STDIO = "stdio"


class MCPOAuthConfig(BaseModel):
    """OAuth authorization_code config for MCP servers requiring user login."""

    authorization_url: str = Field(description="IdP authorization endpoint URL.")
    token_url: str = Field(description="IdP token endpoint URL.")
    client_id: str = Field(description="OAuth public client ID (no secret needed for PKCE).")
    scopes: Optional[List[str]] = Field(default=None, description="OAuth scopes to request.")


class OAuthKeyExchange:
    """RSA keypair for secure auth code transit from frontend to Holmes.

    Holmes generates a keypair and sends the public key to the frontend.
    The frontend encrypts the OAuth authorization code with it.
    Holmes decrypts with the private key, then exchanges the code for
    a token server-side (so the access token never leaves the cluster).
    """

    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def get_public_key_pem(self) -> str:
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def decrypt(self, encrypted_b64: str) -> str:
        """Decrypt a base64-encoded RSA-OAEP ciphertext."""
        ciphertext = base64.b64decode(encrypted_b64)
        plaintext = self._private_key.decrypt(
            ciphertext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return plaintext.decode()


def _generate_pkce() -> Tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Returns (code_verifier, code_challenge).
    """
    import hashlib
    import secrets

    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


class OAuthTokenCache:
    """TTL cache for OAuth access tokens keyed by conversation ID."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, token = entry
            if time.monotonic() >= expires_at:
                del self._cache[key]
                return None
            return token

    def set(self, key: str, token: str) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic() + self._ttl, token)

    def has(self, key: str) -> bool:
        return self.get(key) is not None


class _PendingOAuthExchange:
    """State for a pending OAuth approval: key exchange, PKCE verifier, and config."""

    def __init__(self, key_exchange: OAuthKeyExchange, code_verifier: str, oauth_config: MCPOAuthConfig, redirect_uri: str) -> None:
        self.key_exchange = key_exchange
        self.code_verifier = code_verifier
        self.oauth_config = oauth_config
        self.redirect_uri = redirect_uri


# Global caches
_oauth_token_cache = OAuthTokenCache()
_pending_exchanges: Dict[str, _PendingOAuthExchange] = {}
_exchanges_lock = threading.Lock()


def _get_conversation_key(request_context: Optional[Dict[str, Any]]) -> str:
    """Extract a conversation key from request context headers."""
    if request_context:
        headers = request_context.get("headers", {})
        for key in ("X-Conversation-Id", "x-conversation-id", "X-Session-Id", "x-session-id"):
            if key in headers:
                return str(headers[key])
    return "__default__"


class MCPConfig(ToolsetConfig):
    mode: MCPMode = Field(
        default=MCPMode.SSE,
        title="Mode",
        description="Connection mode to use when talking to the MCP server.",
        examples=[MCPMode.STREAMABLE_HTTP],
    )
    url: AnyUrl = Field(
        title="URL",
        description="MCP server URL (for SSE or Streamable HTTP modes).",
        examples=["http://example.com:8000/mcp/messages"],
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None,
        title="Headers",
        description="Optional HTTP headers to include in requests (e.g., Authorization).",
        examples=[{"Authorization": "Bearer YOUR_TOKEN"}],
    )
    verify_ssl: bool = Field(
        default=True,
        title="Verify SSL",
        description="Whether to verify SSL certificates (set to false for local/dev servers without valid SSL).",
        examples=[False],
    )
    extra_headers: Optional[Dict[str, str]] = Field(
        default=None,
        title="Extra Headers",
        description="Template headers that will be rendered with request context and environment variables.",
        examples=[
            {
                "X-Custom-Header": "{{ request_context.headers['X-Custom-Header'] }}",
                "X-Api-Key": "{{ env.API_KEY }}",
            }
        ],
    )
    icon_url: str = Field(
        default="https://registry.npmmirror.com/@lobehub/icons-static-png/1.46.0/files/light/mcp.png",
        description="Icon URL for this MCP server, displayed in the UI for tool calls.",
        examples=["https://cdn.simpleicons.org/github/181717"],
    )
    oauth: Optional[MCPOAuthConfig] = Field(
        default=None,
        title="OAuth",
        description="OAuth authorization_code configuration. When set, users authenticate via browser before tools can be used.",
    )

    def get_lock_string(self) -> str:
        return str(self.url)


class StdioMCPConfig(ToolsetConfig):
    mode: MCPMode = Field(
        default=MCPMode.STDIO,
        title="Mode",
        description="Stdio mode runs an MCP server as a local subprocess.",
        examples=[MCPMode.STDIO],
    )
    command: str = Field(
        title="Command",
        description="The command to start the MCP server (e.g., npx, uv, python).",
        examples=["npx"],
    )
    args: Optional[List[str]] = Field(
        default=None,
        title="Arguments",
        description="Arguments to pass to the MCP server command.",
        examples=[["-y", "@modelcontextprotocol/server-github"]],
    )
    env: Optional[Dict[str, str]] = Field(
        default=None,
        title="Environment Variables",
        description="Environment variables to set for the MCP server process.",
        examples=[{"GITHUB_PERSONAL_ACCESS_TOKEN": "{{ env.GITHUB_TOKEN }}"}],
    )
    icon_url: str = Field(
        default="https://registry.npmmirror.com/@lobehub/icons-static-png/1.46.0/files/light/mcp.png",
        description="Icon URL for this MCP server, displayed in the UI for tool calls.",
        examples=["https://cdn.simpleicons.org/github/181717"],
    )

    def get_lock_string(self) -> str:
        return str(self.command)


def _get_mcp_log_file(server_name: str) -> TextIO:
    """Get a file handle for MCP server stderr output.

    Redirects MCP subprocess stderr to ~/.holmes/logs/mcp/<server_name>.log
    so it doesn't pollute the CLI output.
    """
    log_dir = os.path.join(config_path_dir, "logs", "mcp")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{server_name}.log")
    display_logger.info(f"MCP server '{server_name}' logs: {log_path}")
    return open(log_path, "w")


def _inject_oauth_token(
    toolset: "RemoteMCPToolset",
    request_context: Optional[Dict[str, Any]],
    headers: Optional[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    """Inject cached OAuth Bearer token into headers if available."""
    if not isinstance(toolset._mcp_config, MCPConfig) or not toolset._mcp_config.oauth:
        return headers

    conv_key = _get_conversation_key(request_context)
    cached_token = _oauth_token_cache.get(conv_key)
    if cached_token:
        headers = headers or {}
        headers["Authorization"] = f"Bearer {cached_token}"
        logger.debug("OAuth token injected for conversation %s on server %s", conv_key, toolset.name)
    else:
        logger.warning("OAuth MCP server %s: no cached token for conversation %s — request will likely 401", toolset.name, conv_key)
    return headers


def decrypt_code_and_exchange_for_token(tool_call_id: str, encrypted_payload: str, request_context: Optional[Dict[str, Any]]) -> None:
    """Decrypt an OAuth authorization code and exchange it for an access token.

    The frontend encrypts a JSON payload: {"code": "...", "redirect_uri": "..."}.
    Holmes decrypts it, then exchanges the code at the IdP's token_url using the
    PKCE code_verifier (generated during requires_approval). The access token
    stays server-side and never transits through the frontend.

    Called from tool_calling_llm._execute_tool_decisions() when a decision
    includes an encrypted_token from the frontend OAuth flow.
    """
    with _exchanges_lock:
        pending = _pending_exchanges.pop(tool_call_id, None)

    if pending is None:
        logger.error("OAuth exchange failed: no pending key exchange for tool_call_id=%s (possible timeout or duplicate)", tool_call_id)
        return

    try:
        # Decrypt the payload from frontend
        logger.warning("OAuth: decrypting auth code payload for tool_call_id=%s", tool_call_id)
        decrypted = pending.key_exchange.decrypt(encrypted_payload)
        payload = json.loads(decrypted)
        auth_code = payload["code"]
        redirect_uri = payload.get("redirect_uri", "")
        logger.warning("OAuth: auth code decrypted, exchanging at token endpoint %s", pending.oauth_config.token_url)

        # Exchange auth code for access token at the IdP's token endpoint (server-side)
        token_response = httpx.post(
            pending.oauth_config.token_url,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": pending.oauth_config.client_id,
                "code_verifier": pending.code_verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if token_response.status_code != 200:
            logger.error(
                "OAuth token exchange failed: HTTP %d from %s — response: %s",
                token_response.status_code, pending.oauth_config.token_url, token_response.text[:500],
            )
            token_response.raise_for_status()

        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error("OAuth token exchange: response missing 'access_token' field. Keys: %s", list(token_data.keys()))
            return

        conv_key = _get_conversation_key(request_context)
        _oauth_token_cache.set(conv_key, access_token)
        logger.info("OAuth token cached for conversation %s (exchanged via %s)", conv_key, pending.oauth_config.token_url)
    except json.JSONDecodeError:
        logger.exception("OAuth token exchange: failed to parse JSON response from %s", pending.oauth_config.token_url)
    except httpx.HTTPStatusError:
        pass  # Already logged above
    except Exception:
        logger.exception("OAuth token exchange failed (tool_call_id=%s, token_url=%s)", tool_call_id, pending.oauth_config.token_url)


@asynccontextmanager
async def get_initialized_mcp_session(
    toolset: "RemoteMCPToolset", request_context: Optional[Dict[str, Any]] = None
):
    if toolset._mcp_config is None:
        raise ValueError("MCP config is not initialized")

    if isinstance(toolset._mcp_config, StdioMCPConfig):
        server_params = StdioServerParameters(
            command=toolset._mcp_config.command,
            args=toolset._mcp_config.args or [],
            env=toolset._mcp_config.env,
        )
        errlog = _get_mcp_log_file(toolset.name)
        try:
            async with stdio_client(server_params, errlog=errlog) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    _ = await session.initialize()
                    yield session
        finally:
            errlog.close()
    elif toolset._mcp_config.mode == MCPMode.SSE:
        url = str(toolset._mcp_config.url)
        httpx_factory = create_mcp_http_client_factory(toolset._mcp_config.verify_ssl)
        rendered_headers = _inject_oauth_token(toolset, request_context, toolset._render_headers(request_context))
        async with sse_client(
            url,
            rendered_headers,
            sse_read_timeout=SSE_READ_TIMEOUT,
            httpx_client_factory=httpx_factory,
        ) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                _ = await session.initialize()
                yield session
    else:
        url = str(toolset._mcp_config.url)
        httpx_factory = create_mcp_http_client_factory(toolset._mcp_config.verify_ssl)
        rendered_headers = _inject_oauth_token(toolset, request_context, toolset._render_headers(request_context))
        async with streamablehttp_client(
            url,
            headers=rendered_headers,
            sse_read_timeout=SSE_READ_TIMEOUT,
            httpx_client_factory=httpx_factory,
        ) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                _ = await session.initialize()
                yield session


class RemoteMCPTool(Tool):
    toolset: "RemoteMCPToolset" = Field(exclude=True)

    def requires_approval(
        self, params: Dict, context: ToolInvokeContext
    ) -> Optional[ApprovalRequirement]:
        """Prompt user for OAuth browser login when no cached token exists."""
        if not isinstance(self.toolset._mcp_config, MCPConfig) or not self.toolset._mcp_config.oauth:
            return None

        oauth_config = self.toolset._mcp_config.oauth
        conv_key = _get_conversation_key(context.request_context)

        if _oauth_token_cache.has(conv_key):
            logger.debug("OAuth MCP %s: cached token found for conversation %s, skipping approval", self.toolset.name, conv_key)
            return None

        logger.info("OAuth MCP %s: no cached token for conversation %s, requesting user authentication", self.toolset.name, conv_key)

        # Generate keypair for secure auth code transit and PKCE for token exchange
        key_exchange = OAuthKeyExchange()
        code_verifier, code_challenge = _generate_pkce()

        # The frontend must tell us its redirect_uri so we can include it in the token exchange.
        # We provide a placeholder; the frontend will use its own callback URL and include it
        # in the encrypted payload alongside the auth code.
        with _exchanges_lock:
            _pending_exchanges[context.tool_call_id] = _PendingOAuthExchange(
                key_exchange=key_exchange,
                code_verifier=code_verifier,
                oauth_config=oauth_config,
                redirect_uri="",  # Set by frontend in the encrypted payload
            )

        # Inject OAuth metadata into params so the frontend can initiate browser login.
        # Frontend handles: open browser to authorization_url with code_challenge,
        # user logs in, gets auth code, encrypts it with our public key, sends it back.
        # Holmes handles: decrypt auth code, exchange at token_url with code_verifier.
        params["__oauth_metadata"] = {
            "authorization_url": oauth_config.authorization_url,
            "client_id": oauth_config.client_id,
            "scopes": oauth_config.scopes or [],
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "encryption_public_key": key_exchange.get_public_key_pem(),
        }

        return ApprovalRequirement(
            needs_approval=True,
            reason=f"OAuth authentication required for MCP server '{self.toolset.name}'",
        )

    def _is_placeholder_connect_tool(self) -> bool:
        return self.name.endswith("_connect") and "requires OAuth" in self.description

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        try:
            # For OAuth placeholder tools: load real tools after authentication
            if self._is_placeholder_connect_tool():
                return self._invoke_oauth_connect(params, context)

            # Serialize calls to the same MCP server to prevent SSE conflicts
            # Different servers can still run in parallel
            if not self.toolset._mcp_config:
                raise ValueError("MCP config not initialized")

            lock = get_server_lock(str(self.toolset._mcp_config.get_lock_string()))
            with lock:
                return asyncio.run(self._invoke_async(params, context.request_context))
        except Exception as e:
            error_detail = _extract_root_error_message(e)
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=error_detail,
                params=params,
                invocation=f"MCPtool {self.name} with params {params}",
            )

    def _invoke_oauth_connect(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        """Handle the OAuth placeholder tool: load real tools from the MCP server after authentication."""
        try:
            if not self.toolset._mcp_config:
                raise ValueError("MCP config not initialized")

            lock = get_server_lock(str(self.toolset._mcp_config.get_lock_string()))
            with lock:
                tools_result = asyncio.run(self.toolset._get_server_tools_with_context(context.request_context))

            real_tools = [RemoteMCPTool.create(tool, self.toolset) for tool in tools_result.tools]

            if real_tools:
                # Replace the placeholder with real tools on the toolset
                self.toolset.tools = real_tools

                # Register new tools in the tool executor so the LLM can call them
                tool_executor = getattr(context.llm, "tool_executor", None)
                if tool_executor:
                    # Remove the placeholder
                    tool_executor.tools_by_name.pop(self.name, None)
                    tool_executor._tool_to_toolset.pop(self.name, None)
                    # Register real tools
                    for tool in real_tools:
                        tool_executor.tools_by_name[tool.name] = tool
                        tool_executor._tool_to_toolset[tool.name] = self.toolset

                tool_names = [t.name for t in real_tools]
                logger.warning("OAuth MCP %s: loaded %d tools after authentication: %s", self.toolset.name, len(real_tools), tool_names)
                return StructuredToolResult(
                    status=StructuredToolResultStatus.SUCCESS,
                    data=f"Successfully authenticated and discovered {len(real_tools)} tools: {', '.join(tool_names)}. You can now call these tools directly.",
                    params=params,
                    invocation=f"OAuth connect to {self.toolset.name}",
                )
            else:
                return StructuredToolResult(
                    status=StructuredToolResultStatus.ERROR,
                    error=f"Authenticated but no tools found on MCP server {self.toolset.name}",
                    params=params,
                    invocation=f"OAuth connect to {self.toolset.name}",
                )
        except Exception as e:
            error_detail = _extract_root_error_message(e)
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=f"OAuth connect failed: {error_detail}",
                params=params,
                invocation=f"OAuth connect to {self.toolset.name}",
            )

    @staticmethod
    def _is_content_error(content: str) -> bool:
        try:  # aws mcp sometimes returns an error in content - status code != 200
            json_content: dict = json.loads(content)
            status_code = json_content.get("response", {}).get("status_code", 200)
            return status_code >= 300
        except Exception:
            return False

    async def _invoke_async(
        self, params: Dict, request_context: Optional[Dict[str, Any]]
    ) -> StructuredToolResult:
        async with get_initialized_mcp_session(
            self.toolset, request_context
        ) as session:
            tool_result = await session.call_tool(self.name, params)

        merged_text = " ".join(c.text for c in tool_result.content if c.type == "text")

        is_error = tool_result.isError or self._is_content_error(merged_text)

        images = None
        if not is_error:
            images = [
                {"data": c.data, "mimeType": c.mimeType}
                for c in tool_result.content
                if c.type == "image"
            ] or None

        return StructuredToolResult(
            status=(
                StructuredToolResultStatus.ERROR if is_error
                else StructuredToolResultStatus.SUCCESS
            ),
            data=merged_text,
            images=images,
            params=params,
            invocation=f"MCPtool {self.name} with params {params}",
        )

    @classmethod
    def create(
        cls,
        tool: MCP_Tool,
        toolset: "RemoteMCPToolset",
    ):
        parameters = cls.parse_input_schema(tool.inputSchema)
        return cls(
            name=tool.name,
            description=tool.description or "",
            parameters=parameters,
            toolset=toolset,
        )

    @classmethod
    def parse_input_schema(
        cls, input_schema: dict[str, Any]
    ) -> Dict[str, ToolParameter]:
        required_list = input_schema.get("required", [])
        schema_params = input_schema.get("properties", {})
        parameters = {}
        for key, val in schema_params.items():
            parameters[key] = cls._parse_tool_parameter(
                val, root_schema=input_schema, required=key in required_list
            )

        return parameters

    @classmethod
    def _resolve_schema(
        cls, schema: dict[str, Any], root_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolves $ref and extracts the first non-null type from anyOf/oneOf/allOf."""
        if not isinstance(schema, dict):
            return schema

        # 1. Resolve $ref
        if "$ref" in schema:
            ref_path = str(schema["$ref"])
            if ref_path.startswith("#/"):
                parts = ref_path[2:].split("/")
                resolved = root_schema
                for part in parts:
                    if isinstance(resolved, dict):
                        resolved = resolved.get(part, {})
                    else:
                        resolved = {}
                        break

                # Recursively resolve the matched definition in case it contains more refs/anyOf
                resolved_schema = dict(schema)
                resolved_schema.pop("$ref")
                resolved_schema.update(cls._resolve_schema(resolved, root_schema))
                return resolved_schema

        # 2. Handle anyOf / oneOf / allOf for nullable or union types
        for compound_key in ["anyOf", "oneOf", "allOf"]:
            if compound_key in schema and isinstance(schema[compound_key], list):
                if compound_key == "allOf":
                    merged = dict(schema)
                    merged.pop(compound_key)
                    for sub_schema in schema[compound_key]:
                        if isinstance(sub_schema, dict):
                            resolved_sub = cls._resolve_schema(sub_schema, root_schema)
                            if resolved_sub.get("type") != "null":
                                for k, v in resolved_sub.items():
                                    if k == "properties" and isinstance(v, dict):
                                        merged.setdefault("properties", {}).update(v)
                                    elif k == "required" and isinstance(v, list):
                                        reqs = merged.setdefault("required", [])
                                        for req in v:
                                            if req not in reqs:
                                                reqs.append(req)
                                    elif k == "type":
                                        if "type" not in merged or merged["type"] == "null":
                                            merged["type"] = v
                                    else:
                                        merged[k] = v
                    return merged
                else:
                    for sub_schema in schema[compound_key]:
                        if isinstance(sub_schema, dict):
                            resolved_sub = cls._resolve_schema(sub_schema, root_schema)
                            # Skip null types, pick the first valid underlying schema type
                            if resolved_sub.get("type") != "null":
                                merged = dict(schema)
                                merged.pop(compound_key)
                                merged.update(resolved_sub)
                                return merged

        return schema

    @classmethod
    def _parse_tool_parameter(
        cls, schema: dict[str, Any], root_schema: dict[str, Any], required: bool = True
    ) -> ToolParameter:
        """Recursively parse a JSON Schema property into a ToolParameter.

        This preserves nested items, properties, and enum from MCP tool schemas
        so that the OpenAI-formatted schema sent to the LLM accurately describes
        complex parameter types (arrays, objects).
        """
        schema = cls._resolve_schema(schema, root_schema)

        param_type = schema.get("type", "string")

        items = None
        if "items" in schema and isinstance(schema["items"], dict):
            items = cls._parse_tool_parameter(
                schema["items"], root_schema, required=True
            )

        properties = None
        if "properties" in schema and isinstance(schema["properties"], dict):
            nested_required = schema.get("required", [])
            properties = {
                name: cls._parse_tool_parameter(
                    prop, root_schema, required=name in nested_required
                )
                for name, prop in schema["properties"].items()
            }

        enum = schema.get("enum")

        additional_properties = None
        raw_ap = schema.get("additionalProperties")
        if raw_ap is not None:
            if isinstance(raw_ap, bool):
                additional_properties = raw_ap
            elif isinstance(raw_ap, dict):
                # Resolve $ref pointers so the LLM sees concrete types, but
                # preserve compound keywords (anyOf/oneOf) intact — _resolve_schema
                # collapses those to a single branch which loses type information
                # (e.g. string|array becomes just string).
                if "$ref" in raw_ap:
                    additional_properties = cls._resolve_schema(raw_ap, root_schema)
                else:
                    additional_properties = raw_ap

        # Capture JSON Schema validation keywords that aren't modeled as
        # dedicated ToolParameter fields.  These are passed through to the
        # OpenAI-formatted schema so the LLM sees constraints like array
        # length limits, numeric ranges, and string patterns.
        _PASSTHROUGH_KEYWORDS = {
            "minItems", "maxItems",
            "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
            "minLength", "maxLength",
            "pattern",
            "default",
        }
        json_schema_extra = {k: v for k, v in schema.items() if k in _PASSTHROUGH_KEYWORDS}

        return ToolParameter(
            description=schema.get("description"),
            type=param_type,
            required=required,
            items=items,
            properties=properties,
            enum=enum,
            additional_properties=additional_properties,
            json_schema_extra=json_schema_extra or None,
        )

    def get_parameterized_one_liner(self, params: Dict) -> str:
        # AWS MCP cli_command
        if params and params.get("cli_command"):
            return f"{params.get('cli_command')}"

        # gcloud MCP run_gcloud_command
        if self.name == "run_gcloud_command" and params and "args" in params:
            args = params.get("args", [])
            if isinstance(args, list):
                return f"gcloud {' '.join(str(arg) for arg in args)}"

        if self.name and params and "args" in params:
            args = params.get("args", [])
            if isinstance(args, list):
                return f"{self.name} {' '.join(str(arg) for arg in args)}"

        return f"{self.toolset.name}: {self.name} {params}"


class RemoteMCPToolset(Toolset):
    config_classes: ClassVar[list[Type[Union[MCPConfig, StdioMCPConfig]]]] = [
        MCPConfig,
        StdioMCPConfig,
    ]
    description: str = "MCP server toolset"
    tools: List[RemoteMCPTool] = Field(default_factory=list)  # type: ignore
    _mcp_config: Optional[Union[MCPConfig, StdioMCPConfig]] = None

    def _render_headers(
        self, request_context: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, str]]:
        """
        Merge and render headers for MCP connection.

        Process:
        1. Start with 'headers' field (backward compatibility, passed as-is)
        2. Render 'extra_headers' via Jinja2 templates
        3. Merge them (later layers take precedence)

        Returns:
            Merged headers dictionary or None
        """
        if not isinstance(self._mcp_config, MCPConfig):
            return None

        # Start with direct headers (no rendering, backward compatibility)
        final_headers: Dict[str, str] = {}
        if self._mcp_config.headers:
            final_headers.update(self._mcp_config.headers)

        # Render and merge config-level extra_headers
        if self._mcp_config.extra_headers:
            rendered = render_header_templates(
                extra_headers=self._mcp_config.extra_headers,
                request_context=request_context,
                source_name=self.name,
            )
            if rendered:
                final_headers.update(rendered)

        return final_headers if final_headers else None

    def model_post_init(self, __context: Any) -> None:
        self.prerequisites = [
            CallablePrerequisite(callable=self.prerequisites_callable)
        ]
        # Set icon from config if specified
        if self.icon_url is None and self.config:
            self.icon_url = self.config.get("icon_url")

    @model_validator(mode="before")
    @classmethod
    def migrate_url_to_config(cls, values: dict[str, Any]) -> dict[str, Any]:
        """
        Migrates url from field parameter to config object.
        If url is passed as a parameter, it's moved to config (or config is created if it doesn't exist).
        """
        if not isinstance(values, dict) or "url" not in values:
            return values

        url_value = values.pop("url")
        if url_value is None:
            return values

        config = values.get("config")
        if config is None:
            config = {}
            values["config"] = config

        toolset_name = values.get("name", "unknown")
        if "url" in config:
            logging.warning(
                f"Toolset {toolset_name}: has two urls defined, remove the 'url' field from the toolset configuration and keep the 'url' in the config section."
            )
            return values

        logging.warning(
            f"Toolset {toolset_name}: 'url' field has been migrated to config. "
            "Please move 'url' to the config section."
        )
        config["url"] = url_value
        return values

    def prerequisites_callable(self, config) -> Tuple[bool, str]:
        try:
            if not config:
                return (False, f"Config is required for {self.name}")

            mode_value = config.get("mode", MCPMode.SSE.value)
            allowed_modes = [e.value for e in MCPMode]
            if mode_value not in allowed_modes:
                return (
                    False,
                    f'Invalid mode "{mode_value}", allowed modes are {", ".join(allowed_modes)}',
                )

            if mode_value == MCPMode.STDIO.value:
                self._mcp_config = StdioMCPConfig(**config)
            else:
                self._mcp_config = MCPConfig(**config)
                clean_url_str = str(self._mcp_config.url).rstrip("/")

                if self._mcp_config.mode == MCPMode.SSE and not clean_url_str.endswith(
                    "/sse"
                ):
                    self._mcp_config.url = AnyUrl(clean_url_str + "/sse")

            # For OAuth-protected servers, skip full MCP session init (it will 401).
            # Just verify the server is reachable and register a placeholder tool
            # that triggers the OAuth flow on first use. Tools are loaded after auth.
            if isinstance(self._mcp_config, MCPConfig) and self._mcp_config.oauth:
                return self._check_oauth_server_reachable()

            tools_result = asyncio.run(self._get_server_tools())

            self.tools = [
                RemoteMCPTool.create(tool, self) for tool in tools_result.tools
            ]

            if not self.tools:
                logging.warning(f"mcp server {self.name} loaded 0 tools.")

            return (True, "")
        except Exception as e:
            error_detail = _extract_root_error_message(e)
            return (
                False,
                f"Failed to load mcp server {self.name}: {error_detail}"
                ". If the server is still starting up, Holmes will retry automatically",
            )

    def _check_oauth_server_reachable(self) -> Tuple[bool, str]:
        """For OAuth MCP servers, verify reachability without authenticating.

        If a cached token exists (from a previous request in the same conversation),
        load the real tools directly. Otherwise, register a placeholder tool that
        triggers the OAuth flow on first use.
        """
        assert isinstance(self._mcp_config, MCPConfig)
        url = str(self._mcp_config.url).rstrip("/")

        # If we already have a cached token, try to load real tools directly
        # This happens on subsequent requests in the same conversation after OAuth
        if _oauth_token_cache.has("__default__"):
            try:
                tools_result = asyncio.run(self._get_server_tools())
                self.tools = [RemoteMCPTool.create(tool, self) for tool in tools_result.tools]
                if self.tools:
                    logging.warning(f"OAuth MCP server {self.name}: loaded {len(self.tools)} tools using cached token")
                    return (True, "")
            except Exception as e:
                logging.warning(f"OAuth MCP server {self.name}: cached token failed, falling back to placeholder: {_extract_root_error_message(e)}")

        try:
            # Try the well-known endpoint first (no auth needed)
            response = httpx.get(
                f"{url}/.well-known/oauth-protected-resource",
                timeout=10,
                verify=self._mcp_config.verify_ssl,
            )
            if response.status_code not in (200, 401):
                # Also try the root — a 401 means server is up but needs auth
                response = httpx.post(url, timeout=10, verify=self._mcp_config.verify_ssl)

            if response.status_code not in (200, 401):
                return (False, f"MCP server {self.name} returned HTTP {response.status_code}")

        except Exception as e:
            return (False, f"MCP server {self.name} unreachable: {_extract_root_error_message(e)}")

        # Register a placeholder tool that will trigger OAuth on first call.
        # After auth succeeds, _invoke will load the real tools dynamically.
        from mcp.types import Tool as MCP_Tool
        placeholder = MCP_Tool(
            name=f"{self.name}_connect",
            description=f"Connect to {self.name} (requires OAuth authentication). Call this tool to authenticate and discover available tools.",
            inputSchema={"type": "object", "properties": {}},
        )
        self.tools = [RemoteMCPTool.create(placeholder, self)]
        logging.info(f"OAuth MCP server {self.name} is reachable, registered placeholder tool (auth required)")
        return (True, "")

    async def _get_server_tools(self):
        async with get_initialized_mcp_session(self, None) as session:
            return await session.list_tools()

    async def _get_server_tools_with_context(self, request_context: Optional[Dict[str, Any]]):
        async with get_initialized_mcp_session(self, request_context) as session:
            return await session.list_tools()
