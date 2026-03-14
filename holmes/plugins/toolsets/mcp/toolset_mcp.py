import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type, Union

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool as MCP_Tool
from pydantic import AnyUrl, Field, model_validator

from holmes.common.env_vars import SSE_READ_TIMEOUT
from holmes.core.tools import (
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
        async with stdio_client(server_params) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                _ = await session.initialize()
                yield session
    elif toolset._mcp_config.mode == MCPMode.SSE:
        url = str(toolset._mcp_config.url)
        httpx_factory = create_mcp_http_client_factory(toolset._mcp_config.verify_ssl)
        rendered_headers = toolset._render_headers(request_context)
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
        rendered_headers = toolset._render_headers(request_context)
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

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        try:
            # Serialize calls to the same MCP server to prevent SSE conflicts
            # Different servers can still run in parallel
            if not self.toolset._mcp_config:
                raise ValueError("MCP config not initialized")

            params = self._coerce_params(params)
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
        return StructuredToolResult(
            status=(
                StructuredToolResultStatus.ERROR
                if (tool_result.isError or self._is_content_error(merged_text))
                else StructuredToolResultStatus.SUCCESS
            ),
            data=merged_text,
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
                val, required=key in required_list
            )

        return parameters

    @classmethod
    def _parse_tool_parameter(
        cls, schema: dict[str, Any], required: bool = True
    ) -> ToolParameter:
        """Recursively parse a JSON Schema property into a ToolParameter.

        This preserves nested items, properties, and enum from MCP tool schemas
        so that the OpenAI-formatted schema sent to the LLM accurately describes
        complex parameter types (arrays, objects).
        """
        param_type = schema.get("type", "string")

        items = None
        if "items" in schema and isinstance(schema["items"], dict):
            items = cls._parse_tool_parameter(schema["items"], required=True)

        properties = None
        if "properties" in schema and isinstance(schema["properties"], dict):
            nested_required = schema.get("required", [])
            properties = {
                name: cls._parse_tool_parameter(
                    prop, required=name in nested_required
                )
                for name, prop in schema["properties"].items()
            }

        enum = schema.get("enum")

        return ToolParameter(
            description=schema.get("description"),
            type=param_type,
            required=required,
            items=items,
            properties=properties,
            enum=enum,
            default=schema.get("default"),
            format=schema.get("format"),
            pattern=schema.get("pattern"),
            examples=schema.get("examples"),
            minimum=schema.get("minimum"),
            maximum=schema.get("maximum"),
            min_length=schema.get("minLength"),
            max_length=schema.get("maxLength"),
            min_items=schema.get("minItems"),
            max_items=schema.get("maxItems"),
        )

    def _coerce_params(self, params: Dict) -> Dict:
        """Coerce LLM-produced arguments to match the MCP tool's schema.

        LLMs sometimes produce stringified JSON for array/object params, or send
        null/string "null" for numeric fields. This fixes those mismatches before
        forwarding to the MCP server.
        """
        if not params:
            return params

        coerced = {}
        for key, value in params.items():
            schema = self.parameters.get(key)
            if schema is None:
                coerced[key] = value
                continue

            expected_type = schema.type
            is_nullable = False
            if isinstance(expected_type, list):
                # e.g. ["string", "null"] - use first non-null type
                is_nullable = "null" in expected_type
                non_null = [t for t in expected_type if t != "null"]
                expected_type = non_null[0] if non_null else "string"

            coerced_value = self._coerce_value(value, expected_type)
            # Drop None for non-nullable params (LLM sent null for a required field)
            if coerced_value is None and not is_nullable:
                continue
            # Recurse into objects to strip nulls from non-nullable nested fields
            if isinstance(coerced_value, dict) and schema.properties:
                coerced_value = self._coerce_nested_object(coerced_value, schema.properties)
            coerced[key] = coerced_value
        return coerced

    @classmethod
    def _coerce_nested_object(cls, obj: Dict, properties: Dict[str, "ToolParameter"]) -> Dict:
        """Strip null values from nested object fields that are not nullable."""
        result = {}
        for k, v in obj.items():
            prop_schema = properties.get(k)
            if prop_schema is None:
                result[k] = v
                continue

            prop_type = prop_schema.type
            is_nullable = False
            if isinstance(prop_type, list):
                is_nullable = "null" in prop_type
                non_null = [t for t in prop_type if t != "null"]
                prop_type = non_null[0] if non_null else "string"

            coerced_v = cls._coerce_value(v, prop_type)
            if coerced_v is None and not is_nullable:
                continue
            # Recurse deeper if needed
            if isinstance(coerced_v, dict) and prop_schema.properties:
                coerced_v = cls._coerce_nested_object(coerced_v, prop_schema.properties)
            result[k] = coerced_v
        return result

    @staticmethod
    def _coerce_value(value: Any, expected_type: str) -> Any:
        """Coerce a single value to match the expected JSON Schema type."""
        # Drop null/None for non-nullable types
        if value is None or value == "null":
            if expected_type in ("number", "integer", "array", "object"):
                return None  # will be stripped below
            return value

        # String → array/object: parse stringified JSON
        if isinstance(value, str) and expected_type in ("array", "object", "record"):
            try:
                parsed = json.loads(value)
                if expected_type == "array" and isinstance(parsed, list):
                    return parsed
                if expected_type in ("object", "record") and isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass

        # String → number/integer
        if isinstance(value, str) and expected_type in ("number", "integer"):
            try:
                return int(value) if expected_type == "integer" else float(value)
            except (ValueError, TypeError):
                pass

        return value

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

    async def _get_server_tools(self):
        async with get_initialized_mcp_session(self, None) as session:
            return await session.list_tools()
