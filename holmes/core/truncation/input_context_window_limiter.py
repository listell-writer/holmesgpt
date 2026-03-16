import logging
import time
from typing import Any, Callable, Optional

import sentry_sdk
from pydantic import BaseModel

from holmes.common.env_vars import (
    ENABLE_CONVERSATION_HISTORY_COMPACTION,
    MAX_OUTPUT_TOKEN_RESERVATION,
)
from holmes.core.llm import (
    LLM,
    ContextWindowUsage,
    get_context_window_compaction_threshold_pct,
)
from holmes.core.llm_usage import RequestStats
from holmes.core.models import TruncationMetadata, TruncationResult
from holmes.core.truncation.compaction import compact_conversation_history
from holmes.utils.stream import StreamEvents, StreamMessage


class ContextWindowOverflowError(Exception):
    """Raised when conversation exceeds context window and cannot be compacted."""

    def __init__(self, current_tokens: int, max_tokens: int, compaction_attempted: bool):
        self.current_tokens = current_tokens
        self.max_tokens = max_tokens
        self.compaction_attempted = compaction_attempted

        if compaction_attempted:
            message = (
                f"The conversation history is too long ({current_tokens:,} tokens) and could not be "
                f"summarized to fit within the context window ({max_tokens:,} tokens). "
                "This is likely a bug. Please report it at https://github.com/robusta-dev/holmesgpt/issues "
                "and start a new conversation in the meantime."
            )
        else:
            message = (
                f"The conversation ({current_tokens:,} tokens) exceeds the context window "
                f"({max_tokens:,} tokens). This is likely a bug. Please report it at "
                "https://github.com/robusta-dev/holmesgpt/issues and start a new conversation "
                "in the meantime."
            )
        super().__init__(message)


def check_compaction_needed(
    llm: "LLM", messages: list[dict], tools: Optional[list[dict[str, Any]]]
) -> Optional[StreamMessage]:
    """Check if compaction is needed and return a COMPACTION_START event if so.

    This is separated from limit_input_context_window so the caller can yield
    the START event to the SSE stream *before* the blocking compaction call.
    """
    if not ENABLE_CONVERSATION_HISTORY_COMPACTION:
        return None

    initial_tokens = llm.count_tokens(messages=messages, tools=tools)  # type: ignore
    max_context_size = llm.get_context_window_size()
    maximum_output_token = llm.get_maximum_output_token()

    if (initial_tokens.total_tokens + maximum_output_token) > (
        max_context_size * get_context_window_compaction_threshold_pct() / 100
    ):
        num_messages = len(messages)
        return StreamMessage(
            event=StreamEvents.CONVERSATION_HISTORY_COMPACTION_START,
            data={
                "content": f"Compacting conversation history ({initial_tokens.total_tokens} tokens, {num_messages} messages)...",
                "metadata": {
                    "initial_tokens": initial_tokens.total_tokens,
                    "num_messages": num_messages,
                    "max_context_size": max_context_size,
                    "threshold_pct": get_context_window_compaction_threshold_pct(),
                },
            },
        )
    return None


def _truncate_tool_message(
    msg: dict, allocated_space: int, needed_space: int
) -> TruncationMetadata:
    msg_content = msg["content"]
    tool_call_id = msg["tool_call_id"]
    tool_name = msg["name"]

    truncation_notice = "\n\n[TRUNCATED]"

    if allocated_space <= 0:
        truncated_content = ""
    elif allocated_space <= len(truncation_notice):
        # When space is very limited, just show partial notice
        truncated_content = truncation_notice[:allocated_space]
    else:
        # Normal truncation: content + notice
        content_space = allocated_space - len(truncation_notice)
        truncated_content = msg_content[:content_space] + truncation_notice

    msg["content"] = truncated_content
    # Remove token_count since it's now invalid
    msg.pop("token_count", None)

    return TruncationMetadata(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        original_size=needed_space,
        truncated_size=len(truncated_content),
    )


@sentry_sdk.trace
def truncate_messages_to_fit_context(
    messages: list[dict],
    max_context_size: int,
    maximum_output_token: int,
    count_tokens_fn: Callable[[list[dict]], ContextWindowUsage],
) -> TruncationResult:
    """Truncate tool messages to fit within context window.

    Uses fair allocation: each tool message gets an equal share of available space.
    Smaller messages that don't need their full allocation donate excess to larger ones.

    Args:
        messages: List of chat messages
        max_context_size: Maximum context window size
        maximum_output_token: Tokens reserved for output
        count_tokens_fn: Function to count tokens in messages

    Returns:
        TruncationResult with truncated messages and metadata
    """
    available_for_input = max_context_size - maximum_output_token

    # Identify tool messages and calculate non-tool content size
    tool_indices = []
    tool_sizes = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            tool_indices.append(i)
            # Use cached token_count if available, otherwise calculate
            size = msg.get("token_count", len(msg.get("content", "")))
            tool_sizes.append(size)

    if not tool_indices:
        return TruncationResult(truncated_messages=messages, truncation_metadata=[])

    # Calculate non-tool content size
    non_tool_messages = [msg for i, msg in enumerate(messages) if i not in tool_indices]
    non_tool_tokens = count_tokens_fn(non_tool_messages).total_tokens

    available_for_tools = available_for_input - non_tool_tokens

    if available_for_tools <= 0:
        raise Exception(
            f"Non-tool content ({non_tool_tokens} tokens) exceeds the maximum context size "
            f"({available_for_input} tokens available for input)"
        )

    total_tool_size = sum(tool_sizes)
    if total_tool_size <= available_for_tools:
        return TruncationResult(truncated_messages=messages, truncation_metadata=[])

    # Fair allocation algorithm
    num_tools = len(tool_indices)
    base_allocation = available_for_tools // num_tools
    initial_remainder = available_for_tools % num_tools
    allocations = [base_allocation] * num_tools

    # Redistribute from tools that don't need full allocation
    excess = initial_remainder  # Include remainder from integer division
    needs_more = []
    for i, size in enumerate(tool_sizes):
        if size <= allocations[i]:
            excess += allocations[i] - size
            allocations[i] = size
        else:
            needs_more.append(i)

    # Distribute excess to tools that need more
    if needs_more and excess > 0:
        extra_per_tool = excess // len(needs_more)
        remainder = excess % len(needs_more)
        for i in needs_more:
            allocations[i] += extra_per_tool
            if remainder > 0:
                allocations[i] += 1
                remainder -= 1

    # Apply truncation
    truncation_metadata = []
    for idx, tool_idx in enumerate(tool_indices):
        msg = messages[tool_idx]
        needed = tool_sizes[idx]
        allocated = allocations[idx]

        if needed > allocated:
            metadata = _truncate_tool_message(msg, allocated, needed)
            truncation_metadata.append(metadata)

    return TruncationResult(
        truncated_messages=messages, truncation_metadata=truncation_metadata
    )


class ContextWindowLimiterOutput(BaseModel):
    metadata: dict
    messages: list[dict]
    events: list[StreamMessage]
    max_context_size: int
    maximum_output_token: int
    tokens: ContextWindowUsage
    conversation_history_compacted: bool
    compaction_usage: Optional["RequestStats"] = None


@sentry_sdk.trace
def limit_input_context_window(
    llm: LLM, messages: list[dict], tools: Optional[list[dict[str, Any]]]
) -> ContextWindowLimiterOutput:
    t0 = time.monotonic()
    events = []
    metadata: dict = {}
    initial_tokens = llm.count_tokens(messages=messages, tools=tools)  # type: ignore
    max_context_size = llm.get_context_window_size()
    maximum_output_token = min(llm.get_maximum_output_token(), MAX_OUTPUT_TOKEN_RESERVATION)
    available_for_input = max_context_size - maximum_output_token
    conversation_history_compacted = False
    compaction_usage = RequestStats()

    compaction_threshold = max_context_size * get_context_window_compaction_threshold_pct() / 100

    if ENABLE_CONVERSATION_HISTORY_COMPACTION and (
        initial_tokens.total_tokens + maximum_output_token
    ) > compaction_threshold:
        num_messages_before = len(messages)
        compaction_result = compact_conversation_history(
            original_conversation_history=messages, llm=llm
        )
        compaction_usage = compaction_result.usage
        compacted_tokens = llm.count_tokens(compaction_result.messages_after_compaction, tools=tools)
        compacted_total_tokens = compacted_tokens.total_tokens

        if compacted_total_tokens < initial_tokens.total_tokens:
            messages = compaction_result.messages_after_compaction
            num_messages_after = len(messages)
            compression_ratio = round((1 - compacted_total_tokens / initial_tokens.total_tokens) * 100, 1)
            compaction_message = f"The conversation history has been compacted from {initial_tokens.total_tokens} to {compacted_total_tokens} tokens"
            logging.info(compaction_message)
            conversation_history_compacted = True

            # Extract the LLM-generated summary from the compacted messages
            # Structure is: [system_prompt?, last_user_prompt?, assistant_summary, continuation_marker]
            compaction_summary = None
            for msg in compaction_result.messages_after_compaction:
                if msg.get("role") == "assistant":
                    compaction_summary = msg.get("content")
                    break

            compaction_stats: dict = {
                "initial_tokens": initial_tokens.total_tokens,
                "compacted_tokens": compacted_total_tokens,
                "compression_ratio_pct": compression_ratio,
                "num_messages_before": num_messages_before,
                "num_messages_after": num_messages_after,
                "max_context_size": max_context_size,
                "threshold_pct": get_context_window_compaction_threshold_pct(),
            }
            if compaction_usage:
                compaction_stats["compaction_cost"] = {
                    "total_cost": compaction_usage.total_cost,
                    "prompt_tokens": compaction_usage.prompt_tokens,
                    "completion_tokens": compaction_usage.completion_tokens,
                    "total_tokens": compaction_usage.total_tokens,
                }

            events.append(
                StreamMessage(
                    event=StreamEvents.CONVERSATION_HISTORY_COMPACTED,
                    data={
                        "content": compaction_message,
                        "compaction_summary": compaction_summary,
                        "messages": compaction_result.messages_after_compaction,
                        "metadata": compaction_stats,
                    },
                )
            )
            events.append(
                StreamMessage(
                    event=StreamEvents.AI_MESSAGE,
                    data={"content": compaction_message},
                )
            )
        else:
            logging.warning(
                f"Failed to reduce token count when compacting conversation history. "
                f"Original tokens: {initial_tokens.total_tokens}. Compacted tokens: {compacted_total_tokens}"
            )

    tokens = llm.count_tokens(messages=messages, tools=tools)  # type: ignore

    if tokens.total_tokens > available_for_input:
        logging.error(
            f"Context window overflow: {tokens.total_tokens} tokens exceeds "
            f"available space of {available_for_input} tokens (max: {max_context_size}, "
            f"reserved for output: {maximum_output_token})"
        )
        raise ContextWindowOverflowError(
            current_tokens=tokens.total_tokens,
            max_tokens=available_for_input,
            compaction_attempted=conversation_history_compacted
            or (
                ENABLE_CONVERSATION_HISTORY_COMPACTION
                and (initial_tokens.total_tokens + maximum_output_token) > compaction_threshold
            ),
        )

    elapsed_ms = (time.monotonic() - t0) * 1000
    logging.debug(f"limit_input_context_window: {elapsed_ms:.1f}ms total | {tokens.total_tokens} tokens")

    return ContextWindowLimiterOutput(
        events=events,
        messages=messages,
        metadata=metadata,
        max_context_size=max_context_size,
        maximum_output_token=maximum_output_token,
        tokens=tokens,
        conversation_history_compacted=conversation_history_compacted,
        compaction_usage=compaction_usage,
    )
