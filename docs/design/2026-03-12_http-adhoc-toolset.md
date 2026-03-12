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
  1. Yes (one-time)
  2. Yes, and remember for this session
  3. No, and tell Holmes what to do differently
```

If the user picks option 2, the endpoint is remembered for the session. All subsequent requests matching any of those path patterns on that host with GET are auto-approved.

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

When the user approves with "remember for this session," the `suggested_endpoint` is stored. Future requests are checked with:

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

### How Tool Approval Works Today

The approval system was built for the bash toolset and has two paths:

**CLI path** (`interactive.py`):
- Tool returns `APPROVAL_REQUIRED` status
- `_handle_tool_call_approval()` calls `approval_callback(tool_result)`
- `handle_tool_approval()` shows an interactive menu: Yes / Yes+remember / No+feedback
- If approved, tool is re-invoked with `user_approved=True`
- If "remember," prefixes are saved to `~/.holmes/bash_approved_prefixes.yaml`

**Server/HTTP API path** (`call_stream` in `tool_calling_llm.py`):
- Tool returns `APPROVAL_REQUIRED` status
- Stream emits `APPROVAL_REQUIRED` event with `PendingToolApproval` data:
  ```json
  {
    "tool_call_id": "call_abc",
    "tool_name": "bash",
    "description": "bash",
    "params": {"command": "...", "suggested_prefixes": ["..."]}
  }
  ```
- Stream **stops**. Client must make a new POST request.
- Client sends `ToolApprovalDecision`:
  ```json
  {
    "tool_call_id": "call_abc",
    "approved": true,
    "save_prefixes": ["kubectl get"]
  }
  ```
- `process_tool_decisions()` executes the tool with `user_approved=True`
- If `save_prefixes` provided, stores them in tool result message metadata as `bash_session_approved_prefixes`
- On future requests, `extract_bash_session_prefixes(messages)` scans conversation history for this metadata → populates `context.session_approved_prefixes`

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

**`PendingToolApproval`** — no changes needed. It already carries `tool_name` and `params`, which is tool-agnostic. For `http_adhoc_request`, `params` contains `url`, `method`, `suggested_endpoint`, etc. The client renders the approval UI based on `tool_name`.

**`ToolApprovalDecision`** — extend with a generic field:

```python
class ToolApprovalDecision(BaseModel):
    tool_call_id: str
    approved: bool
    save_prefixes: Optional[List[str]] = None          # existing, bash (backwards compat)
    save_session_data: Optional[Dict[str, Any]] = None  # NEW, generic
```

For HTTP ad-hoc, the client sends:
```json
{
  "tool_call_id": "call_xyz",
  "approved": true,
  "save_session_data": {
    "http_endpoints": [{
      "host": "api.pagerduty.com",
      "path_patterns": ["/incidents/*", "/services/*"],
      "methods": ["GET"]
    }]
  }
}
```

Alternatively, the server can derive what to save from the tool's `ApprovalRequirement` when the user simply sends `approved: true` with a `remember: true` flag. This avoids requiring the client to echo back structured data it doesn't understand:

```python
class ToolApprovalDecision(BaseModel):
    tool_call_id: str
    approved: bool
    remember: bool = False                        # NEW: "don't ask again for similar"
    save_prefixes: Optional[List[str]] = None     # existing, backwards compat
```

With `remember: true`, the server re-calls `tool.requires_approval()` to get `ApprovalRequirement`, extracts the save data (prefixes for bash, endpoints for HTTP), and stores it in message metadata. The client just sends yes/no/remember — it doesn't need to know the save format per tool type.

**`ToolInvokeContext`** — extend:

```python
class ToolInvokeContext(BaseModel):
    session_approved_prefixes: List[str] = []                # existing (bash)
    session_approved_endpoints: List[Dict[str, Any]] = []    # NEW (http)
```

**`ApprovalRequirement`** — extend:

```python
class ApprovalRequirement(BaseModel):
    needs_approval: bool
    reason: str = ""
    prefixes_to_save: Optional[List[str]] = None          # existing (bash)
    endpoints_to_save: Optional[List[Dict]] = None         # NEW (http)
```

**Session metadata** — add `extract_http_session_endpoints(messages)` alongside existing `extract_bash_session_prefixes(messages)`. Both scan tool result messages for their respective metadata keys.

### Existing Client Compatibility

A client that only knows about bash approval (sends `save_prefixes`) continues to work unchanged. The new `remember` and `save_session_data` fields are optional with defaults. Server-side extraction via `remember: true` means even a minimal client that just sends `approved: true, remember: true` gets full session memory without understanding tool-specific save formats.

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

### 7. Generic `save_data` replacing `save_prefixes`

**Idea:** Replace `save_prefixes` with a fully generic `save_data: Dict[str, Any]` that all tools use.

**Why not for phase 1:** Breaking change for existing clients that send `save_prefixes`. Adding `remember: bool` alongside `save_prefixes` (kept for backwards compat) is non-breaking and achieves the same goal. Full generalization can happen later.

## Implementation Plan

### New Files

1. **`holmes/plugins/toolsets/http/http_adhoc_toolset.py`**
   - `HttpAdhocToolset(Toolset)` — always enabled, no prerequisites needed
   - `HttpAdhocRequest(Tool, JsonFilterMixin)` — tool name `http_adhoc_request`
   - `requires_approval()` checks `suggested_endpoint` against session-approved endpoints
   - `_invoke()` makes the request (reuses `requests` library, same as `HttpRequest`)
   - Endpoint matching: same `fnmatch` logic as `HttpToolset._match_path()`

2. **`holmes/plugins/toolsets/http/adhoc_instructions.jinja2`**
   - LLM instructions for the ad-hoc tool
   - Explains `suggested_endpoint`, batch path patterns, auth via headers
   - Tells LLM to prefer configured toolsets when available

3. **`holmes/plugins/toolsets/http/cli_approved_endpoints.py`**
   - CLI-mode persistence: load/save `~/.holmes/http_approved_endpoints.yaml`
   - Same pattern as `bash/common/cli_prefixes.py`

### Modified Files

4. **`holmes/core/tools.py`**
   - `ToolInvokeContext`: add `session_approved_endpoints: List[Dict[str, Any]] = []`
   - `ApprovalRequirement`: add `endpoints_to_save: Optional[List[Dict]] = None`

5. **`holmes/core/models.py`**
   - `ToolApprovalDecision`: add `remember: bool = False`

6. **`holmes/core/tool_calling_llm.py`**
   - Add `extract_http_session_endpoints(messages)` — same pattern as `extract_bash_session_prefixes`
   - Wire `session_approved_endpoints` through `_invoke_llm_tool_call` → `ToolInvokeContext`
   - In `process_tool_decisions`: when `remember=True` and tool has `endpoints_to_save`, store in metadata as `http_session_approved_endpoints`

7. **`holmes/interactive.py`**
   - Extend `handle_tool_approval()` to handle non-bash tools:
     - Show tool name (not "Bash command") based on `tool_name`
     - For `http_adhoc_request`: show host, paths, methods from `suggested_endpoint`
     - "Remember" option saves endpoints (CLI path) or returns appropriate data
   - Add `_save_approved_endpoints()` for CLI persistence

8. **`holmes/plugins/toolsets/__init__.py`**
   - Register `HttpAdhocToolset` in the built-in toolset list

### Tests

9. **`tests/test_http_adhoc_session_flow.py`**
   - Mirror of `test_bash_session_prefix_flow.py` for HTTP ad-hoc
   - Test: first request → approval → execute → second request same host/path → auto-approved
   - Test: new path pattern → needs new approval
   - Test: new method on same host → needs new approval
   - Test: cross-conversation isolation

10. **Unit tests for endpoint matching**
    - Path glob matching with `fnmatch`
    - Host exact matching
    - Method matching
