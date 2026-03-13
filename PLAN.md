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

## Implementation Plan

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

### Step 3: Add approval callback support inside `call_stream()`

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

    while True:
        stream = self.call_stream(
            msgs=messages if tool_decisions else messages,  # first call vs resumption
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
                # Collect tool calls for LLMResult
                pass  # extract from event.data
            elif event.event == StreamEvents.APPROVAL_REQUIRED:
                # Invoke self.approval_callback for each pending tool
                # Build tool_decisions list
                # Save messages from event.data
                # Break to re-invoke call_stream
                pass
            elif event.event == StreamEvents.ANSWER_END:
                answer_data = event.data
            # Other events (TOKEN_COUNT, AI_MESSAGE, etc.) — log or discard

        if answer_data:
            return LLMResult(
                result=answer_data["content"],
                tool_calls=answer_data["tool_calls"],
                num_llm_calls=answer_data["num_llm_calls"],
                prompt=answer_data["prompt"],
                messages=answer_data["messages"],
                metadata=answer_data["metadata"],
                **answer_data["costs"],
            )

        # If we got APPROVAL_REQUIRED and built tool_decisions, loop continues
        if not tool_decisions:
            raise Exception("Stream ended without ANSWER_END or APPROVAL_REQUIRED")
```

### Step 5: Simplify `prompt_call()` and `messages_call()`

These already delegate to `call()` — they should continue to work unchanged. Verify parameter passing is correct.

### Step 6: Handle logging in `call()` wrapper

`call()` currently logs intermediate text and reasoning to rich console. Two options:
- **Option A**: Have `call()` wrapper intercept `AI_MESSAGE` events and log them (preserves current CLI behavior).
- **Option B**: Let `call_stream()` always log, not just yield.

**Decision**: Option A — `call()` wrapper logs `AI_MESSAGE` content. `call_stream()` only yields. This keeps `call_stream()` pure (no side effects beyond tool execution).

### Step 7: Update callers (if needed)

- **`holmes/main.py`**: No change — calls `call(messages, trace_span=...)`, still works.
- **`holmes/interactive.py`**: No change — calls `call(messages, trace_span=..., tool_number_offset=..., cancel_event=...)`, still works.
- **`holmes/checks/checks.py`**: No change — calls `call(messages, response_format=...)`, still works.
- **`server.py` non-streaming**: No change — calls `messages_call()` which calls `call()`.
- **`server.py` streaming**: No change — calls `call_stream()` directly, existing params still work.
- **`experimental/ag-ui/server-agui.py`**: No change — calls `call_stream()` directly.

### Step 8: Delete dead code

Remove the old `call()` loop body (~220 lines). The method stays but becomes ~40 lines.

### Step 9: Run tests

```bash
poetry run pytest tests -m "not llm" --no-cov
```

## Files to Modify

| File | Change |
|---|---|
| `holmes/core/tool_calling_llm.py` | Main refactor — `call_stream()` gets new params, `call()` becomes wrapper |
| (No other files should need changes) | Callers' interfaces are preserved |

## Risks

1. **Approval round-trip overhead**: Each approval creates a new generator. Context window limiting and tool re-fetch run again. Acceptable — approval is rare and these operations are cheap compared to LLM calls.

2. **Behavioral subtlety with `tool_number_offset`**: When `call()` re-invokes after approval, the offset needs to account for tools already executed. Must track this across rounds.

3. **Cost accumulation across rounds**: If `call()` re-invokes `call_stream()` after approval, costs from the first invocation are in the first stream's events. The wrapper must accumulate costs across all rounds.

4. **Logging parity**: Current `call()` logs tool counts, intermediate text, etc. via `logging.info` with rich markup. The wrapper must replicate this from stream events to avoid silent regression in CLI output.

5. **`cancel_event` in generator**: If cancellation fires mid-yield, the generator raises `LLMInterruptedError`. The `call()` wrapper must propagate this. Should work naturally since the exception propagates through the `for event in stream` loop.
