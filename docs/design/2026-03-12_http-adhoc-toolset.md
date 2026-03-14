# HTTP Ad-Hoc Toolset

## Intent

HolmesGPT can query HTTP APIs through the configured HTTP toolset, but that requires users to pre-configure endpoints, auth, and path whitelists in YAML before Holmes can make any request. This creates a chicken-and-egg problem: users need to know the API well enough to write the config, but they often want Holmes to help them explore the API in the first place.

The HTTP ad-hoc toolset is a generic HTTP tool that works without pre-configuration. The LLM proposes which hosts, paths, and methods it needs; the user approves at runtime; and Holmes makes the requests. This serves two use cases:

1. **Exploration** — testing an API to later generate a configured HTTP toolset
2. **On-demand access** — hitting an API at runtime when no configured toolset exists for it

The tool name is `http_adhoc_request`. It is a separate tool from configured HTTP toolsets (e.g., `dagster_request`, `pagerduty_request`) and can coexist with them.

## How It Works

### Tool Interface

The tool exposes the same core parameters as the configured `HttpRequest` tool, plus a `suggested_endpoint` parameter for the approval mechanism:

```
Tool name: http_adhoc_request

Parameters:
  url:                string (required)  — Full URL to request
  method:             string (optional)  — HTTP method, default GET
  body:               string (optional)  — Request body (JSON string)
  headers:            string (optional)  — Additional headers as JSON object
  suggested_endpoint: object (required)  — Endpoint approval request
    host:             string             — Hostname to access
    path_patterns:    array[string]      — Glob patterns for paths
    methods:          array[string]      — HTTP methods needed
  max_depth:          integer (optional) — Truncate nested JSON (from JsonFilterMixin)
  jq:                 string (optional)  — jq filter expression (from JsonFilterMixin)
```

### First Request to a New Endpoint

The LLM proposes the full set of paths it expects to need:

```json
{
  "url": "https://api.pagerduty.com/incidents?limit=5",
  "method": "GET",
  "headers": "{\"Authorization\": \"Token token=u+abcdef123\"}",
  "suggested_endpoint": {
    "host": "api.pagerduty.com",
    "path_patterns": ["/incidents/*", "/services/*", "/users/*", "/log_entries/*"],
    "methods": ["GET"]
  }
}
```

### Approval Flow

The system checks `suggested_endpoint` against session-approved endpoints. If the host + paths + methods combination is not yet approved, the tool returns `APPROVAL_REQUIRED`:

```
⚠️ HTTP request to new endpoint:
  Host: api.pagerduty.com
  Paths: /incidents/*, /services/*, /users/*, /log_entries/*
  Methods: GET

Do you want to proceed?
  1. Yes
  2. No, and tell Holmes what to do differently
```

**Approve once = approve all matching.** There is no "one-time" vs "remember" distinction. When the user approves, the endpoint (host + paths + methods) is remembered for the rest of the session. All subsequent requests matching any of those path patterns on that host with GET are auto-approved.

This is simpler than the bash toolset's three-option menu (Yes / Yes+remember / No) because the approval unit here is always explicit and structured — the user sees exactly what host, paths, and methods they're approving. There's no ambiguity about scope, so there's no reason to offer a one-time-only option.

This works because the approval decision is **entirely tool-internal**. The `requires_approval(params, context)` method runs before every `_invoke()`. It extracts session-approved endpoints from the conversation history in `context.messages`, checks whether the current request matches, and returns `None` (no approval needed) or `ApprovalRequirement`. No changes to the base `Tool.invoke()` flow or the broader approval framework are needed.

### Incremental Path Discovery

Later, the LLM discovers it needs an endpoint it didn't anticipate:

```json
{
  "url": "https://api.pagerduty.com/incidents/P123/notes",
  "method": "POST",
  "body": "{\"note\": {\"content\": \"Investigating...\"}}",
  "suggested_endpoint": {
    "host": "api.pagerduty.com",
    "path_patterns": ["/incidents/*/notes"],
    "methods": ["POST"]
  }
}
```

This is a new method (POST) and a new path pattern. Approval is requested again. The user sees exactly what's being added. Auth for the host is already known from the headers the LLM has been using.

### Auth Handling

**Phase 1: LLM-controlled headers.** The LLM includes auth in the `headers` parameter. This means the LLM sees the API key (provided by the user in the conversation or read from an environment variable). This is the simplest approach and is acceptable for interactive exploration/usage.

The LLM instructions tell it to ask the user for credentials if not already provided, and to include them in the `headers` parameter on every request.

### Remember Rule

The session-approved unit is: **host + method + path pattern**.

When the user approves, the `suggested_endpoint` is stored in the tool result message metadata. Future requests are checked with:

1. Does the URL's hostname match an approved endpoint's `host`?
2. Does the URL's path match any of the approved endpoint's `path_patterns` (via `fnmatch`)?
3. Is the request method in the approved endpoint's `methods`?

All three must match for auto-approval. This is the same matching logic used by the configured HTTP toolset's `EndpointConfig`.

### LLM Instructions

The toolset provides instructions that tell the LLM:

- On first request to a new host, suggest all path patterns you expect to need in `suggested_endpoint` to minimize repeated approval prompts
- Prefer GET unless the API requires POST (e.g., GraphQL, search endpoints)
- If a configured toolset exists for a host (e.g., `dagster_request`), prefer that over `http_adhoc_request`
- Include authentication in the `headers` parameter — ask the user for credentials if not provided
- Use `jq` and `max_depth` to keep responses manageable

## Approval Protocol

### How Tool Approval Works After Refactoring

> **Context:** The `call()` / `call_stream()` refactoring (branch `claude/refactor-tool-calling-streaming-thzaD`) unified the two independent agentic loops. `call_stream()` is now the single source of truth; `call()` is a thin wrapper that drains the stream. This eliminated `_handle_tool_call_approval()` and `messages_call()`. Both CLI and server now follow the same event-driven approval path.

**Single approval path (both CLI and server):**
1. Tool returns `APPROVAL_REQUIRED` status during tool execution inside `call_stream()`
2. `call_stream()` emits `APPROVAL_REQUIRED` event with `PendingToolApproval` data + `tool_results` map (full `StructuredToolResult` objects, keyed by `tool_call_id`)
3. Stream **stops**

**Server path:** Client makes a new POST request with `ToolApprovalDecision` list.

**CLI path:** `call()` wrapper catches the `APPROVAL_REQUIRED` event, calls `_build_approval_decisions()` which invokes `self.approval_callback(tool_result)` for each pending tool, then re-invokes `call_stream()` with the resulting `tool_decisions`. This is transparent — the `while True` loop in `call()` handles the round-trip.

Both paths converge on `process_tool_decisions()` which executes approved tools and inserts results into the message history.

**`ToolApprovalDecision`** now has a `feedback` field:
```python
class ToolApprovalDecision(BaseModel):
    tool_call_id: str
    approved: bool
    save_prefixes: Optional[List[str]] = None
    feedback: Optional[str] = None  # User feedback when denying
```

### What's Still Bash-Specific in the Framework

Despite the refactoring, the framework (`tool_calling_llm.py`) still has bash-specific code at these sites:

1. **`extract_bash_session_prefixes(messages)`** — called at line 326 in `process_tool_decisions()` and line 964 in `call_stream()`'s main loop
2. **`session_approved_prefixes` parameter threading** — passed through 4 layers: `call_stream()` → `_invoke_llm_tool_call()` → `_get_tool_call_result()` → `_directly_invoke_tool_call()` → `ToolInvokeContext`
3. **`save_prefixes` storage** — `process_tool_decisions()` line 365-371 checks `decision.save_prefixes` and writes `bash_session_approved_prefixes` into message metadata

Adding HTTP ad-hoc would mean duplicating all of this. The solution remains the same as originally planned: give tools access to conversation history and let them manage their own session state.

### How Non-Bash Approval Works Today (MCP, Remediation)

Any toolset can mark tools as requiring approval via config:

```yaml
toolsets:
  my-mcp-server:
    approval_required_tools: ["restart_*", "delete_*"]
```

This triggers `_check_approval_config()` which returns `ApprovalRequirement(needs_approval=True, prefixes_to_save=None)`. The approval flow works, but:

- **No session memory.** `prefixes_to_save` is `None`, so even if the user approves, nothing is saved. Every call to the same tool requires fresh approval.
- **CLI UI is bash-specific.** The "remember" option shows `<command>` as the prefix display, which is meaningless for non-bash tools.
- **Server protocol works** but `save_prefixes` has no equivalent for non-bash tools, so the client has nothing to send back for "remember."

In short: non-bash tools get one-time approval only. There is no session persistence mechanism for them.

### Protocol Changes for HTTP Ad-Hoc

**No changes to the approval protocol.** The tool handles everything internally.

**`PendingToolApproval`** — no changes. It already carries `tool_name` and `params`, which is tool-agnostic. For `http_adhoc_request`, `params` contains `url`, `method`, `suggested_endpoint`, etc. The client renders the approval UI based on `tool_name`.

**`ToolApprovalDecision`** — no changes needed for HTTP ad-hoc. The `feedback` field (added by the refactoring) already covers denial feedback.

**`ApprovalRequirement`** — no changes. Stays as `needs_approval: bool` + `reason: str`. Tool-specific save data (prefixes, endpoints) is **not** on this model — each tool manages its own session persistence.

### Decoupling Session State from the Framework

**Solution: give tools access to conversation history and let them manage their own session state.**

**`ToolInvokeContext`** — replace tool-specific fields with the conversation messages:

```python
class ToolInvokeContext(BaseModel):
    # ...existing fields...
    messages: List[Dict[str, Any]] = []  # NEW: full conversation history
    # session_approved_prefixes removed — bash tool extracts from messages itself
```

Each tool extracts its own session data from `context.messages`:
- Bash tool calls `extract_bash_session_prefixes(context.messages)` internally
- HTTP ad-hoc tool calls its own `extract_http_session_endpoints(context.messages)` internally
- Future approval-aware tools do the same — zero framework changes needed

**`StructuredToolResult`** — add an opaque metadata field for tools to pass session data back:

```python
class StructuredToolResult(BaseModel):
    # ...existing fields...
    session_metadata: Optional[Dict[str, Any]] = None  # NEW: tool-controlled session data
```

When the HTTP ad-hoc tool executes after approval, it sets:
```python
result.session_metadata = {
    "http_session_approved_endpoints": [{"host": "...", "path_patterns": [...], "methods": [...]}]
}
```

The framework stores this in message metadata **without understanding it** — just passes it through to `tool_call_metadata` in `format_tool_result_data()`. On future calls, the tool reads it back from `context.messages`.

This makes the framework fully tool-agnostic. The bash tool can be refactored to use `session_metadata` too (returning `{"bash_session_approved_prefixes": [...]}`) instead of relying on the client to send `save_prefixes`. But this refactor is optional — the existing bash flow continues to work alongside the new pattern.

**`ApprovalRequirement`** — remove `prefixes_to_save`. The bash tool can handle the `suggested_prefixes` param rewriting in its own `requires_approval()` method instead of relying on the base class `invoke()` to do it. `ApprovalRequirement` becomes:

```python
class ApprovalRequirement(BaseModel):
    needs_approval: bool
    reason: str = ""
```

Clean, tool-agnostic.

### How Session State Flows (Server Path)

```
1. LLM calls http_adhoc_request(url=..., suggested_endpoint=...)

2. Tool.invoke() calls requires_approval(params, context)
   → Tool scans context.messages for http_session_approved_endpoints
   → Not found → returns ApprovalRequirement(needs_approval=True)

3. Framework returns APPROVAL_REQUIRED to client (no tool-specific logic)

4. Client approves → framework calls _invoke() with user_approved=True

5. Tool executes HTTP request, returns StructuredToolResult with:
   session_metadata={"http_session_approved_endpoints": [{...}]}

6. Framework stores session_metadata in tool_call_metadata (opaque pass-through)

7. Next tool call: Tool scans context.messages, finds its own metadata → auto-approves
```

### How Session State Flows (CLI Path)

Same as server. The `call()` wrapper's `while True` loop handles the `APPROVAL_REQUIRED` → `_build_approval_decisions()` → re-invoke `call_stream(tool_decisions=...)` round-trip automatically. The tool can additionally persist to a file (`~/.holmes/http_approved_endpoints.yaml`) for cross-session memory. This is done inside the tool — the framework doesn't know about it. Same pattern as bash's `cli_prefixes.py`.

### Existing Client Compatibility

**No client changes required.** The protocol is unchanged. The only difference is internal: session state management moves from the framework into the tools.

The client can optionally render the approval prompt differently for `http_adhoc_request` vs `bash` by inspecting `tool_name` and `params` in `PendingToolApproval`. But this is a presentation concern, not a protocol change.

## Decisions Required

### Decision 1: Bash session_metadata migration — now, later, or never?

The plan proposes `session_metadata` on `StructuredToolResult` as the tool-agnostic way for tools to persist session state into messages. The HTTP ad-hoc tool will use this from day one. The question is what to do with the **existing** bash session state mechanism.

**Option A: Migrate bash to session_metadata now (as part of this PR)**
- Remove `extract_bash_session_prefixes()` from framework, move into bash tool
- Remove `save_prefixes` handling from `process_tool_decisions()` — bash tool writes its own `session_metadata`
- Remove `session_approved_prefixes` from `ToolInvokeContext` — bash reads from `context.messages`
- Remove `prefixes_to_save` from `ApprovalRequirement` — bash handles param rewriting in `requires_approval()`
- **Pro:** Clean cut. Framework becomes fully tool-agnostic in one PR. No two parallel mechanisms.
- **Con:** Larger PR. Touches bash tests, server tests, approval workflow tests. Risk of breaking the existing bash approval flow. The `save_prefixes` field on `ToolApprovalDecision` is part of the server API — clients may send it. Need to keep accepting it but ignore it (or deprecate).

**Option B: Leave bash alone, add session_metadata alongside it**
- Add `messages` to `ToolInvokeContext` (bash ignores it, still reads `session_approved_prefixes`)
- Add `session_metadata` to `StructuredToolResult` (HTTP ad-hoc uses it, bash doesn't)
- Framework passes through `session_metadata` opaquely AND still does the bash-specific extraction/storage
- **Pro:** Smaller PR. No risk to existing bash flow. HTTP ad-hoc works independently.
- **Con:** Two parallel session mechanisms. Framework stays bash-aware. `ToolInvokeContext` has both `messages` and `session_approved_prefixes`. Confusing for future contributors.

**Option C: Migrate bash in a separate follow-up PR**
- This PR: add `messages` + `session_metadata`, build HTTP ad-hoc tool using them
- Next PR: migrate bash to use `messages` + `session_metadata`, remove framework bash code
- **Pro:** Each PR is focused. HTTP ad-hoc doesn't depend on bash migration succeeding.
- **Con:** Temporary state with two mechanisms. But it's explicitly temporary with a clear follow-up.

### Decision 2: What happens to `save_prefixes` on `ToolApprovalDecision`?

The server API currently accepts `save_prefixes` from clients. If bash migrates to `session_metadata`, clients sending `save_prefixes` would have no effect.

**Option A: Keep accepting, ignore silently**
- `process_tool_decisions()` stops writing `bash_session_approved_prefixes` to metadata from `save_prefixes`
- Old clients still work — approval succeeds, session memory works via `session_metadata` from the tool
- **Pro:** No breaking change.
- **Con:** Silent behavior change — client thinks it's saving prefixes but the tool is doing it.

**Option B: Keep accepting, tool reads it from decision**
- Pass the full `ToolApprovalDecision` through to the tool (on context or as param) so the bash tool can read `save_prefixes` directly
- **Pro:** Preserves exact client behavior.
- **Con:** Tools shouldn't need to know about `ToolApprovalDecision`.

**Option C: Deprecate — accept but log warning**
- Log a deprecation warning when `save_prefixes` is received
- Tool manages its own session state via `session_metadata`
- **Pro:** Signals to clients that this field is going away.
- **Con:** Adds noise if clients don't update quickly.

**Recommendation:** Option A (keep accepting, ignore silently). The `session_metadata` mechanism replaces it cleanly. Clients don't need to change — session memory works either way.

*Only relevant if Decision 1 is A or C. If Decision 1 is B, `save_prefixes` continues to work as-is.*

### Decision 3: CLI approval UI — generic or tool-specific rendering?

The `handle_tool_approval()` function in `interactive.py` currently shows a bash-specific prompt. With HTTP ad-hoc, it needs to handle a second tool type.

**Option A: Tool provides its own approval display text**
- Add a method like `get_approval_display(params) -> str` to `Tool` base class
- `handle_tool_approval()` calls it and renders the string
- Each tool controls its own presentation
- **Pro:** Fully extensible. Adding a new approval-aware tool requires zero changes to `interactive.py`.
- **Con:** Couples display logic to tool classes. Bash tool needs to implement it too.

**Option B: Switch on tool_name in handle_tool_approval**
- `if tool_name == "bash": ...bash UI... elif tool_name == "http_adhoc_request": ...http UI...`
- **Pro:** Simple, explicit. Two tools = two branches.
- **Con:** Doesn't scale. But we only have two tools.

**Option C: Generic display from params**
- Show tool name + all params as a formatted dict. No tool-specific rendering.
- **Pro:** Zero per-tool work.
- **Con:** Ugly for users. `suggested_endpoint` as raw JSON is hard to read.

**Recommendation:** Option A. The refactoring made `_build_approval_decisions()` generic — it calls `self.approval_callback(tool_result)` regardless of tool type. The callback receives a `StructuredToolResult` which has `invocation` and `params`. The tool can provide a formatted string via a method, keeping `interactive.py` tool-agnostic.

### Decision 4: Should `_build_approval_decisions` pass messages to the re-check?

Currently `_build_approval_decisions()` (line 510-552) re-checks approval via `_is_tool_call_already_approved()`. This method reads the bash toolset's allow list from disk. After the migration, the re-check would need to scan `messages` for `session_metadata` instead.

**Option A: Pass messages to _is_tool_call_already_approved**
- The method checks both disk state and message-based session state
- **Pro:** Correct for both bash (disk) and HTTP ad-hoc (messages).
- **Con:** `_is_tool_call_already_approved` gets more complex.

**Option B: Remove the re-check, let process_tool_decisions handle it**
- `_build_approval_decisions` always prompts. `process_tool_decisions` invokes with `user_approved=True`, tool's own `_invoke` can skip the request if it wants.
- **Pro:** Simpler. The re-check is an optimization for batch approval (tool A approves a prefix that tool B also needs).
- **Con:** Extra approval prompts in batch scenarios. Rare but annoying.

**Option C: Let the tool itself decide during _build_approval_decisions**
- Call `tool.requires_approval(params, context_with_messages)` again in the re-check
- The tool knows its own session state best
- **Pro:** Clean separation. Framework delegates to tool.
- **Con:** Need to construct a `ToolInvokeContext` with current messages. More plumbing.

**Recommendation:** Option C aligns best with the "tools manage their own state" principle. But Option A is simpler for now if we want to ship faster.

## Alternatives Considered

### 1. Same tool name as configured HTTP toolset

**Idea:** Use `http_request` for both configured and ad-hoc, and route based on whether the host matches a configured endpoint.

**Why not:** Adds routing complexity. If someone names their configured toolset `http`, the tool names collide. Two separate tools with clear names (`dagster_request` for configured, `http_adhoc_request` for ad-hoc) is simpler and lets the LLM instructions explain when to use which.

### 2. Host-only approval (no paths)

**Idea:** Approve at the host level — "allow all requests to api.pagerduty.com with GET." Simpler, fewer approval prompts.

**Why not:** Insufficient control. Some APIs have destructive paths alongside read-only ones on the same host. Path-level approval lets users constrain what Holmes can access. The LLM mitigates the UX cost by suggesting all needed paths upfront in a single batch.

### 3. Auth provided at approval time

**Idea:** Extend the approval prompt to collect credentials (API key, token) when the user approves a new host. The toolset manages auth automatically, and the LLM never sees credentials.

**Why not for phase 1:** Requires extending the approval protocol with an `auth` field, and both CLI and server UI need new input flows. The LLM-controlled headers approach works now with zero protocol changes. Can be added as a phase 2 improvement.

### 4. No session memory (approve every call)

**Idea:** Same as how MCP remediation tools work today — every call requires fresh approval.

**Why not:** HTTP exploration involves many requests (10+). Approving each one individually makes the tool unusable. The bash toolset's session memory pattern exists precisely because per-call approval doesn't scale. The same applies here.

### 5. Enabled only via opt-in flag

**Idea:** `--enable-http-adhoc` or config opt-in.

**Why not:** The tool does nothing without user approval anyway. Having it available by default just means the LLM can offer to use it when relevant. If the user never approves a request, it's as if the tool doesn't exist. The approval mechanism is the gating — no need for a separate opt-in.

### 6. Tool named `http_explore_request`

**Idea:** Name reflects the exploration use case.

**Why not:** The tool isn't just for exploration. It's for any ad-hoc HTTP access — runtime queries, one-off API calls, testing. "Explore" undersells the capability. "Ad-hoc" accurately describes the approval model: on-demand, not pre-configured.

### 7. Tool-specific fields on ToolInvokeContext and ApprovalRequirement

**Idea:** Add `session_approved_endpoints` to `ToolInvokeContext` and `endpoints_to_save` to `ApprovalRequirement`, mirroring how bash uses `session_approved_prefixes` and `prefixes_to_save`.

**Why not:** This makes the framework aware of every approval-aware tool's session format. The framework currently has bash-specific extraction (`extract_bash_session_prefixes`), threading (`session_approved_prefixes` parameter through 4-5 call layers), and storage (`save_prefixes` → `bash_session_approved_prefixes` in metadata) — all in `tool_calling_llm.py`. Adding HTTP ad-hoc would duplicate all of this. Every future approval-aware tool would need more framework changes.

Instead, pass the conversation `messages` on `ToolInvokeContext` and let each tool extract its own session data. Add `session_metadata` on `StructuredToolResult` for tools to pass opaque session data back. The framework passes it through without understanding it. This is a small refactor of the bash tool but eliminates all tool-specific code from the framework.

### 8. "Yes (one-time)" vs "Yes, and remember" distinction

**Idea:** Like the bash toolset, offer both a one-time approval and a persistent approval option.

**Why not:** The bash toolset needs this because the approval granularity is ambiguous — a prefix like `kubectl get` covers many commands, and users may want to approve one specific command without blanket-approving the prefix. For HTTP ad-hoc, the user sees the exact host, path patterns, and methods they're approving. The scope is explicit and structured, so there's no ambiguity. A simpler approve/deny is sufficient, and the approval always persists for the session. This also avoids needing to extend `ToolApprovalDecision` with a `remember` flag or change the client.

## Implementation Plan

### Phase 1: Framework Changes (Tool-Agnostic)

These changes decouple session state from the framework. They're needed regardless of decisions above.

1. **`holmes/core/tools.py`**
   - `ToolInvokeContext`: add `messages: List[Dict[str, Any]] = []` (conversation history)
   - `StructuredToolResult`: add `session_metadata: Optional[Dict[str, Any]] = None`
   - `ApprovalRequirement`: remove `prefixes_to_save` (Decision 1A/C only; keep if 1B)
   - `Tool.invoke()`: remove `prefixes_to_save` handling at lines 293-295 (Decision 1A/C only). Bash tool handles param rewriting in its own `requires_approval()`.

2. **`holmes/core/models.py`**
   - `format_tool_result_data()`: pass through `session_metadata` from `StructuredToolResult` into `tool_call_metadata`

3. **`holmes/core/tool_calling_llm.py`** — pass `messages` to `ToolInvokeContext`
   - `_directly_invoke_tool_call()` (line 585): add `messages` parameter, pass to `ToolInvokeContext`
   - `_get_tool_call_result()` (line 617): thread `messages` through
   - `_invoke_llm_tool_call()` (line 714): thread `messages` through
   - `call_stream()` main loop (line 966): pass `messages` to `_invoke_llm_tool_call()`
   - `process_tool_decisions()` (line 334): pass `messages` to `_invoke_llm_tool_call()`

   **If Decision 1A:** Also remove `extract_bash_session_prefixes` calls (lines 326, 964), remove `session_approved_prefixes` parameter from the 4-layer chain, remove `save_prefixes` handling in `process_tool_decisions` (lines 363-371).

   **If Decision 1B/C:** Keep existing bash code, add `messages` alongside `session_approved_prefixes`.

### Phase 2: Bash Tool Migration (Decision 1A only, skip if 1B/C)

4. **`holmes/plugins/toolsets/bash/bash_toolset.py`**
   - `requires_approval()`: extract session prefixes from `context.messages` instead of `context.session_approved_prefixes`
   - `requires_approval()`: handle `suggested_prefixes` param rewriting internally (moved from base `Tool.invoke()`)
   - `_invoke()`: on successful execution after approval, set `result.session_metadata = {"bash_session_approved_prefixes": [...]}`

5. **Update bash tests**
   - `tests/test_bash_session_prefix_flow.py` — adapt to read from `context.messages`
   - `tests/test_bash_toolset_validation.py` — update `ToolInvokeContext` construction
   - `tests/test_tool_calling_llm.py` — update approval tests if `save_prefixes` behavior changes

### Phase 3: HTTP Ad-Hoc Tool (New Files)

6. **`holmes/plugins/toolsets/http/http_adhoc_toolset.py`**
   - `HttpAdhocToolset(Toolset)` — always enabled, no prerequisites needed
   - `HttpAdhocRequest(Tool, JsonFilterMixin)` — tool name `http_adhoc_request`
   - `requires_approval()` extracts session-approved endpoints from `context.messages`, checks current request against them
   - `_invoke()` makes the request (reuses `requests` library, same as `HttpRequest`), sets `session_metadata` on result
   - `extract_http_session_endpoints(messages)` — tool-internal helper, same pattern as bash's `extract_bash_session_prefixes`
   - Endpoint matching: same `fnmatch` logic as `HttpToolset._match_path()`

7. **`holmes/plugins/toolsets/http/adhoc_instructions.jinja2`**
   - LLM instructions for the ad-hoc tool
   - Explains `suggested_endpoint`, batch path patterns, auth via headers
   - Tells LLM to prefer configured toolsets when available

8. **`holmes/plugins/toolsets/http/cli_approved_endpoints.py`**
   - CLI-mode persistence: load/save `~/.holmes/http_approved_endpoints.yaml`
   - Same pattern as `bash/common/cli_prefixes.py`
   - Called from within the tool, not from the framework

### Phase 4: CLI Approval UI

9. **`holmes/interactive.py`** (depends on Decision 3)
   - **If 3A:** Add `get_approval_display()` method to `Tool` base class. `handle_tool_approval()` calls it. Bash and HTTP ad-hoc each implement their own rendering.
   - **If 3B:** Add `elif tool_name == "http_adhoc_request"` branch to `handle_tool_approval()`.

### Phase 5: Registration + Tests

10. **`holmes/plugins/toolsets/__init__.py`**
    - Register `HttpAdhocToolset` in the built-in toolset list

11. **`tests/test_http_adhoc_session_flow.py`**
    - Mirror of `test_bash_session_prefix_flow.py` for HTTP ad-hoc
    - Test: first request → approval → execute → second request same host/path → auto-approved
    - Test: new path pattern → needs new approval
    - Test: new method on same host → needs new approval
    - Test: cross-conversation isolation

12. **Unit tests for endpoint matching**
    - Path glob matching with `fnmatch`
    - Host exact matching
    - Method matching
