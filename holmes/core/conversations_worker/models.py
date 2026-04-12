from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ConversationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class ConversationTask(BaseModel):
    """A claimed conversation ready for processing."""

    conversation_id: str
    account_id: str
    cluster_id: str
    origin: str
    request_sequence: int
    metadata: Dict[str, Any] = {}

    # Extracted from ConversationEvents during history reconstruction
    ask: Optional[str] = None
    conversation_history: Optional[List[dict]] = None
    model: Optional[str] = None
    additional_system_prompt: Optional[str] = None
    enable_tool_approval: bool = False
    tool_decisions: Optional[list] = None


class ConversationReassignedError(Exception):
    """Raised when a conversation has been reassigned to another Holmes instance.

    This happens when ``post_conversation_events`` returns ``None`` because
    the ``holmes_id`` or ``request_sequence`` no longer match.
    """
