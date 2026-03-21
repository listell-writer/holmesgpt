# Toolset Classes — Current State & Refactoring Specs

> **Status: PENDING** — This document describes a refactoring that is being *considered*, not yet implemented. The current codebase does not reflect these changes.

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

## Phase 1: Centralize Enable/Disable Logic & Extract ToolsetRegistry

> **Status: APPROVED** — These are the first concrete steps. No other refactoring is in scope.

### Problem Statement

The enable/disable policy for toolsets is scattered across 6+ locations:

| Location | What it decides |
|---|---|
| `Toolset.enabled` default (`tools.py:703`) | Built-ins start `enabled=False` |
| `ToolsetYamlFromConfig.enabled` default (`tools.py:1083`) | Config overrides start `enabled=True` |
| `__init__.py:80` | MCP from file → `enabled=True` via `setdefault` |
| `_load_toolsets_from_config` (`toolset_manager.py:289-293`) | Custom toolsets default enabled unless explicitly disabled |
| `_list_all_toolsets` (`toolset_manager.py:182-190`) | Auto-enable built-ins if no `missing_config` |
| `missing_config` property (`tools.py:857-884`) | Bakes in `enabled`/`is_default` policy guards into what should be a fact-check |

Additionally, `ToolsetManager` mixes two responsibilities:
- **Registry**: discovering, loading, merging, and deciding what's enabled
- **Lifecycle**: prerequisites, caching, lazy init, status management

### Step 1: Clean up `missing_config` → pure fact-check

**Current** (`tools.py:857-884`): Mixes policy (`self.enabled`, `self.is_default` early returns) with fact-checking (do config classes have required fields without values?).

**Target**: `missing_config` becomes a pure predicate — "does this toolset need config that wasn't provided?"

```python
@property
def missing_config(self) -> bool:
    """True when config_classes have required fields and no config was provided."""
    if not self.config_classes:
        return False

    requires_config = any(
        cls.has_required_fields()
        for cls in self.config_classes
        if hasattr(cls, "has_required_fields")
    )
    if not requires_config:
        return False

    return self.config is None
```

No `self.enabled`, no `self.is_default` guards. Those move to `should_enable_toolset`.

### Step 2: Create `ToolsetRegistry`

**New file**: `holmes/core/toolset_registry.py`

**Responsibility**: Discovering, loading, merging toolsets from all sources, and deciding which are enabled. Returns a fully resolved `dict[str, Toolset]` with `enabled` already set.

```python
class ToolsetRegistry:
    """Discovers, loads, merges toolsets and decides which are enabled."""

    def __init__(
        self,
        toolsets_config: dict[str, dict[str, Any]],
        custom_toolset_paths: list[FilePath],
        additional_toolsets: list[Toolset],
        custom_runbook_catalogs: list[Union[str, FilePath]],
    ): ...

    def get_all_toolsets(
        self,
        dal: Optional[SupabaseDal] = None,
        auto_enable: bool = False,
        tag_filter: Optional[list[ToolsetTag]] = None,
    ) -> dict[str, Toolset]:
        """Return all toolsets with enabled state resolved.

        Pipeline:
        1. discover_builtin_toolsets() — YAML files + Python classes
        2. apply_config_overrides() — user toolsets config merged onto builtins
        3. apply_custom_toolsets() — custom toolset files merged in
        4. apply_additional_toolsets() — programmatic toolsets added
        5. For each toolset: toolset.enabled = should_enable_toolset(...)
        6. Filter by tag_filter if provided
        """
        ...

    def should_enable_toolset(
        self,
        toolset: Toolset,
        explicitly_configured: bool,
        auto_enable: bool,
    ) -> bool:
        """Single source of truth for whether a toolset should be enabled.

        Args:
            toolset: The toolset to evaluate.
            explicitly_configured: True if the user named this toolset in their config.
            auto_enable: True if auto-enabling all toolsets that can work without config.
        """
        # Explicitly configured → respect the enabled flag from config
        if explicitly_configured:
            return toolset.enabled

        # Custom/MCP/HTTP/DATABASE toolsets default to enabled
        if toolset.type in (
            ToolsetType.CUSTOMIZED, ToolsetType.MCP,
            ToolsetType.HTTP, ToolsetType.DATABASE, ToolsetType.MONGODB,
        ):
            return True

        # Built-in + auto_enable → enable if config requirements are met
        if auto_enable:
            return not toolset.missing_config

        return False
```

**Methods migrating from current locations**:

| Current location | Method | New location |
|---|---|---|
| `plugins/toolsets/__init__.py` | `load_builtin_toolsets()` | `ToolsetRegistry._discover_builtin_toolsets()` |
| `plugins/toolsets/__init__.py` | `load_python_toolsets()` | `ToolsetRegistry._discover_python_toolsets()` |
| `plugins/toolsets/__init__.py` | `load_toolsets_from_config()` | `ToolsetRegistry._parse_toolset_config()` |
| `plugins/toolsets/__init__.py` | `load_toolsets_from_file()` | `ToolsetRegistry._load_toolsets_from_file()` |
| `ToolsetManager` | `_load_toolsets_from_config()` | `ToolsetRegistry._apply_config_overrides()` |
| `ToolsetManager` | `load_custom_toolsets()` | `ToolsetRegistry._apply_custom_toolsets()` |
| `ToolsetManager` | `_load_toolsets_from_paths()` | `ToolsetRegistry._load_toolsets_from_paths()` |
| `ToolsetManager` | `add_or_merge_onto_toolsets()` | `ToolsetRegistry._merge_onto()` |
| `ToolsetManager._list_all_toolsets` | enable_all logic (lines 182-190) | `ToolsetRegistry.should_enable_toolset()` |
| `ToolsetManager._list_all_toolsets` | tag filtering (lines 218-223) | `ToolsetRegistry.get_all_toolsets()` |
| `ToolsetManager` | `_inject_fast_model_into_transformers()` | stays on `ToolsetManager` (lifecycle concern) |

### Step 3: Simplify `ToolsetManager` → rename to lifecycle role

**Rename the main public method**: `list_toolsets()` → `prepare_toolsets()`

`ToolsetManager` keeps:
- `prepare_toolsets()` — gets toolsets from registry, checks prerequisites, returns ready-to-use list
- `refresh_toolset_status()` — eager prerequisite check + cache to disk
- `load_toolset_with_status()` — restore from cache + lazy init
- `check_toolset_prerequisites()` / `_check_config_prerequisites()` — prerequisite orchestration
- `_inject_fast_model_into_transformers()` — transformer config injection
- Status cache I/O

`ToolsetManager.__init__` takes a `ToolsetRegistry` instead of raw config dicts:

```python
class ToolsetManager:
    def __init__(
        self,
        registry: ToolsetRegistry,
        custom_toolsets_from_cli: Optional[List[FilePath]] = None,
        toolset_status_location: Optional[FilePath] = None,
        global_fast_model: Optional[str] = None,
    ): ...

    def prepare_toolsets(
        self,
        dal: Optional[SupabaseDal] = None,
        tag_filter: Optional[List[ToolsetTag]] = None,
        auto_enable: bool = False,
        defer_prerequisites: bool = True,
        force_recheck: bool = False,
    ) -> List[Toolset]:
        """Get toolsets from registry and prepare them for use."""
        toolsets = self.registry.get_all_toolsets(
            dal=dal, auto_enable=auto_enable, tag_filter=tag_filter,
        )
        # ... prerequisite checking, caching, lazy init ...
```

### What's NOT in scope

- Extracting `PrerequisiteChecker` from `Toolset` (future work)
- Extracting `ApprovalPolicy` from `Tool` (future work)
- Unifying eager/lazy loading paths (future work)
- Changing `ToolExecutor` (already clean)
- Renaming `ToolsetManager` class itself (keep it, just clarify its role)

### Key invariants to preserve

1. **Merge order**: builtins → config overrides → custom files → additional programmatic
2. **Config overrides can disable builtins**: `kubernetes/logs: { enabled: false }` must still work
3. **MCP servers default enabled** whether from config or file
4. **Custom toolsets from CLI raise on conflict** with existing toolset names
5. **Cache restoration** sets `enabled` from cached status (manager's responsibility, not registry's)
6. **`is_default` toolsets** — `missing_config` currently treats these as "not missing config". After cleanup, `should_enable_toolset` must handle `is_default` builtins. If `is_default=True` and `auto_enable=True`, enable even if `missing_config=True`... OR we need to verify no `is_default` toolset actually has required config classes. Must check.
7. **Deprecated toolset name mapping** (`coralogix/logs` → `coralogix`) — stays in registry

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
- `load_toolset_with_status()` — lazy, uses cache + deferred init

These could be unified into one loading path where `defer_slow_checks=True` skips callable/command prerequisites and marks toolsets for lazy init.

**Benefit:** One code path to understand and maintain.
**Risk:** Medium — the caching behavior in `load_toolset_with_status` is interleaved with the loading logic.
