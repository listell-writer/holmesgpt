"""Subagent (dispatch_agent / Task) tool.

This module implements a Claude Code-style subagent system: the main agent can
spawn focused child agents that share the same model and the same toolset, but
operate with an isolated context window. Each child runs an independent
agentic loop and returns only its final answer to the parent.

The whole feature is gated behind a single boolean flag: ``subagents_enabled``.
When False, the tool is not registered and the LLM never sees it. When True,
the parent ToolCallingLLM exposes ``dispatch_agent`` to the model and child
agents are constructed with ``subagents_enabled=False`` so they cannot
recursively spawn further subagents.
"""

import logging
from typing import Any, Dict, Optional

from holmes.core.tools import (
    StructuredToolResult,
    StructuredToolResultStatus,
    Tool,
    ToolInvokeContext,
    ToolParameter,
    Toolset,
    ToolsetTag,
)

DISPATCH_AGENT_TOOL_NAME = "dispatch_agent"

# Default cap on subagent agentic-loop iterations. Subagents are intended to be
# narrowly scoped, so we cap them well below the parent's typical max_steps.
DEFAULT_SUBAGENT_MAX_STEPS = 50

_REQUEST_STATS_FIELDS = (
    "total_cost",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "max_completion_tokens_per_call",
    "max_prompt_tokens_per_call",
    "num_compactions",
)


def _extract_request_stats(result: Any) -> Dict[str, Any]:
    """Pull just the RequestStats fields out of an LLMResult into a plain dict.

    LLMResult inherits from RequestStats, so all of these attributes exist.
    Returning a dict (not a RequestStats instance) avoids importing RequestStats
    in tools.py — keeps the StructuredToolResult model layering clean.
    """
    return {field: getattr(result, field, None) for field in _REQUEST_STATS_FIELDS}


SUBAGENT_SYSTEM_PROMPT = (
    "You are a focused subagent spawned by a parent investigation agent. "
    "You have access to the same tools as the parent agent and use the same model, "
    "but you operate with a fresh, isolated context window — you do NOT see the "
    "parent's conversation history.\n\n"
    "Carry out the task described in the user message. Use the available tools to "
    "gather any data you need. When you are done, produce a single, concise, factual "
    "final answer that the parent agent can consume directly. Do not include "
    "intermediate reasoning, raw tool output, or speculative side-discussions in "
    "your final answer — return only what the parent asked for."
)


class DispatchAgentTool(Tool):
    name: str = DISPATCH_AGENT_TOOL_NAME
    description: str = (
        "Launch a focused subagent that has access to the same tools and uses the "
        "same model as you, but operates with a fresh, isolated context window. "
        "Use this when a subtask would otherwise pollute your main context with "
        "many intermediate tool results (e.g. wide searches, exploratory lookups, "
        "or deep dives into a single resource). The subagent does NOT see your "
        "conversation history, so the prompt must be self-contained. The subagent "
        "returns only its final summarized answer."
    )
    parameters: Dict[str, ToolParameter] = {
        "task_description": ToolParameter(
            type="string",
            required=True,
            description="A short (3-5 word) label describing the subtask, used for logging.",
        ),
        "prompt": ToolParameter(
            type="string",
            required=True,
            description=(
                "The full task prompt for the subagent. Must be self-contained "
                "because the subagent does NOT see your conversation history. "
                "Include all relevant context, constraints, and what you want back."
            ),
        ),
    }

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        # Late import to avoid a circular dependency: tool_calling_llm imports
        # tools.py, which is loaded before this module.
        from holmes.core.tool_calling_llm import ToolCallingLLM

        # The JSON Schema declares both fields as strings, but the schema
        # coercer only converts FROM string to other types — not the reverse —
        # so a misbehaving model could still hand us a number/array here.
        # Reject explicitly rather than crashing in .strip().
        raw_task_description = params.get("task_description", "subagent task")
        raw_prompt = params.get("prompt", "")

        if raw_task_description is not None and not isinstance(raw_task_description, str):
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error="dispatch_agent 'task_description' must be a string.",
                params=params,
            )
        if not isinstance(raw_prompt, str):
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error="dispatch_agent 'prompt' must be a string.",
                params=params,
            )

        task_description = (raw_task_description or "subagent task").strip() or "subagent task"
        prompt = raw_prompt.strip()

        if not prompt:
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error="dispatch_agent requires a non-empty 'prompt' parameter.",
                params=params,
            )

        parent_agent = context.parent_agent
        if parent_agent is None:
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=(
                    "dispatch_agent invoked without a parent agent reference. "
                    "Subagents are not enabled in this run."
                ),
                params=params,
            )

        # Defense in depth: refuse to dispatch if the calling agent itself does
        # not have subagents enabled. This catches cases where dispatch_agent
        # somehow leaks into a child's tool list (e.g. via a shared executor)
        # and prevents recursive subagent spawning.
        if not getattr(parent_agent, "subagents_enabled", False):
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=(
                    "dispatch_agent cannot be invoked from a subagent. Only the "
                    "top-level agent is allowed to spawn subagents."
                ),
                params=params,
            )

        logging.info(
            f"[subagent] dispatching '{task_description}' "
            f"(prompt length={len(prompt)} chars)"
        )

        child_max_steps = min(
            DEFAULT_SUBAGENT_MAX_STEPS, getattr(parent_agent, "max_steps", DEFAULT_SUBAGENT_MAX_STEPS)
        )

        # Children are spawned with subagents_enabled=False so they cannot
        # recursively dispatch further subagents. This matches the Claude Code
        # convention: only the top-level agent has the Task tool.
        # We pass the parent's *base* executor (the original, un-cloned one
        # that does not contain DispatchAgentTool) so the child's LLM cannot
        # see dispatch_agent in its tool list at all.
        child_executor = getattr(
            parent_agent, "_base_tool_executor", parent_agent.tool_executor
        )
        child = ToolCallingLLM(
            tool_executor=child_executor,
            max_steps=child_max_steps,
            llm=parent_agent.llm,
            tool_results_dir=getattr(parent_agent, "tool_results_dir", None),
            tracer=getattr(parent_agent, "tracer", None),
            subagents_enabled=False,
        )

        messages = [
            {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            result = child.call(
                messages=messages,
                request_context=context.request_context,
            )
        except Exception as e:
            logging.exception(f"[subagent] '{task_description}' failed")
            return StructuredToolResult(
                status=StructuredToolResultStatus.ERROR,
                error=f"Subagent failed: {e}",
                params=params,
            )

        answer = (result.result or "").strip()
        if not answer:
            return StructuredToolResult(
                status=StructuredToolResultStatus.NO_DATA,
                error="Subagent finished without producing a final answer.",
                params=params,
                subagent_stats=_extract_request_stats(result),
                subagent_num_llm_calls=result.num_llm_calls,
            )

        return StructuredToolResult(
            status=StructuredToolResultStatus.SUCCESS,
            data=answer,
            params=params,
            subagent_stats=_extract_request_stats(result),
            subagent_num_llm_calls=result.num_llm_calls,
        )

    def get_parameterized_one_liner(self, params: Dict[str, Any]) -> str:
        task = params.get("task_description") or "subtask"
        return f"Dispatch subagent: {task}"


class DispatchAgentToolset(Toolset):
    """Toolset providing the dispatch_agent tool for spawning focused subagents."""

    def __init__(self) -> None:
        super().__init__(
            name="subagent",
            description=(
                "Spawn focused subagents that share the main agent's model and tools "
                "but have isolated context windows."
            ),
            enabled=True,
            tools=[DispatchAgentTool()],
            tags=[ToolsetTag.CORE],
        )
