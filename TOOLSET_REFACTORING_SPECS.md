# Toolset Classes — Current State & Refactoring Specs

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
| **Config introspection** | `missing_config` (property), `get_config_example()`, `get_config_schema()` |
| **Override/merge** | `override_with(override)` |
| **LLM instructions** | `_load_llm_instructions()`, `_load_llm_instructions_from_file()` |
| **Env/command utils** | `get_environment_variables()`, `interpolate_command()` |

**God-object concerns:**
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

**God-object concerns:**
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

**Status: Clean.** This is the simplest class. Well-scoped responsibilities.

### ToolsetManager (`holmes/core/toolset_manager.py` ~350 lines)

**What it is:** Orchestrates the entire toolset loading pipeline: discovery → config merge → prerequisite checking → caching.

**Key fields:**
- `toolsets` — user config for built-in overrides
- `custom_toolsets`, `custom_toolsets_from_cli`, `additional_toolsets` — extra sources
- `toolset_status_location` — path to cached status JSON
- `global_fast_model` — propagated to all transformers

**Loading pipeline:**
1. Load built-in toolsets (YAML + Python from `plugins/toolsets/`)
2. Override/merge with user `toolsets` config
3. Merge in `custom_toolsets` files
4. Merge in `additional_toolsets` (programmatic)
5. Filter by tags
6. Inject `global_fast_model` into transformers
7. Run prerequisite checks (or defer for lazy init)

**God-object concerns:**
- Mixes loading, merging, caching, and prerequisite orchestration
- Multiple code paths for eager vs lazy loading (`_list_all_toolsets` vs `load_toolset_with_status`)
- Status caching logic (JSON file read/write) is interleaved with loading
- Deprecated methods still present (`list_console_toolsets`, `list_server_toolsets`)

---

## Identified God-Object Challenges

### 1. Toolset: Lifecycle + Data mixed

The `Toolset` class is both:
- A **value object** describing what tools are available and how to configure them
- A **stateful lifecycle manager** tracking initialization state, running health checks, transitioning status

**Symptom:** `check_prerequisites()`, `check_config_prerequisites()`, and `lazy_initialize()` all mutate `self.status` and `self.error` with complex branching between fast/slow prerequisite types.

### 2. Toolset: Override/merge is complex

`override_with()` handles merging fields from YAML config overrides with special rules (skip None, skip empty lists, handle tools list merge). This is configuration merging logic that doesn't belong on the domain object.

### 3. Tool: Approval logic is entangled

`Tool.invoke()` checks approval against toolset patterns (`restricted_tools`, `approval_required_tools`), session state (`session_approved_prefixes`), and tool-specific overrides (`requires_approval()`). This is authorization policy spread across multiple classes.

### 4. ToolsetManager: Too many loading strategies

`ToolsetManager` has both eager (`_list_all_toolsets`) and lazy (`load_toolset_with_status`) paths with subtle behavioral differences. The caching layer adds another dimension.

### 5. Prerequisite system: 4 types, 2 phases, 1 method

Prerequisites have 4 concrete types (Static, Env, Command, Callable) split across 2 phases (fast config-only vs full init). The splitting logic lives in `Toolset.check_prerequisites()` and `check_config_prerequisites()` via isinstance checks.

---

## Refactoring Options

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
- `load_toolset_with_status()` — lazy, uses cache + deferred init

These could be unified into one loading path where `defer_slow_checks=True` skips callable/command prerequisites and marks toolsets for lazy init.

**Benefit:** One code path to understand and maintain.
**Risk:** Medium — the caching behavior in `load_toolset_with_status` is interleaved with the loading logic.

### Recommended priority

1. **Option A (PrerequisiteChecker)** — highest impact, lowest risk, most lines moved out of Toolset
2. **Option D (Unified loading)** — simplifies ToolsetManager, reduces confusion
3. **Option C (ApprovalPolicy)** — cleaner Tool class, independently testable
4. **Option B (ToolsetMerger)** — nice to have, but merge logic is stable and rarely changed
