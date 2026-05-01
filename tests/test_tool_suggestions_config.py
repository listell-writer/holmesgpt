"""Unit tests for the tool_suggestions matrix wiring used by LLM evals."""

from __future__ import annotations

import json

import pytest

from holmes.core.models import ToolCallResult
from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus
from tests.llm.utils.tool_suggestions_config import (
    SUGGEST_RUNBOOKS_NOOP_RESPONSE,
    SUGGEST_RUNBOOKS_SYSTEM_PROMPT,
    SUGGEST_RUNBOOKS_TOOL_NAME,
    ToolSuggestionsConfig,
    append_suggest_runbooks_system_prompt,
    extract_suggested_memories,
    get_tool_suggestions_configs,
    parse_tool_suggestions_configs,
)


def test_default_matrix_runs_both_variants(monkeypatch):
    monkeypatch.delenv("TOOL_SUGGESTIONS_CONFIGS", raising=False)
    configs = get_tool_suggestions_configs()
    names = [c.name for c in configs]
    assert names == ["on", "off"]
    assert [c.enabled for c in configs] == [True, False]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ["on", "off"]),
        ("on", ["on"]),
        ("off", ["off"]),
        ("off,on", ["off", "on"]),
        # Whitespace and duplicates collapse to a single ordered run.
        ("  on , on , off ", ["on", "off"]),
    ],
)
def test_parse_tool_suggestions_configs(raw, expected):
    configs = parse_tool_suggestions_configs(raw)
    assert [c.name for c in configs] == expected


def test_parse_tool_suggestions_configs_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown tool_suggestions variant"):
        parse_tool_suggestions_configs("garbage")


def test_append_system_prompt_only_when_enabled():
    on = ToolSuggestionsConfig(name="on", enabled=True)
    off = ToolSuggestionsConfig(name="off", enabled=False)

    assert append_suggest_runbooks_system_prompt(None, off) is None
    assert append_suggest_runbooks_system_prompt("EXISTING", off) == "EXISTING"

    only = append_suggest_runbooks_system_prompt(None, on)
    assert only == SUGGEST_RUNBOOKS_SYSTEM_PROMPT

    appended = append_suggest_runbooks_system_prompt("EXISTING", on)
    assert appended.startswith("EXISTING")
    assert SUGGEST_RUNBOOKS_SYSTEM_PROMPT in appended


def _make_tool_call(tool_name: str, params):
    return ToolCallResult(
        tool_call_id="x",
        tool_name=tool_name,
        description=f"{tool_name}({json.dumps(params)})",
        result=StructuredToolResult(
            status=StructuredToolResultStatus.SUCCESS,
            data=SUGGEST_RUNBOOKS_NOOP_RESPONSE,
            params=params,
        ),
    )


def test_extract_suggested_memories_from_params():
    payload = {
        "suggestions": [
            {
                "title": "OOM debugging",
                "symptoms": "pods restarting",
                "instructions": "check memory limits first",
                "alerts": ["KubePodCrashLooping"],
                "importance": "high",
            }
        ]
    }
    tcr = _make_tool_call(SUGGEST_RUNBOOKS_TOOL_NAME, payload)
    memories = extract_suggested_memories([tcr])
    assert len(memories) == 1
    assert memories[0]["title"] == "OOM debugging"


def test_extract_suggested_memories_ignores_other_tools():
    other = _make_tool_call("kubectl_get", {"resource": "pods"})
    assert extract_suggested_memories([other]) == []


def test_extract_suggested_memories_handles_none_and_empty():
    assert extract_suggested_memories(None) == []
    assert extract_suggested_memories([]) == []


def test_extract_suggested_memories_falls_back_to_description():
    """When ``result.params`` is missing, parse the JSON from the description."""
    payload = {
        "suggestions": [
            {
                "title": "fallback",
                "symptoms": "s",
                "instructions": "i",
                "alerts": [],
                "importance": "low",
            }
        ]
    }

    class FakeResult:
        params = None

    class FakeToolCall:
        tool_name = SUGGEST_RUNBOOKS_TOOL_NAME
        description = f"{SUGGEST_RUNBOOKS_TOOL_NAME}({json.dumps(payload)})"
        result = FakeResult()
        params = None

    memories = extract_suggested_memories([FakeToolCall()])
    assert [m["title"] for m in memories] == ["fallback"]
