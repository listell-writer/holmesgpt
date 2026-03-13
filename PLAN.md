# Refactor: Make `call()` a thin wrapper around `call_stream()`

## Goal

Unify the two LLM execution loops in `ToolCallingLLM` so that `call_stream()` is the single source of truth and `call()` is a thin wrapper that drains the stream and reconstructs an `LLMResult`.

## Current State

### Two independent loops

`call()` (lines 414-631) and `call_stream()` (lines 938-1218) in `holmes/core/tool_calling_llm.py` are two separate implementations of the same agentic loop. They share ~80% of their logic (LLM completion, tool execution, context window limiting, compaction cost tracking, repeated tool call prevention, runbook tool refresh) but have diverged in several ways.

### Callers of `call()`

| Caller | File:Line | Parameters used |
|---|---|---|
| CLI `ask` (non-interactive) | `holmes/main.py:376` | `messages`, `trace_span` |
| CLI interactive mode | `holmes/interactive.py:1518` | `messages`, `trace_span`, `tool_number_offset`, `cancel_event` |
| Health checks | `holmes/checks/checks.py:73` | `messages`, `response_format` |
| Server non-streaming | `server.py:401` (via `messages_call()`) | `messages`, `trace_span`, `response_format`, `request_context` |

### Callers of `call_stream()`

| Caller | File:Line | Parameters used |
|---|---|---|
| Server streaming | `server.py:386` | `msgs`, `enable_tool_approval`, `tool_decisions`, `response_format`, `request_context` |
| AG-UI experimental | `experimental/ag-ui/server-agui.py:134` | `msgs`, `enable_tool_approval` |

### LLMResult field usage across callers

| Field | main.py | interactive.py | checks.py | server.py |
|---|---|---|---|---|
| `result` | yes | yes | yes | yes |
| `messages` | yes | yes | no | yes |
| `tool_calls` | yes | yes | no (empty) | yes |
| `metadata` | via model_dump | no | no | yes |
| `prompt` | via model_dump | no | no | no |
| `num_llm_calls` | via model_dump | no | no | no |
| cost fields | yes (displayed) | no | no | no |
| `model_dump()` | yes (JSON file) | no | no | no |

**Note:** `main.py:385` calls `response.model_dump()` to serialize the entire `LLMResult` to a JSON file, so every field must be populated faithfully.

## Differences Between `call()` and `call_stream()`

### Signature

- `call()`: `messages, response_format, user_prompt, trace_span, tool_number_offset, cancel_event, request_context`
- `call_stream()`: `system_prompt, user_prompt, response_format, msgs, enable_tool_approval, tool_decisions, request_context`
- `call_stream()` builds messages internally from `system_prompt`/`user_prompt`/`msgs`; `call()` expects pre-built messages.

### Return type

- `call()` returns `LLMResult` (Pydantic model with `result`, `tool_calls`, `num_llm_calls`, `prompt`, `messages`, cost fields, `metadata`).
- `call_stream()` yields `StreamMessage` objects and ends with `ANSWER_END` containing `content`, `messages`, `metadata`.

### Tracing

- `call()` passes caller-provided `trace_span` to `_invoke_llm_tool_call`.
- `call_stream()` hardcodes `DummySpan()`.

### Cancellation

- `call()` checks `cancel_event` (threading.Event) before LLM calls and between tool futures.
- `call_stream()` has no cancellation support.

### Tool approval

- `call()` uses synchronous `self.approval_callback` — blocks thread, gets `(approved, feedback)`, continues loop.
- `call_stream()` uses `enable_tool_approval` flag — collects `PendingToolApproval` list, yields `APPROVAL_REQUIRED` event, returns. Caller resumes with new invocation passing `tool_decisions`.

### Token counting

- `call()` counts tokens only at the final response.
- `call_stream()` counts tokens after every LLM call AND after every tool result batch.

### Streaming events

- `call()` produces none — logs to rich console instead.
- `call_stream()` yields: `START_TOOL`, `TOOL_RESULT`, `AI_MESSAGE`, `ANSWER_END`, `TOKEN_COUNT`, `APPROVAL_REQUIRED`, `CONVERSATION_HISTORY_COMPACTED` (from `limit_result.events`).

### Compaction events

- `call()` discards `limit_result.events`.
- `call_stream()` yields them.

### Cost tracking

- `call()` returns costs as top-level fields in `LLMResult` (via `**costs.model_dump()`).
- `call_stream()` puts costs in `metadata["costs"]`.

### Missing fields in `call_stream()` ANSWER_END

- `num_llm_calls` (iteration count)
- `prompt` (JSON-serialized messages)
- `tool_calls` (list of all tool call dicts)
- Individual cost fields (only has `metadata["costs"]` dict)

### Session prefix extraction

- `call()` extracts bash session prefixes only inside `_handle_tool_call_approval`.
- `call_stream()` extracts before each tool execution batch.

### Reasoning/text mid-loop

- `call()` logs reasoning and intermediate text to rich console via `logging.info`.
- `call_stream()` yields `AI_MESSAGE` events.

### Console logging

- `call()` logs directly during execution: tool call counts (`logging.info`), AI intermediate text, reasoning content, blank lines after tool batches.
- `call_stream()` does not log to console — yields events instead. The caller is responsible for presentation.

## Decision: Approval Mechanism

**Chosen approach: Option 4 — Always yield + wrapper re-invokes**

The unified `call_stream()` always yields `APPROVAL_REQUIRED` and returns when tools need approval. The `call()` wrapper:
1. Drains the stream
2. If it encounters `APPROVAL_REQUIRED`, invokes `self.approval_callback` for each pending tool
3. Re-invokes `call_stream()` with the `tool_decisions` and saved messages
4. Loops until it gets `ANSWER_END`

This matches how the server already handles approval resumption today.

**Trade-offs accepted:**
- Each approval round creates a new generator and re-enters the loop (context window limiting, tool re-fetch run again) — slight inefficiency but keeps the code simple.
- `call()` wrapper needs a while loop to handle multiple approval rounds in one conversation turn.

## Decision: Console Logging

**`call_stream()` only yields events — no console logging side effects.**

The `call()` wrapper intercepts stream events and logs to console:
- `AI_MESSAGE` → `logging.info` with rich markup (reasoning + text)
- `START_TOOL` → log tool call count
- `TOOL_RESULT` → (already logged inside `_invoke_llm_tool_call`)
- `ANSWER_END` → no logging needed (caller handles display)

This keeps `call_stream()` pure and testable, while preserving CLI output for `call()` callers.

## Decision: `tool_number_offset` parameter

**Not adding `tool_number_offset` as a parameter to `call_stream()`.**

### What `tool_number` is

Tool numbers are sequential labels (1, 2, 3...) passed to `ToolInvokeContext` for each tool execution. They serve one purpose: the bash toolset uses them to create numbered temp files for tool results (e.g., `tool_result_1.txt`, `tool_result_2.txt`) so the LLM can reference them.

### Why `call()` has the parameter today

In interactive mode, `call()` is invoked once per conversation turn. If turn 1 executed tools 1-5, turn 2 should start at 6. So `interactive.py` passes `tool_number_offset=len(all_tool_calls_history)` to maintain globally unique numbers across the conversation.

### Why `call_stream()` doesn't need it

`call_stream()` is stateless between invocations — the server/AG-UI callers don't track tool numbers across requests. Tool numbers start at 1 each time, and that's fine because the tool result files are ephemeral per-request.

### What the `call()` wrapper does

The wrapper passes its own `tool_number_offset` when constructing stream events. But since `call_stream()` handles tool numbering internally (starting from 0 and incrementing), the wrapper needs a different approach:

**Option chosen:** Add `tool_number_offset` to `call_stream()` signature. It's a trivial addition (just changes the initial value of the local variable) and avoids the wrapper needing to intercept and renumber tool events. On re-invocation after approval, the wrapper passes `tool_number_offset` updated to account for tools already executed.

*Actually, we do add the parameter* — it's simple, non-breaking (default 0), and avoids complexity in the wrapper. The key insight is that the wrapper must track the running total across approval re-invocations.

## Test Coverage Assessment

### Existing coverage

| Area | Coverage | Tests |
|---|---|---|
| Happy path (LLM + tools → result) | Good | LLM eval tests via `messages_call()` → `call()` |
| Streaming approval events | Partial | `test_approval_workflow.py` (mocks `process_tool_decisions`) |
| Checks via `call()` | Mocked | `tests/checks/` (mock LLMResult) |
| Server non-streaming | Mocked | `test_server_endpoints.py` (mock response) |

### Critical gaps — no tests exist for:

| Area | Risk if broken by refactor |
|---|---|
| `call()` loop internals (direct unit test) | HIGH — no baseline |
| `call_stream()` loop internals (direct unit test) | HIGH — no baseline |
| Approval callback flow in `call()` | HIGH — completely untested |
| Cancellation (`cancel_event`) | MEDIUM — interactive only |
| Context window compaction integration | MEDIUM |
| Cost/metadata accumulation accuracy | MEDIUM — `model_dump()` caller |
| Tool number offset tracking | LOW — cosmetic |
| Equivalence between `call()` and `call_stream()` | CRITICAL — the whole point |

### Test-first strategy

**Before refactoring, write these baseline tests:**

1. **Equivalence test**: Same mocked LLM + tools → verify `call()` result matches reconstructed result from `call_stream()` events (field by field).
2. **Multi-iteration test**: LLM returns tool calls for 2-3 rounds → verify `tool_calls`, `num_llm_calls`, `messages` are all correct.
3. **Approval callback test for `call()`**: Mock a tool returning `APPROVAL_REQUIRED` → verify callback is invoked → verify tool re-execution.
4. **Cost accumulation test**: Multiple LLM iterations → verify cost fields sum correctly.
5. **Cancellation test**: Set `cancel_event` mid-execution → verify `LLMInterruptedError` raised.

These tests run against the *current* code first (establish baseline), then again after refactoring (verify no regression).

## Implementation Plan

### Step 0: Write baseline tests (NEW — before any refactoring)

Write the 5 tests described above targeting the current `call()` and `call_stream()` implementations. Run them green. These become our regression safety net.

### Step 1: Add missing parameters to `call_stream()`

Add to signature:
- `trace_span` (default `DummySpan()`) — pass through to `_invoke_llm_tool_call`
- `cancel_event: Optional[threading.Event]` (default `None`) — check before LLM calls and between tool futures, raise `LLMInterruptedError`
- `tool_number_offset: int` (default `0`) — initialize `tool_number_offset` from parameter instead of always 0

Keep existing parameters: `system_prompt`, `user_prompt`, `response_format`, `msgs`, `enable_tool_approval`, `tool_decisions`, `request_context`.

### Step 2: Enrich `ANSWER_END` event data

Add to the `ANSWER_END` yield:
- `tool_calls`: accumulated list of all tool call dicts (same format as `call()` currently returns)
- `num_llm_calls`: iteration count `i`
- `prompt`: `json.dumps(messages, indent=2)`
- `costs`: `costs.model_dump()` (already in metadata, ensure it's there)

### Step 3: Add approval handling inside `call_stream()` (no change needed)

When a tool returns `APPROVAL_REQUIRED`:
- If `enable_tool_approval` is True: yield `APPROVAL_REQUIRED` and return (existing behavior)
- If `enable_tool_approval` is False: convert to ERROR (existing behavior)

No callback logic inside `call_stream()` — that lives in the `call()` wrapper.

### Step 4: Rewrite `call()` as a thin wrapper

```python
def call(self, messages, response_format=None, user_prompt=None,
         trace_span=DummySpan(), tool_number_offset=0,
         request_context=None, cancel_event=None) -> LLMResult:

    all_tool_calls = []
    tool_decisions = None
    total_num_llm_calls = 0

    while True:
        stream = self.call_stream(
            msgs=messages,
            response_format=response_format,
            enable_tool_approval=self.approval_callback is not None,
            tool_decisions=tool_decisions,
            trace_span=trace_span,
            cancel_event=cancel_event,
            tool_number_offset=tool_number_offset,
            request_context=request_context,
        )

        tool_decisions = None
        answer_data = None

        for event in stream:
            if event.event == StreamEvents.TOOL_RESULT:
                all_tool_calls.append(event.data)
                tool_number_offset += 1
            elif event.event == StreamEvents.AI_MESSAGE:
                # Log to console (preserves CLI behavior)
                if event.data.get("reasoning"):
                    logging.info(f"[italic dim]AI reasoning:\n\n{event.data['reasoning']}[/italic dim]\n")
                if event.data.get("content"):
                    logging.info(f"[bold {AI_COLOR}]AI:[/bold {AI_COLOR}] {event.data['content']}")
            elif event.event == StreamEvents.APPROVAL_REQUIRED:
                # Invoke approval callback, build tool_decisions
                messages = event.data["messages"]
                pending = event.data["pending_approvals"]
                tool_decisions = self._handle_approval_from_stream(pending)
                break
            elif event.event == StreamEvents.ANSWER_END:
                answer_data = event.data

        if answer_data:
            total_num_llm_calls += answer_data.get("num_llm_calls", 0)
            return LLMResult(
                result=answer_data["content"],
                tool_calls=all_tool_calls + answer_data.get("tool_calls", []),
                num_llm_calls=total_num_llm_calls,
                prompt=answer_data.get("prompt"),
                messages=answer_data["messages"],
                metadata=answer_data.get("metadata"),
                **answer_data.get("costs", {}),
            )

        if not tool_decisions:
            raise Exception("Stream ended without ANSWER_END or APPROVAL_REQUIRED")
```

### Step 5: Simplify `prompt_call()` and `messages_call()`

These already delegate to `call()` — they should continue to work unchanged. Verify parameter passing is correct.

### Step 6: Delete dead code

Remove the old `call()` loop body (~220 lines). The method stays but becomes ~40 lines.

### Step 7: Run tests

```bash
poetry run pytest tests -m "not llm" --no-cov
```

Run the baseline tests from Step 0 again to verify equivalence.

## Files to Modify

| File | Change |
|---|---|
| `holmes/core/tool_calling_llm.py` | Main refactor — `call_stream()` gets new params + enriched ANSWER_END, `call()` becomes wrapper |
| `tests/test_tool_calling_llm_equivalence.py` (NEW) | Baseline + regression tests |
| (No other files should need changes) | Callers' interfaces are preserved |

## Risks

1. **Approval round-trip overhead**: Each approval creates a new generator. Context window limiting and tool re-fetch run again. Acceptable — approval is rare and these operations are cheap compared to LLM calls.

2. **`max_steps` consumed across approval re-invocations**: Each `call_stream()` invocation resets `i = 0`. If the first invocation used 5 of 15 steps before yielding `APPROVAL_REQUIRED`, the re-invocation gets a fresh 15. This is slightly more permissive than today's single-loop behavior. Options: (a) accept it (approval is rare), (b) add a `max_steps` parameter to `call_stream()` and pass remaining steps. **Decision: Accept it for now** — approval rarely happens and the extra headroom is harmless.

3. **Tool call merging across approval rounds**: The wrapper must accumulate `tool_calls` from TOOL_RESULT events across all invocations AND from the final ANSWER_END. Must not double-count.

4. **Cost accumulation across rounds**: Costs from the first invocation are in stream events. The wrapper must accumulate across all rounds. The final ANSWER_END costs only cover its own invocation — wrapper must merge.

5. **Logging parity**: Current `call()` logs tool counts, intermediate text, etc. via `logging.info` with rich markup. The wrapper must replicate this from stream events to avoid silent regression in CLI output.

6. **`cancel_event` in generator**: If cancellation fires mid-execution inside the generator, `LLMInterruptedError` propagates out through the wrapper's `for event in stream` loop naturally. No special handling needed.

7. **`_runbook_in_use` thread safety** (pre-existing): `CheckRunner` shares one `ToolCallingLLM` across threads in `ThreadPoolExecutor`. The `_runbook_in_use` flag is a latent race condition. Not introduced by this refactor, not triggered today (checks don't use runbooks), but worth documenting.
