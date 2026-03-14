from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class InitEvent:
    """Structured event emitted during HolmesGPT initialization.

    Used by interactive/UI callers to render real-time progress.
    Non-interactive callers simply don't pass an ``on_event`` callback
    and the existing ``display_logger`` messages remain unchanged.
    """

    kind: str
    """Event kind — one of:
    - ``toolset_checking``: a toolset prerequisite check has started
    - ``toolset_ready``   : a single toolset finished prerequisite checks
    - ``toolset_lazy``    : a toolset passed config checks and is deferred
    - ``datasource_count``: summary of how many datasources are available
    - ``model_loaded``    : the LLM model was resolved
    - ``tool_override``   : a toolset or tool name collision was detected
    - ``refreshing``      : toolset cache is being refreshed
    - ``info``            : generic informational message
    """

    name: str = ""
    status: str = ""  # "enabled", "failed", "disabled"
    message: str = ""
    error: str = ""
    count: int = 0


EventCallback = Optional[Callable[[InitEvent], None]]
"""Signature for the ``on_event`` callback threaded through initialization."""
