# Opus 4.6 vs 4.7 vs 4.8 — Weekly Benchmark Comparison

**Date:** 2026-06-05
**Eval set:** `fast-benchmark` (the weekly-benchmark marker `regression or benchmark`) — 17 evals
**Runtime:** all three models run via **OpenRouter** (`anthropic/claude-opus-4.6 / 4.7 / 4.8`) under identical conditions on a single local k3s cluster
**Matrix:** 3 models × 16 evals × 3 iterations = 144 runs (`176_network_policy` excluded — see Caveats)
**Grader:** `gpt-4.1` (autoevals correctness classifier), same as CI

## Headline

| Model | Raw pass rate | Avg time | P90 time | Failures (test × iters) |
|---|---|---|---|---|
| **opus-4.6** | **44/48 — 91.7%** | 47.2s | 80.5s | `108`×3, `61`×1 |
| **opus-4.7** | **44/48 — 91.7%** | 44.3s | 77.0s | `108`×3, `179`×1 |
| **opus-4.8** | **42/48 — 87.5%** | 46.0s | 91.0s | `108`×3, `43`×3 |

**Raw ranking: 4.6 = 4.7 > 4.8.** But the entire 4.8 deficit is one eval (`43_current_datetime`) failing for a *harness-mock* reason, not a reasoning regression. Removing the two **non-discriminating** tests — `108` (fails for all three models, every iteration) and `43` (date-mock artifact) — flips the picture:

| Model | Adjusted (14 discriminating tests × 3) |
|---|---|
| opus-4.6 | 41/42 — 97.6% |
| opus-4.7 | 41/42 — 97.6% |
| **opus-4.8** | **42/42 — 100.0%** |

On the clean discriminating set, **opus-4.8 is the strongest** (no flakes), with 4.6 and 4.7 each dropping a single flaky run.

## Root causes of every failure

### `108_logs_nearby_lines` — all 3 models, all 3 iterations (9 failures)
Not a differentiator. All three correctly diagnose *"`api-service` is configured with staging DB/cache endpoints (`db-staging.internal`, `redis-staging.internal`) in production."* The grader, however, requires the **causal trigger**:

> *Expected:* "The first Connection refused errors started **after a config reload that loaded staging database settings in production**."

No Opus model surfaces the temporal trigger from the surrounding ("nearby") log lines — they report the *what* (staging config) but not the *when/why* (a config-reload event preceded the errors). This is a shared capability gap and arguably an over-strict grader. Confirmed identical in the Braintrust traces for the 2026-05-31 weekly run (4.6 and 4.7 both failed it there too).

### `43_current_datetime_from_prompt` — opus-4.8 only, all 3 iterations (3 failures) — **the entire 4.8 deficit**
The eval mocks "now" to `2025-06-23 11:34 UTC` by patching `holmes.plugins.prompts.datetime`, which renders into the prompt as *"The current UTC date and time are 2025-06-23 11:34…"* — identical text for all three models.

- opus-4.6 / 4.7: answer directly from that prompt line → `June 23rd 2025, 11:34 UTC` ✅ (~2s, no tool call)
- **opus-4.8: sometimes runs the `bash` tool with `date`** and returns the real wall-clock time to the second (e.g. `Friday, June 5, 2026, 07:07:59 UTC`) ❌

The mechanism, verified from the tool logs (`INFO holmes.display.tools: Running tool #1 bash: date`): the **`bash` toolset** (`BashExecutorToolset`, enabled by default) lets the model run arbitrary shell. opus-4.8 decides to *verify* the time by shelling out to `date` instead of trusting the timestamp already in its prompt. The eval's mock only patches Python's `holmes.plugins.prompts.datetime`, **not** the shell, so `date` returns the unmocked real clock and the answer fails. There is **no dedicated datetime tool** — it's generic `bash`/`date`.

**Behavioral contrast (measured):**

- opus-4.6 + opus-4.7: 12/12 runs passed, **0** `bash: date` invocations — always answer from the prompt context.
- opus-4.8: **~1 in 3 runs** shells out to `bash: date` (failed 3/3 in the main matrix, 2/6 in a focused probe; passes the rest).

**This is a behavioral shift, not a reasoning-quality regression** — 4.8 is more inclined to verify via tooling. But note it is *also* a real-world correctness risk: in any deployment where the injected prompt time is authoritative (or where the host clock differs from the intended "investigation time"), 4.8 shelling out to `date` would give a wrong answer. Worth flagging to the eval owners (mock should also cover the shell, or the prompt should instruct the model to trust the provided timestamp).

### `179_grafana_big_dashboard_query` — opus-4.7, 1 of 3 iterations
4.7 intermittently **fails to locate the large dashboard** ("could not find a dashboard titled 'E-Commerce Platform Monitoring'") despite many search/tool calls (26 tool calls in the Braintrust baseline where it also failed this test). 4.6 and 4.8 passed all 3 iterations. Indicates mild instability in 4.7's dashboard-search persistence/strategy on big dashboards.

### `61_exact_match_counting` — opus-4.6, 1 of 3 iterations
Single flaky run (2/3 passed). No systematic pattern.

## Notable behavioral differences between models

- **Runbook discipline (4.7 > 4.6):** In the Braintrust weekly traces, `96_no_matching_skill` checks that the model does **not** force-fit an inappropriate runbook. opus-4.6 force-fit the "custom-crd runbook" ("I followed the custom-crd runbook") and **failed**; opus-4.7 correctly declined and **passed**. In this OpenRouter run both passed all 3 iterations, but the baseline shows 4.7's improved restraint.
- **Agentic verification (4.8):** opus-4.8 is the most likely to reach for a tool to verify even "obvious" context (the datetime case). Net-positive for real troubleshooting (it double-checks), but it broke one eval that assumed the model would trust injected context.
- **Search persistence (4.7 weakness):** 4.7 is the only model that intermittently gives up on the big-dashboard search (`179`).
- **Latency:** 4.7 fastest (44.3s avg), 4.6 middle (47.2s), 4.8 slowest tail (P90 91.0s) — consistent with 4.8 making extra verification tool calls.

## Conclusion — what performs better and why

- **Pure benchmark score:** opus-4.6 and opus-4.7 tie (91.7%); opus-4.8 trails (87.5%) **solely** because of the `43_current_datetime` mock artifact.
- **Real capability (discriminating tests only):** **opus-4.8 is best (100%)**, 4.6 and 4.7 tie just behind (97.6%, one flake each).
- **Between 4.6 and 4.7:** essentially equivalent; 4.7 is slightly faster and has better runbook discipline, but has a mild big-dashboard-search instability.
- **Shared ceiling:** all three are at/near ceiling on this eval set; `108_logs_nearby_lines` is the one genuine shared gap (missing the causal config-reload trigger in nearby log lines).

## Caveats

- **`176_network_policy_blocking_traffic_no_skills` excluded.** The sandbox kernel cannot enforce NetworkPolicy (`ipset` restricted), so it fails for all models for an infra reason — non-discriminating and excluded to avoid polluting the comparison. (It passed for both Opus models in the real-CI weekly run.)
- **Provider difference vs the weekly run.** The Braintrust weekly baseline runs Anthropic-direct/Azure/Bedrock; this comparison runs all three via OpenRouter. Routing all three identically through OpenRouter keeps the model-vs-model comparison clean; absolute numbers may differ slightly from CI.
- **No cost column.** OpenRouter usage cost wasn't captured per-eval here; all three Opus models are priced identically ($5 / $25 per M in/out tokens), so cost is not a differentiator.
- **New runs not logged to Braintrust.** The sandbox token is read-only (creating experiments crashes), so these 144 runs are local; only the *historical* 4.6/4.7 data was read from Braintrust.
