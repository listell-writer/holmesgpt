# SigNoz (MCP)

The [SigNoz MCP server](https://github.com/SigNoz/signoz-mcp-server) gives Holmes access to logs, traces, metrics, alerts, and dashboards on your SigNoz instance. It works with both SigNoz Cloud and self-hosted SigNoz.

## Prerequisites

- A SigNoz instance (Cloud or self-hosted) at version **v0.55+**
- A **service-account API key** with at least the **Viewer** role
  (Settings → Service Accounts → create service account → assign role → generate key)
- Access to the SigNoz MCP server — one of:
    - The Go binary from [github.com/SigNoz/signoz-mcp-server](https://github.com/SigNoz/signoz-mcp-server/releases)
    - The Docker image `signoz/signoz-mcp-server:latest`
    - The hosted Cloud endpoint `https://mcp.<region>.signoz.cloud/mcp` (SigNoz Cloud only)

## Configuration

=== "Holmes CLI (stdio, self-hosted MCP binary)"

    Install the binary once on your machine:

    ```bash
    go install github.com/SigNoz/signoz-mcp-server/cmd/server@latest
    # or download a release tarball:
    #   curl -L https://github.com/SigNoz/signoz-mcp-server/releases/latest/download/signoz-mcp-server_linux_amd64.tar.gz | tar xz
    ```

    Add to **~/.holmes/config.yaml**:

    ```yaml
    mcp_servers:
      signoz:
        description: "SigNoz observability (logs, traces, metrics, alerts, dashboards)"
        config:
          mode: stdio
          command: "/home/you/go/bin/server"        # path to the installed binary
          env:
            SIGNOZ_URL: "{{ env.SIGNOZ_URL }}"      # e.g. https://yourtenant.us.signoz.cloud
            SIGNOZ_API_KEY: "{{ env.SIGNOZ_API_KEY }}"
            LOG_LEVEL: "error"
            OTEL_SDK_DISABLED: "true"               # suppress the binary's own OTel exporter
          icon_url: "https://cdn.simpleicons.org/signoz/e75536"
        llm_instructions: |
          Use SigNoz to investigate production incidents:
            1. For log errors, call `signoz_search_logs` with a `query` (free-text), `service`, and optional `severity`.
            2. For trace/latency issues, call `signoz_search_traces` or `signoz_get_trace_details`.
            3. For "what is slow?" questions, start with `signoz_list_services` then `signoz_get_service_top_operations`.
            4. For alerts, call `signoz_list_alerts` and `signoz_get_alert_history`.
    ```

    Then export the credentials in your shell:

    ```bash
    export SIGNOZ_URL="https://yourtenant.us.signoz.cloud"
    export SIGNOZ_API_KEY="your-service-account-api-key"
    ```

    --8<-- "snippets/toolset_refresh_warning.md"

=== "Holmes CLI (streamable-http, SigNoz Cloud)"

    SigNoz Cloud hosts the MCP server at `https://mcp.<region>.signoz.cloud/mcp`.
    Pick the region that matches your tenant (`us`, `us2`, `eu`, `in`).

    Add to **~/.holmes/config.yaml**:

    ```yaml
    mcp_servers:
      signoz:
        description: "SigNoz observability (logs, traces, metrics, alerts, dashboards)"
        config:
          mode: streamable-http
          url: "https://mcp.us2.signoz.cloud/mcp"
          extra_headers:
            Authorization: "Bearer {{ env.SIGNOZ_API_KEY }}"
          icon_url: "https://cdn.simpleicons.org/signoz/e75536"
    ```

=== "Holmes Helm Chart"

    Run the SigNoz MCP server in HTTP mode as a sidecar in your cluster, then point Holmes at it.

    **Create the secret:**

    ```bash
    kubectl create secret generic signoz-mcp-credentials \
      --from-literal=SIGNOZ_URL="https://yourtenant.us2.signoz.cloud" \
      --from-literal=SIGNOZ_API_KEY="your-service-account-api-key" \
      -n holmes-mcp
    ```

    **Deploy the MCP server:**

    ```yaml
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: signoz-mcp-server
      namespace: holmes-mcp
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: signoz-mcp-server
      template:
        metadata:
          labels:
            app: signoz-mcp-server
        spec:
          containers:
            - name: signoz-mcp
              image: signoz/signoz-mcp-server:latest
              env:
                - name: TRANSPORT_MODE
                  value: "http"
                - name: MCP_SERVER_PORT
                  value: "8000"
                - name: SIGNOZ_URL
                  valueFrom:
                    secretKeyRef: {name: signoz-mcp-credentials, key: SIGNOZ_URL}
                - name: SIGNOZ_API_KEY
                  valueFrom:
                    secretKeyRef: {name: signoz-mcp-credentials, key: SIGNOZ_API_KEY}
                - name: OTEL_SDK_DISABLED
                  value: "true"
              ports:
                - containerPort: 8000
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: signoz-mcp-server
      namespace: holmes-mcp
    spec:
      selector:
        app: signoz-mcp-server
      ports:
        - port: 8000
          targetPort: 8000
    ```

    **Holmes values:**

    ```yaml
    mcp_servers:
      signoz:
        description: "SigNoz observability"
        config:
          url: "http://signoz-mcp-server.holmes-mcp.svc.cluster.local:8000/mcp"
          mode: streamable-http
          icon_url: "https://cdn.simpleicons.org/signoz/e75536"
    ```

## Testing the Connection

```bash
holmes ask "List the services reporting to SigNoz in the last hour"
```

## Common Use Cases

```bash
holmes ask "Search SigNoz for ERROR logs from payment-api in the last 30 minutes and summarize the top failure modes"
```

```bash
holmes ask "Show me the slowest operations in checkout-service over the last hour"
```

```bash
holmes ask "What SigNoz alerts are currently firing, and what's the history for the top one?"
```

```bash
holmes ask "Find traces where the database span took longer than 2s and tell me which service is responsible"
```

## Additional Resources

- [SigNoz MCP server repository](https://github.com/SigNoz/signoz-mcp-server)
- [SigNoz MCP server docs](https://signoz.io/docs/ai/signoz-mcp-server/)
- [SigNoz service accounts & API keys](https://signoz.io/docs/userguide/service-accounts/)
