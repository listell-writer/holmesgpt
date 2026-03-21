# Toolset Classes — Current Architecture

## Current Class Map

### Toolset (`holmes/core/tools.py` ~360 lines)

**What it is:** Base class for all toolsets. A toolset is a named collection of tools with prerequisites and config.

**Fields:**
- Identity: `name`, `description`, `docs_url`, `icon_url`, `type`, `path`
- State: `enabled`, `is_default`, `status` (ENABLED/DISABLED/FAILED), `error`
- Config: `config`, `config_classes` (class var)
- Tools: `tools: List[Tool]`, `restricted_tools`, `approval_required_tools`
- Prerequisites: `prerequisites: List[Union[Static, Command, Env, Callable]]`
- LLM: `llm_instructions`, `transformers`
- Tags: `tags: List[ToolsetTag]` (CORE, CLUSTER, CLI)
- Lazy init: `_lazy_init`, `_initialized`, `_init_lock`

**Methods (grouped by responsibility):**

| Responsibility | Methods |
|---|---|
| **Prerequisite checking** | `check_prerequisites()`, `check_config_prerequisites()`, `lazy_initialize()` |
| **Config introspection** | `missing_config` (property — pure fact-check), `get_config_example()`, `get_config_schema()` |
| **Override/merge** | `override_with(override)` |
| **LLM instructions** | `_load_llm_instructions()`, `_load_llm_instructions_from_file()` |
| **Env/command utils** | `get_environment_variables()`, `interpolate_command()` |

**Remaining god-object concerns:**
- Toolset is both a **data container** (tools, config, identity) and a **lifecycle manager** (prerequisites, lazy init, status transitions)
- The prerequisite system has 4 different types with different execution patterns (fast vs slow) baked into one method
- `override_with()` has complex merge logic that handles YAML↔Python toolset differences
- `_load_llm_instructions()` does Jinja2 rendering — orthogonal to toolset management

### Tool (`holmes/core/tools.py` ~260 lines)

**What it is:** A single callable operation exposed to the LLM.

**Fields:**
- `name`, `description`, `parameters: Dict[str, ToolParameter]`
- `user_description`, `icon_url`, `restricted`
- `transformers`, `_transformer_instances` (cached)

**Methods:**

| Responsibility | Methods |
|---|---|
| **Execution** | `invoke()`, `_invoke()` (abstract) |
| **LLM format** | `get_openai_format()` |
| **Approval** | `requires_approval()`, `_get_approval_requirement()`, `_check_approval_config()`, `_is_restricted()` |
| **Transformers** | `_apply_transformers()` |
| **Type coercion** | `_coerce_params()` |
| **Display** | `get_parameterized_one_liner()` |

**Remaining god-object concerns:**
- Tool mixes execution, approval, transformation, and serialization
- Approval logic is complex (pattern matching against toolset config, session prefixes)
- Transformer apply/revert logic is non-trivial

### ToolExecutor (`holmes/core/tools_utils/tool_executor.py` ~100 lines)

**What it is:** Read-only facade that provides fast O(1) tool lookups and lazy init triggering.

**Fields:**
- `toolsets` — all toolsets (for external access)
- `enabled_toolsets` — filtered to ENABLED status
- `tools_by_name` — flat dict of tool name → Tool
- `_tool_to_toolset` — reverse map tool name → parent Toolset

**Methods:**
- `get_tool_by_name(name)` — lookup
- `ensure_toolset_initialized(tool_name)` — trigger lazy init on first use
- `get_all_tools_openai_format(include_restricted)` — serialize for LLM

**Status: Clean.** Well-scoped responsibilities.

### ToolsetRegistry (`holmes/core/toolset_registry.py` ~540 lines)

**What it is:** Discovers, loads, merges toolsets from all sources and decides which are enabled. Returns a fully resolved `dict[str, Toolset]` with `enabled` already set. Does NOT run prerequisites.

**Fields:**
- `toolsets_config` — user config dict for built-in overrides and custom toolsets
- `custom_toolset_paths` — paths to custom toolset YAML files
- `additional_toolsets` — programmatically-added toolsets
- `custom_runbook_catalogs` — runbook catalog paths (passed through to builtin discovery)

**Pipeline (`get_all_toolsets()`):**
1. `_discover_builtin_toolsets()` — YAML files + Python classes from `plugins/toolsets/`
2. `_apply_config_overrides()` — user config merged onto builtins
3. `_load_custom_toolsets()` — custom toolset files merged in
4. Additional programmatic toolsets added
5. `should_enable_toolset()` decides enabled state for each toolset
6. Filter by tag if provided

**Key method — `should_enable_toolset()`:**
Single source of truth for whether a toolset should be enabled:
- Explicitly configured → respect the `enabled` flag from config
- Custom/MCP/HTTP/DATABASE toolsets → default to enabled
- Built-in + `auto_enable` → enable if `missing_config` is False

**Module-level helpers** (moved from `plugins/toolsets/__init__.py`):
- `_discover_builtin_toolsets()` — loads YAML + Python builtins
- `_discover_python_toolsets()` — instantiates all Python toolset classes
- `_load_toolsets_from_file()` — parses a single YAML file into toolsets
- `_parse_toolset_config()` — parses config dicts into typed Toolset objects (MCP, HTTP, DATABASE, YAML)
- `_merge_onto()` — merges new toolsets into existing dict via `override_with()`

### ToolsetManager (`holmes/core/toolset_manager.py` ~660 lines)

**What it is:** Manages toolset lifecycle: prerequisites, caching, status. Delegates discovery/loading/enabling to `ToolsetRegistry`.

**Key fields:**
- `registry` — the `ToolsetRegistry` instance
- `custom_toolsets_from_cli` — CLI-provided toolset files (not cached, always rechecked)
- `global_fast_model` — propagated to all transformers
- `toolset_status_location` — path to cached status JSON
- `config_file_path` — main config path for hash tracking

**Public API:**
- `prepare_toolsets()` — primary method: registry → fast_model injection → prerequisites → return
- `refresh_toolsets_and_get_changes()` — re-checks all toolsets and diffs against previous status

**Deprecated wrappers** (delegate to `prepare_toolsets`):
- `list_toolsets()`, `list_console_toolsets()`, `list_server_toolsets()`
- `refresh_server_toolsets_and_get_changes()`

**Backwards-compatible accessors** (proxy to `registry`):
- Properties: `toolsets`, `custom_toolsets`, `additional_toolsets`, `custom_runbook_catalogs`
- Methods: `load_custom_toolsets()`, `add_or_merge_onto_toolsets()`

**Internal methods:**
- `_list_all_toolsets()` — gets toolsets from registry + injects fast_model + optional prerequisites
- `_refresh_toolset_status()` / `refresh_toolset_status` — eager check + cache to disk
- `_load_toolset_with_status()` / `load_toolset_with_status` — restore from cache + lazy init
- `check_toolset_prerequisites()` — threaded prerequisite checking
- `_check_config_prerequisites()` — fast config-only checks
- `_inject_fast_model_into_transformers()` — 130-line method for transformer config injection

---

## Invariants

1. **Merge order**: builtins → config overrides → custom files → additional programmatic
2. **Config overrides can disable builtins**: `kubernetes/logs: { enabled: false }` works
3. **MCP servers default enabled** whether from config or file
4. **Custom toolsets from CLI raise on conflict** with existing toolset names
5. **Cache restoration** sets `enabled` from cached status (manager's job, not registry's).
   Config hash checking invalidates cache when config files change.
6. **`is_default` toolsets** — no `is_default=True` toolset has required config classes,
   so `missing_config` returns False for them regardless (no special branch needed)
7. **Deprecated toolset name mapping** (`coralogix/logs` → `coralogix`) — handled in registry
8. **TUI direct mutation** — `toolset_config_tui.py` sets `toolset.enabled = True` directly,
   bypassing the registry pipeline (legitimate, `enabled` is a public field)
9. **`missing_config`** is a pure fact-check: "do config_classes have required fields and no
   config was provided?" No `self.enabled` or `self.is_default` guards.

---

## Room for Improvement

### 1. Backwards-compat shim overload on `ToolsetManager`

`ToolsetManager` carries ~70 lines of backwards-compatible accessors (properties `toolsets`,
`custom_toolsets`, etc.) and deprecated wrappers (`list_toolsets`, `list_console_toolsets`,
`load_custom_toolsets`, `add_or_merge_onto_toolsets`). These exist solely for tests and
external callers that haven't migrated. The `add_or_merge_onto_toolsets` @staticmethod with
arg-sniffing for two calling conventions is particularly awkward.

**Fix:** Update callers to use `ToolsetRegistry` directly, then remove the shims.

### 2. `_list_all_toolsets` name clash with old code

`_list_all_toolsets` is the actual implementation in `ToolsetManager` but tests mock it as
if it were the old `_list_all_toolsets`. The new canonical name would be something like
`_resolve_and_check`, but renaming would break test mocks again.

**Fix:** Once the deprecated wrappers are removed, rename freely.

### 3. `_load_toolset_with_status` is 120 lines of tangled cache + prerequisites

The lazy-loading path (`_load_toolset_with_status`) mixes cache I/O, status restoration,
MCP eager-init branching, CLI toolset conflict checking, and additional-toolset prerequisite
checking in one method. This is the single hardest method to follow in the codebase.

**Fix (future):** Split into `_restore_from_cache()`, `_check_prerequisites_by_strategy()`,
and `_handle_cli_toolsets()`.

### 4. `_inject_fast_model_into_transformers` is 130 lines of verbose logging

~60% of this method is debug/info logging. The actual injection logic is ~20 lines total.
The method also reaches into `tool._transformer_instances` (a private field) to force
recreation, coupling it tightly to `Tool` internals.

**Fix:** Extract a `_inject_into_transformer(transformer)` helper; move transformer
recreation to a `Tool.recreate_transformer_instances()` method.

### 5. `_apply_config_overrides` sets `enabled=True` on custom toolsets in config dict

Lines 200-207 in `toolset_registry.py` mutate the config dict to force `enabled=True/False`
on custom toolsets before parsing. Then `should_enable_toolset()` reads `toolset.enabled`
back for explicitly-configured toolsets. This means the enable decision is split: half in
`_apply_config_overrides` (writing into the dict) and half in `should_enable_toolset`
(reading it back out). The `should_enable_toolset` docstring claims to be the single
source of truth, but it isn't for this case.

**Fix:** Stop mutating config dicts. Have `_apply_config_overrides` pass through the raw
config, then let `should_enable_toolset` handle the "custom toolsets default enabled unless
explicitly disabled" rule.

### 6. Re-exports in two places

`plugins/toolsets/__init__.py` and `toolset_manager.py` both re-export registry functions
under old names. The `toolset_manager.py` re-exports exist purely so test mocks at
`holmes.core.toolset_manager.load_builtin_toolsets` continue to work (they don't, since
the registry calls the function directly — the tests were already updated to mock at
`holmes.core.toolset_registry._discover_builtin_toolsets`). The `toolset_manager.py`
re-exports are now dead code.

**Fix:** Remove the re-exports from `toolset_manager.py`; keep only
`plugins/toolsets/__init__.py` for backwards compat.

### 7. `custom_toolsets_from_cli` lives on manager, not registry

CLI toolset paths are handled in `_load_toolset_with_status` by calling
`self.registry._load_toolsets_from_paths(self.custom_toolsets_from_cli, ...)` — reaching
into the registry's private method with data the registry doesn't own. This is because CLI
toolsets need conflict checking against cached names, which is a manager concern.

**Fix:** Either give the registry a `cli_toolset_paths` field with a separate
`get_cli_toolsets()` method, or extract a `_load_and_check_cli_toolsets()` method on the
manager that doesn't reach into registry internals.

### 8. `_discover_python_toolsets` is a 90-line import list

Every Python toolset class is imported inside `_discover_python_toolsets()` to avoid
circular imports. This function is a hardcoded registry of all Python toolsets. Adding a
new Python toolset requires editing this function.

**Fix (future):** Use a registration decorator pattern, or a plugin entry-point system,
so toolsets self-register.

---

## Future Refactoring Options (not in current scope)

### Option A: Extract PrerequisiteChecker

**Extract prerequisite checking into a separate class.**

```
PrerequisiteChecker:
  - check_all(toolset) → (status, error)
  - check_config_only(toolset) → (status, error)
  - lazy_initialize(toolset) → (status, error)
```

**Benefit:** Toolset becomes a simpler data container. Lifecycle transitions are explicit.
**Risk:** Low — prerequisite logic is already self-contained within `check_prerequisites()`.

### Option B: Extract ToolsetMerger

**Extract override/merge logic from both Toolset and ToolsetManager.**

```
ToolsetMerger:
  - merge_config(base: Toolset, override: ToolsetYamlFromConfig) → Toolset
  - merge_all(builtins, user_config, custom_files, additional) → List[Toolset]
```

**Benefit:** Loading pipeline becomes a clear sequence of steps. Merge rules are centralized.
**Risk:** Medium — merge logic currently accesses private Toolset internals.

### Option C: Extract ApprovalPolicy

**Extract approval logic from Tool into a policy object.**

```
ApprovalPolicy:
  - check(tool, params, context) → ApprovalRequirement
  - is_restricted(tool, toolset) → bool
```

**Benefit:** Authorization rules are testable in isolation. Tool.invoke() becomes simpler.
**Risk:** Low — approval logic is already somewhat isolated in `_get_approval_requirement()`.

### Option D: Simplify ToolsetManager loading paths

**Merge eager and lazy loading into a single path with a `defer_slow_checks` flag.**

Currently there are:
- `_list_all_toolsets()` — eager, runs all prerequisites
- `_load_toolset_with_status()` — lazy, uses cache + deferred init

These could be unified into one loading path where `defer_slow_checks=True` skips callable/command prerequisites and marks toolsets for lazy init.

**Benefit:** One code path to understand and maintain.
**Risk:** Medium — the caching behavior in `_load_toolset_with_status` is interleaved with the loading logic.
