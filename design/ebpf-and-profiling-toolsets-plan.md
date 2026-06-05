# Plan: On-Demand eBPF Observability & Application Diagnostics for HolmesGPT

**Status:** Research / design proposal (no implementation yet)
**Author:** investigation for @natan
**Scope:** How to let HolmesGPT gather observability data *on demand* that the
existing stack does not collect — system-level eBPF (bpftrace / Inspektor
Gadget / continuous-profiler queries) and application-specific diagnostics
(JVM via `jattach` + async-profiler, Go via `pprof`, Python via `py-spy`).

---

## 1. Goal

Today HolmesGPT can only read what observability tools already collect (metrics
in Prometheus, logs in Loki, traces, etc.). When a user asks *"why is this
service slow / burning CPU / blocking on disk?"* and there is no pre-existing
profile or metric for it, Holmes is stuck.

We want Holmes to **generate the data on demand** at investigation time by
running short-lived diagnostics against a live pod or node:

- **eBPF / kernel-level**: CPU profiling, off-CPU/scheduler latency, block-I/O
  latency, syscall tracing, TCP/DNS tracing, page faults, etc.
- **Application-specific**: JVM thread/heap/CPU profiles (`jattach`,
  `async-profiler`/JFR, `jstack`, `jmap`), Go `pprof` (CPU/heap/goroutine),
  Python `py-spy`.

Two parts to this design:
1. **Architecture** — *how* a Holmes toolset launches a diagnostic in-cluster.
2. **Testability** — *what* we can actually verify in (a) the Claude sandbox
   k3s cluster and (b) GitHub Actions CI evals tagged `regression`, and where
   eBPF specifically hits a wall.

---

## 2. How HolmesGPT toolsets work (the relevant 5%)

(References for implementers — verified against the current tree.)

- A toolset is either a **YAML** file (`holmes/plugins/toolsets/*.yaml`, parsed
  into `YAMLToolset`/`YAMLTool`, see `holmes/core/tools.py:547` and `:1119`) or a
  **Python** class (`Toolset`/`Tool` subclass, `holmes/core/tools.py:284` and
  `:733`). YAML tools render a Jinja2 `command:`/`script:` and run it via
  `subprocess.run(..., shell=True, executable="/bin/bash")`
  (`holmes/core/tools.py:670`). Python tools override `_invoke()` and return a
  `StructuredToolResult` (`status`/`error`/`data`/`invocation`).
- Toolsets gate on **prerequisites** (`holmes/core/tools.py:698-730`):
  `command:` (e.g. `kubectl version --client`), `env:` (an env flag that must be
  set), or a `CallablePrerequisite` health check. Disabled-by-default toolsets
  flip on only when their env flag is present.
- Loading/registration: YAML files auto-load; Python toolsets are listed in
  `load_python_toolsets()` (`holmes/plugins/toolsets/__init__.py:99`).

**The two most relevant pieces of prior art already in the repo:**

| Existing toolset | Mechanism | Why it matters here |
|---|---|---|
| `inspektor_gadget.yaml` | `kubectl debug --profile=sysadmin node/<node> --image=ghcr.io/inspektor-gadget/ig -- ig run <gadget>` | This is **already eBPF on a node via an ephemeral debug pod**. Gated behind `ENABLE_INSPEKTOR_GADGET`. Our eBPF work should extend this rather than reinvent it. |
| `kubectl_run/kubectl_run_toolset.py` | `kubectl run <pod> --image=<img> --rm --attach -- <cmd>` | Pattern for launching a throwaway diagnostic pod with image/command allow-listing (`validate_image_and_commands`). Disabled by default. |

So HolmesGPT *already ships eBPF tooling* via Inspektor Gadget. The new work is
(a) broadening the eBPF gadget coverage to performance profiling, and (b) adding
the **application-level** profilers, which IG does not cover.

---

## 3. The delivery-mechanism question (core architecture decision)

The hard part is not "which profiler" — it's **how Holmes injects the profiler
next to a running process it does not control**. Five options:

### Option A — `kubectl debug node/<node>` (privileged node debug pod)
What Inspektor Gadget already uses. A privileged pod in the host PID/net
namespace, can load eBPF for the whole node.

- ✅ Full kernel access; one mechanism covers all node-wide eBPF.
- ✅ No changes to the target workload; no sidecar; nothing persists.
- ❌ Node-level → noisy; must filter by `--k8s-namespace`/`--k8s-podname`.
- ❌ Requires a privileged-pod-capable node and `node/debug` RBAC.
- **Best for:** system-wide eBPF (CPU profile, biolatency, runqlat, tcptracer).

### Option B — `kubectl debug <pod> --target=<container>` (ephemeral container sharing the target's namespaces)
An ephemeral container is injected **into the target pod**, sharing its PID
namespace (and optionally `--profile=sysadmin` for `SYS_PTRACE`). The profiler
sees the target process as a normal PID and reaches it over `localhost`.

- ✅ Pod-scoped, low noise; reaches the exact target process.
- ✅ Ideal for **app profilers**: `py-spy dump --pid`, `jattach`, `jstack`,
  `curl localhost:6060/debug/pprof/...` all work because PID/net are shared.
- ✅ No image rebuild, no sidecar baked into the deployment.
- ❌ Needs Kubernetes ≥1.25 (ephemeral containers GA) and `pods/ephemeralcontainers` RBAC.
- ❌ `SYS_PTRACE` / `shareProcessNamespace` needed for `py-spy`/`jattach`.
- **Best for:** JVM/Go/Python application diagnostics. **Recommended primary
  mechanism for the app-diagnostics toolsets.**

### Option C — Persistent DaemonSet agent (Pyroscope/Parca/IG-as-daemonset) queried over HTTP
A continuous profiler runs cluster-wide; Holmes only *queries* it (like the
Prometheus toolset queries Prometheus).

- ✅ Holmes toolset is a **pure HTTP client** — no privilege, no kernel deps,
  trivially testable (mock the HTTP API, exactly like `prometheus/`).
- ✅ Profiling already happening continuously → can answer about the *past*.
- ❌ Requires the user to have deployed Parca/Pyroscope/Grafana-Phlare; not
  truly "on demand" for un-instrumented workloads.
- **Best for:** a `pyroscope`/`parca` query toolset, complementary to A/B.

### Option D — `kubectl run` throwaway pod (existing `kubectl-run` toolset)
- ✅ Already implemented, allow-listed.
- ❌ Does **not** share the target's PID/net namespace → useless for attaching a
  profiler to another pod's process. Only good for self-contained probes.

### Option E — Sidecar baked into the Deployment
Rejected: requires mutating the user's workload; not on-demand; persistent.

**Recommendation:** A (extend Inspektor Gadget) for kernel/eBPF, B (ephemeral
debug container) for app profilers, C (HTTP query) as an optional add-on for
shops that run a continuous profiler. Reuse the existing `ENABLE_*` env-gating
and image allow-listing patterns for safety.

---

## 4. Per-capability implementation sketches

### 4.1 eBPF — extend Inspektor Gadget (Option A)
IG already exposes `snapshot_process/socket`, `trace_exec/open/tcp/dns`,
`traceloop`, `tcpdump`. Add performance-oriented gadgets as new tools in
`inspektor_gadget.yaml` (same `kubectl debug node/... -- ig run <gadget>`
template), e.g. CPU profiling (`profile_cpu` → folded stacks), block-I/O
latency, run-queue latency, page faults. Output is JSON/folded-stacks; pipe
through an `llm_summarize` transformer because raw stacks are huge.

Alternative for shops *not* running IG: a thin `bpftrace` YAML toolset that does
`kubectl debug --profile=sysadmin node/<node> --image=<bpftrace-img> -- bpftrace
-e '<one-liner>'` with an **allow-list of vetted one-liners** (never free-form
`-e` from the LLM — that is arbitrary kernel code). IG is the safer default
because gadgets are pre-compiled and signed.

### 4.2 JVM diagnostics — new `jvm_diagnostics` toolset (Option B)
Ephemeral debug container (image bundling `jattach` + `async-profiler` + JDK
tools) injected into the target pod with shared PID ns + `SYS_PTRACE`:

- `jstack` / `jattach threaddump` → thread dump (deadlocks, blocked threads).
- `async-profiler` (`asprof`) CPU/alloc/lock profile for N seconds →
  **collapsed/folded** output, then summarize to top-N stacks (flamegraph SVG is
  useless to an LLM; folded text top-frames is what to feed it).
- `jattach` `jcmd` for `GC.heap_info`, `VM.flags`, `Thread.print`.
- `jmap -histo` (bounded) for the top heap consumers.

Bound every profile to a short fixed duration (e.g. 10–30s) and cap output size.

### 4.3 Go — `pprof` toolset (Option B, lightest of all)
For services exposing `net/http/pprof`: from an ephemeral container (or even
`kubectl exec` if the target has the endpoint), `curl
localhost:<port>/debug/pprof/{profile,heap,goroutine}` then `go tool pprof -top`
(or the standalone `pprof` binary) → text top-N. **No kernel features needed at
all** — pure userspace HTTP + symbolization.

### 4.4 Python — `py-spy` (Option B)
`py-spy dump --pid <pid>` and `py-spy record` from an ephemeral container with
`SYS_PTRACE`. Uses `process_vm_readv`/`ptrace`, **not** eBPF — so it works
anywhere ptrace is allowed, independent of the kernel's eBPF config.

### 4.5 Continuous-profiler query — `pyroscope`/`parca` toolset (Option C)
Thin HTTP wrapper modeled on `prometheus/` (server-side filtering by
service/profile-type/time-range, `JsonFilterMixin` for the rest). Pure client;
the easiest to unit-test.

### 4.6 Output handling (applies to all)
Profiles are enormous and not LLM-digestible raw. Rules:
- Always request **folded/collapsed** or `-top` text, never SVG/binary.
- Bound sampling duration and truncate to top-N frames server-side where
  possible; otherwise route through `llm_summarize`.
- Follow the repo rule: return full underlying error + exact command on failure
  so the LLM can self-correct.

---

## 5. Security & safety

These are the most privileged tools Holmes would ship, so:

- **Off by default**, gated behind explicit env flags per family (mirror
  `ENABLE_INSPEKTOR_GADGET`), e.g. `ENABLE_EBPF_PROFILING`,
  `ENABLE_APP_PROFILING`.
- **Image allow-listing** for any `kubectl run`/`debug` image, reusing
  `validate_image_and_commands` from the `kubectl-run` toolset.
- **No free-form `bpftrace -e` / arbitrary kernel code** from the LLM — only
  vetted gadgets or an allow-listed one-liner catalog.
- **Bounded** sampling duration, output size, and tool timeout on every tool.
- RBAC documented as a prerequisite: `nodes/debug`,
  `pods/ephemeralcontainers`, privileged PSA where needed.
- Read-only intent preserved (profilers observe, they don't mutate). `jattach`
  *can* invoke mutating jcmds — restrict the jcmd allow-list to read-only ones.
- Approval flow: consider routing these through the existing approval mechanism
  given their privilege.

---

## 6. Testability — what I can actually verify, and where eBPF breaks

This was an explicit ask. I probed both target environments.

### 6.1 Claude sandbox (the k3s brought up by `scripts/setup-sandbox-k8s.sh`)
The k3s "node" is a container sharing the **host kernel**. I inspected that
kernel directly (`/proc/config.gz`, `/sys`, `/proc/sys`). Findings:

| Capability eBPF needs | Sandbox host kernel (6.18.5) | Verdict |
|---|---|---|
| `CONFIG_KPROBES` | **not set** | ✗ no kprobe/kretprobe attach |
| BTF (`/sys/kernel/btf/vmlinux`) | **missing**; `CONFIG_DEBUG_INFO_BTF` absent | ✗ no CO-RE / libbpf / IG / modern bcc |
| `CONFIG_BPF_JIT` | **not set** | ✗ (only `HAVE_EBPF_JIT=y`) |
| tracefs (`/sys/kernel/debug/tracing`) | **not mounted** | ✗ no tracepoints/ftrace attach |
| `/lib/modules`, kernel headers | **absent** | ✗ bcc can't compile, no module load |
| `kernel.perf_event_paranoid` | `2` | ✗ restricts perf sampling |
| `kernel.unprivileged_bpf_disabled` | `2` | bpf() only for privileged |
| `CONFIG_BPF` / `CONFIG_BPF_SYSCALL` / `CONFIG_PERF_EVENTS` | `=y` | partial — syscall exists |

**Conclusion: eBPF *tracing* cannot run in the Claude sandbox.** No BTF + no
kprobes + no tracefs + no BPF JIT means bpftrace, BCC and Inspektor Gadget will
all fail to load programs regardless of privilege. This is a property of the
sandbox's stripped kernel, not something a setup script can fix. (Consistent
with the CLAUDE.md note that even NetworkPolicy/`ipset` enforcement is
impossible here.)

**What *does* work in the sandbox** (no eBPF dependency):
- **Go `pprof`** — pure userspace HTTP + symbolization. Fully runnable.
- **`async-profiler`/`jattach`/`jstack`** — JVMTI/perf-fallback (`itimer` mode),
  ptrace-based; no eBPF. Runnable (no Yama ptrace_scope restriction observed).
- **`py-spy`** — ptrace/`process_vm_readv`; no eBPF. Runnable.

So the **application-diagnostics** half of this project is verifiable in the
sandbox; the **eBPF** half is not.

### 6.2 GitHub Actions CI evals (KIND on `ubuntu` runners, tagged `regression`)
CI (`.github/workflows/eval-regression.yaml`) runs KIND `kindest/node:v1.35.0`
with Calico on GitHub-hosted Ubuntu runners, executing `-m "llm and
(regression)"` live (`RUN_LIVE=true`). Unlike the sandbox, GitHub's Ubuntu
runner kernels **do** ship BTF and kprobes, so eBPF *can* technically run inside
a privileged KIND pod. **But** for a `regression`-tagged test that is a poor
fit:
- IG/bpftrace images are large (slow pulls, the suite is already pull-bound).
- Requires privileged pods + host mounts in KIND — flaky across runner kernels.
- `regression` tag contract (pyproject.toml): must pass 30+ iterations reliably,
  fast, no external deps. eBPF profiling output is non-deterministic and heavy.

**Recommendation for the eval strategy:**

| Capability | Sandbox | CI regression eval | Recommended test |
|---|---|---|---|
| Go `pprof` | ✅ runs | ✅ light, deterministic-ish | **Primary `regression` eval** (Phase 1). Inject a known hot function, assert Holmes names it via `include_tool_calls` + the function name. |
| `jattach`/async-profiler | ✅ runs | ✅ feasible | Eval tagged e.g. `easy`, not regression at first (JVM image weight + sampling variance). |
| `py-spy` | ✅ runs | ✅ feasible | Same as JVM. |
| eBPF (IG/bpftrace) | ❌ blocked | ⚠️ possible but heavy/flaky | **Not** `regression`. Tag `manual`/`network`-style opt-in eval; cover the toolset logic with **non-live unit tests** that mock `kubectl debug` output (the `responses`/subprocess-mock pattern). |
| Pyroscope/Parca query | ✅ (HTTP mock) | ✅ | Cloud-service-style eval with mocked HTTP, like elasticsearch evals. No kernel needed. |

The hallucination-proofing trick from CLAUDE.md applies well here: have
`before_test` deploy an app with a **uniquely named hot function** (e.g.
`compute_checkout_hash_v7x9`) and put that exact name in `expected_output` (which
the LLM never sees) — Holmes can only produce it by actually profiling.

---

## 7. Recommended phased roadmap

1. **Phase 1 — Go `pprof` toolset (Option B).** Lowest risk, no kernel deps,
   runs in sandbox *and* CI. Ship with a `regression` eval (unique-hot-function
   trick). Proves the ephemeral-debug-container plumbing end-to-end.
2. **Phase 2 — `jvm_diagnostics` (jattach + async-profiler).** Reuses Phase 1
   plumbing. Eval tagged non-regression initially; promote once stable over 30
   iterations.
3. **Phase 3 — `py-spy` (Python).** Same plumbing, small surface.
4. **Phase 4 — eBPF.** Extend `inspektor_gadget.yaml` with profiling gadgets
   (CPU/biolatency/runqlat) + optional vetted-`bpftrace` toolset + optional
   `pyroscope` HTTP query toolset. Unit-test with mocked `kubectl debug` output;
   eval is opt-in/`manual`, **not** regression, because the sandbox can't run it
   and CI regression can't keep it reliable.

Each new toolset also updates the five doc/index locations per CLAUDE.md
(README data-sources table, `why-holmesgpt.md`, builtin-toolsets index +
dedicated page, logo).

---

## 8. Open questions for @natan

1. **Distribution of the profiler images** — do we publish Holmes-owned debug
   images (jattach+asprof+pprof+py-spy bundled) to a registry, or require users
   to BYO and allow-list them? Affects air-gapped users.
2. **Robusta/SaaS deployment** — are privileged `kubectl debug node` and
   `pods/ephemeralcontainers` acceptable in the managed RBAC, or should eBPF be
   CLI-only and the SaaS path lean on the Pyroscope/Parca *query* toolset
   (Option C)?
3. **Approval gating** — should these privileged tools always require the
   interactive approval flow, given their blast radius?
4. **Continuous vs on-demand priority** — is the primary value "profile a live
   process right now" (Options A/B) or "query an existing continuous profiler"
   (Option C)? That decides whether Phase 4 leads with IG/bpftrace or Pyroscope.
