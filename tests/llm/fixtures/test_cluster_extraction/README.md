# Cluster Extraction Evals

Evals for the cluster extraction flow used by HolmesGPT in the Robusta SaaS
(`relay`). When a user chats with Holmes through Slack/MS-Teams/the web UI,
relay must figure out which cluster the user is asking about. The current
implementation lives at `relay/pkg/holmes/common/cluster_helpers.py::
extract_message_cluster` and asks an LLM to answer with one word: a cluster
name or `RequestClusterSelection`.

Users have reported failures such as:
- A user with a cluster called `production` asks about "production nodes" and
  the LLM answers `RequestClusterSelection` instead of `production`.
- A user with a single connected `prod` cluster asks "is anything broken?"
  and the LLM asks for clarification (the production code falls back to
  the only connected cluster anyway, but the LLM call is wasted).

## Layout

```
test_cluster_extraction/
  README.md
  001_direct_mention_prod_cluster/
    test_case.yaml
  002_implicit_production_keyword/
    test_case.yaml
  ...
```

Each `test_case.yaml` contains:

```yaml
description: "Short description"
available_clusters: [prod, staging]
channel_id: null            # optional
custom_extraction_prompt: ""  # optional, mirrors the per-account prompt
conversation_history:
  - role: user
    content: "..."
expected_cluster: prod        # null means RequestClusterSelection
expected_cluster_with_list: prod  # optional override for the with_cluster_list variant
tags: [hard]                  # free-form, not validated
```

## Running

```bash
# Default models: claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5
poetry run pytest tests/llm/test_cluster_extraction.py -m llm --no-cov

# Override models
CLUSTER_EXTRACTION_MODELS=claude-haiku-4-5 \
  poetry run pytest tests/llm/test_cluster_extraction.py -m llm --no-cov

# Single case
poetry run pytest tests/llm/test_cluster_extraction.py -m llm --no-cov \
  -k 002_implicit_production_keyword
```

The parametrization is `case x model x variant`, where variant is `current`
(production prompt, no cluster list) and `with_cluster_list` (candidate prompt
that shows the LLM the available clusters). With 15 cases and 3 models that
yields 90 test runs.

## Adding a case

1. Pick the next 3-digit number.
2. Make a directory `<NNN>_<snake_case_description>/`.
3. Write `test_case.yaml` using the schema above.
4. If the canonical answer differs between prompt variants, set
   `expected_cluster_with_list`.
