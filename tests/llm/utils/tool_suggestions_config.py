"""Tool suggestions matrix configuration for parameterized eval runs.

This module defines a "tool_suggestions" matrix that runs each eval twice
by default — once with the SUGGEST_RUNBOOKS frontend tool (and matching
system prompt addition) injected, and once without — so we can compare
results in CI / regression reports and see which "skills/memories" the
LLM would have generated for each eval via Braintrust traces.

Format: TOOL_SUGGESTIONS_CONFIGS='on,off' (comma-separated list of
configuration names; defaults to running both variants).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from holmes.core.tools_utils.frontend_tools import build_frontend_noop_tool

SUGGEST_RUNBOOKS_TOOL_NAME = "suggest_runbooks"


# Description shown to the LLM as the tool's description.
#
# Hermes-style framing: skills capture PROCEDURAL memory — when to use the
# skill + the investigation procedure + pitfalls — NOT root-cause conclusions
# from a single incident. A note like "NetworkPolicy label mismatch caused
# timeouts" is transient (once fixed it's gone); the durable form is "when
# investigating service-to-service timeouts in this cluster, check NetworkPolicy
# label selectors before iptables — recurring source of timeouts here."
SUGGEST_RUNBOOKS_TOOL_DESCRIPTION = (
    "Call this tool at the end of your response if the investigation revealed "
    "a reusable QUERY / ACCESS PATTERN for diagnosing similar problems in this "
    "environment in the future. Each suggestion becomes a separate skill the "
    "user can choose to save.\n\n"
    "Before suggesting, review the skills you already fetched from the catalog "
    "during this investigation. Do NOT suggest skills that duplicate or overlap "
    "with existing ones — only suggest genuinely new insights not already "
    "captured.\n\n"
    "Capture PROCEDURE — the investigation METHODOLOGY that will keep being "
    "useful, not the specific findings from this one incident:\n"
    "- What to query/check FIRST when these symptoms appear, and in what order\n"
    "- What to query/check LAST or SKIP because it usually wastes time here\n"
    "- Environment-specific access patterns (custom labels, naming conventions, "
    "  where logs/metrics actually live in this cluster, which dashboards are "
    "  the entry point)\n"
    "- Tool / query gotchas (e.g. \"kubectl logs --previous needed for restart "
    "  loops\", \"this team's apps don't expose /metrics on the obvious port\")\n"
    "- Lessons from failed tool calls — what didn't work and what to try instead\n\n"
    "DO capture (durable, methodology-level):\n"
    "- \"Investigating frontend→backend timeouts in this cluster: first check "
    "  NetworkPolicy label selectors on both sides — common cause here.\"\n"
    "- \"For payments-namespace OOMs: pull memory limits AND the actual RSS "
    "  from cAdvisor before assuming a leak — JVM apps in this team are "
    "  consistently under-limited.\"\n\n"
    "DO NOT capture (transient or generic):\n"
    "- Root-cause conclusions from THIS incident "
    "  (e.g. \"label mismatch caused timeouts\" — once fixed, useless).\n"
    "- Exact resource names, namespaces, file paths, timestamps, pod IDs.\n"
    "- Generic Kubernetes knowledge any LLM already has "
    "  (\"check pod status with kubectl get pods\").\n"
    "- One-time fixes that won't recur.\n\n"
    "Prefer titles framed as \"Investigating X in <env-context>\" or "
    "\"<env-context>: how to diagnose Y\" — NOT \"X was caused by Y\".\n\n"
    "CRITICAL: This tool is silent. The user sees suggestions as UI chips they "
    "can accept or ignore. Never say \"I'll remember\", \"noted\", \"saved\", "
    "or acknowledge this tool in any way — even if the user explicitly asks "
    "you to remember something. Just call the tool silently and continue your "
    "response as if it doesn't exist. Never refer to these as \"runbooks\" — "
    "they are \"skills\"."
)


SUGGEST_RUNBOOKS_NOOP_RESPONSE = (
    "Do not acknowledge this tool call. Do not say you saved, remembered, or "
    "will remember anything. Continue naturally as if this tool was never called."
)


SUGGEST_RUNBOOKS_TOOL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Short name framed as an investigation procedure, "
                            "NOT as a specific finding. GOOD examples: "
                            '"Investigating payments-namespace OOMs", '
                            '"Diagnosing frontend→backend timeouts in <cluster>". '
                            "BAD examples (these are transient root-cause "
                            'conclusions): "DEPLOY_ENV missing causes crash", '
                            '"NetworkPolicy label mismatch caused timeouts".'
                        ),
                    },
                    "symptoms": {
                        "type": "string",
                        "description": (
                            "When-to-use trigger. What alerts, observable "
                            "symptoms, or initial-question shapes should make "
                            "Holmes load this skill next time? Phrase as "
                            "recognizable patterns, not the cause of THIS "
                            'incident (e.g. "User asks why service A can\'t '
                            'reach service B and both pods look healthy" — '
                            'NOT "frontend was getting NetworkPolicy-blocked").'
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "PROCEDURE for Holmes next time. Write as direct "
                            "imperative commands describing the investigation "
                            "ORDER and ACCESS PATTERNS. Focus on:\n"
                            "  - What to query FIRST (skip approaches that "
                            "failed in this investigation)\n"
                            "  - What NOT to do / what wastes time here\n"
                            "  - Where data actually lives in this environment "
                            "(custom labels, log destinations, dashboards)\n"
                            "  - Tool quirks specific to this stack\n"
                            "Do NOT include: specific resource names, "
                            "timestamps, the root cause of this incident, or "
                            "generic Kubernetes advice. Methodology only."
                        ),
                    },
                    "alerts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Specific Kubernetes alert names this knowledge "
                            "applies to, empty array if not alert-specific"
                        ),
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": (
                            "high = will recur and saves significant debugging time, "
                            "medium = likely useful, low = nice to have"
                        ),
                    },
                },
                "required": [
                    "title",
                    "symptoms",
                    "instructions",
                    "alerts",
                    "importance",
                ],
            },
        }
    },
    "required": ["suggestions"],
}


# System prompt addition appended whenever the SUGGEST_RUNBOOKS tool is injected.
SUGGEST_RUNBOOKS_SYSTEM_PROMPT = (
    f"When you use the {SUGGEST_RUNBOOKS_TOOL_NAME} tool, NEVER acknowledge it "
    "in your response. Do not say \"Got it\", \"I'll remember\", \"going "
    "forward\", \"from now on\", \"noted\", or anything similar. The tool is "
    "invisible to the user — they see suggestions as UI chips and decide "
    "whether to save them. Your response must read as if no memory or "
    "learning took place. Even if the user says \"remember this\" or \"save "
    "this\", do not confirm — just silently call the tool and respond normally."
)


@dataclass
class ToolSuggestionsConfig:
    """A single variant in the tool_suggestions matrix."""

    name: str
    enabled: bool

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ToolSuggestionsConfig({self.name}, enabled={self.enabled})"


_KNOWN_CONFIGS: Dict[str, ToolSuggestionsConfig] = {
    "on": ToolSuggestionsConfig(name="on", enabled=True),
    "off": ToolSuggestionsConfig(name="off", enabled=False),
}


def parse_tool_suggestions_configs(raw: str) -> List[ToolSuggestionsConfig]:
    """Parse a comma-separated string of variant names into configs.

    Returns the default matrix (both ``on`` and ``off``) when ``raw`` is
    empty. Raises ``ValueError`` for unknown variant names so typos in CI
    fail loudly rather than silently dropping a column from the matrix.
    """
    if not raw or not raw.strip():
        return [_KNOWN_CONFIGS["on"], _KNOWN_CONFIGS["off"]]

    seen: List[str] = []
    configs: List[ToolSuggestionsConfig] = []
    for name in raw.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name not in _KNOWN_CONFIGS:
            raise ValueError(
                f"Unknown tool_suggestions variant: '{name}'. "
                f"Allowed: {sorted(_KNOWN_CONFIGS)}"
            )
        if name in seen:
            continue
        seen.append(name)
        configs.append(_KNOWN_CONFIGS[name])

    return configs or [_KNOWN_CONFIGS["on"], _KNOWN_CONFIGS["off"]]


def get_tool_suggestions_configs() -> List[ToolSuggestionsConfig]:
    """Return the active tool_suggestions matrix.

    By default the matrix is both ``on`` and ``off`` so regression eval
    reports show results with and without the SUGGEST_RUNBOOKS tool. The
    set can be overridden with the ``TOOL_SUGGESTIONS_CONFIGS`` env var,
    e.g. ``TOOL_SUGGESTIONS_CONFIGS=off`` to skip the on variant locally.
    """
    return parse_tool_suggestions_configs(os.environ.get("TOOL_SUGGESTIONS_CONFIGS", ""))


def maybe_inject_suggest_runbooks_tool(
    ai: Any, config: ToolSuggestionsConfig
) -> Tuple[Any, bool]:
    """If ``config.enabled``, return a clone of ``ai`` with the SUGGEST_RUNBOOKS
    frontend noop tool injected, plus a flag indicating injection happened.

    Otherwise return ``ai`` unchanged.
    """
    if not config.enabled:
        return ai, False

    tool = build_frontend_noop_tool(
        name=SUGGEST_RUNBOOKS_TOOL_NAME,
        description=SUGGEST_RUNBOOKS_TOOL_DESCRIPTION,
        parameters=SUGGEST_RUNBOOKS_TOOL_PARAMETERS,
        canned_response=SUGGEST_RUNBOOKS_NOOP_RESPONSE,
    )
    cloned_executor = ai.tool_executor.clone_with_extra_tools([tool])
    return ai.with_executor(cloned_executor), True


def append_suggest_runbooks_system_prompt(
    additional_system_prompt: Optional[str], config: ToolSuggestionsConfig
) -> Optional[str]:
    """Append the SUGGEST_RUNBOOKS system prompt block when enabled."""
    if not config.enabled:
        return additional_system_prompt
    if additional_system_prompt:
        return f"{additional_system_prompt}\n\n{SUGGEST_RUNBOOKS_SYSTEM_PROMPT}"
    return SUGGEST_RUNBOOKS_SYSTEM_PROMPT


def extract_suggested_memories(tool_calls: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Pull the parsed ``suggestions`` arrays out of any SUGGEST_RUNBOOKS calls
    found in the LLM tool-call history. Each dict is one suggestion; multiple
    calls are flattened in the order they occurred.
    """
    if not tool_calls:
        return []

    memories: List[Dict[str, Any]] = []
    for tc in tool_calls:
        if getattr(tc, "tool_name", None) != SUGGEST_RUNBOOKS_TOOL_NAME:
            continue
        params = _extract_tool_call_params(tc)
        if not params:
            continue
        suggestions = params.get("suggestions") or []
        if not isinstance(suggestions, list):
            continue
        for suggestion in suggestions:
            if isinstance(suggestion, dict):
                memories.append(suggestion)

    return memories


def _extract_tool_call_params(tool_call: Any) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of the tool-call arguments dict.

    The runtime stores arguments on ``tool_call.result.params`` (set by the
    ``FrontendNoopTool._invoke``). When a different code path is exercised
    we fall back to ``tool_call.params`` and to the raw JSON description.
    """
    result = getattr(tool_call, "result", None)
    params = getattr(result, "params", None) if result is not None else None
    if isinstance(params, dict):
        return params

    fallback = getattr(tool_call, "params", None)
    if isinstance(fallback, dict):
        return fallback

    description = getattr(tool_call, "description", "") or ""
    if "{" in description and "}" in description:
        try:
            payload = description[description.index("{") : description.rindex("}") + 1]
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, json.JSONDecodeError):
            logging.debug(
                "Could not parse SUGGEST_RUNBOOKS arguments from tool call description"
            )

    return None
