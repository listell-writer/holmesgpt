# Operator Mode Improvements — Event-Driven Triggers & End-to-End Flows

## Why this doc

Holmes Operator (`holmes_operator/`) today is a solid skeleton but feels half-baked
in practice: it can run a check **once on create** (`HealthCheck`) or **on a cron
schedule** (`ScheduledHealthCheck`), and that's it. Both are *pull/poll* models.

The most common thing a platform team actually wants — "investigate automatically
**when something happens**" (a deploy rolls out, a pod crashloops, an alert fires) —
has no first-class path. You can approximate it, but every approximation is awkward
(see "The deploy-rollout flow today" below). This doc diagnoses the gaps against
real user flows and proposes a phased plan, with **event-driven triggers** as the
headline.

## Current state (grounded in code)

- `HealthCheck` CRD → `@kopf.on.create` in `holmes_operator/handlers/healthcheck.py`
  runs the check immediately, calls the Holmes API (`/api/checks/execute`), writes
  pass/fail/error + rationale to status, optionally notifies Slack/PagerDuty when
  `mode: alert`.
- `ScheduledHealthCheck` CRD → `holmes_operator/handlers/scheduledhealthcheck.py`
  registers an APScheduler `CronTrigger` (`scheduler/manager.py`). On each tick,
  `scheduler/job_executor.py` *creates a `HealthCheck`* owned by the schedule and
  watches it to completion, keeping the last N runs in `status.history`.
- Triggering primitives available: **create-once** and **cron**. Nothing else.
- Modes: `alert`, `monitor` (`models.py:CheckMode`). No remediation.
- Scheduler state lives in `MemoryJobStore`; schedules are reloaded from CRDs on
  startup, but any in-flight/transient state is lost on restart. Single replica,
  no leader election.

## The core gap

**There is no event-driven trigger primitive.** Everything else compounds from this.
"Run an investigation when X happens" is the natural shape of most operator use
cases, and the operator can't express it.

### The deploy-rollout flow today (the user's example)

`docs/operator/deployment-verification.md` papers over the gap two ways, both awkward:

1. **Embed a `HealthCheck` in the app manifest.** Couples every app's manifests to
   Holmes, needs a uniquely-versioned check name per release (hand-templated in CI),
   and only fires if someone remembers to apply it. It does **not** fire on
   `kubectl set image`, HPA-driven template changes, GitOps drift sync, or rollbacks.
2. **Bash-poll `kubectl get hc ... -o jsonpath` in CI** to gate the pipeline.
   Brittle, and re-implements what the operator should provide natively.

The user has to wire the trigger themselves, per deploy, per repo. That's the
half-baked feeling.

## User flows & where each breaks

| Flow | What the user wants | Works today? |
|------|--------------------|--------------|
| **Deploy verification** | On every rollout of a service, wait for it to settle, compare health vs the previous version, alert/PR on regression | ❌ Manual per-deploy wiring; no auto-trigger on rollout |
| **Incident triage** | When an alert fires / a pod CrashLoops / a Job fails, auto-kick a root-cause investigation and attach findings | ❌ Only approximable by polling "are there alerts?" every N min — laggy, wasteful, fires LLM calls even when healthy |
| **Continuous monitoring** | Periodically sanity-check a service | ✅ `ScheduledHealthCheck` — but no reuse/templating, so N services = N near-identical YAMLs |
| **Remediation** | Don't just tell me — open a PR / roll back | ❌ `mode` is only `alert`/`monitor`, despite the docs advertising "open PRs to fix" |

## Proposed improvements

### 1. `HealthCheckTrigger` CRD — event-driven investigations (headline)

A CRD that declaratively binds **an event** to **a check**. On fire, it spawns a
`HealthCheck` (reusing the entire existing execution/history/notification path from
`job_executor.py`), with event context injected into the query template.

```yaml
apiVersion: holmesgpt.dev/v1alpha1
kind: HealthCheckTrigger
metadata:
  name: verify-checkout-rollouts
  namespace: production
spec:
  on:
    - type: DeploymentRollout          # fire when the pod template / image changes
      selector:
        matchLabels: { app: checkout-api }
  settle:                              # wait for the rollout to stabilize first
    waitFor: RolloutComplete           # or a fixed "120s"
    timeout: 300s
  check:
    query: |
      checkout-api was rolled out to {{ .new.image }} (was {{ .old.image }}).
      Compare error rates, latency, restarts, and logs before vs after the
      rollout and flag regressions.
    timeout: 120
    mode: alert
    destinations:
      - { type: slack, config: { channel: "#deploy-alerts" } }
  cooldown: 10m                        # don't re-fire for the same target in window
  dedupe: true
```

**Event sources, tiered:**

- *Phase 1 (K8s-native, kopf already watches these):*
  `DeploymentRollout` / `StatefulSetRollout` / `DaemonSetRollout` /
  `RolloutRollout` (Argo) — fire on pod-template/image change;
  `PodCrashLoop` / `PodOOMKilled` / `PodImagePullError`; `JobFailed`;
  `KubernetesWarningEvent` (generic, with a `reason` filter).
- *Phase 2:* `AlertManagerAlert` — the operator exposes a webhook receiver;
  AlertManager POSTs alerts to it; alert → check. This is the bridge to the
  incident-triage flow and is the single biggest unlock after rollout triggers.
- *Later:* `ResourceChange` — generic GVK + selector + jsonpath predicate.

**Implementation fit (low new surface):**

- kopf already provides `@kopf.on.create/update/field` for arbitrary resources, so a
  trigger controller can register watches per `HealthCheckTrigger`. Rollout detection
  = `@kopf.on.update` on Deployments comparing `spec.template` hash old→new.
- "settle" = reuse `SchedulerManager` with a one-shot `DateTrigger` (instead of
  `CronTrigger`) that fires when `status.updatedReplicas == replicas &&
  availableReplicas == replicas`, or on timeout.
- On fire, call the same `HealthCheck`-creation path `job_executor.py` already uses
  for scheduled runs, with `ownerReferences` back to the trigger.

### 2. Noise & cost control (mandatory for event-driven, not optional)

An event-driven LLM trigger without guardrails is a cost/DoS footgun (an alert storm
→ hundreds of LLM investigations). Required knobs:

- `cooldown` + `dedupe` per (trigger, target) — state persisted in trigger `status`
  so it survives operator restarts (today's `MemoryJobStore` would lose it).
- Rate limit per trigger / namespace / global; `maxConcurrentInvestigations`.
- **Global budget guard**: stop spawning checks once spend exceeds a threshold. The
  execution path already records usage (`request_type="health_check"`,
  `UsageRecorderState` in `checks_api.py`), so the guard has data to read.

### 3. Reuse via selectors + templates

Today, monitoring 50 services means 50 near-identical YAMLs. Two complementary fixes:

- **Selector fan-out**: one `HealthCheckTrigger`/`ScheduledHealthCheck` targets many
  resources via label selector, templating per-target context into the query.
- **`HealthCheckTemplate`** referenced via `templateRef` for shared query + timeout +
  mode + destinations (one "deploy verification" template reused everywhere).

### 4. Remediation, not just detection

Add `mode: remediate` (or an `actions:` block): on failure, let Holmes open a GitHub
PR (the GitHub MCP integration the docs already recommend) or run an allowlisted
action, gated by `approvalRequired`, with the action recorded in status. The product
already *promises* "open PRs to fix" in `docs/operator/index.md` — the operator just
has no first-class path for it yet.

### 5. Reliability & onboarding

- **Persist trigger state in CRD status** (cooldown/dedupe/settle) instead of memory.
- **HA**: enable kopf peering / leader election for a "24/7" product.
- **Ergonomics**: `holmes operator ...` CLI verbs to scaffold triggers and run a
  check locally without applying to the cluster; a Helm `values.yaml` block to
  declare starter triggers (e.g. "deploy verification for all Deployments in ns X")
  so day-1 value needs zero hand-written CRDs.

## The deploy-rollout flow, redesigned (one-time setup, zero per-deploy work)

1. Platform team applies **one** `HealthCheckTrigger` (or enables it via Helm values)
   selecting `app in (...)` or all Deployments in `production`.
2. Any rollout — CI, Argo, `kubectl set image`, rollback — updates the pod template.
3. The operator's rollout watcher detects the image/template change, records old→new
   in trigger status, and starts a settle timer.
4. When the new ReplicaSet is fully available (or on timeout), the operator spawns a
   `HealthCheck` with the templated query.
5. Holmes investigates with all connected toolsets; on regression → Slack/PagerDuty
   alert and (optionally) a GitHub PR to roll back or fix. Cooldown prevents
   re-firing on the same revision.
6. CI gating becomes `kubectl wait` (or `holmes operator wait`) on the auto-created
   HealthCheck for this revision — no bespoke bash poll.

The shift: from "remember to attach a check to each deploy" to "declare once, fires
on every rollout automatically."

## Phased roadmap

- **Phase 1 — `HealthCheckTrigger` with K8s event sources** (highest leverage,
  smallest surface). DeploymentRollout + PodCrashLoop + JobFailed + generic
  WarningEvent; settle/cooldown/dedupe with state in status; spawn via the existing
  HealthCheck path. Docs with the deploy-rollout flow as the flagship example.
- **Phase 2 — AlertManager webhook receiver** (incident triage) + global
  budget/rate guard.
- **Phase 3 — `HealthCheckTemplate` + selector fan-out** (reuse) and
  `mode: remediate` with GitHub PR + approval gating.
- **Phase 4 — HA** (leader election), CLI ergonomics, Helm-declared starter triggers.

## Open design questions

- **CRD shape**: new `HealthCheckTrigger` (stays in the HealthCheck family, minimal
  change) vs. a more general `Investigation` + `InvestigationTrigger` rename. This
  doc assumes the former.
- **Settle semantics** for non-Deployment kinds (Argo Rollouts, StatefulSets) —
  per-kind readiness predicates.
- **Budget guard scope** — per-namespace vs. global, and behaviour on breach
  (skip silently, alert, or both).
</content>
