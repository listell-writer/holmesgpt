# PagerDuty

By enabling this toolset, HolmesGPT can query PagerDuty incidents, services, on-call schedules, escalation policies, and log entries. This is useful during alert triage — Holmes can pull the current on-call rotation, find related incidents, or read the notes on an incident without leaving its investigation loop.

The toolset is a thin wrapper around the PagerDuty REST API. Responses are returned verbatim so the LLM sees the full API surface.

## Configuration

**Create a User API Token:**

In PagerDuty, go to the top-right avatar → **My Profile** → **User Settings** tab → **API Access** → **Create API User Token**. Copy the token immediately (PagerDuty shows it only once).

The token inherits the owning user's role. For read-only investigation, any role works. If you enable write operations (see below), the user must be **Responder, Manager, Admin, or Account Owner** — Observers cannot modify incidents.

=== "Holmes CLI"

    Add to your config file (`~/.holmes/config.yaml`):

    ```yaml
    toolsets:
      pagerduty:
        enabled: true
        config:
          api_key: "your-user-api-token"
          region: "us"          # or "eu" for EU PagerDuty accounts
    ```

    To test, run:

    ```bash
    holmes ask "show me the open PagerDuty incidents from the last 24 hours"
    ```

    --8<-- "snippets/toolset_refresh_warning.md"

=== "Robusta Helm Chart"

    ```yaml
    holmes:
      additionalEnvVars:
        - name: PAGERDUTY_USER_API_KEY
          valueFrom:
            secretKeyRef:
              name: pagerduty-credentials
              key: api-key
      toolsets:
        pagerduty:
          enabled: true
          config:
            api_key: "{{ env.PAGERDUTY_USER_API_KEY }}"
            region: "us"
    ```

    --8<-- "snippets/helm_upgrade_command.md"

### Configuration fields

| Field | Required | Default | Description |
|---|---|---|---|
| `api_key` | yes | — | User API Token. |
| `region` | no | `us` | `us` for `api.pagerduty.com`, `eu` for `api.eu.pagerduty.com`. |
| `enable_write` | no | `false` | Allow POST/PUT to a curated set of incident-management endpoints (see below). |
| `from_email` | no | — | Email of the token owner. Sent as `From:` header. Required if `enable_write` is true — PagerDuty requires it for `POST /incidents` and `POST /incidents/{id}/notes`. |

### Enabling write operations

By default, only `GET` requests are allowed. To let Holmes add notes, request responders, change incident status, snooze, or merge incidents:

```yaml
toolsets:
  pagerduty:
    enabled: true
    config:
      api_key: "{{ env.PAGERDUTY_USER_API_KEY }}"
      from_email: "holmes-bot@yourcompany.com"
      enable_write: true
```

Allowed writes when enabled: `POST/PUT /incidents`, `POST /incidents/{id}/notes`, `POST /incidents/{id}/responder_requests`, `POST /incidents/{id}/status_updates`, `POST /incidents/{id}/snooze`, `POST /incidents/{id}/merge`. All other mutating endpoints remain blocked.

## Plan gating

Some endpoints require a PagerDuty paid add-on or plan tier. The toolset forwards the HTTP error verbatim so Holmes can recognize and explain the limitation.

| Endpoint | Plan requirement |
|---|---|
| `/incidents/{id}/outlier_incident`, `/past_incidents`, `/related_incidents` | **AIOps / Event Intelligence** add-on |
| `/incident_workflows` | **Business** plan or higher |
| `/audit/records` | **Enterprise** plan |

Query `GET /abilities` to list the features your account has purchased.

## Common use cases

```text
Show me the open PagerDuty incidents for service P12ABCD from the last 2 hours.
```

```text
Who is currently on-call for the "Payments" escalation policy?
```

```text
Summarize the notes on PagerDuty incident PXYZ123.
```

```text
List the teams that have been assigned incidents today.
```

## Alternatives

- **Official hosted MCP server** — PagerDuty offers a managed MCP server at `https://mcp.pagerduty.com/mcp`. You can wire it up through Holmes's generic MCP toolset:

    ```yaml
    toolsets:
      pagerduty-mcp:
        type: mcp
        enabled: true
        config:
          mode: streamable-http
          url: https://mcp.pagerduty.com/mcp
          headers:
            Authorization: "Token {{ env.PAGERDUTY_USER_API_KEY }}"
    ```

    The MCP server exposes ~60 opinionated, typed tools. The native toolset above is a smaller, thin-wrapper alternative that passes the raw REST API through to Holmes.
