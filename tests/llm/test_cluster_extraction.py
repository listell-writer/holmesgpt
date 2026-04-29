# type: ignore
"""Evals for the cluster extraction flow used by HolmesGPT in the Robusta SaaS (relay).

The extraction prompt and logic live in relay at
`relay/pkg/holmes/common/cluster_helpers.py::extract_message_cluster`.

This eval vendors the prompt template so the test can run from holmesgpt
without depending on relay. Keep the constants in this module in sync
with that file.

Each fixture under `fixtures/test_cluster_extraction/<id>/test_case.yaml`
describes:
  - conversation_history: messages exchanged with the bot
  - available_clusters:   the clusters connected to the account
  - expected_cluster:     the cluster the LLM should answer with, or null
                          to mean RequestClusterSelection
  - channel_id, custom_extraction_prompt: optional extras
  - expected_cluster_with_list: optional override for the with_cluster_list
                                variant when the answer differs

The test parametrizes over (case, model, prompt variant). Models are
controlled via the CLUSTER_EXTRACTION_MODELS env var (comma-separated).
The two prompt variants are:
  - current:           the live production prompt (no cluster list shown)
  - with_cluster_list: candidate prompt that lists available clusters

Scoring is exact, case-insensitive match on the first answer token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import litellm
import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "test_cluster_extraction"

DEFAULT_MODELS = [
    # Bedrock EU cross-region inference profile IDs (matches relay's eu-south-2 setup).
    # Pass as `bedrock/<id>` to litellm; the standalone runner strips that prefix
    # before calling the boto3 Converse API.
    "bedrock/eu.anthropic.claude-opus-4-7",
    "bedrock/eu.anthropic.claude-sonnet-4-6",
    "bedrock/eu.anthropic.claude-haiku-4-5-20251001-v1:0",
]

PROMPT_VARIANTS = ["current", "with_cluster_list"]

# Vendored from relay/pkg/holmes/common/cluster_helpers.py::extract_message_cluster.
# Do not edit independently of the source unless you are running an A/B prompt
# experiment - in which case, add a new variant rather than changing this one.
_BASE_INTRO = (
    "This is a conversation between the user and the bot. "
    "The user is asking a question on some cluster:"
)
_BASE_INSTRUCTION = (
    "given this conversation, what is the name of the cluster the user is referring to?\n"
    "The cluster can be in the alert labels or annotation, or the user may indicate it in the conversation."
    "The user may ask about different clusters, along the conversation. select the latest cluster the user is reffered to."
    "it can be referred as cluster or cluster_name or cluster_id, for example: stg cluster, asia cluster etc..."
)
_BASE_TAIL = (
    "Please answer with one word which is the cluster name, "
    "or say RequestClusterSelection if you don't know."
)


def _build_prompt(
    *,
    conversation_history: List[dict],
    available_clusters: List[str],
    channel_id: Optional[str],
    custom_extraction_prompt: str,
    include_cluster_list: bool,
) -> str:
    parts: List[str] = [_BASE_INTRO]
    if channel_id:
        parts.append(
            f"This conversation is happening in the channel with id: {channel_id}."
        )
    for message in conversation_history:
        role = message.get("role")
        content = message.get("content")
        if content and role in ("assistant", "user"):
            parts.append(content + "\n")

    if include_cluster_list:
        parts.append(
            "The clusters connected to this account are: "
            + ", ".join(available_clusters)
            + ". Only answer with one of these names; otherwise say RequestClusterSelection."
        )

    parts.append(_BASE_INSTRUCTION + (custom_extraction_prompt or ""))
    parts.append(_BASE_TAIL)
    return "\n".join(parts)


@dataclass
class ClusterExtractionCase:
    id: str
    description: str
    available_clusters: List[str]
    conversation_history: List[dict]
    expected_cluster: Optional[str]  # None => RequestClusterSelection
    expected_cluster_with_list: Optional[str] = None  # override for with_cluster_list variant
    channel_id: Optional[str] = None
    custom_extraction_prompt: str = ""
    tags: List[str] = field(default_factory=list)

    def expected_for(self, variant: str) -> Optional[str]:
        if variant == "with_cluster_list" and self.expected_cluster_with_list is not None:
            return self.expected_cluster_with_list
        return self.expected_cluster


def _load_cases() -> List[ClusterExtractionCase]:
    cases: List[ClusterExtractionCase] = []
    if not FIXTURES_DIR.exists():
        return cases
    for case_dir in sorted(FIXTURES_DIR.iterdir()):
        if not case_dir.is_dir():
            continue
        cfg_path = case_dir / "test_case.yaml"
        if not cfg_path.exists():
            continue
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        cases.append(
            ClusterExtractionCase(
                id=case_dir.name,
                description=data.get("description", ""),
                available_clusters=data.get("available_clusters", []),
                conversation_history=data.get("conversation_history", []),
                expected_cluster=data.get("expected_cluster"),
                expected_cluster_with_list=data.get("expected_cluster_with_list"),
                channel_id=data.get("channel_id"),
                custom_extraction_prompt=data.get("custom_extraction_prompt", "") or "",
                tags=data.get("tags", []) or [],
            )
        )
    return cases


def _get_models() -> List[str]:
    val = os.environ.get("CLUSTER_EXTRACTION_MODELS")
    if val:
        return [m.strip() for m in val.split(",") if m.strip()]
    return DEFAULT_MODELS


def _normalize_answer(text: str) -> str:
    """Extract the first 'word' from the model answer, stripping common punctuation/quotes."""
    if not text:
        return ""
    cleaned = text.strip().strip("`'\"")
    tokens = cleaned.split()
    first = tokens[0] if tokens else ""
    return first.strip(".,!?:;`'\"()[]")


_CASES = _load_cases()
_CASE_IDS = [c.id for c in _CASES]


@pytest.mark.llm
@pytest.mark.parametrize("variant", PROMPT_VARIANTS)
@pytest.mark.parametrize("model", _get_models())
@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_cluster_extraction(
    case: ClusterExtractionCase, model: str, variant: str, request
) -> None:
    prompt = _build_prompt(
        conversation_history=case.conversation_history,
        available_clusters=case.available_clusters,
        channel_id=case.channel_id,
        custom_extraction_prompt=case.custom_extraction_prompt,
        include_cluster_list=(variant == "with_cluster_list"),
    )

    # Note: do not pass temperature - newer Claude models (Opus 4.7+) reject it.
    # max_tokens=64 alone keeps the answer short and structured.
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=64,
    )
    raw = response["choices"][0]["message"]["content"] or ""
    answer = _normalize_answer(raw)

    # Surface the raw answer in pytest properties for the eval report.
    request.node.user_properties.append(("raw_answer", raw))
    request.node.user_properties.append(("normalized_answer", answer))
    request.node.user_properties.append(("prompt_variant", variant))

    expected = case.expected_for(variant)
    if expected is None:
        assert answer == "RequestClusterSelection", (
            f"[{case.id}][{model}][{variant}] expected RequestClusterSelection, "
            f"got {answer!r}\nFull output: {raw!r}"
        )
    else:
        assert answer.lower() == expected.lower(), (
            f"[{case.id}][{model}][{variant}] expected {expected!r}, "
            f"got {answer!r}\nFull output: {raw!r}"
        )
