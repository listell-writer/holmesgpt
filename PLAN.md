# Refactor: Make `call()` a thin wrapper around `call_stream()`

## Goal

Unify the two LLM execution loops in `ToolCallingLLM` so that `call_stream()` is the single source of truth and `call()` is a thin wrapper that drains the stream and reconstructs an `LLMResult`.

## Current State

### Two independent loops

`call()` (lines 414-631) and `call_stream()` (lines 938-1218) in `holmes/core/tool_calling_llm.py` are two separate implementations of the same agentic loop. They share ~80% of their logic (LLM completion, tool execution, context window limiting, compaction cost tracking, repeated tool call prevention, runbook tool refresh) but have diverged in several ways.

Neither method actually uses `llm.completion(stream=True)`. Both call `llm.completion()` synchronously per iteration. "Streaming" in `call_stream()` means yielding events between iterations, not streaming tokens.

### Callers of `call()`

| Caller | File:Line | Parameters used |
|---|---|---|
| CLI `ask` (non-interactive) | `holmes/main.py:376` | `messages`, `trace_span` |
| CLI interactive mode | `holmes/interactive.py:1518` | `messages`, `trace_span`, `tool_number_offset`, `cancel_event` |
| Health checks | `holmes/checks/checks.py:73` | `messages`, `response_format` |
| Server non-streaming | `server.py:401` (via `messages_call()`) | `messages`, `trace_span`, `response_format`, `request_context` |

**Note on interactive mode threading:** `interactive.py` runs `call()` in a daemon thread (`threading.Thread(target=_run_ai_call, daemon=True)`). A separate escape-key listener on the main thread can set `cancel_event` to interrupt. `LLMInterruptedError` raised inside `call()` propagates out of the thread and is caught via `call_error[0]`. This works identically if `call()` internally uses a generator — the exception propagates through the `for event in stream` loop.

### Callers of `call_stream()`

| Caller | File:Line | Parameters used |
|---|---|---|
| Server streaming | `server.py:386` | `msgs`, `enable_tool_approval`, `tool_decisions`, `response_format`, `request_context` |
| AG-UI experimental | `experimental/ag-ui/server-agui.py:134` | `msgs`, `enable_tool_approval` |

Server streaming wraps the generator with `stream_chat_formatter()` (in `holmes/utils/stream.py:66`) which converts `StreamMessage` objects to SSE text events. It expects `ANSWER_END` or `APPROVAL_REQUIRED` as terminal events.

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

### Key data structures

**`ToolCallResult`** (`holmes/core/models.py:22-64`) has three serialization methods:
- `as_tool_call_message()` → dict with `role: "tool"`, formatted content string with metadata. Used to add tool results to the conversation `messages` list.
- `as_tool_result_response()` → dict with `tool_call_id`, `tool_name`, `description`, `role`, `result` (model_dump). This is what `call()` accumulates into `LLMResult.tool_calls`.
- `as_streaming_tool_result_response()` → dict with `tool_call_id`, `name` (not `tool_name`!), `description`, `role`, `result` (model_dump). This is what `call_stream()` puts in `TOOL_RESULT` event data.

**Key difference:** `as_tool_result_response()` has `tool_name` key. `as_streaming_tool_result_response()` has `name` key. These are NOT the same format. The `call()` wrapper cannot simply collect `TOOL_RESULT` event data as `LLMResult.tool_calls` — the field names differ.

**`PendingToolApproval`** (`holmes/core/models.py:94-100`): `tool_call_id`, `tool_name`, `description`, `params`. This is what `APPROVAL_REQUIRED` events contain in `pending_approvals` list.

**`ToolApprovalDecision`** (`holmes/core/models.py:103-108`): `tool_call_id`, `approved`, `save_prefixes`. This is what `call_stream()` accepts via `tool_decisions` parameter to resume after approval.

### How `call_stream()` handles tool_decisions on re-invocation

When `call_stream()` is re-invoked with `tool_decisions` + saved `msgs`:
1. Lines 953-958: Calls `self.process_tool_decisions(msgs, tool_decisions)` at the top
2. `process_tool_decisions()` (lines 260-367): Finds pending tool calls in the message history (marked with `pending_approval=True`), executes approved ones, creates error messages for denied ones, inserts results into messages
3. Then the normal loop continues — the LLM sees the tool results and responds

### How `call()` handles approval today (via callback)

When `call()` encounters `APPROVAL_REQUIRED` status on a tool:
1. Line 600: Calls `self._handle_tool_call_approval(tool_call_result, ...)`
2. `_handle_tool_call_approval()` (lines 868-936):
   - If no `self.approval_callback`: converts to ERROR
   - Re-checks if approval still needed (another tool may have approved the prefix)
   - Calls `self.approval_callback(tool_call_result.result)` → blocks, gets `(approved, feedback)`
   - If approved: re-executes tool with `user_approved=True`
   - If denied: sets ERROR status with feedback
3. The loop continues with the tool result (approved or denied)

**In interactive mode**, `approval_callback` is wrapped to coordinate terminal state (cbreak mode vs normal mode for prompt_toolkit). See `interactive.py:1484-1499`.

## Differences Between `call()` and `call_stream()`

### Signature

- `call()`: `messages, response_format, user_prompt, trace_span, tool_number_offset, cancel_event, request_context`
- `call_stream()`: `system_prompt, user_prompt, response_format, msgs, enable_tool_approval, tool_decisions, request_context`
- `call_stream()` builds messages internally from `system_prompt`/`user_prompt`/`msgs`; `call()` expects pre-built messages.

### Message building in `call_stream()`

`call_stream()` lines 960-966 builds its own messages list:
```python
messages: list[dict] = []
if system_prompt:
    messages.append({"role": "system", "content": system_prompt})
if user_prompt:
    messages.append({"role": "user", "content": user_prompt})
if msgs:
    messages.extend(msgs)
```

**Implication for wrapper:** `call()` callers pass pre-built messages that already contain system/user messages. The wrapper must pass them via `msgs=messages` and leave `system_prompt`/`user_prompt` empty to avoid duplication.

### Return type

- `call()` returns `LLMResult` (Pydantic model with `result`, `tool_calls`, `num_llm_calls`, `prompt`, `messages`, cost fields, `metadata`).
- `call_stream()` yields `StreamMessage` objects and ends with `ANSWER_END` containing `content`, `messages`, `metadata`.

### Tracing

- `call()` passes caller-provided `trace_span` to `_invoke_llm_tool_call`.
- `call_stream()` hardcodes `DummySpan()` at line 1111.

### Cancellation

- `call()` checks `cancel_event` (threading.Event) at 3 points: before each iteration (line 435), after LLM response (line 510), and between tool futures (line 583).
- `call_stream()` has no cancellation support.

### Tool approval

- `call()` uses synchronous `self.approval_callback` — blocks thread, gets `(approved, feedback)`, continues loop.
- `call_stream()` uses `enable_tool_approval` flag — collects `PendingToolApproval` list, yields `APPROVAL_REQUIRED` event, returns. Caller resumes with new invocation passing `tool_decisions`.

### Token counting

- `call()` counts tokens only at the final response (line 534).
- `call_stream()` counts tokens after every LLM call (line 1060) AND after every tool result batch (line 1170).

### Streaming events

- `call()` produces none — logs to rich console instead.
- `call_stream()` yields: `START_TOOL`, `TOOL_RESULT`, `AI_MESSAGE`, `ANSWER_END`, `TOKEN_COUNT`, `APPROVAL_REQUIRED`, `CONVERSATION_HISTORY_COMPACTED` (from `limit_result.events`).

### Compaction events

- `call()` discards `limit_result.events`.
- `call_stream()` yields them.

### Cost tracking

- `call()` returns costs as top-level fields in `LLMResult` (via `**costs.model_dump()`).
- `call_stream()` puts costs in `metadata["costs"]` dict and yields them in `TOKEN_COUNT` events.

### Missing fields in `call_stream()` ANSWER_END

Current ANSWER_END data (line 1073-1080):
```python
{"content": response_message.content, "messages": messages, "metadata": metadata}
```

Missing vs what `LLMResult` needs:
- `num_llm_calls` (iteration count `i`)
- `prompt` (JSON-serialized messages)
- `tool_calls` (list of tool call dicts in `as_tool_result_response()` format)
- Individual cost fields as top-level keys (only has `metadata["costs"]` as nested dict)

### Session prefix extraction

- `call()` extracts bash session prefixes only inside `_handle_tool_call_approval` (line 310 via `process_tool_decisions`).
- `call_stream()` extracts before each tool execution batch (line 1100).

### Reasoning/text mid-loop

- `call()` logs reasoning via `logging.info` with rich markup (line 529-531), logs intermediate AI text (line 555).
- `call_stream()` yields `AI_MESSAGE` event with `content` and `reasoning` fields (lines 1086-1093).

### Console logging in `call()`

`call()` logs these during execution:
- Reasoning content: `logging.info(f"[italic dim]AI reasoning:\n\n{...}[/italic dim]\n")` (line 529-531)
- AI intermediate text: `logging.info(f"[bold {AI_COLOR}]AI:[/bold {AI_COLOR}] {text_response}")` (line 555)
- Tool call count: `logging.info(f"The AI requested [bold]{len(tools_to_call)}[/bold] tool call(s).")` (line 556-557)
- Blank line after tool batch: `logging.info("")` (line 629)
- Tool execution logging happens inside `_invoke_llm_tool_call` — shared by both paths.

### Internal tool_calls tracking

Both methods track tool calls internally for repeated-call prevention:
- `call()` line 425-427: `tool_calls: list[dict] = []` (for safeguards) + `all_tool_calls = []` (for LLMResult)
- `call_stream()` line 967: `tool_calls: list[dict] = []` (for safeguards only — no `all_tool_calls` equivalent)

`call_stream()` does NOT accumulate an `all_tool_calls` list for its ANSWER_END. It only tracks `tool_calls` for the repeated-call safeguard.

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

The `call()` wrapper intercepts stream events and logs to console, matching current `call()` output:
- `AI_MESSAGE` → `logging.info` for reasoning and/or text content
- `START_TOOL` → log tool call count (batch all START_TOOL events from one iteration, log count)
- Blank line after tool results complete in a batch
- `TOOL_RESULT` → tool execution logging already happens inside `_invoke_llm_tool_call` (shared code, no change needed)
- `ANSWER_END` → no logging (caller handles display)

This keeps `call_stream()` pure and testable, while preserving CLI output for `call()` callers.

**Note:** `call_stream()` already doesn't log to console — this is documenting the status quo, not a change.

## Decision: `tool_number_offset` parameter

**Add `tool_number_offset: int = 0` to `call_stream()` signature.**

### What `tool_number` is

Tool numbers are sequential labels (1, 2, 3...) passed to `ToolInvokeContext` for each tool execution. The bash toolset uses them to create numbered temp files for tool results (e.g., `tool_result_1.txt`, `tool_result_2.txt`) so the LLM can reference saved outputs by number.

### Why `call()` has the parameter today

In interactive mode, `call()` is invoked once per conversation turn. If turn 1 executed tools 1-5, turn 2 should start at 6. So `interactive.py` passes `tool_number_offset=len(all_tool_calls_history)` to maintain globally unique numbers across the conversation.

### Why we add it to `call_stream()`

It's trivial (changes one local variable's initial value from 0 to the parameter), non-breaking (default 0 preserves all existing callers), and avoids the wrapper needing to intercept and renumber tool events.

On approval re-invocation, the `call()` wrapper passes `tool_number_offset` updated to account for tools already executed in prior rounds.

## Decision: tool_calls format in ANSWER_END

**Add `all_tool_calls` accumulation to `call_stream()` and include in ANSWER_END.**

`as_tool_result_response()` (used by `call()` for `LLMResult.tool_calls`) has key `tool_name`.
`as_streaming_tool_result_response()` (used by `call_stream()` for `TOOL_RESULT` events) has key `name`.

These are different formats. Rather than having the wrapper convert between them, we add a parallel `all_tool_calls` list to `call_stream()` (same as `call()` has today) and include it in `ANSWER_END` data in the `as_tool_result_response()` format.

The wrapper uses `ANSWER_END.tool_calls` for `LLMResult.tool_calls`. The `TOOL_RESULT` events continue using the streaming format (unchanged, no breaking change for server/AG-UI consumers).

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

### Step 0: Write baseline tests (before any refactoring)

Write the 5 tests described above targeting the current `call()` and `call_stream()` implementations. Run them green. These become our regression safety net.

### Step 1: Add missing parameters to `call_stream()`

Add to signature:
- `trace_span` (default `DummySpan()`) — pass through to `_invoke_llm_tool_call` (replacing hardcoded `DummySpan()` at line 1111)
- `cancel_event: Optional[threading.Event]` (default `None`) — check before LLM calls and between tool futures, raise `LLMInterruptedError` (3 check points, matching `call()`)
- `tool_number_offset: int` (default `0`) — initialize local `tool_number_offset` from parameter instead of hardcoded 0 at line 973

Keep existing parameters unchanged: `system_prompt`, `user_prompt`, `response_format`, `msgs`, `enable_tool_approval`, `tool_decisions`, `request_context`.

### Step 2: Enrich `call_stream()` internals and ANSWER_END

1. Add `all_tool_calls: list[dict] = []` accumulation (parallel to `tool_calls` safeguard list), using `as_tool_result_response()` format — same as `call()` does today.

2. Change ANSWER_END yield (line 1073-1080) to include:
```python
yield StreamMessage(
    event=StreamEvents.ANSWER_END,
    data={
        "content": response_message.content,
        "messages": messages,
        "metadata": metadata,
        "tool_calls": all_tool_calls,
        "num_llm_calls": i,
        "prompt": json.dumps(messages, indent=2),
        "costs": costs.model_dump(),
    },
)
```

**Existing `TOOL_RESULT` events are unchanged** — they continue using `as_streaming_tool_result_response()` format. No breaking change for server/AG-UI consumers.

### Step 3: Approval handling (no change to `call_stream()`)

`call_stream()` already handles approval correctly:
- If `enable_tool_approval` is True: yields `APPROVAL_REQUIRED` and returns (existing behavior)
- If `enable_tool_approval` is False: converts to ERROR (existing behavior)

No callback logic inside `call_stream()` — that lives in the `call()` wrapper.

### Step 4: Rewrite `call()` as a thin wrapper

```python
@sentry_sdk.trace
def call(self, messages, response_format=None, user_prompt=None,
         trace_span=DummySpan(), tool_number_offset=0,
         request_context=None, cancel_event=None) -> LLMResult:
    """Synchronous wrapper around call_stream(). Drains the generator
    and reconstructs an LLMResult."""

    all_tool_calls = []
    tool_decisions = None
    total_num_llm_calls = 0
    total_costs = LLMCosts()

    while True:
        stream = self.call_stream(
            msgs=messages,  # pass pre-built messages via msgs, NOT system_prompt/user_prompt
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
                tool_number_offset += 1  # track for re-invocation offset
            elif event.event == StreamEvents.AI_MESSAGE:
                # Log to console — preserves current CLI behavior
                reasoning = event.data.get("reasoning")
                content = event.data.get("content")
                if reasoning:
                    logging.info(f"[italic dim]AI reasoning:\n\n{reasoning}[/italic dim]\n")
                if content and content.strip():
                    logging.info(f"[bold {AI_COLOR}]AI:[/bold {AI_COLOR}] {content}")
            elif event.event == StreamEvents.START_TOOL:
                pass  # tool execution logging happens inside _invoke_llm_tool_call
            elif event.event == StreamEvents.APPROVAL_REQUIRED:
                messages = event.data["messages"]
                pending = event.data["pending_approvals"]
                tool_decisions = self._build_approval_decisions(pending)
                break  # exit for-loop, re-invoke call_stream in while-loop
            elif event.event == StreamEvents.ANSWER_END:
                answer_data = event.data
            # TOKEN_COUNT, CONVERSATION_HISTORY_COMPACTED — ignored by wrapper

        if answer_data:
            total_num_llm_calls += answer_data.get("num_llm_calls", 0)
            all_tool_calls.extend(answer_data.get("tool_calls", []))
            return LLMResult(
                result=answer_data["content"],
                tool_calls=all_tool_calls,
                num_llm_calls=total_num_llm_calls,
                prompt=answer_data.get("prompt"),
                messages=answer_data["messages"],
                metadata=answer_data.get("metadata"),
                **answer_data.get("costs", {}),
            )

        if not tool_decisions:
            raise Exception("Stream ended without ANSWER_END or APPROVAL_REQUIRED")
```

### Step 4b: New helper method `_build_approval_decisions()`

This replaces the inline approval handling from `_handle_tool_call_approval()`. For each pending approval:
1. If no `self.approval_callback`: this path shouldn't happen (we only set `enable_tool_approval=True` when callback exists), but handle defensively by denying all.
2. Call `self.approval_callback(tool_result)` → gets `(approved, feedback)` synchronously.
3. Build `ToolApprovalDecision(tool_call_id=..., approved=..., save_prefixes=...)`.

```python
def _build_approval_decisions(self, pending_approvals: list[dict]) -> list[ToolApprovalDecision]:
    """Convert pending approvals to decisions using self.approval_callback."""
    decisions = []
    for pending in pending_approvals:
        if not self.approval_callback:
            decisions.append(ToolApprovalDecision(tool_call_id=pending["tool_call_id"], approved=False))
            continue

        # Build a StructuredToolResult for the callback
        # (callback expects the tool result to display approval prompt)
        tool_result = StructuredToolResult(
            status=StructuredToolResultStatus.APPROVAL_REQUIRED,
            params=pending.get("params"),
            # ... other fields needed by callback
        )
        approved, feedback = self.approval_callback(tool_result)
        decisions.append(ToolApprovalDecision(
            tool_call_id=pending["tool_call_id"],
            approved=approved,
        ))
    return decisions
```

**Note:** The exact `StructuredToolResult` construction needs care — the callback in interactive mode displays the tool invocation details. Must match what `_handle_tool_call_approval` currently passes. This is an implementation detail to resolve during coding.

### Step 5: Simplify `prompt_call()` and `messages_call()`

These already delegate to `call()` — no changes needed. Verify parameter passing is correct.

### Step 6: Delete dead code

Remove the old `call()` loop body (~220 lines). The method stays but becomes ~50 lines (wrapper + logging).

### Step 7: Run tests

```bash
poetry run pytest tests -m "not llm" --no-cov
```

Run the baseline tests from Step 0 again to verify equivalence.

## Files to Modify

| File | Change |
|---|---|
| `holmes/core/tool_calling_llm.py` | Main refactor — `call_stream()` gets new params + enriched ANSWER_END + `all_tool_calls` tracking, `call()` becomes wrapper, new `_build_approval_decisions()` helper |
| `tests/test_tool_calling_llm_baseline.py` (NEW) | Baseline + regression tests |
| (No other files should need changes) | Callers' interfaces are preserved |

## Risks

1. **Approval round-trip overhead**: Each approval creates a new generator. Context window limiting and tool re-fetch run again. Acceptable — approval is rare and these operations are cheap compared to LLM calls.

2. **`max_steps` consumed across approval re-invocations**: Each `call_stream()` invocation resets `i = 0`. If the first invocation used 5 of 15 steps before yielding `APPROVAL_REQUIRED`, the re-invocation gets a fresh 15 steps. This is slightly more permissive than today's single-loop behavior. **Decision: Accept it for now** — approval rarely happens and the extra headroom is harmless.

3. **Tool call merging across approval rounds**: The wrapper accumulates `tool_calls` from each round's `ANSWER_END`. Must not double-count. Since each `call_stream()` invocation has its own `all_tool_calls` list, and the wrapper extends from each, this should be clean.

4. **Cost accumulation across rounds**: Each `call_stream()` invocation has its own `LLMCosts` object. The ANSWER_END costs only cover that invocation. If there are multiple approval rounds, the wrapper must sum costs from each round's ANSWER_END. The pseudocode currently takes costs from the final ANSWER_END only — **this is a bug in the pseudocode**. Must accumulate across rounds.

5. **Logging parity**: Current `call()` logs tool counts (`"The AI requested N tool call(s)."`). `call_stream()` doesn't yield an event with this info directly — it yields individual `START_TOOL` events. The wrapper needs to batch these and log the count. Alternatively, add tool count logging inside `call_stream()` (it's just `logging.info`, not a yield). **Decision: Add the logging.info inside call_stream()** for tool counts — it's operational logging, not a stream event, and matches how tool execution logging already happens inside `_invoke_llm_tool_call`.

6. **`cancel_event` in generator**: If cancellation fires mid-execution inside the generator, `LLMInterruptedError` propagates out through the wrapper's `for event in stream` loop naturally. No special handling needed.

7. **`_runbook_in_use` thread safety** (pre-existing): `CheckRunner` shares one `ToolCallingLLM` across threads in `ThreadPoolExecutor`. The `_runbook_in_use` flag is a latent race condition. Not introduced by this refactor, not triggered today (checks don't use runbooks), but worth documenting.

8. **`_build_approval_decisions()` must construct the right `StructuredToolResult`**: The approval callback (especially in interactive mode) displays tool details to the user. The `StructuredToolResult` passed to it must contain the same info that `_handle_tool_call_approval` currently provides (invocation string, params, etc.). The `APPROVAL_REQUIRED` event's `pending_approvals` contains `PendingToolApproval` dicts — need to verify these have enough info, or enrich them.

9. **`APPROVAL_REQUIRED` event data for the wrapper**: Currently `APPROVAL_REQUIRED` data contains `pending_approvals` as a list of `PendingToolApproval.model_dump()` dicts. The wrapper needs `tool_call_id` and enough info to call the approval callback. The current `PendingToolApproval` has `tool_call_id`, `tool_name`, `description`, `params` — this should be sufficient but needs verification against what the interactive callback actually displays.
