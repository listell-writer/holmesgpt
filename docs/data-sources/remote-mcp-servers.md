# MCP Servers

HolmesGPT can integrate with MCP (Model Context Protocol) servers to access external data sources and tools in real time.

## Transport Modes

HolmesGPT supports three MCP transport modes:

1. **`streamable-http`** (Recommended): Modern HTTP-based transport. Use this for new integrations.
2. **`stdio`**: Direct process communication via standard input/output. Supported directly in CLI; supported on Kubernetes via [Supergateway](https://github.com/supercorp-ai/supergateway).
3. **`sse`** (Deprecated): Legacy Server-Sent Events transport. Use `streamable-http` instead.

## Streamable-HTTP (Recommended)

=== "Holmes CLI"

    Add to `~/.holmes/config.yaml`:

    ```yaml
    mcp_servers:
      dynatrace:
        description: "Dynatrace observability platform"
        config:
          url: "http://dynatrace-mcp:8000/mcp/messages"
          mode: streamable-http
          headers:
            Authorization: "Bearer {{ env.DYNATRACE_API_KEY }}"
          icon_url: "https://cdn.simpleicons.org/dynatrace/1496FF"  # Optional: icon for UI
        # llm_instructions tells Holmes WHEN and HOW to use this server
        llm_instructions: "Use Dynatrace to investigate application performance issues, analyze distributed traces, and query infrastructure metrics. Prefer this over Prometheus for APM data."
    ```

    ```bash
    holmes ask "What services have high error rates in Dynatrace?"
    ```

=== "Holmes Helm Chart"

    Add to your Helm values:

    ```yaml
    additionalEnvVars:
      - name: DYNATRACE_API_KEY
        valueFrom:
          secretKeyRef:
            name: mcp-credentials
            key: api_key

    mcp_servers:
      dynatrace:
        description: "Dynatrace observability platform"
        config:
          url: "http://dynatrace-mcp:8000/mcp/messages"
          mode: streamable-http
          headers:
            Authorization: "Bearer {{ env.DYNATRACE_API_KEY }}"
          icon_url: "https://cdn.simpleicons.org/dynatrace/1496FF"  # Optional: icon for UI
        # llm_instructions tells Holmes WHEN and HOW to use this server
        llm_instructions: "Use Dynatrace to investigate application performance issues, analyze distributed traces, and query infrastructure metrics. Prefer this over Prometheus for APM data."
    ```

    ```bash
    helm upgrade holmes robusta/holmes --values=values.yaml
    ```

=== "Robusta Helm Chart"

    Add to your `generated_values.yaml`:

    ```yaml
    holmes:
      additionalEnvVars:
        - name: DYNATRACE_API_KEY
          valueFrom:
            secretKeyRef:
              name: mcp-credentials
              key: api_key

      mcp_servers:
        dynatrace:
          description: "Dynatrace observability platform"
          config:
            url: "http://dynatrace-mcp:8000/mcp/messages"
            mode: streamable-http
            headers:
              Authorization: "Bearer {{ env.DYNATRACE_API_KEY }}"
            icon_url: "https://cdn.simpleicons.org/dynatrace/1496FF"  # Optional: icon for UI
          # llm_instructions tells Holmes WHEN and HOW to use this server
          llm_instructions: "Use Dynatrace to investigate application performance issues, analyze distributed traces, and query infrastructure metrics. Prefer this over Prometheus for APM data."
    ```

    ```bash
    helm upgrade robusta robusta/robusta --values=generated_values.yaml --set clusterName=<YOUR_CLUSTER_NAME>
    ```

The URL path depends on your MCP server (e.g., `/mcp/messages`, `/mcp`, or a custom path). Check your server's documentation.

## Stdio

Stdio mode runs MCP servers as subprocesses, communicating via standard input/output.

=== "Holmes CLI"

    Add to `~/.holmes/config.yaml`:

    ```yaml
    mcp_servers:
      ticket_db:
        description: "Internal ticket database"
        config:
          mode: stdio
          command: "python3"
          args:
            - "/path/to/my_mcp_server.py"
          env:
            CUSTOM_VAR: "value"
        # llm_instructions tells Holmes WHEN and HOW to use this server
        llm_instructions: "Use this server to query the internal ticket database. Search for related incidents by error message or service name."
    ```

    ```bash
    holmes ask "Find tickets related to payment service errors"
    ```

    Ensure required dependencies (e.g., `mcp`, `fastmcp` packages) are installed in your environment.

=== "Holmes Helm Chart"

    !!! warning "Stdio requires Supergateway for Kubernetes"
        Stdio mode cannot run directly in the Holmes container due to missing dependencies. Run your stdio MCP server in a separate pod using [Supergateway](https://github.com/supercorp-ai/supergateway) to expose it as HTTP.

    **Create a Docker image with your MCP server:**

    ```dockerfile
    FROM supercorp/supergateway:latest

    USER root
    # Install your MCP server dependencies
    # Example: RUN apk add --no-cache python3 py3-pip
    # Example: RUN pip3 install --no-cache-dir --break-system-packages your-mcp-package
    USER node

    EXPOSE 8000
    # Replace with your MCP server command. Examples:
    #   CMD ["--port", "8000", "--stdio", "python3", "-m", "your_mcp_module"]
    #   CMD ["--port", "8000", "--stdio", "python3", "/app/stdio_server.py"]
    #   CMD ["--port", "8000", "--stdio", "npx", "-y", "@your-org/your-mcp-server@latest"]
    CMD ["--port", "8000", "--stdio", "python3", "-m", "your_mcp_module"]
    ```

    **Deploy the MCP server pod:**

    ```yaml
    apiVersion: v1
    kind: Pod
    metadata:
      name: ticket-db-mcp
      labels:
        app: ticket-db-mcp
    spec:
      containers:
        - name: supergateway
          image: your-registry/your-mcp-server:latest
          ports:
            - containerPort: 8000
          args:
            - "--stdio"
            # Replace with your MCP server command
            # Examples: "python3 -m your_mcp_module", "python3 /app/stdio_server.py", "npx -y @your-org/your-mcp-server@latest"
            - "python3 -m your_mcp_module"
            - "--port"
            - "8000"
            - "--logLevel"
            - "debug"
          env:
            - name: API_KEY
              valueFrom:
                secretKeyRef:
                  name: mcp-credentials
                  key: api_key
          stdin: true
          tty: true
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: ticket-db-mcp
    spec:
      selector:
        app: ticket-db-mcp
      ports:
        - protocol: TCP
          port: 8000
          targetPort: 8000
      type: ClusterIP
    ```

    **Connect Holmes to the MCP server:**

    ```yaml
    mcp_servers:
      ticket_db:
        description: "Internal ticket database"
        config:
          url: "http://ticket-db-mcp.default.svc.cluster.local:8000/sse"
          mode: sse
        # llm_instructions tells Holmes WHEN and HOW to use this server
        llm_instructions: "Use this server to query the internal ticket database. Search for related incidents by error message or service name."
    ```

    ```bash
    helm upgrade holmes robusta/holmes --values=values.yaml
    ```

=== "Robusta Helm Chart"

    !!! warning "Stdio requires Supergateway for Kubernetes"
        Stdio mode cannot run directly in the Holmes container due to missing dependencies. Run your stdio MCP server in a separate pod using [Supergateway](https://github.com/supercorp-ai/supergateway) to expose it as HTTP.

    **Create a Docker image with your MCP server:**

    ```dockerfile
    FROM supercorp/supergateway:latest

    USER root
    # Install your MCP server dependencies
    # Example: RUN apk add --no-cache python3 py3-pip
    # Example: RUN pip3 install --no-cache-dir --break-system-packages your-mcp-package
    USER node

    EXPOSE 8000
    # Replace with your MCP server command. Examples:
    #   CMD ["--port", "8000", "--stdio", "python3", "-m", "your_mcp_module"]
    #   CMD ["--port", "8000", "--stdio", "python3", "/app/stdio_server.py"]
    #   CMD ["--port", "8000", "--stdio", "npx", "-y", "@your-org/your-mcp-server@latest"]
    CMD ["--port", "8000", "--stdio", "python3", "-m", "your_mcp_module"]
    ```

    **Deploy the MCP server pod:**

    ```yaml
    apiVersion: v1
    kind: Pod
    metadata:
      name: ticket-db-mcp
      labels:
        app: ticket-db-mcp
    spec:
      containers:
        - name: supergateway
          image: your-registry/your-mcp-server:latest
          ports:
            - containerPort: 8000
          args:
            - "--stdio"
            # Replace with your MCP server command
            # Examples: "python3 -m your_mcp_module", "python3 /app/stdio_server.py", "npx -y @your-org/your-mcp-server@latest"
            - "python3 -m your_mcp_module"
            - "--port"
            - "8000"
            - "--logLevel"
            - "debug"
          env:
            - name: API_KEY
              valueFrom:
                secretKeyRef:
                  name: mcp-credentials
                  key: api_key
          stdin: true
          tty: true
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: ticket-db-mcp
    spec:
      selector:
        app: ticket-db-mcp
      ports:
        - protocol: TCP
          port: 8000
          targetPort: 8000
      type: ClusterIP
    ```

    **Connect Holmes to the MCP server:**

    ```yaml
    holmes:
      mcp_servers:
        ticket_db:
          description: "Internal ticket database"
          config:
            url: "http://ticket-db-mcp.default.svc.cluster.local:8000/sse"
            mode: sse
          # llm_instructions tells Holmes WHEN and HOW to use this server
          llm_instructions: "Use this server to query the internal ticket database. Search for related incidents by error message or service name."
    ```

    ```bash
    helm upgrade robusta robusta/robusta --values=generated_values.yaml --set clusterName=<YOUR_CLUSTER_NAME>
    ```

## SSE (Deprecated)

SSE transport is deprecated. Use `streamable-http` for new integrations.

=== "Holmes CLI"

    Add to `~/.holmes/config.yaml`:

    ```yaml
    mcp_servers:
      legacy_analytics:
        description: "Legacy analytics platform (SSE transport)"
        config:
          url: "http://analytics-mcp:8000/sse"
          mode: sse
        llm_instructions: "Query historical analytics data. Use for trend analysis over periods longer than 30 days."
    ```

=== "Holmes Helm Chart"

    ```yaml
    mcp_servers:
      legacy_analytics:
        description: "Legacy analytics platform (SSE transport)"
        config:
          url: "http://analytics-mcp:8000/sse"
          mode: sse
        llm_instructions: "Query historical analytics data. Use for trend analysis over periods longer than 30 days."
    ```

=== "Robusta Helm Chart"

    ```yaml
    holmes:
      mcp_servers:
        legacy_analytics:
          description: "Legacy analytics platform (SSE transport)"
          config:
            url: "http://analytics-mcp:8000/sse"
            mode: sse
          llm_instructions: "Query historical analytics data. Use for trend analysis over periods longer than 30 days."
    ```

The URL should end with `/sse`. If it doesn't, HolmesGPT will automatically append it.

## OAuth Authentication

MCP servers that require OAuth can be configured with the `oauth` block. When Holmes encounters an OAuth-protected MCP server, it prompts the user to authenticate via their browser. After authentication, Holmes exchanges the authorization code for an access token and caches it for the duration of the conversation.

!!! warning "Authorization URL must be browser-accessible"
    The `authorization_url` is opened in the user's browser for login. If it points to a cluster-internal service (e.g., `http://keycloak.default.svc.cluster.local:8080/...`), the user won't be able to reach it. Use an externally-accessible URL (via Ingress, LoadBalancer, or port-forward) for `authorization_url`. The `token_url` can be cluster-internal since Holmes calls it server-side.

=== "Holmes CLI"

    Add to `~/.holmes/config.yaml`:

    ```yaml
    mcp_servers:
      my_secure_server:
        description: "OAuth-protected MCP server"
        config:
          url: "http://my-mcp-server:8000"
          mode: streamable-http
          oauth:
            authorization_url: "https://idp.example.com/authorize"
            token_url: "http://keycloak.default.svc.cluster.local:8080/realms/my-realm/protocol/openid-connect/token"
            client_id: "holmes-client"
            scopes:
              - "mcp:tools"
        llm_instructions: "Use this server for secure data queries."
    ```

=== "Holmes Helm Chart"

    ```yaml
    mcp_servers:
      my_secure_server:
        description: "OAuth-protected MCP server"
        config:
          url: "http://my-mcp-server:8000"
          mode: streamable-http
          oauth:
            authorization_url: "https://idp.example.com/authorize"
            token_url: "http://keycloak.default.svc.cluster.local:8080/realms/my-realm/protocol/openid-connect/token"
            client_id: "holmes-client"
            scopes:
              - "mcp:tools"
        llm_instructions: "Use this server for secure data queries."
    ```

=== "Robusta Helm Chart"

    ```yaml
    holmes:
      mcp_servers:
        my_secure_server:
          description: "OAuth-protected MCP server"
          config:
            url: "http://my-mcp-server:8000"
            mode: streamable-http
            oauth:
              authorization_url: "https://idp.example.com/authorize"
              token_url: "http://keycloak.default.svc.cluster.local:8080/realms/my-realm/protocol/openid-connect/token"
              client_id: "holmes-client"
              scopes:
                - "mcp:tools"
          llm_instructions: "Use this server for secure data queries."
    ```

**OAuth configuration fields:**

| Field | Description |
|-------|-------------|
| `authorization_url` | IdP authorization endpoint (must be browser-accessible) |
| `token_url` | IdP token endpoint (can be cluster-internal) |
| `client_id` | OAuth client ID registered with your IdP |
| `scopes` | List of OAuth scopes to request |

**How it works:**

1. Holmes detects the MCP server requires OAuth and prompts the user to authenticate
2. The user's browser opens the `authorization_url` for login
3. After login, the authorization code is encrypted and sent back to Holmes
4. Holmes exchanges the code for an access token at the `token_url` (server-side, within the cluster)
5. The access token is cached for the conversation and used for all subsequent MCP requests

The access token never leaves the cluster. Only the authorization code transits through the browser, encrypted with a one-time RSA key.

## Advanced Configuration

**Dynamic Headers with Request Context**

MCP servers can forward HTTP headers from the incoming request to the MCP backend. Use `extra_headers` with Jinja2 templates referencing `request_context.headers`. Header lookups are case-insensitive. You can also use environment variables (`{{ env.MY_VAR }}`) or combine them (`Bearer {{ request_context.headers['token'] }}`).

=== "Holmes CLI"

    Not applicable — request context is only available when running Holmes as a server.

=== "Holmes Helm Chart"

    ```yaml
    mcp_servers:
      customer_data:
        description: "Customer data API (requires per-request auth)"
        config:
          url: "http://customer-api:8000/mcp"
          mode: streamable-http
          extra_headers:
            X-Auth-Token: "{{ request_context.headers['X-Auth-Token'] }}"
        llm_instructions: "Query customer account details and subscription status. Use when investigating user-reported issues."
    ```

=== "Robusta Helm Chart"

    ```yaml
    holmes:
      mcp_servers:
        customer_data:
          description: "Customer data API (requires per-request auth)"
          config:
            url: "http://customer-api:8000/mcp"
            mode: streamable-http
            extra_headers:
              X-Auth-Token: "{{ request_context.headers['X-Auth-Token'] }}"
          llm_instructions: "Query customer account details and subscription status. Use when investigating user-reported issues."
    ```

For full details on template syntax, blocked headers, precedence rules, and examples for other toolset types, see [HTTP Header Propagation](header-propagation.md).

## Configuration Format Migration

The MCP server configuration format has been updated. The `url` field must now be inside the `config` section.

**Old format (deprecated):**

```yaml
mcp_servers:
  my_server:
    url: "http://example.com:8000/mcp/messages"
    description: "My server"
```

**New format:**

```yaml
mcp_servers:
  my_server:
    description: "My server"
    config:
      url: "http://example.com:8000/mcp/messages"
      mode: streamable-http
```

The old format still works but will log a migration warning.
