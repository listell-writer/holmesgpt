"""Per-user OAuth MCP tool cache and toolset detection.

Caches loaded MCP tools per (user_id, toolset_name) with a TTL to avoid
re-connecting to the MCP server on every request.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from holmes.core.oauth_utils import _get_token_manager

logger = logging.getLogger(__name__)

_MCP_TOOLS_CACHE_TTL = 300  # 5 minutes


@dataclass
class _LoadedToolsEntry:
    """Cached MCP tools loaded after OAuth authentication."""

    tools: List[Any]  # List[RemoteMCPTool] — forward ref
    toolset: Any  # RemoteMCPToolset — forward ref
    loaded_at: float = field(default_factory=time.monotonic)


def _extract_root_error_message(exc: Exception) -> str:
    """Extract the actual error message from an ExceptionGroup."""
    current: BaseException = exc
    while hasattr(current, "exceptions") and current.exceptions:
        current = current.exceptions[0]
    return str(current)


class OAuthToolsCache:
    """Manages per-user caching of MCP tools loaded after OAuth authentication."""

    def __init__(self) -> None:
        self._cache: Dict[str, _LoadedToolsEntry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _get_oauth_mcp_toolsets(toolsets: List[Any]) -> list:
        """Return OAuth-enabled MCP toolsets from the given list."""
        from holmes.plugins.toolsets.mcp.toolset_mcp import RemoteMCPToolset
        return [
            ts for ts in toolsets
            if isinstance(ts, RemoteMCPToolset) and ts.is_oauth_enabled
        ]

    @staticmethod
    def has_oauth_mcp_toolsets(toolsets: List[Any]) -> bool:
        """Quick check: are there any OAuth-enabled MCP toolsets?"""
        return bool(OAuthToolsCache._get_oauth_mcp_toolsets(toolsets))

    def load_authenticated_tools(
        self,
        toolsets: List[Any],
        request_context: Optional[Dict[str, Any]],
    ) -> Dict[str, List[Any]]:
        """Load real MCP tools for OAuth toolsets that have cached tokens.

        Checks the token manager for existing tokens (cache -> refresh -> DB -> disk).
        Loaded tools are cached per (user_id, toolset_name) with a 5-minute TTL.

        Returns a dict of toolset_name -> list of RemoteMCPTool to replace placeholders.
        The shared tool executor is NOT modified.
        """
        from holmes.plugins.toolsets.mcp.toolset_mcp import RemoteMCPTool, get_server_lock

        result: Dict[str, List[Any]] = {}
        now = time.monotonic()
        token_manager = _get_token_manager()

        for ts in self._get_oauth_mcp_toolsets(toolsets):

            oauth_config = ts._mcp_config.oauth
            user_id = (request_context or {}).get("user_id", "__no_user__")

            # Check if user has a token (has_token checks in-memory only, get_access_token also checks DB/disk)
            if not token_manager.has_token(oauth_config, request_context):
                token = token_manager.get_access_token(oauth_config, request_context)
                if not token:
                    logger.info(
                        "OAuth MCP %s: no token found for user %s (authorization_url=%s, client_id=%s)",
                        ts.name, user_id, oauth_config.authorization_url, oauth_config.client_id,
                    )
                    continue
            cache_key = f"{user_id}:{ts.name}"

            # Check tool cache for a fresh entry
            with self._lock:
                entry = self._cache.get(cache_key)
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
                    with self._lock:
                        self._cache[cache_key] = _LoadedToolsEntry(
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


# ── Singleton ─────────────────────────────────────────────────────────────

_oauth_tools_cache = OAuthToolsCache()


def has_oauth_mcp_toolsets(toolsets: List[Any]) -> bool:
    """Module-level convenience function."""
    return OAuthToolsCache.has_oauth_mcp_toolsets(toolsets)


def load_authenticated_oauth_tools(
    toolsets: List[Any],
    request_context: Optional[Dict[str, Any]],
) -> Dict[str, List[Any]]:
    """Module-level convenience function."""
    return _oauth_tools_cache.load_authenticated_tools(toolsets, request_context)
