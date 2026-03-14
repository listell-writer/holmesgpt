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

No caller uses `system_prompt` or `user_prompt` parameters of `call_stream()`. Both pass pre-built messages via `msgs=`.

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

**Format mismatch (resolved by Step 1b):** `as_tool_result_response()` uses key `tool_name`. `as_streaming_tool_result_response()` uses key `name`. After Step 1b unifies these into a single method emitting both keys, `TOOL_RESULT` event data and `LLMResult.tool_calls` use the same format, and the `call()` wrapper can safely collect from `TOOL_RESULT` events directly.

**`StructuredToolResult`** (`holmes/core/tools.py:86-95`): Has fields `status`, `error`, `data`, `invocation`, `params`, etc. The approval callback in interactive mode reads `tool_result.invocation` (the command string) and `tool_result.params.suggested_prefixes` to display the approval prompt.

**`PendingToolApproval`** (`holmes/core/models.py:94-100`): `tool_call_id`, `tool_name`, `description`, `params`. This is what `APPROVAL_REQUIRED` events currently contain — but it's **missing `invocation`**, which the approval callback needs. The `APPROVAL_REQUIRED` event must be enriched with full `StructuredToolResult` objects.

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

### Bash prefix saving — two parallel mechanisms

CLI and server use different mechanisms to persist approved bash command prefixes:

**CLI path** (current `call()` → `_handle_tool_call_approval()` → `self.approval_callback()`):
- `interactive.py:handle_tool_approval()` line 770-772: calls `_save_approved_prefixes(prefixes)` directly to `~/.holmes/bash_approved_prefixes.yaml`
- Returns `(True, None)` — the caller (`_handle_tool_call_approval`) doesn't know about the saved prefixes
- No session memory in messages — disk file is the persistence mechanism
- On subsequent tool calls in the same turn, `requires_approval()` re-reads the disk file → finds prefix → approved

**Server path** (current `call_stream()`):
- Client sends `ToolApprovalDecision.save_prefixes` on re-invocation
- `process_tool_decisions()` line 348-354: stores in message metadata as `bash_session_approved_prefixes`
- `extract_bash_session_prefixes()` reads from messages before each tool execution batch

These are parallel and independent mechanisms. Both continue to work in the refactored model (see Decisions section).

## Differences Between `call()` and `call_stream()`

### Signature

- `call()`: `messages, response_format, user_prompt, trace_span, tool_number_offset, cancel_event, request_context`
- `call_stream()`: `system_prompt, user_prompt, response_format, msgs, enable_tool_approval, tool_decisions, request_context`

`user_prompt` on `call()` is dead code — declared but never read by any logic. Will be removed.

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

### Console logging in `call()`

`call()` logs these during execution:
- Reasoning content: `logging.info(f"[italic dim]AI reasoning:\n\n{...}[/italic dim]\n")` (line 529-531)
- AI intermediate text: `logging.info(f"[bold {AI_COLOR}]AI:[/bold {AI_COLOR}] {text_response}")` (line 555)
- Tool call count: `logging.info(f"The AI requested [bold]{len(tools_to_call)}[/bold] tool call(s).")` (line 556-557) — logged BEFORE tool execution
- Blank line after tool batch: `logging.info("")` (line 629)
- Tool execution logging happens inside `_invoke_llm_tool_call` — shared by both paths, no change needed.

`call_stream()` does not log to console — it yields events instead. The `AI_MESSAGE` event carries `content` and `reasoning` fields (lines 1086-1093).

### Internal tool_calls tracking

Both methods track tool calls for repeated-call prevention:
- `call()` line 425-427: `tool_calls: list[dict] = []` (for safeguards) + `all_tool_calls = []` (for LLMResult)
- `call_stream()` line 967: `tool_calls: list[dict] = []` (for safeguards only — no `all_tool_calls` equivalent)

`call_stream()` does NOT accumulate an `all_tool_calls` list. It only tracks `tool_calls` for the repeated-call safeguard. This must be added.

## Decisions

### Approval Mechanism: Always yield + wrapper re-invokes

The unified `call_stream()` always yields `APPROVAL_REQUIRED` and returns when tools need approval. The `call()` wrapper:
1. Drains the stream
2. If it encounters `APPROVAL_REQUIRED`, invokes `self.approval_callback` for each pending tool
3. Re-invokes `call_stream()` with the `tool_decisions` and saved messages
4. Loops until it gets `ANSWER_END`

This matches how the server already handles approval resumption today.

**Trade-offs accepted:**
- Each approval round creates a new generator and re-enters the loop (context window limiting, tool re-fetch run again) — slight inefficiency but keeps the code simple.
- `call()` wrapper needs a while loop to handle multiple approval rounds in one conversation turn.

**Duplicate approval prevention:** `call_stream()` yields APPROVAL_REQUIRED for ALL pending tools at once. `_build_approval_decisions()` iterates through them and calls the callback for each. Before calling the callback, it re-checks whether approval is still needed (using `_is_tool_call_already_approved`, same logic as current line 891). If a previous tool in the same batch already saved the prefix to disk, the re-check finds it approved and auto-approves without prompting. This preserves the current UX exactly.

### Approval Event Data: Include full StructuredToolResult

The `APPROVAL_REQUIRED` event must carry full `StructuredToolResult` objects alongside the existing `pending_approvals`. The interactive approval callback (`interactive.py:742`) reads `tool_result.invocation` to display the command and `tool_result.params.suggested_prefixes` for the "don't ask again" option.

**Why full StructuredToolResult (Option 4) rather than adding fields to PendingToolApproval (Option 1):**
`StructuredToolResult` is already part of the public streaming API — every `TOOL_RESULT` event contains `result: model_dump()` via `as_streaming_tool_result_response()`. Adding it to `APPROVAL_REQUIRED` events doesn't introduce any new type exposure. Option 1 (adding `invocation` + `suggested_prefixes` to `PendingToolApproval`) would be cleaner if `StructuredToolResult` weren't already public, but since it is, Option 4 is simpler with no downside.

Add `tool_results: dict[str, StructuredToolResult]` (keyed by `tool_call_id`) to the event data. No SSE wire format change — `stream_chat_formatter` only reads `pending_approvals`, `content`, and `messages` from this event.

### Bash prefix saving in refactored model

Both CLI and server mechanisms continue to work unchanged:
- **CLI:** `_build_approval_decisions()` calls the callback → callback saves to disk. `ToolApprovalDecision.save_prefixes` is set to None (callback returns `(approved, feedback)`, not prefixes). `process_tool_decisions()` gets `save_prefixes=None` → no session memory update. Correct — disk file is the CLI persistence mechanism. Subsequent tools in the same turn re-read the disk file.
- **Server:** No change — `call_stream()` receives `tool_decisions` from the client with `save_prefixes` populated.

The callback signature doesn't need to change.

### Console Logging: All in `call()` wrapper

`call_stream()` only yields events — no console logging side effects (this is already the status quo).

The `call()` wrapper intercepts stream events and logs to console:
- `AI_MESSAGE` → `logging.info` for reasoning and/or text content
- `START_TOOL` → count per batch, log when first `TOOL_RESULT` arrives: `logging.info(f"The AI requested {count} tool call(s).")`. This matches current timing — `START_TOOL` events are yielded before execution, `TOOL_RESULT` after. Logging on first TOOL_RESULT means all START_TOOLs for the batch are already counted.
- After all `TOOL_RESULT` events in a batch → `logging.info("")` (blank line)
- `ANSWER_END` → no logging (caller handles display)

### Remove dead parameters

- Remove `system_prompt` and `user_prompt` from `call_stream()`. No caller uses these. Message building becomes: `messages: list[dict] = list(msgs) if msgs else []`
- Remove `user_prompt` from `call()`. Dead code — declared but never read. Update `prompt_call()` to stop passing it.
- Remove `messages_call()`. Pass-through to `call()` with no logic. Update its 4 callers (`server.py:401`, `tests/test_cache.py`, `tests/test_server_endpoints.py`, `tests/llm/test_ask_holmes.py:253`) to call `call()` directly.

### Add parameters to `call_stream()`

- `trace_span` (default `DummySpan()`) — pass through to `_invoke_llm_tool_call` (replacing hardcoded `DummySpan()` at line 1111)
- `cancel_event: Optional[threading.Event]` (default `None`) — check before LLM calls and between tool futures, raise `LLMInterruptedError` (3 check points, matching `call()`)
- `tool_number_offset: int` (default `0`) — initialize local variable from parameter instead of hardcoded 0 at line 973

### Add `all_tool_calls` to `call_stream()` and ANSWER_END

Add a parallel `all_tool_calls: list[dict] = []` list inside `call_stream()` using `as_tool_result_response()` format. Include it in `ANSWER_END` data as `tool_calls`. This serves streaming clients that want a complete tool call list without tracking individual `TOOL_RESULT` events.

When `process_tool_decisions()` returns events at the top of `call_stream()` (re-invocation with `tool_decisions`), those TOOL_RESULT events contain tool call data for approved/denied tools. `call_stream()` must extract tool call info from these events and append to `all_tool_calls` before entering the main loop. This ensures approved tools appear in the final ANSWER_END.

The `call()` wrapper does NOT use ANSWER_END's `tool_calls` — it accumulates directly from `TOOL_RESULT` events as they stream by (see Step 3 wrapper code). This is how the wrapper retains tool results from interrupted rounds (APPROVAL_REQUIRED exits before ANSWER_END). After Step 1b unifies the serialization format, `TOOL_RESULT` event data and `as_tool_result_response()` format are identical — both contain `tool_name` and `name` keys.

### Cost handling across approval rounds

Each conversation turn should show total cost including all approval re-invocations. The `call()` wrapper sums costs from each round's ANSWER_END. Even though interactive mode doesn't display costs today, the data should be correct for when it does.

**`LLMCosts` field aggregation semantics** (matching `_process_cost_info()` at lines 175-188):
- **Sum fields:** `total_cost`, `total_tokens`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `num_compactions`
- **Max fields:** `max_prompt_tokens_per_call`, `max_completion_tokens_per_call`
- **Optional-sum field:** `cached_tokens` — is `Optional[int]`, needs None-safe handling: `(a or 0) + (b or 0)`, return None only if both inputs are None

### `max_steps` across approval re-invocations

Each `call_stream()` invocation resets `i = 0`. This is slightly more permissive than today's single-loop behavior. Accepted — approval rarely happens, extra headroom is harmless.

## Test Coverage Assessment

### Existing test coverage

| Test file | What it covers | Method tested | Mocking level |
|---|---|---|---|
| `tests/llm/test_ask_holmes.py` | 100+ real scenarios with real LLM + real infra | `call()` via `messages_call()` | None — real LLM, real tools. Best regression safety net but slow (marked `@pytest.mark.llm`). |
| `tests/test_approval_workflow.py` | Streaming approval: APPROVAL_REQUIRED events, approve/reject/execute flows | `call_stream()` via server endpoint | Mocks LLM responses and `process_tool_decisions()`. Does NOT test actual tool re-execution after approval. |
| `tests/test_bash_session_prefix_flow.py` | Bash session prefix memory and approval workflow | `call_stream()` via server endpoint | Real `ToolCallingLLM` with mocked LLM and bash tool. |
| `tests/test_cache.py` | Token caching across multiple LLM calls | `call()` via `messages_call()` | Real LLM calls (marked `@pytest.mark.llm`). |
| `tests/test_server_endpoints.py` | Server API: non-streaming chat, images | `call()` via `messages_call()` | Mocks entire LLM response. |
| `tests/checks/test_checks_cli.py` | CLI checks in monitor/inline mode | `call()` | Mocks LLMResult return value. |
| `tests/checks/test_checks_api.py` | Health check execution via API | `call()` | Mocks LLMResult return value. |
| `tests/test_interactive.py` | Interactive slash commands and feedback | `call()` | Mocks entire `ToolCallingLLM` with `Mock(spec=ToolCallingLLM)`. |
| `tests/core/test_safeguards.py` | Repeated tool call prevention | `prevent_overly_repeated_tool_call()` only | Unit test of helper function in isolation. |

### Critical gaps

| Area | Risk if broken by refactor |
|---|---|
| Direct unit tests for `call()` loop logic | HIGH — no baseline to detect behavioral changes |
| Direct unit tests for `call_stream()` loop logic | HIGH — no baseline to detect behavioral changes |
| Approval callback flow in `call()` (`_handle_tool_call_approval`) | HIGH — completely untested, refactor replaces it entirely |
| Cancellation (`cancel_event` + `LLMInterruptedError`) | MEDIUM — only used by interactive mode |
| Context window compaction integration | MEDIUM — compaction logic runs but output never verified |
| Cost/metadata accumulation accuracy across iterations | MEDIUM — `main.py` serializes via `model_dump()`, costs must be correct |
| Tool number offset tracking across turns | LOW — cosmetic (temp file numbering) |
| Side-by-side `call()` vs `call_stream()` equivalence | CRITICAL — the entire point of this refactor |

No fast unit tests exist for the core loop mechanics. We must write targeted unit tests before refactoring to establish a baseline.

## Test Plan

### Baseline tests (Step 0 — before any refactoring)

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
- Mock tool executor's `side_effect` to set `cancel_event` synchronously when invoked (no sleep/timer — deterministic)
- Verify `LLMInterruptedError` is raised

**Test 7: Tool returning ERROR status**
- Mock LLM to call a tool on iteration 1, mock tool to return ERROR status, mock LLM to give text answer on iteration 2
- Verify `call()` continues the loop — LLM receives the error and responds
- Verify `call_stream()` yields TOOL_RESULT with error data, then ANSWER_END

**Test 8: `max_steps` boundary**
- Set `max_steps=2` on the ToolCallingLLM
- Mock LLM to always return tool calls (never a text answer)
- Verify the loop terminates after 2 iterations
- Verify the result contains the LLM's last response content

**Test 9: `response_format` passthrough**
- Mock LLM, call with `response_format={"type": "json_object"}`
- Verify the format is passed through to `litellm.completion()` call args

**Test 10: Mixed batch — successful tools retained across approval round**
- Mock LLM to request 2 tool calls in one batch: `tool_a` (succeeds) and `tool_b` (requires approval)
- Set `approval_callback` that approves `tool_b`
- Call `ai.call(messages)`
- Verify `LLMResult.tool_calls` contains **both** tools — `tool_a` from the interrupted first round AND `tool_b` from the approval re-invocation
- This tests the wrapper's TOOL_RESULT accumulation: without it, `tool_a`'s result would be lost when `APPROVAL_REQUIRED` interrupts before `ANSWER_END`

**Test 11: Denied tool preserves user feedback**
- Mock LLM to return a tool call requiring approval
- Set `approval_callback` that returns `(False, "try using namespace kube-system instead")`
- Call `ai.call(messages)`
- Verify the tool result message sent to the LLM contains `"User feedback: try using namespace kube-system instead"` (not a generic denial message)
- This tests that `ToolApprovalDecision.feedback` flows through `process_tool_decisions()` correctly

### Post-enrichment test (after Step 2)

**Test 2: Equivalence**
- Same mocked setup as Test 1
- Run both `call()` and enriched `call_stream()` with identical inputs
- Verify `call()` result fields match what you'd reconstruct from `call_stream()` ANSWER_END
- Compare: `result`, `messages` (length and structure), `tool_calls` (count), `num_llm_calls`
- Requires Step 2 enrichment of ANSWER_END

### Post-refactor test (after Step 3)

**Test 6: Approval via re-invocation**
- Mock LLM to return a tool call
- Mock tool to return `APPROVAL_REQUIRED`
- Set `approval_callback` that approves
- Call refactored `call()` (which internally uses `call_stream()` + re-invocation)
- Verify the full flow: stream yields `APPROVAL_REQUIRED`, wrapper calls callback, re-invokes with `tool_decisions`, gets `ANSWER_END`
- Verify `LLMResult` has correct `tool_calls` (including the approved tool), `messages`, costs

## Implementation Plan

### Step 0: Write baseline tests

Write Tests 1, 3, 4, 5, 7, 8, 9. Run green against current code.

### Step 1: Simplify `call_stream()` signature

Remove `system_prompt` and `user_prompt` parameters. Simplify message building to:
```python
messages: list[dict] = list(msgs) if msgs else []
```

Add new parameters:
- `trace_span` (default `DummySpan()`) — pass through to `_invoke_llm_tool_call` (replacing hardcoded `DummySpan()` at line 1111)
- `cancel_event: Optional[threading.Event]` (default `None`) — check before LLM calls and between tool futures, raise `LLMInterruptedError` (3 check points, matching `call()`)
- `tool_number_offset: int` (default `0`) — initialize local variable from parameter instead of hardcoded 0 at line 973

### Step 1b: Unify tool result serialization

**Must happen before Step 2** — Step 2 adds `all_tool_calls` accumulation using `as_tool_result_response()` format, and Step 3's wrapper collects from `TOOL_RESULT` events directly. Both require a single consistent format.

1. Modify `as_tool_result_response()` in `holmes/core/models.py` to emit both `tool_name` and `name` keys (see "Included Cleanup" section for exact code).
2. Delete `as_streaming_tool_result_response()`.
3. Replace all callers of `as_streaming_tool_result_response()` with `as_tool_result_response()`:
   - `tool_calling_llm.py` lines 342, 1142, 1157, 1166
   - `process_tool_decisions()` in `tool_calling_llm.py`
4. Update tests: `tests/test_structured_toolcall_result.py` (rename test, assert both keys present), `tests/core/test_tool_output_deduplication.py`.

### Step 2: Enrich `call_stream()` internals and ANSWER_END

1. Add `all_tool_calls: list[dict] = []` accumulation using `as_tool_result_response()` format. Append to `all_tool_calls` in the same `else` branch (line 1160-1167) where successful results are appended to `tool_calls` and `messages`. When `process_tool_decisions()` returns events at the top of `call_stream()` (re-invocation with `tool_decisions`), extract tool call info from those TOOL_RESULT events and append to `all_tool_calls` before entering the main loop. This ensures approved tools from the previous round appear in `LLMResult.tool_calls`.

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

### Step 2b: Write Test 2 (Equivalence)

Now that ANSWER_END is enriched, write Test 2 to verify `call()` result matches enriched `call_stream()` ANSWER_END. Run green.

### Step 3: Rewrite `call()` as a thin wrapper

```python
@sentry_sdk.trace
def call(self, messages, response_format=None,
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
        saw_tool_results = False  # tracks whether to log blank line after batch

        for event in stream:
            # Log blank line when a tool batch ends (transition away from TOOL_RESULT)
            if saw_tool_results and event.event != StreamEvents.TOOL_RESULT:
                logging.info("")
                saw_tool_results = False

            if event.event == StreamEvents.START_TOOL:
                start_tool_count += 1
            elif event.event == StreamEvents.TOOL_RESULT:
                tool_number_offset += 1
                saw_tool_results = True
                # Accumulate tool results as they stream by.
                # This captures ALL tools (successful + approval-required)
                # across all rounds, so LLMResult.tool_calls is complete
                # even when an APPROVAL_REQUIRED interrupts before ANSWER_END.
                all_tool_calls.append(event.data)
                if start_tool_count > 0:
                    logging.info(
                        f"The AI requested [bold]{start_tool_count}[/bold] tool call(s)."
                    )
                    start_tool_count = 0
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
                tool_results = event.data["tool_results"]
                tool_decisions = self._build_approval_decisions(pending, tool_results)
                break
            elif event.event == StreamEvents.ANSWER_END:
                answer_data = event.data

        if answer_data:
            total_num_llm_calls += answer_data.get("num_llm_calls", 0)
            # Note: all_tool_calls already accumulated from TOOL_RESULT events above.
            # ANSWER_END's tool_calls field exists for streaming clients that don't
            # track TOOL_RESULT events; the wrapper doesn't need it.
            round_costs = answer_data.get("costs", {})
            accumulated_costs = _sum_costs(accumulated_costs, round_costs)
            cost_fields = {k: v for k, v in accumulated_costs.items() if k in LLMCosts.model_fields}
            return LLMResult(
                result=answer_data["content"],
                tool_calls=all_tool_calls,
                num_llm_calls=total_num_llm_calls,
                prompt=answer_data.get("prompt"),
                messages=answer_data["messages"],
                metadata=answer_data.get("metadata"),
                **cost_fields,
            )

        if not tool_decisions:
            raise Exception("Stream ended without ANSWER_END or APPROVAL_REQUIRED")
```

### Step 3b: Helper methods

**`_build_approval_decisions()`**: For each pending approval:
1. Re-check if approval is still needed via `_is_tool_call_already_approved()` (a previous tool in the same batch may have just saved the prefix to disk). If already approved, auto-approve without prompting.
2. Otherwise, call `self.approval_callback(tool_result)` with the full `StructuredToolResult` from the event data.
3. Return list of `ToolApprovalDecision`. Does NOT populate `save_prefixes` — CLI saves to disk via callback; server gets `save_prefixes` from client directly. **Does** populate `feedback` from the callback's second return value when denied — this preserves the user's corrective guidance for the LLM (see below).

**Preserve user denial feedback in approval decisions:**

Currently, when a user denies a tool and provides corrective guidance (e.g., "try a different namespace"), `_handle_tool_call_approval()` (line 929) includes that feedback in the error message sent to the LLM: `"User denied command execution. User feedback: try a different namespace"`. However, `process_tool_decisions()` (line 335) uses a hardcoded generic `"Tool execution was denied by the user."` — the feedback is lost because `ToolApprovalDecision` has no field for it.

Fix: Add `feedback: Optional[str] = None` to `ToolApprovalDecision` in `models.py`. Update `process_tool_decisions()` denial branch (line 328-337) to include feedback when present:
```python
feedback_text = f" User feedback: {decision.feedback}" if decision and decision.feedback else ""
error=f"Tool execution was denied by the user.{feedback_text}"
```

This is a pre-existing bug in the server path (server clients could never send feedback on denial), but becomes critical for the CLI path once `call()` routes through `process_tool_decisions()`.

**`_sum_costs()`**: Module-level function.
```python
def _sum_costs(a: dict, b: dict) -> dict:
    """Sum two cost dicts across approval rounds."""
    SUM_FIELDS = ["total_cost", "total_tokens", "prompt_tokens", "completion_tokens", "reasoning_tokens", "num_compactions"]
    MAX_FIELDS = ["max_prompt_tokens_per_call", "max_completion_tokens_per_call"]
    result = {}
    for f in SUM_FIELDS:
        result[f] = a.get(f, 0) + b.get(f, 0)
    for f in MAX_FIELDS:
        result[f] = max(a.get(f, 0), b.get(f, 0))
    a_c, b_c = a.get("cached_tokens"), b.get("cached_tokens")
    result["cached_tokens"] = (a_c or 0) + (b_c or 0) if a_c is not None or b_c is not None else None
    return result
```

**Cost dict safety:** Filter to known fields before unpacking into `LLMResult`:
```python
cost_fields = {k: v for k, v in accumulated_costs.items() if k in LLMCosts.model_fields}
```

### Step 4: Remove `messages_call()`, update callers

Delete `messages_call()`. Update its 4 callers to call `call()` directly:
- `server.py:401` → `ai.call(messages=messages, ...)`
- `tests/test_cache.py` → update call site
- `tests/test_server_endpoints.py` → update call site
- `tests/llm/test_ask_holmes.py:253` → `ai.call(messages=messages, trace_span=llm_span)`

### Step 5: Delete dead code

- Remove old `call()` loop body (~220 lines). The method stays but becomes ~60 lines.
- Remove `_handle_tool_call_approval()` — no longer called.
- Remove `messages_call()` — 4 callers updated in Step 4.
- Remove `user_prompt` parameter from `call()`. Update `prompt_call()` to stop passing it.
- `process_tool_decisions()` stays — still called by `call_stream()` on re-invocation.

### Step 5b: Write Tests 6, 10, 11 (post-refactor approval edge cases)

Write Tests 6, 10, 11. Run green.

### Step 6: Run all tests

```bash
poetry run pytest tests -m "not llm" --no-cov
```

## Files to Modify

| File | Change |
|---|---|
| `holmes/core/tool_calling_llm.py` | Main refactor: simplify `call_stream()` signature, add params, enrich ANSWER_END + APPROVAL_REQUIRED, `call()` becomes wrapper, add `_build_approval_decisions()` + `_sum_costs()`, delete old loop + `_handle_tool_call_approval()` + `messages_call()`, remove dead `user_prompt` param. Update `as_streaming_tool_result_response()` → `as_tool_result_response()` at 4 call sites. Fix `process_tool_decisions()` denial branch to include user feedback from `ToolApprovalDecision.feedback`. |
| `holmes/core/models.py` | Unify `as_tool_result_response()` to emit both `tool_name` and `name` keys. Delete `as_streaming_tool_result_response()`. Add `feedback: Optional[str] = None` to `ToolApprovalDecision`. |
| `tests/test_tool_calling_llm_baseline.py` (NEW) | All tests (1-9) |
| `tests/test_structured_toolcall_result.py` | Update `test_as_streaming_tool_result_response` → test unified method, assert both keys |
| `tests/core/test_tool_output_deduplication.py` | Replace `as_streaming_tool_result_response()` with `as_tool_result_response()` |
| `server.py` | Replace `ai.messages_call(...)` with `ai.call(...)` |
| `tests/test_cache.py` | Replace `messages_call()` with `call()` |
| `tests/test_server_endpoints.py` | Replace `messages_call()` with `call()` |
| `tests/llm/test_ask_holmes.py` | Replace `messages_call()` with `call()` |
| `docs/reference/python-sdk.md` | Remove `messages_call` row from API reference table (line 287) |

No changes needed to: `main.py`, `interactive.py`, `checks.py`, `experimental/ag-ui/server-agui.py`, `holmes/utils/stream.py`, `holmes/core/tools.py`.

Note: `prompt_call()` is inside `tool_calling_llm.py` — its `user_prompt=user_prompt` kwarg is removed as part of the main file change.

## Risks

1. **Approval round-trip overhead**: Each approval creates a new generator. Context window limiting and tool re-fetch run again. Acceptable — approval is rare and these are cheap vs LLM calls.

2. **`max_steps` across approval re-invocations**: Each `call_stream()` invocation resets `i = 0`. Slightly more permissive than today's single-loop behavior. Accepted — approval is rare, extra headroom is harmless.

3. **`_build_approval_decisions()` must pass correct data to callback**: The interactive callback reads `tool_result.invocation` and `tool_result.params.suggested_prefixes`. The enriched `APPROVAL_REQUIRED` event must carry the full `StructuredToolResult` with these fields populated. This data is available inside `call_stream()` at the point where `APPROVAL_REQUIRED` status is detected — it's the `tool_call_result.result` object.

4. **`_runbook_in_use` thread safety** (pre-existing): `CheckRunner` shares one `ToolCallingLLM` across threads. The `_runbook_in_use` flag is a latent race condition. Not introduced by this refactor, not triggered today (checks don't use runbooks).

5. **`_is_tool_call_already_approved` must work inside `_build_approval_decisions()`**: The re-check reads the bash toolset's current allow list. For CLI, this means re-reading the disk file. For this to catch a prefix saved by a callback earlier in the same batch, the disk write must be flushed before the re-check reads. This is the case — `save_cli_bash_tools_approved_prefixes()` uses `open()/write()/close()` synchronously.

## Included Cleanup: Unify `as_tool_result_response()` / `as_streaming_tool_result_response()`

### Problem

`ToolCallResult` (`holmes/core/models.py:22-64`) has two nearly identical serialization methods that differ only in the key used for the tool name:

```python
# as_tool_result_response() — uses "tool_name"
{"tool_call_id": ..., "tool_name": ..., "description": ..., "role": "tool", "result": ...}

# as_streaming_tool_result_response() — uses "name"
{"tool_call_id": ..., "name": ..., "description": ..., "role": "tool", "result": ...}
```

This appears to be an accidental divergence, not intentional. Clients use both keys.

### Solution

Delete `as_streaming_tool_result_response()`. Modify `as_tool_result_response()` to emit **both** keys:

```python
def as_tool_result_response(self):
    result_dump = self.result.model_dump()
    result_dump["data"] = self.result.get_stringified_data()

    return {
        "tool_call_id": self.tool_call_id,
        "tool_name": self.tool_name,
        "name": self.tool_name,  # backwards compat: streaming consumers read "name"
        "description": self.description,
        "role": "tool",
        "result": result_dump,
    }
```

Replace all callers of `as_streaming_tool_result_response()` with `as_tool_result_response()`.

### Rationale

- **Backwards compatible**: Clients reading `tool_name` (non-streaming path) still work. Clients reading `name` (streaming path) still work. No client breaks.
- **Single source of truth**: One method, one format, one place to maintain.
- **Intentional redundancy**: Both keys carry the same value. The duplicate key is a deliberate migration bridge — documented with the comment `# backwards compat: streaming consumers read "name"`. This is better than the status quo of two separate functions where nobody realizes they produce different output.
- **Callers to update**: `tool_calling_llm.py` (3 sites: lines 342, 1142, 1157, 1166), `process_tool_decisions()`, `tests/test_structured_toolcall_result.py`, `tests/core/test_tool_output_deduplication.py`.

### When to drop the duplicate key

Use the investigation prompt below on client repositories. Once all clients are updated to read a single key, remove the other from `as_tool_result_response()`.

**Investigation prompt for client repositories:**
```
Search this codebase for how it handles tool call results from the HolmesGPT API.
Specifically:

1. Find code that parses the `tool_calls` array from non-streaming ChatResponse
   (the /api/chat endpoint). Look for access to the key "tool_name" on each tool
   call result object.

2. Find code that parses SSE TOOL_RESULT events from the streaming /api/chat
   endpoint. Look for access to the key "name" on the event data.

3. List every place these keys are read, with file paths and line numbers.

4. Are both "tool_name" and "name" used, or only one? Could they be unified to
   a single key without breaking anything?

Context: HolmesGPT has unified its two serialization methods into one that emits
both "tool_name" and "name" keys with the same value. We want to eventually drop
one key. Which key(s) does this client actually depend on?
```
