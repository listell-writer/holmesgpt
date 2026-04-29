"""Reproduction for the customer-reported context-window blowup when querying
Elasticsearch documents that contain very large fields (>10MB).

The test runs against the live Elasticsearch cluster pointed at by the
ELASTICSEARCH_URL / ELASTICSEARCH_API_KEY env vars (the same cloud instance
used by the eval suite).
"""

import json
import os
import uuid

import pytest
import requests

from holmes.core.tools import StructuredToolResultStatus
from holmes.plugins.toolsets.elasticsearch.elasticsearch import (
    ElasticsearchConfig,
    ElasticsearchDataToolset,
    ElasticsearchSearch,
)


pytestmark = pytest.mark.skipif(
    not (os.environ.get("ELASTICSEARCH_URL") and os.environ.get("ELASTICSEARCH_API_KEY")),
    reason="Requires ELASTICSEARCH_URL and ELASTICSEARCH_API_KEY env vars",
)


HUGE_FIELD_BYTES = 12 * 1024 * 1024  # 12 MB — exceeds the 10 MB customer threshold
RESPONSE_BUDGET_BYTES = 2 * 1024 * 1024  # tool result must stay well under raw doc size


@pytest.fixture
def es_index_with_oversized_doc():
    """Create a temporary index containing a single document with a 12 MB field."""
    url = os.environ["ELASTICSEARCH_URL"].rstrip("/")
    key = os.environ["ELASTICSEARCH_API_KEY"]
    headers = {
        "Authorization": f"ApiKey {key}",
        "Content-Type": "application/json",
    }
    index = f"holmes-test-large-doc-{uuid.uuid4().hex[:8]}"

    huge_payload = "x" * HUGE_FIELD_BYTES
    doc = {
        "title": "test-large",
        "marker": "HOLMES_LARGE_DOC_MARKER",
        "huge_field": huge_payload,
    }

    create = requests.put(
        f"{url}/{index}/_doc/1",
        params={"refresh": "true"},
        headers=headers,
        data=json.dumps(doc),
        timeout=120,
    )
    create.raise_for_status()
    try:
        yield index
    finally:
        requests.delete(f"{url}/{index}", headers=headers, timeout=30)


def _build_search_tool() -> ElasticsearchSearch:
    toolset = ElasticsearchDataToolset()
    toolset.config = ElasticsearchConfig(
        api_url=os.environ["ELASTICSEARCH_URL"],
        api_key=os.environ["ELASTICSEARCH_API_KEY"],
        timeout_seconds=120,
    )
    return ElasticsearchSearch(toolset)


def test_search_truncates_oversized_fields(es_index_with_oversized_doc):
    """Searching an index with a >10MB field must not return the raw payload.

    Customer report: 'if you query a document bigger than 10MB the agent drops
    everything and can't see it because of context window limits.' The toolset
    should truncate oversized fields per-document so the small fields remain
    usable and the agent can choose to drill into the large field via source
    filtering.
    """
    index = es_index_with_oversized_doc
    tool = _build_search_tool()

    result = tool._invoke(
        {"index": index, "query": {"match_all": {}}, "size": 10},
        context=None,  # _invoke doesn't read the context
    )

    assert result.status == StructuredToolResultStatus.SUCCESS, result.error

    serialized = json.dumps(result.data, default=str)
    assert len(serialized) < RESPONSE_BUDGET_BYTES, (
        f"Tool result is {len(serialized)} bytes — large field was NOT truncated. "
        "This is the customer-reported context-window blowup."
    )

    # Small fields must survive truncation so the agent can still reason about the doc.
    assert "HOLMES_LARGE_DOC_MARKER" in serialized
    assert "test-large" in serialized

    # The huge field must be replaced with a notice that explains how to fetch it.
    assert "huge_field" in serialized
    lowered = serialized.lower()
    assert "truncat" in lowered, "Truncation notice missing from response"
    # Notice should mention source filtering so the LLM knows how to recover the field.
    assert "_source" in serialized or "source" in lowered


def test_search_does_not_truncate_small_fields(es_index_with_oversized_doc):
    """Sanity check: small fields are returned verbatim, no truncation noise."""
    index = es_index_with_oversized_doc
    tool = _build_search_tool()

    result = tool._invoke(
        {
            "index": index,
            "query": {"match_all": {}},
            "size": 10,
            "source": ["title", "marker"],  # exclude huge_field via source filter
        },
        context=None,
    )

    assert result.status == StructuredToolResultStatus.SUCCESS, result.error
    serialized = json.dumps(result.data, default=str)

    # When the LLM excludes the huge field via source filtering, the raw
    # payload must not appear and there is nothing to truncate.
    assert "HOLMES_LARGE_DOC_MARKER" in serialized
    assert "truncat" not in serialized.lower()
    # The 12MB "xxxx..." payload must not appear anywhere in the response.
    assert "x" * 1000 not in serialized
    assert len(serialized) < 10_000
