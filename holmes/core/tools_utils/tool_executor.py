import logging
from typing import Dict, List, Optional

import sentry_sdk

from holmes.core.init_event import EventCallback, StatusEvent, StatusEventKind
from holmes.core.tools import (
    Tool,
    Toolset,
    ToolsetStatusEnum,
)

display_logger = logging.getLogger("holmes.display.tool_executor")


class ToolExecutor:
    def __init__(self, toolsets: List[Toolset], on_event: EventCallback = None):
        # TODO: expose function for this instead of callers accessing directly
        self.toolsets = toolsets

        self.enabled_toolsets: list[Toolset] = [
            ts for ts in toolsets if ts.status == ToolsetStatusEnum.ENABLED
        ]

        toolsets_by_name: dict[str, Toolset] = {}
        for ts in self.enabled_toolsets:
            if ts.name in toolsets_by_name:
                msg = f"Overriding toolset '{ts.name}'!"
                display_logger.warning(msg)
                if on_event is not None:
                    on_event(StatusEvent(kind=StatusEventKind.TOOL_OVERRIDE, name=ts.name, message=msg))
            toolsets_by_name[ts.name] = ts

        self.tools_by_name: dict[str, Tool] = {}
        self._tool_to_toolset: dict[str, Toolset] = {}
        self._toolset_names: set[str] = set(toolsets_by_name.keys())
        for ts in toolsets_by_name.values():
            for tool in ts.tools:
                if tool.icon_url is None and ts.icon_url is not None:
                    tool.icon_url = ts.icon_url
                if tool.name in self.tools_by_name:
                    msg = f"Overriding existing tool '{tool.name} with new tool from {ts.name} at {ts.path}'!"
                    display_logger.warning(msg)
                    if on_event is not None:
                        on_event(StatusEvent(kind=StatusEventKind.TOOL_OVERRIDE, name=tool.name, message=msg))
                self.tools_by_name[tool.name] = tool
                self._tool_to_toolset[tool.name] = ts

    def get_tool_by_name(self, name: str) -> Optional[Tool]:
        if name in self.tools_by_name:
            return self.tools_by_name[name]

        # OAuth MCP toolsets load tools after authentication; check for newly registered tools
        if self._register_oauth_tools():
            if name in self.tools_by_name:
                return self.tools_by_name[name]

        # MCP LLMs sometimes prefix tool names with the toolset name (e.g. "my-mcp_add_numbers")
        stripped = self._try_strip_mcp_prefix(name)
        if stripped:
            return stripped

        logging.warning(f"could not find tool {name}. skipping")
        return None

    def get_toolset_name(self, tool_name: str) -> Optional[str]:
        """Return the toolset name that provides a given tool, or None."""
        ts = self._tool_to_toolset.get(tool_name)
        return ts.name if ts else None

    def ensure_toolset_initialized(self, tool_name: str) -> Optional[str]:
        """Ensure the toolset containing the given tool is lazily initialized.

        For toolsets loaded from cache without full initialization, this triggers
        the deferred prerequisite checks (callable and command prerequisites)
        on first tool use.

        Returns None on success, or an error message string on failure.
        """
        toolset = self._tool_to_toolset.get(tool_name)
        if toolset is None:
            return None

        if toolset.needs_initialization:
            if not toolset.lazy_initialize():
                error_msg = f"Toolset '{toolset.name}' failed to initialize: {toolset.error}"
                logging.error(error_msg)
                return error_msg
        elif toolset.status == ToolsetStatusEnum.FAILED:
            # Toolset was already initialized but failed — don't let tools execute
            error_msg = f"Toolset '{toolset.name}' is unavailable: {toolset.error}"
            logging.error(error_msg)
            return error_msg

        return None

    @sentry_sdk.trace
    def get_all_tools_openai_format(
        self,
        include_restricted: bool = True,
    ):
        """Get all tools in OpenAI format.

        Args:
            include_restricted: If False, filter out tools marked as restricted.
                               Set to True when runbook is in use or restricted
                               tools are explicitly enabled.
        """
        tools = []
        for tool in self.tools_by_name.values():
            # Filter out restricted tools if not authorized
            if not include_restricted and tool._is_restricted():
                continue
            tools.append(tool.get_openai_format())
        return tools

    # ── OAuth MCP helpers ──────────────────────────────────────────────

    def _register_oauth_tools(self) -> bool:
        """Register tools from OAuth MCP toolsets that loaded tools after authentication.

        Returns True if any new tools were registered.
        """
        registered = False
        seen_toolsets: set[str] = set()
        for ts in list(self._tool_to_toolset.values()):
            if ts.name in seen_toolsets:
                continue
            seen_toolsets.add(ts.name)
            for tool in ts.tools:
                if tool.name not in self.tools_by_name:
                    self.tools_by_name[tool.name] = tool
                    self._tool_to_toolset[tool.name] = ts
                    logging.info(f"Registered OAuth MCP tool '{tool.name}' from toolset '{ts.name}'")
                    registered = True
        return registered

    def _try_strip_mcp_prefix(self, name: str) -> Optional[Tool]:
        """MCP LLMs sometimes prefix tool names with the toolset name (e.g. "my-mcp_add_numbers"
        instead of "add_numbers"). Try stripping known toolset prefixes."""
        for ts_name in self._toolset_names:
            prefix = f"{ts_name}_"
            if name.startswith(prefix):
                stripped = name[len(prefix):]
                if stripped in self.tools_by_name:
                    logging.warning(f"Tool '{name}' not found, matched '{stripped}' after stripping prefix '{ts_name}_'")
                    return self.tools_by_name[stripped]
        return None

    def with_replaced_tools(
        self,
        replacements: Dict[str, List[Tool]],
    ) -> "ToolExecutor":
        """Create a shallow copy with placeholder OAuth tools replaced by real tools.

        For each toolset_name in replacements, removes all existing tools belonging
        to that toolset and adds the replacement tools instead.

        The original ToolExecutor is NOT modified.
        """
        new = object.__new__(ToolExecutor)
        new.toolsets = list(self.toolsets)
        new.enabled_toolsets = list(self.enabled_toolsets)
        new._toolset_names = set(self._toolset_names)
        new.tools_by_name = dict(self.tools_by_name)
        new._tool_to_toolset = dict(self._tool_to_toolset)

        for toolset_name, new_tools in replacements.items():
            # Find the toolset object
            toolset = None
            for ts in new.enabled_toolsets:
                if ts.name == toolset_name:
                    toolset = ts
                    break
            if toolset is None:
                continue

            # Remove existing tools for this toolset (the placeholders)
            to_remove = [
                tool_name
                for tool_name, ts in new._tool_to_toolset.items()
                if ts.name == toolset_name
            ]
            for tool_name in to_remove:
                new.tools_by_name.pop(tool_name, None)
                new._tool_to_toolset.pop(tool_name, None)

            # Add replacement tools
            for tool in new_tools:
                if tool.icon_url is None and toolset.icon_url is not None:
                    tool.icon_url = toolset.icon_url
                new.tools_by_name[tool.name] = tool
                new._tool_to_toolset[tool.name] = toolset

        return new
