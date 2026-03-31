# Deployment Verification

A common pattern is deploying a HealthCheck alongside your application to verify the new version is working correctly. HealthChecks run automatically on creation, and **re-run automatically whenever you change the spec** (e.g., updating the query to reference a new image tag). This means you can include a HealthCheck in the same manifest as your deployment and it will re-execute on every `kubectl apply` that changes the spec.

## One-Time Verification with HealthCheck

Include a [HealthCheck](health-checks.md) in the same manifest as your deployment. Update the query to reference the new version — this bumps `metadata.generation` and triggers automatic re-execution.

```yaml
# app-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-api
  namespace: production
spec:
  replicas: 3
  selector:
    matchLabels:
      app: checkout-api
  template:
    metadata:
      labels:
        app: checkout-api
    spec:
      containers:
        - name: checkout-api
          image: myregistry/checkout-api:v2.4.1
---
apiVersion: holmesgpt.dev/v1alpha1
kind: HealthCheck
metadata:
  name: checkout-api-deploy-check
  namespace: production
  labels:
    app: checkout-api
spec:
  query: "Is the checkout-api deployment in 'production' fully rolled out with all replicas ready and not crash-looping? Check that pods are running image v2.4.1 and responding without errors."
  timeout: 120
  mode: alert
  destinations:
    - type: slack
      config:
        channel: "#deploy-alerts"
```

Apply both together:

```bash
kubectl apply -f app-deployment.yaml
```

On the first deploy, the HealthCheck is created and runs immediately. On subsequent deploys, update the query (e.g., change `v2.4.1` to `v2.4.2`) — the operator detects the spec change and re-runs the check automatically. If pods crash or fail readiness, the check fails and alerts your team.

## Gating CI/CD on the Result

After applying, poll for the result to gate your pipeline:

```bash
# Wait for the check to complete, then read the result
for i in $(seq 1 30); do
  RESULT=$(kubectl get hc checkout-api-deploy-v2-4-1 -n production -o jsonpath='{.status.result}' 2>/dev/null)
  if [ "$RESULT" = "pass" ]; then
    echo "Deploy verified healthy"
    exit 0
  elif [ "$RESULT" = "fail" ] || [ "$RESULT" = "error" ]; then
    echo "Deploy check failed:"
    kubectl get hc checkout-api-deploy-v2-4-1 -n production -o jsonpath='{.status.message}'
    exit 1
  fi
  sleep 10
done
echo "Timed out waiting for health check"
exit 1
```

## Ongoing Monitoring with ScheduledHealthCheck

One-time deploy checks catch immediate failures, but some problems only appear later — memory leaks, connection pool exhaustion, gradual performance degradation. [Scheduled Health Checks](scheduled-health-checks.md) run on a cron schedule to catch these regressions automatically.

## Tips for Deployment HealthChecks

- **Use a fixed name, update the query.** The operator re-runs the check whenever the spec changes, so you don't need a unique name per deploy. Just reference the new version in the query (e.g., `"...running image v2.4.2..."`) and `kubectl apply` will trigger re-execution automatically.
- **For audit trails**, you can still use versioned names (e.g., `checkout-api-deploy-v2-4-1`) so each deploy creates a distinct resource. This is optional — use it when you want historical results to persist side by side.
- **Set a longer timeout** (60–120s) to give the rollout time to complete before Holmes evaluates.
- **Use labels** like `deploy-version` to query checks for a specific release: `kubectl get hc -l deploy-version=v2.4.1`.
- **Force re-run without spec changes:** If you need to re-run the exact same check, use `kubectl annotate hc/checkout-api-deploy-check holmesgpt.dev/rerun=true`. The annotation is cleared automatically after execution.
- **Combine with ArgoCD**: If you use ArgoCD, the query can reference sync status — e.g., *"Is the ArgoCD application 'checkout-api' synced and healthy with no degraded resources?"* — since Holmes has access to the [ArgoCD toolset](../data-sources/builtin-toolsets/argocd.md).
