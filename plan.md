# Refactor: Simplify Tool-Calling & Streaming Cost Tracking

## Goal
Kill intermediate types (`LLMResponseUsage`, `CompactionUsage`, `_process_cost_info`, `_sum_costs`, `_get_tool_call_result`) and give `LLMCosts` (renamed `RequestStats`) a `+=` operator and `from_response()` class method so cost accumulation is a one-liner. Rename `TokenCountMetadata` → `ContextWindowUsage` and `get_llm_usage` → `build_usage_metadata`. Inline `_get_tool_call_result` into `_invoke_llm_tool_call`. Simplify `_is_tool_call_already_approved` signature.

**No Pydantic AI dependency** — this is pure internal refactoring.

**Wire protocol unchanged** — all SSE event shapes (field names, nesting) stay identical.

## Regression Strategy
- Run `pytest tests -m "not llm" --no-cov` **before** any changes, save output
- Run same suite **after** all changes, compare pass/fail counts
- Additionally, add a new blackbox SSE-shape test that asserts the exact JSON structure an HTTP client would see from `ANSWER_END`, `TOKEN_COUNT`, and `APPROVAL_REQUIRED` events (testing the public contract, not internals)

## Steps

### Step 1: Run baseline tests
```
poetry run pytest tests -m "not llm" --no-cov -q 2>&1 | tail -20 > /tmp/baseline_tests.txt
```

### Step 2: Add `__iadd__` and `from_response()` to `LLMCosts` → rename to `RequestStats`

**File: `holmes/core/tool_calling_llm.py`**

- Rename `LLMCosts` → `RequestStats`
- Add `from_response(cls, response) -> RequestStats` classmethod that absorbs `extract_usage_from_response` logic
- Add `__iadd__(self, other: RequestStats) -> RequestStats` that does field-level accumulation (replacing `_process_cost_info` and the 8-line compaction block)
- `LLMResult(RequestStats)` — parent class rename only, no field changes
- Delete `_process_cost_info`, `_sum_costs`, `_SUM_COST_FIELDS`, `_MAX_COST_FIELDS`

### Step 3: Kill `CompactionUsage`, use `RequestStats` in compaction

**File: `holmes/core/truncation/compaction.py`**

- Delete `CompactionUsage` class
- Delete `_extract_compaction_usage()` function
- Import `RequestStats` from `tool_calling_llm`
- `CompactionResult.usage` type changes to `RequestStats`
- `compact_conversation_history` uses `RequestStats.from_response(response)` instead of `_extract_compaction_usage(response)`

**File: `holmes/core/truncation/input_context_window_limiter.py`**

- Change `CompactionUsage` import → `RequestStats`
- `ContextWindowLimiterOutput.compaction_usage` type → `RequestStats`

### Step 4: Kill `LLMResponseUsage` NamedTuple

**File: `holmes/core/llm_usage.py`**

- Delete `LLMResponseUsage` class
- Keep `extract_usage_from_response()` and `_extract_detail_field()` — but change return type to a plain dict (same keys: cost, total_tokens, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens)
- This function is still used by `RequestStats.from_response()` and `test_cache.py`

### Step 5: Rename `TokenCountMetadata` → `ContextWindowUsage`

**File: `holmes/core/llm.py`**

- Rename `TokenCountMetadata` → `ContextWindowUsage` (class name only, field names unchanged)
- Rename `get_llm_usage()` → `build_usage_metadata()`

**Update imports in:**
- `holmes/utils/stream.py`
- `holmes/core/truncation/input_context_window_limiter.py`
- `tests/conftest.py`
- `tests/core/tools_utils/test_tool_context_window_limiter.py`
- `tests/core/test_truncation.py`
- `tests/core/test_feedback.py`
- `tests/test_bash_session_prefix_flow.py`
- `tests/test_tool_calling_llm.py`
- `tests/test_approval_workflow.py`
- `tests/llm/utils/braintrust.py`
- `examples/custom_llm.py`

### Step 6: Simplify `call_stream()` cost accumulation

**File: `holmes/core/tool_calling_llm.py`**

Replace in `call_stream()`:
- `costs = LLMCosts()` → `stats = RequestStats()`
- The 8-line compaction accumulation block → `stats += limit_result.compaction_usage`
- `_process_cost_info(full_response, costs, ...)` → `stats += RequestStats.from_response(full_response)` plus the 3-line `LOG_LLM_USAGE_RESPONSE` block inline
- `costs.model_dump()` → `stats.model_dump()`

Replace in `call()`:
- `_sum_costs(accumulated_costs, round_costs)` → `accumulated_stats += RequestStats(**round_costs)`
- `cost_fields = {k: v ...}` filter → `**accumulated_stats.model_dump()` (already filtered since RequestStats has exactly the right fields)

### Step 7: Inline `_get_tool_call_result` into `_invoke_llm_tool_call`

**File: `holmes/core/tool_calling_llm.py`**

- Move the body of `_get_tool_call_result` into the `else` branch of `_invoke_llm_tool_call` (where `tool_to_call.function` exists)
- Delete `_get_tool_call_result` method

### Step 8: Simplify `_is_tool_call_already_approved` signature

**File: `holmes/core/tool_calling_llm.py`**

- Change from `(self, tool_call_result: ToolCallResult)` to `(self, tool_name: str, params: dict)`
- Update the single call site in `_build_approval_decisions`

### Step 9: Add blackbox SSE-shape regression test

**File: `tests/test_tool_calling_llm.py`**

Add a test class `TestSSEEventShapes` that:
1. Runs `call_stream()` through a 2-iteration scenario (tool call + final answer)
2. Collects all `StreamMessage` events
3. For each `TOKEN_COUNT` event: asserts `data["metadata"]` contains exactly `{"costs": {...}, "usage": {...}, "tokens": {...}, "max_tokens": int, "max_output_tokens": int}` with the right key set
4. For the `ANSWER_END` event: asserts `data` contains exactly `{"content": str, "messages": list, "metadata": dict, "tool_calls": list, "num_llm_calls": int, "prompt": str, "costs": dict}` and that `costs` has all expected keys (`total_cost`, `total_tokens`, `prompt_tokens`, `completion_tokens`, `cached_tokens`, `reasoning_tokens`, `max_completion_tokens_per_call`, `max_prompt_tokens_per_call`, `num_compactions`)
5. For `APPROVAL_REQUIRED` (separate scenario): asserts shape includes `pending_approvals`, `costs`, `requires_approval`, `tool_results`, `num_llm_calls`

This tests the **public contract** as an HTTP client would see it.

### Step 10: Run tests, compare results
```
poetry run pytest tests -m "not llm" --no-cov -q 2>&1 | tail -20 > /tmp/after_tests.txt
diff /tmp/baseline_tests.txt /tmp/after_tests.txt
```

### Step 11: Update test imports
Fix any remaining test files that import deleted types (`CompactionUsage`, `LLMResponseUsage`).
