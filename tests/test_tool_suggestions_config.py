"""Unit tests for the SUGGEST_RUNBOOKS wiring used by LLM evals."""

from __future__ import annotations

import json

from holmes.core.models import ToolCallResult
from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus
from tests.llm.utils.tool_suggestions_config import (
    SUGGEST_RUNBOOKS_NOOP_RESPONSE,
    SUGGEST_RUNBOOKS_SYSTEM_PROMPT,
    SUGGEST_RUNBOOKS_TOOL_NAME,
    append_suggest_runbooks_system_prompt,
    extract_suggested_memories,
)


def test_append_system_prompt_standalone():
    assert append_suggest_runbooks_system_prompt(None) == SUGGEST_RUNBOOKS_SYSTEM_PROMPT


def test_append_system_prompt_extends_existing():
    appended = append_suggest_runbooks_system_prompt("EXISTING")
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
                "skill_domain": "prometheus",
                "title": "Querying checkout-service metrics uses non-default label",
                "when_to_use": "Any PromQL for checkout-service in this cluster",
                "failed_call": (
                    'PromQL: sum(rate(http_requests_total{app="checkout"}[5m]))'
                    " — returned empty"
                ),
                "working_call": (
                    "PromQL: sum(rate(http_requests_total"
                    '{service.team/component="checkout"}[5m]))'
                ),
                "why_env_specific": (
                    "This team overrides the default app= label with their"
                    " own taxonomy"
                ),
                "importance": "high",
            }
        ]
    }
    tcr = _make_tool_call(SUGGEST_RUNBOOKS_TOOL_NAME, payload)
    memories = extract_suggested_memories([tcr])
    assert len(memories) == 1
    assert "checkout-service" in memories[0]["title"]
    assert "service.team/component" in memories[0]["working_call"]
    assert memories[0]["skill_domain"] == "prometheus"


def test_write_memories_consolidates_by_skill_domain(tmp_path):
    """Three quirks across two domains should produce two consolidated
    SKILL.md files — not three single-quirk files. The elasticsearch one
    must contain BOTH ES quirks under a single body section.
    """
    from tests.llm.utils.tool_suggestions_config import (
        write_memories_as_skill_files,
    )

    memories = [
        {
            "skill_domain": "elasticsearch",
            "title": "app-X uses 'severity' not 'level'",
            "when_to_use": "Any ES level query on app-X",
            "failed_call": "term level=ERROR",
            "working_call": "term severity=ERROR",
            "why_env_specific": "Custom team schema.",
            "importance": "medium",
        },
        {
            "skill_domain": "elasticsearch",
            "title": "app-Y uses 'ingest_ts' keyword not '@timestamp'",
            "when_to_use": "Date-range on app-Y",
            "failed_call": "range @timestamp",
            "working_call": "range ingest_ts",
            "why_env_specific": "No ECS @timestamp; custom keyword.",
            "importance": "high",
        },
        {
            "skill_domain": "loki",
            "title": "Streams use 'acme_service' not 'service'",
            "when_to_use": "Loki query for checkout in app-263",
            "failed_call": "{service='checkout'}",
            "working_call": "{acme_service='checkout'}",
            "why_env_specific": "Promtail emits acme_service.",
            "importance": "high",
        },
    ]

    written = write_memories_as_skill_files(memories, str(tmp_path))
    assert len(written) == 2, f"expected 2 domain skills, got {len(written)}"

    # Find the elasticsearch and loki dirs by name.
    es_dir = next(d for d in written if "elasticsearch" in d)
    loki_dir = next(d for d in written if "loki" in d)

    es_md = (tmp_path / es_dir.split("/")[-1] / "SKILL.md").read_text()
    loki_md = (tmp_path / loki_dir.split("/")[-1] / "SKILL.md").read_text()

    # ES skill must contain both quirks in one body.
    assert "name: quirks-for-querying-elasticsearch" in es_md
    assert "app-X uses 'severity' not 'level'" in es_md
    assert "app-Y uses 'ingest_ts'" in es_md
    assert es_md.count("## 1.") == 1
    assert es_md.count("## 2.") == 1

    # Loki skill has the one Loki quirk.
    assert "name: quirks-for-querying-loki" in loki_md
    assert "acme_service" in loki_md
    assert "## 1." in loki_md and "## 2." not in loki_md


def test_write_memories_fallback_domain_when_missing():
    """Memories without skill_domain fall back to a single ``general``
    skill rather than crashing or producing N anonymous files."""
    import tempfile, os
    from tests.llm.utils.tool_suggestions_config import (
        write_memories_as_skill_files,
    )

    memories = [
        {"title": "Quirk A", "when_to_use": "x", "failed_call": "a",
         "working_call": "b", "why_env_specific": "y", "importance": "low"},
        {"title": "Quirk B", "when_to_use": "x", "failed_call": "a",
         "working_call": "b", "why_env_specific": "y", "importance": "low"},
    ]
    with tempfile.TemporaryDirectory() as td:
        written = write_memories_as_skill_files(memories, td)
        assert len(written) == 1
        content = open(os.path.join(written[0], "SKILL.md")).read()
        assert "quirks-for-querying-general" in content
        assert "Quirk A" in content and "Quirk B" in content


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
                "when_to_use": "w",
                "failed_call": "f",
                "working_call": "wc",
                "why_env_specific": "y",
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
