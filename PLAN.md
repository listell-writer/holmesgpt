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

**Interactive mode threading:** `interactive.py` runs `call()` in a daemon thread (`threading.Thread(target=_run_ai_call, daemon=True)`). A separate escape-key listener on the main thread can set `cancel_event` to interrupt. `LLMInterruptedError` raised inside `call()` propagates out of the thread and is caught via `call_error[0]`. This works identically if `call()` internally uses a generator — the exception propagates through the `for event in stream` loop.

### Callers of `call_stream()`

| Caller | File:Line | Parameters used |
|---|---|---|
| Server streaming | `server.py:386` | `msgs`, `enable_tool_approval`, `tool_decisions`, `response_format`, `request_context` |
| AG-UI experimental | `experimental/ag-ui/server-agui.py:134` | `msgs`, `enable_tool_approval` |

Server streaming wraps the generator with `stream_chat_formatter()` (`holmes/utils/stream.py:66`) which converts `StreamMessage` objects to SSE text events. It expects `ANSWER_END` or `APPROVAL_REQUIRED` as terminal events.

**Neither caller uses `system_prompt` or `user_prompt` parameters of `call_stream()`.** Both pass pre-built messages via `msgs=`. These params can be removed.

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
- `as_tool_result_response()` → dict with keys `tool_call_id`, **`tool_name`**, `description`, `role`, `result`. This is what `call()` accumulates into `LLMResult.tool_calls`.
- `as_streaming_tool_result_response()` → dict with keys `tool_call_id`, **`name`** (not `tool_name`!), `description`, `role`, `result`. This is what `call_stream()` puts in `TOOL_RESULT` event data.

**Format mismatch:** `as_tool_result_response()` uses key `tool_name`. `as_streaming_tool_result_response()` uses key `name`. The wrapper cannot collect `TOOL_RESULT` event data directly as `LLMResult.tool_calls`.

**`StructuredToolResult`** (`holmes/core/tools.py:86-95`): Has fields `status`, `error`, `data`, `invocation`, `params`, etc. The approval callback in interactive mode reads `tool_result.invocation` (the command string) and `tool_result.params.suggested_prefixes` to display the approval prompt.

**`PendingToolApproval`** (`holmes/core/models.py:94-100`): `tool_call_id`, `tool_name`, `description`, `params`. This is what `APPROVAL_REQUIRED` events currently contain — but it's **missing `invocation`**, which the approval callback needs. Must be enriched (see Decision: Approval Event Data).

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

The callback receives a **full `StructuredToolResult`** object with `invocation` and `params` populated.

**In interactive mode**, the callback is wrapped (`interactive.py:1484-1499`) to coordinate terminal state (cbreak mode vs normal mode for prompt_toolkit). The wrapper (`_wrapped_approval`) sets/clears `approval_active` threading event around the actual callback call.

## Differences Between `call()` and `call_stream()`

### Signature

- `call()`: `messages, response_format, user_prompt, trace_span, tool_number_offset, cancel_event, request_context`
- `call_stream()`: `system_prompt, user_prompt, response_format, msgs, enable_tool_approval, tool_decisions, request_context`

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

No caller uses `system_prompt` or `user_prompt`. All pass pre-built messages via `msgs=`.

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

### Console logging in `call()`

`call()` logs these during execution:
- Reasoning content: `logging.info(f"[italic dim]AI reasoning:\n\n{...}[/italic dim]\n")` (line 529-531)
- AI intermediate text: `logging.info(f"[bold {AI_COLOR}]AI:[/bold {AI_COLOR}] {text_response}")` (line 555)
- Tool call count: `logging.info(f"The AI requested [bold]{len(tools_to_call)}[/bold] tool call(s).")` (line 556-557)
- Blank line after tool batch: `logging.info("")` (line 629)
- Tool execution logging happens inside `_invoke_llm_tool_call` — shared by both paths, no change needed.

`call_stream()` does not log to console — it yields events instead. The `AI_MESSAGE` event carries `content` and `reasoning` fields (lines 1086-1093).

### Internal tool_calls tracking

Both methods track tool calls for repeated-call prevention:
- `call()` line 425-427: `tool_calls: list[dict] = []` (for safeguards) + `all_tool_calls = []` (for LLMResult)
- `call_stream()` line 967: `tool_calls: list[dict] = []` (for safeguards only — no `all_tool_calls` equivalent)

`call_stream()` does NOT accumulate an `all_tool_calls` list. It only tracks `tool_calls` for the repeated-call safeguard.

## Decisions

### Approval Mechanism: Option 4 — Always yield + wrapper re-invokes

The unified `call_stream()` always yields `APPROVAL_REQUIRED` and returns when tools need approval. The `call()` wrapper:
1. Drains the stream
2. If it encounters `APPROVAL_REQUIRED`, invokes `self.approval_callback` for each pending tool
3. Re-invokes `call_stream()` with the `tool_decisions` and saved messages
4. Loops until it gets `ANSWER_END`

This matches how the server already handles approval resumption today.

**Trade-offs accepted:**
- Each approval round creates a new generator and re-enters the loop (context window limiting, tool re-fetch run again) — slight inefficiency but keeps the code simple.
- `call()` wrapper needs a while loop to handle multiple approval rounds in one conversation turn.

### Approval Event Data: Enrich with full StructuredToolResult

The `APPROVAL_REQUIRED` event currently carries `PendingToolApproval` dicts which have `tool_call_id`, `tool_name`, `description`, `params`. But the approval callback in interactive mode needs `StructuredToolResult.invocation` (the command string) and `StructuredToolResult.params.suggested_prefixes`.

**Solution:** Include the full `StructuredToolResult` objects in the `APPROVAL_REQUIRED` event data alongside the existing `pending_approvals`. This way the `call()` wrapper can pass them directly to the callback without reconstruction.

Add `tool_results: list[StructuredToolResult]` (keyed by `tool_call_id`) to the event data. No SSE wire format change — `stream_chat_formatter` only reads `pending_approvals`, `content`, and `messages` from this event.

### Console Logging: All in `call()` wrapper

`call_stream()` only yields events — no console logging side effects (this is already the status quo).

The `call()` wrapper intercepts stream events and logs to console:
- `AI_MESSAGE` → `logging.info` for reasoning and/or text content
- `START_TOOL` → count per iteration, then `logging.info(f"The AI requested {count} tool call(s).")`
- After all `TOOL_RESULT` events in a batch → `logging.info("")` (blank line)
- `ANSWER_END` → no logging (caller handles display)

### Remove `system_prompt`/`user_prompt` from `call_stream()`

No caller uses these. All pass pre-built messages via `msgs=`. Remove them to simplify `call_stream()`. The message building becomes:
```python
messages: list[dict] = list(msgs) if msgs else []
```

### Add `tool_number_offset` to `call_stream()`

Add `tool_number_offset: int = 0` to the signature. Changes one local variable's initial value. Non-breaking (default 0 preserves existing callers).

**What tool numbers are:** Sequential labels (1, 2, 3...) passed to `ToolInvokeContext`. The bash toolset uses them to create numbered temp files (`tool_result_1.txt`, etc.) so the LLM can reference saved outputs.

**Why:** In interactive mode, `call()` is invoked once per conversation turn. If turn 1 executed tools 1-5, turn 2 starts at 6. `interactive.py` passes `tool_number_offset=len(all_tool_calls_history)`. The wrapper passes this through. On approval re-invocation, the wrapper updates the offset to account for tools already executed.

### Add `all_tool_calls` to `call_stream()` and ANSWER_END

Add a parallel `all_tool_calls: list[dict] = []` list using `as_tool_result_response()` format. Include it in `ANSWER_END` data as `tool_calls`. The wrapper uses this for `LLMResult.tool_calls`.

`TOOL_RESULT` events continue using `as_streaming_tool_result_response()` format — no wire format change.

### Cost handling across approval rounds

From a product perspective, each conversation turn should show total cost including all approval re-invocations. The `call()` wrapper sums costs from each round's `ANSWER_END`. Even though interactive mode doesn't display costs today, the data should be correct for when it does.

### `max_steps` across approval re-invocations

Each `call_stream()` invocation resets `i = 0`. This is slightly more permissive than today's single-loop behavior. **Accept it** — approval rarely happens, extra headroom is harmless.

## Test Plan

### Baseline tests (Step 0 — before any refactoring)

These establish behavior of the current code, then verify the refactored code matches.

**Test 1: Multi-iteration happy path**
- Mock LLM to return a tool call on first response, then a text answer on second
- Mock tool executor with a simple tool that returns success
- Call `ai.call(messages)` and `ai.call_stream(msgs=messages)`
- Verify `call()` result:
  - `result` == expected text answer
  - `tool_calls` has 1 entry with correct `tool_name` and `description`
  - `num_llm_calls` == 2
  - `messages` contains: original messages + assistant (tool_calls) + tool result + assistant (answer)
  - cost fields are populated (prompt_tokens > 0, etc.)
  - `prompt` is valid JSON string of messages
- Verify `call_stream()` yields:
  - `START_TOOL` event with tool name
  - `TOOL_RESULT` event with result data
  - `TOKEN_COUNT` events
  - `ANSWER_END` event with `content`, `messages`, `metadata`

**Test 2: Equivalence test**
- Same mocked setup as Test 1
- Run both `call()` and `call_stream()` with identical inputs
- Verify `call()` result fields match what you'd reconstruct from `call_stream()` ANSWER_END
- Compare: `result`, `messages` (length and structure), `tool_calls` (count), `num_llm_calls`

**Test 3: Approval callback flow**
- Mock LLM to return a tool call
- Mock tool to return `APPROVAL_REQUIRED` status with `invocation` and `params`
- Set `approval_callback` that returns `(True, None)`
- Call `ai.call(messages)`
- Verify callback was invoked
- Verify final result includes the approved tool's output (tool was re-executed)

**Test 4: Cost accumulation**
- Mock LLM with 3 iterations (2 tool rounds + final answer)
- Mock cost info on each response (set `_hidden_params` with usage data)
- Call `ai.call(messages)`
- Verify `total_cost` == sum of all iterations
- Verify `prompt_tokens`, `completion_tokens` are summed
- Verify `num_llm_calls` == 3

**Test 5: Cancellation**
- Mock LLM with a tool call response
- Mock tool execution to sleep briefly
- Set `cancel_event` from a timer thread after tool starts
- Verify `LLMInterruptedError` is raised

**Test 6 (post-refactor only): Approval via re-invocation**
- Mock LLM to return a tool call
- Mock tool to return `APPROVAL_REQUIRED`
- Set `approval_callback` that approves
- Call refactored `call()` (which internally uses `call_stream()` + re-invocation)
- Verify the full flow: stream yields `APPROVAL_REQUIRED`, wrapper calls callback, re-invokes with `tool_decisions`, gets `ANSWER_END`
- Verify `LLMResult` has correct `tool_calls`, `messages`, costs

## Implementation Plan

### Step 0: Write baseline tests

Write Tests 1-5 above. Run them green against current code. These become our regression safety net.

### Step 1: Simplify `call_stream()` signature

Remove `system_prompt` and `user_prompt` parameters. Simplify message building to:
```python
messages: list[dict] = list(msgs) if msgs else []
```

Add new parameters:
- `trace_span` (default `DummySpan()`) — pass through to `_invoke_llm_tool_call` (replacing hardcoded `DummySpan()` at line 1111)
- `cancel_event: Optional[threading.Event]` (default `None`) — check before LLM calls and between tool futures, raise `LLMInterruptedError` (3 check points, matching `call()`)
- `tool_number_offset: int` (default `0`) — initialize local variable from parameter instead of hardcoded 0 at line 973

### Step 2: Enrich `call_stream()` internals and ANSWER_END

1. Add `all_tool_calls: list[dict] = []` accumulation using `as_tool_result_response()` format.

2. Enrich ANSWER_END (line 1073-1080):
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

3. Enrich APPROVAL_REQUIRED event data to include `tool_results` dict mapping `tool_call_id` → `StructuredToolResult` (the full objects, for the approval callback).

**No wire format change** — `stream_chat_formatter` only reads `pending_approvals`, `content`, `messages` from `APPROVAL_REQUIRED` events, and only reads `content`, `messages`, `metadata` from `ANSWER_END`. Extra fields are ignored.

### Step 3: Rewrite `call()` as a thin wrapper

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
    accumulated_costs = {}  # sum across approval rounds

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
        start_tool_count = 0

        for event in stream:
            if event.event == StreamEvents.START_TOOL:
                start_tool_count += 1
            elif event.event == StreamEvents.TOOL_RESULT:
                tool_number_offset += 1
                if start_tool_count > 0:
                    # Log tool count once when first result arrives
                    logging.info(
                        f"The AI requested [bold]{start_tool_count}[/bold] tool call(s)."
                    )
                    start_tool_count = 0  # reset so we don't log again for this batch
            elif event.event == StreamEvents.AI_MESSAGE:
                reasoning = event.data.get("reasoning")
                content = event.data.get("content")
                if reasoning:
                    logging.info(
                        f"[italic dim]AI reasoning:\n\n{reasoning}[/italic dim]\n"
                    )
                if content and content.strip():
                    logging.info(
                        f"[bold {AI_COLOR}]AI:[/bold {AI_COLOR}] {content}"
                    )
            elif event.event == StreamEvents.APPROVAL_REQUIRED:
                messages = event.data["messages"]
                pending = event.data["pending_approvals"]
                tool_results = event.data["tool_results"]  # full StructuredToolResult objects
                tool_decisions = self._build_approval_decisions(pending, tool_results)
                break
            elif event.event == StreamEvents.ANSWER_END:
                answer_data = event.data

        if answer_data:
            total_num_llm_calls += answer_data.get("num_llm_calls", 0)
            all_tool_calls.extend(answer_data.get("tool_calls", []))
            round_costs = answer_data.get("costs", {})
            accumulated_costs = _sum_costs(accumulated_costs, round_costs)
            return LLMResult(
                result=answer_data["content"],
                tool_calls=all_tool_calls,
                num_llm_calls=total_num_llm_calls,
                prompt=answer_data.get("prompt"),
                messages=answer_data["messages"],
                metadata=answer_data.get("metadata"),
                **accumulated_costs,
            )

        if not tool_decisions:
            raise Exception("Stream ended without ANSWER_END or APPROVAL_REQUIRED")
```

### Step 3b: Helper methods

**`_build_approval_decisions()`**: For each pending approval, calls `self.approval_callback(tool_result)` with the full `StructuredToolResult` from the event data. Returns list of `ToolApprovalDecision`.

**`_sum_costs()`**: Module-level function. Sums two cost dicts field by field (total_cost, prompt_tokens, etc.). Uses `max()` for `max_prompt_tokens_per_call` and `max_completion_tokens_per_call`.

### Step 4: Verify `prompt_call()` and `messages_call()`

These already delegate to `call()` — no changes needed. `prompt_call()` builds messages and calls `call()`. `messages_call()` calls `call()` directly. Both keep working.

### Step 5: Delete dead code

Remove the old `call()` loop body (~220 lines). The method stays but becomes ~60 lines.

Remove `_handle_tool_call_approval()` if it's no longer called by anything. Check if `process_tool_decisions()` is still needed (yes — it's called by `call_stream()` on re-invocation with `tool_decisions`).

### Step 6: Run tests

```bash
poetry run pytest tests -m "not llm" --no-cov
```

Run baseline tests from Step 0 + Test 6 (post-refactor approval flow).

## Files to Modify

| File | Change |
|---|---|
| `holmes/core/tool_calling_llm.py` | Main refactor: simplify `call_stream()` signature, add params, enrich ANSWER_END + APPROVAL_REQUIRED, `call()` becomes wrapper, add `_build_approval_decisions()` + `_sum_costs()`, delete old loop |
| `tests/test_tool_calling_llm_baseline.py` (NEW) | Baseline + regression tests (Tests 1-6) |

No changes needed to: `main.py`, `interactive.py`, `checks.py`, `server.py`, `experimental/ag-ui/server-agui.py`, `holmes/utils/stream.py`.

## Risks

1. **Approval round-trip overhead**: Each approval creates a new generator. Context window limiting and tool re-fetch run again. Acceptable — approval is rare and these are cheap vs LLM calls.

2. **`max_steps` across approval re-invocations**: Each invocation resets `i = 0`. Slightly more permissive than today. Accepted — approval is rare, extra headroom is harmless.

3. **`_build_approval_decisions()` must pass correct data to callback**: The interactive callback reads `tool_result.invocation` and `tool_result.params.suggested_prefixes`. The enriched `APPROVAL_REQUIRED` event must carry the full `StructuredToolResult` with these fields populated. This data is available inside `call_stream()` at the point where `APPROVAL_REQUIRED` status is detected — it's the `tool_call_result.result` object.

4. **`_runbook_in_use` thread safety** (pre-existing): `CheckRunner` shares one `ToolCallingLLM` across threads. The `_runbook_in_use` flag is a latent race condition. Not introduced by this refactor, not triggered today (checks don't use runbooks).

5. **Logging parity edge case**: Current `call()` logs tool count BEFORE tools execute (line 556-557). The wrapper logs it when it sees `START_TOOL` events which are yielded BEFORE tool execution starts. Timing should match. But the exact log output format must be verified against current behavior.
