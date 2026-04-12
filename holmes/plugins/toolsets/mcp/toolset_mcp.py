import asyncio
import json
import logging
import os
import threading
import time
import yaml as _yaml
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, TextIO, Tuple, Type, Union
from urllib.parse import urlparse

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool as MCP_Tool
from pydantic import AnyUrl, BaseModel, Field, model_validator

from holmes.common.env_vars import SSE_READ_TIMEOUT
from holmes.core.oauth_utils import (
    OAuthEndpoints,
    OAuthTokenExchangeError,
    cli_oauth_flow,
    discover_auth_server_from_prm,
    exchange_code_for_tokens,
    fetch_oauth_metadata,
    generate_pkce,
)
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
from holmes.plugins.toolsets.mcp.oauth_token_manager import OAuthTokenManager, _get_user_id
from holmes.plugins.toolsets.mcp.oauth_token_store import (
    DiskTokenStore,
    OAuthTokenCache,
)
from holmes.utils.definitions import RobustaConfig
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
    """OAuth authorization_code config for MCP servers requiring user login.

    Set enabled=true with no other fields to auto-discover OAuth endpoints
    via the MCP OAuth flow (RFC 9728 Protected Resource Metadata + OIDC Discovery + DCR).

    If any of authorization_url, token_url, or client_id is set, enabled defaults to true.
    """

    enabled: bool = Field(default=False, description="Enable OAuth for this MCP server. Auto-set to true when other OAuth fields are provided.")
    authorization_url: Optional[str] = Field(default=None, description="IdP authorization endpoint URL. Auto-discovered if omitted.")
    token_url: Optional[str] = Field(default=None, description="IdP token endpoint URL. Auto-discovered if omitted.")
    client_id: Optional[str] = Field(default=None, description="OAuth public client ID. Auto-registered via DCR if omitted.")
    scopes: Optional[List[str]] = Field(default=None, description="OAuth scopes to request.")
    registration_endpoint: Optional[str] = Field(default=None, exclude=True, description="DCR endpoint (auto-populated during discovery, not user-facing).")

    @model_validator(mode="after")
    def auto_enable_when_configured(self):
        """Auto-enable OAuth when any endpoint or client_id is explicitly set."""
        if not self.enabled and (self.authorization_url or self.token_url or self.client_id):
            self.enabled = True
        return self


def _get_signing_key() -> Optional[str]:
    """Load the signing_key from Robusta's global_config. Returns None in CLI mode."""
    config_file_path = os.environ.get("RUNNER_CONFIG_PATH", "/etc/robusta/config/active_playbooks.yaml")
    if not os.path.exists(config_file_path):
        return None
    try:
        with open(config_file_path) as f:
            yaml_content = _yaml.safe_load(f)
            config = RobustaConfig(**yaml_content)
            return config.global_config.get("signing_key")
    except Exception:
        logger.warning("Failed to load signing_key from Robusta config", exc_info=True)
        return None


# Singleton token manager — the main interface for all token operations
_token_manager = OAuthTokenManager()
_token_manager.set_signing_key_getter(_get_signing_key)

# Backwards-compat aliases used by existing code and tests
_oauth_token_cache = _token_manager.cache
_disk_token_store = _token_manager.disk_store

# ── Per-user MCP tool cache (TTL-based, never mutates the shared executor) ──

_MCP_TOOLS_CACHE_TTL = 300  # 5 minutes


@dataclass
class _LoadedToolsEntry:
    """Cached MCP tools loaded after OAuth authentication."""

    tools: List[Any]  # List[RemoteMCPTool] — forward ref
    toolset: Any  # RemoteMCPToolset — forward ref
    loaded_at: float = field(default_factory=time.monotonic)


_mcp_tools_cache: Dict[str, _LoadedToolsEntry] = {}
_mcp_tools_cache_lock = threading.Lock()


def _get_oauth_mcp_toolsets(toolsets: List[Any]) -> List["RemoteMCPToolset"]:
    """Return OAuth-enabled MCP toolsets from the given list."""
    return [
        ts for ts in toolsets
        if isinstance(ts, RemoteMCPToolset) and ts.is_oauth_enabled
    ]


def has_oauth_mcp_toolsets(toolsets: List[Any]) -> bool:
    """Quick check: are there any OAuth-enabled MCP toolsets?"""
    return bool(_get_oauth_mcp_toolsets(toolsets))


def load_authenticated_oauth_tools(
    toolsets: List[Any],
    request_context: Optional[Dict[str, Any]],
) -> Dict[str, List[Any]]:
    """Load real MCP tools for OAuth toolsets that have cached tokens.

    Checks the token manager for existing tokens (cache → refresh → DB → disk).
    Loaded tools are cached per (user_id, toolset_name) with a 5-minute TTL.

    Returns a dict of toolset_name -> list of RemoteMCPTool to replace placeholders.
    The shared tool executor is NOT modified.
    """
    result: Dict[str, List[Any]] = {}
    now = time.monotonic()

    for ts in _get_oauth_mcp_toolsets(toolsets):

        oauth_config = ts._mcp_config.oauth
        user_id = (request_context or {}).get("user_id", "__no_user__")

        # Check if user has a token (has_token checks in-memory only, get_access_token also checks DB/disk)
        if not _token_manager.has_token(oauth_config, request_context):
            token = _token_manager.get_access_token(oauth_config, request_context)
            if not token:
                logger.info(
                    "OAuth MCP %s: no token found for user %s (authorization_url=%s, client_id=%s)",
                    ts.name, user_id, oauth_config.authorization_url, oauth_config.client_id,
                )
                continue
        cache_key = f"{user_id}:{ts.name}"

        # Check tool cache for a fresh entry
        with _mcp_tools_cache_lock:
            entry = _mcp_tools_cache.get(cache_key)
            if entry and (now - entry.loaded_at) < _MCP_TOOLS_CACHE_TTL:
                result[ts.name] = entry.tools
                logger.info("OAuth MCP %s: using cached tools for user %s", ts.name, user_id)
                continue

        # Cache miss — load tools from MCP server
        try:
            lock = get_server_lock(str(ts._mcp_config.get_lock_string()))
            with lock:
                tools_result = asyncio.run(ts._get_server_tools_with_context(request_context))

            real_tools = [RemoteMCPTool.create(tool, ts) for tool in tools_result.tools]
            if real_tools:
                with _mcp_tools_cache_lock:
                    _mcp_tools_cache[cache_key] = _LoadedToolsEntry(
                        tools=real_tools,
                        toolset=ts,
                        loaded_at=now,
                    )
                result[ts.name] = real_tools
                logger.warning(
                    "OAuth MCP %s: preloaded %d tools for user %s",
                    ts.name, len(real_tools), user_id,
                )
        except Exception as e:
            logger.warning(
                "OAuth MCP %s: failed to preload tools: %s",
                ts.name, _extract_root_error_message(e),
            )
            # Graceful fallback — user will see the placeholder connect tool

    return result


class _PendingOAuthExchange:
    """State for a pending OAuth approval: PKCE verifier and config."""

    def __init__(self, code_verifier: str, oauth_config: MCPOAuthConfig, redirect_uri: str) -> None:
        self.code_verifier = code_verifier
        self.oauth_config = oauth_config
        self.redirect_uri = redirect_uri


# Pending OAuth exchanges and lock
_pending_exchanges: Dict[str, _PendingOAuthExchange] = {}
_exchanges_lock = threading.Lock()


def set_oauth_dal(dal: Any) -> None:
    """Set the DAL instance for OAuth DB operations. Called during server startup."""
    _token_manager.set_dal(dal)


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
    if not toolset.is_oauth_enabled:
        return headers

    oauth_config = toolset._mcp_config.oauth
    cached_token = _token_manager.get_access_token(oauth_config, request_context)
    if cached_token:
        headers = headers or {}
        headers["Authorization"] = f"Bearer {cached_token}"
        logger.info("OAuth token injected for MCP server %s", toolset.name)
    else:
        logger.warning("OAuth MCP server %s: no cached token — request will likely 401", toolset.name)
    return headers


def exchange_code_for_token(tool_call_id: str, payload_json: str, request_context: Optional[Dict[str, Any]]) -> None:
    """Exchange an OAuth authorization code for an access token.

    Called from tool_calling_llm when a tool approval decision includes an
    OAuth payload from the frontend browser flow.
    """
    with _exchanges_lock:
        pending = _pending_exchanges.pop(tool_call_id, None)

    if pending is None:
        logger.error("OAuth exchange: no pending exchange for tool_call_id=%s", tool_call_id)
        return

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        logger.exception("OAuth exchange: invalid JSON payload for tool_call_id=%s", tool_call_id)
        return

    # Frontend may include client_id from DCR (when Holmes didn't have one at discovery time)
    client_id = payload.get("client_id") or pending.oauth_config.client_id
    if client_id and not pending.oauth_config.client_id:
        pending.oauth_config.client_id = client_id
        logger.info("OAuth: using client_id from frontend DCR: %s", client_id)

    try:
        token_data = exchange_code_for_tokens(
            token_url=pending.oauth_config.token_url,
            code=payload["code"],
            redirect_uri=payload.get("redirect_uri", ""),
            client_id=client_id,
            code_verifier=pending.code_verifier,
        )
    except (OAuthTokenExchangeError, KeyError, Exception):
        logger.exception("OAuth exchange failed (tool_call_id=%s, token_url=%s)", tool_call_id, pending.oauth_config.token_url)
        return

    _token_manager.store_token(pending.oauth_config, token_data, request_context)
    logger.info(
        "OAuth token stored (idp=%s, expires_in=%s, has_refresh=%s)",
        pending.oauth_config.token_url, token_data.get("expires_in"), "refresh_token" in token_data,
    )


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
        if not self.toolset.is_oauth_enabled:
            return None

        oauth_config = self.toolset._mcp_config.oauth
        disk_key = str(self.toolset._mcp_config.url) if isinstance(self.toolset._mcp_config, MCPConfig) else None

        # Try to get a token from cache → refresh → DB → disk
        token = _token_manager.get_access_token(oauth_config, context.request_context, disk_key=disk_key)
        if token:
            logger.info("OAuth MCP %s: token available via manager", self.toolset.name)
            return None

        # No token found anywhere — need to authenticate
        user_id = _get_user_id(context.request_context)

        # Detect CLI vs frontend mode: if request_context exists, the request came
        # through the API server (frontend). CLI calls have request_context=None.
        is_frontend = context.request_context is not None

        if not is_frontend:
            # CLI mode: run browser OAuth flow synchronously
            logger.info("OAuth MCP %s: CLI mode, running browser OAuth flow", self.toolset.name)
            oauth_endpoints = OAuthEndpoints(
                authorization_url=oauth_config.authorization_url,
                token_url=oauth_config.token_url,
                client_id=oauth_config.client_id,
                scopes=oauth_config.scopes,
                registration_endpoint=oauth_config.registration_endpoint,
            )
            token_data = cli_oauth_flow(oauth_endpoints, self.toolset.name)
            if token_data:
                _token_manager.store_token(
                    oauth_config, token_data, context.request_context,
                    disk_key=disk_key, store_to_disk=True,
                )
                logger.info("OAuth MCP %s: CLI auth successful", self.toolset.name)
                return None  # Token obtained, no approval needed
            else:
                logger.warning("OAuth MCP %s: CLI OAuth flow failed", self.toolset.name)
                # Fall through to frontend flow as fallback

        # Frontend mode: use PKCE + approval mechanism
        code_verifier, code_challenge = generate_pkce()

        with _exchanges_lock:
            _pending_exchanges[context.tool_call_id] = _PendingOAuthExchange(
                code_verifier=code_verifier,
                oauth_config=oauth_config,
                redirect_uri="",  # Set by frontend in the payload
            )

        metadata: Dict[str, Any] = {
            "authorization_url": oauth_config.authorization_url,
            "client_id": oauth_config.client_id,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if oauth_config.scopes:
            metadata["scopes"] = oauth_config.scopes
        if oauth_config.registration_endpoint:
            metadata["registration_endpoint"] = oauth_config.registration_endpoint
        params["__oauth_metadata"] = metadata

        return ApprovalRequirement(
            needs_approval=True,
            reason=f"OAuth authentication required for MCP server '{self.toolset.name}'",
        )

    def _is_placeholder_connect_tool(self) -> bool:
        return self.name == self.toolset.connect_tool_name

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
                logger.info("OAuth MCP %s: loaded %d tools after authentication: %s", self.toolset.name, len(real_tools), tool_names)
                return StructuredToolResult(
                    status=StructuredToolResultStatus.SUCCESS,
                    data=f"Successfully authenticated and discovered {len(real_tools)} tools: {', '.join(tool_names)}. You can now call these tools directly.",
                    params=params,
                    invocation=f"OAuth connect to {self.toolset.name}",
                )
            else:
                logger.warning("OAuth MCP %s: authenticated but no tools found", self.toolset.name)
                return StructuredToolResult(
                    status=StructuredToolResultStatus.ERROR,
                    error=f"Authenticated but no tools found on MCP server {self.toolset.name}",
                    params=params,
                    invocation=f"OAuth connect to {self.toolset.name}",
                )
        except Exception as e:
            error_detail = _extract_root_error_message(e)
            logger.warning("OAuth MCP %s: connect failed: %s", self.toolset.name, error_detail)
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

    @property
    def is_oauth_enabled(self) -> bool:
        return isinstance(self._mcp_config, MCPConfig) and bool(self._mcp_config.oauth) and self._mcp_config.oauth.enabled

    @property
    def connect_tool_name(self) -> str:
        """The name of the OAuth placeholder tool for this MCP server."""
        return f"{self.name}_connect"

    def get_oauth_config(self) -> Optional[Dict[str, Any]]:
        """Return OAuth config dict for syncing to DB/frontend, or None if not OAuth-enabled."""
        if not self.is_oauth_enabled:
            return None
        oauth = self._mcp_config.oauth
        return {
            "enabled": True,
            "authorization_url": oauth.authorization_url,
            "token_url": oauth.token_url,
            "client_id": oauth.client_id,
            "scopes": oauth.scopes,
            "registration_endpoint": oauth.registration_endpoint,
        }

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
            if self.is_oauth_enabled:
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
        load the real tools directly. Otherwise, auto-discover OAuth endpoints if needed,
        then register a placeholder tool that triggers the OAuth flow on first use.
        """
        assert isinstance(self._mcp_config, MCPConfig)
        assert self._mcp_config.oauth is not None
        url = str(self._mcp_config.url).rstrip("/")

        # If we already have a cached token (cache → DB → disk), try to load real tools directly
        oauth_config = self._mcp_config.oauth
        disk_key = str(self._mcp_config.url)

        if _token_manager.get_access_token(oauth_config, None, disk_key=disk_key):
            try:
                tools_result = asyncio.run(self._get_server_tools())
                self.tools = [RemoteMCPTool.create(tool, self) for tool in tools_result.tools]
                if self.tools:
                    logging.info(f"OAuth MCP server {self.name}: loaded {len(self.tools)} tools using cached token")
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

            # Auto-discover OAuth endpoints if not configured
            if not oauth_config.authorization_url or not oauth_config.token_url or not oauth_config.client_id:
                discovered = self._discover_oauth_endpoints(url, response)
                if not discovered:
                    return (False, f"MCP server {self.name}: OAuth enabled but auto-discovery failed. Configure authorization_url, token_url, and client_id manually.")

        except Exception as e:
            return (False, f"MCP server {self.name} unreachable: {_extract_root_error_message(e)}")

        # Register a placeholder tool that will trigger OAuth on first call.
        # After auth succeeds, _invoke will load the real tools dynamically.
        placeholder = MCP_Tool(
            name=self.connect_tool_name,
            description=f"Connect to {self.name} (requires OAuth authentication). Call this tool to authenticate and discover available tools.",
            inputSchema={"type": "object", "properties": {}},
        )
        self.tools = [RemoteMCPTool.create(placeholder, self)]
        logging.info(f"OAuth MCP server {self.name} is reachable, registered placeholder tool (auth required)")
        return (True, "")

    def _discover_oauth_endpoints(self, mcp_url: str, initial_response: httpx.Response) -> bool:
        """Auto-discover OAuth endpoints following the MCP SDK's discovery flow.

        Discovery order (matching mcp.client.auth):
        1. Try Protected Resource Metadata (RFC 9728) — path-based, then root-based
        2. If PRM found auth server → fetch its OIDC/OAuth metadata
        3. If PRM not found → legacy fallback on MCP server itself
        4. Dynamic Client Registration deferred to runtime

        Returns True if discovery succeeded and oauth config is fully populated.
        """
        assert isinstance(self._mcp_config, MCPConfig) and self._mcp_config.oauth is not None
        oauth_config = self._mcp_config.oauth
        verify_ssl = self._mcp_config.verify_ssl

        # Step 1: Find auth server via Protected Resource Metadata (RFC 9728)
        auth_server_url, prm_scopes = discover_auth_server_from_prm(
            initial_response, mcp_url, verify_ssl, self.name,
        )
        if prm_scopes and not oauth_config.scopes:
            oauth_config.scopes = prm_scopes

        # Step 2: Fetch OAuth/OIDC metadata
        oidc_config = fetch_oauth_metadata(auth_server_url, mcp_url, verify_ssl, self.name)
        if not oidc_config:
            return False

        if not oauth_config.authorization_url:
            oauth_config.authorization_url = oidc_config.get("authorization_endpoint")
        if not oauth_config.token_url:
            oauth_config.token_url = oidc_config.get("token_endpoint")

        if not oauth_config.authorization_url or not oauth_config.token_url:
            logging.warning("OAuth discovery %s: missing authorization or token endpoint in metadata", self.name)
            return False

        if oidc_config.get("registration_endpoint"):
            oauth_config.registration_endpoint = oidc_config["registration_endpoint"]

        # DCR deferred to runtime — we don't know redirect_uri at startup
        if not oauth_config.client_id:
            if oauth_config.registration_endpoint:
                logging.info("OAuth discovery %s: no client_id, DCR deferred to runtime", self.name)
            else:
                logging.warning("OAuth discovery %s: no client_id and no DCR endpoint", self.name)

        logging.info(
            "OAuth discovery %s complete: authorization_url=%s, token_url=%s, client_id=%s",
            self.name, oauth_config.authorization_url, oauth_config.token_url, oauth_config.client_id,
        )
        return True

    async def _get_server_tools(self):
        async with get_initialized_mcp_session(self, None) as session:
            return await session.list_tools()

    async def _get_server_tools_with_context(self, request_context: Optional[Dict[str, Any]]):
        async with get_initialized_mcp_session(self, request_context) as session:
            return await session.list_tools()
