# Investigating Alerts via the API

If you previously used a dedicated `/api/investigate` endpoint or are coming from the CLI's `holmes investigate` command, this guide shows how to replicate and extend that workflow using the `/api/chat` endpoint.

The key idea: the `/api/chat` endpoint can do everything a dedicated investigation endpoint could — and more — when you combine `additional_system_prompt`, `response_format`, and conversation history.

## Basic Alert Investigation

Pass the alert payload in your question and tell Holmes to investigate it via `additional_system_prompt`:

```bash
curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate this alert:\n\nAlert: KubePodCrashLooping\nNamespace: production\nPod: payment-service-7b4d6f8c9-x2k5m\nSeverity: critical\nSummary: Pod has been crash-looping for 15 minutes\nLabels: {\"app\": \"payment-service\", \"team\": \"payments\"}",
    "additional_system_prompt": "Provide a terse analysis of the following alert and why it is firing. Focus on root cause, not symptoms."
  }'
```

This mirrors exactly what the CLI `holmes investigate alertmanager` does internally — it passes the alert's raw data as the user prompt and adds investigation-specific instructions via the system prompt.

## Structured Investigation Output

Use `response_format` to get results in a consistent, machine-parseable schema — perfect for feeding into dashboards, ticketing systems, or Slack integrations:

```bash
curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate this alert:\n\nAlert: HighMemoryUsage\nNamespace: default\nPod: inventory-api-5c8b7d6e4f-9m3kq\nValue: 95%\nThreshold: 85%",
    "additional_system_prompt": "You are investigating an infrastructure alert. Provide a structured root-cause analysis.",
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "AlertInvestigation",
        "strict": true,
        "schema": {
          "type": "object",
          "properties": {
            "title": {
              "type": "string",
              "description": "Short title summarizing the issue"
            },
            "root_cause": {
              "type": "string",
              "description": "Root cause analysis"
            },
            "severity": {
              "type": "string",
              "enum": ["critical", "high", "medium", "low"],
              "description": "Assessed severity"
            },
            "affected_resources": {
              "type": "array",
              "items": {"type": "string"},
              "description": "List of affected pods, nodes, or services"
            },
            "remediation_steps": {
              "type": "array",
              "items": {"type": "string"},
              "description": "Recommended steps to resolve"
            },
            "evidence": {
              "type": "array",
              "items": {"type": "string"},
              "description": "Key evidence gathered during investigation"
            }
          },
          "required": ["title", "root_cause", "severity", "affected_resources", "remediation_steps", "evidence"],
          "additionalProperties": false
        }
      }
    }
  }'
```

**Response:**

```json
{
  "analysis": "{\"title\": \"Memory leak in inventory-api causing OOM risk\", \"root_cause\": \"The inventory-api pod is leaking memory due to unclosed database connections in the connection pool.\", \"severity\": \"high\", \"affected_resources\": [\"inventory-api-5c8b7d6e4f-9m3kq\", \"inventory-db\"], \"remediation_steps\": [\"Restart the pod to recover immediately\", \"Review connection pool settings in application config\", \"Add connection timeout and max-lifetime settings\"], \"evidence\": [\"Memory usage grew linearly from 40% to 95% over 6 hours\", \"Database connection count: 847 (pool max: 100)\", \"No recent deployments or config changes\"]}",
  "tool_calls": [...],
  "conversation_history": [...]
}
```

!!! note
    The `analysis` field contains a JSON string. Parse it to access the structured data:
    ```python
    import json
    investigation = json.loads(response["analysis"])
    print(investigation["root_cause"])
    ```

## Batch Investigation (Multiple Alerts)

To investigate multiple alerts (like `holmes investigate alertmanager` does), loop over your alerts and call `/api/chat` for each one:

```python
import requests
import json

HOLMES_URL = "http://localhost:8080"
INVESTIGATION_PROMPT = "Provide a terse analysis of this alert and why it is firing."

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "AlertInvestigation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "root_cause": {"type": "string"},
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "remediation_steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "root_cause", "severity", "remediation_steps"],
            "additionalProperties": False,
        },
    },
}

# Fetch alerts from AlertManager
alerts = requests.get("http://alertmanager:9093/api/v2/alerts?filter=active").json()

results = []
for alert in alerts:
    resp = requests.post(f"{HOLMES_URL}/api/chat", json={
        "ask": f"Investigate this alert:\n\n{json.dumps(alert, indent=2)}",
        "additional_system_prompt": INVESTIGATION_PROMPT,
        "response_format": SCHEMA,
    })
    investigation = json.loads(resp.json()["analysis"])
    results.append({
        "alert_name": alert["labels"]["alertname"],
        "investigation": investigation,
    })

# Write results to a file, post to Slack, update a ticket, etc.
for r in results:
    print(f"[{r['investigation']['severity'].upper()}] {r['alert_name']}: {r['investigation']['root_cause']}")
```

## Follow-Up Questions on an Investigation

Use `conversation_history` to ask follow-up questions without losing context. Holmes remembers the tools it called and the data it found:

```bash
# Step 1: Initial investigation
RESPONSE=$(curl -s -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate: Alert KubePodCrashLooping on pod checkout-service-6d9f8c7b5-p4m2n in namespace production",
    "additional_system_prompt": "Investigate this alert. Focus on root cause."
  }')

# Step 2: Ask a follow-up using the conversation history from step 1
HISTORY=$(echo "$RESPONSE" | jq '.conversation_history')

curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"ask\": \"Show me the relevant logs from the last crash\",
    \"conversation_history\": $HISTORY
  }"
```

```bash
# Step 3: Ask for remediation
curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"ask\": \"What are the exact kubectl commands to fix this?\",
    \"conversation_history\": $HISTORY
  }"
```

## Optimizing for Speed

For simple alerts where you want a fast answer without the full planning phase:

```bash
curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate: TargetDown on prometheus-node-exporter in monitoring namespace",
    "additional_system_prompt": "Provide a terse root-cause analysis of this alert.",
    "behavior_controls": {
      "todowrite_instructions": false,
      "todowrite_reminder": false,
      "style_guide": false
    }
  }'
```

This skips the TodoWrite planning phase and style guide, reducing token usage and latency — similar to the CLI's `--fast-mode` flag.

## Streaming Investigations

For UIs that want to show investigation progress in real-time:

```bash
curl -N -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate: HighErrorRate on api-gateway in production, error rate 12% (threshold 5%)",
    "additional_system_prompt": "Investigate this alert and provide root cause analysis.",
    "stream": true
  }'
```

SSE events let you show users what Holmes is doing as it investigates:

- `start_tool_calling` — Holmes is running a tool (e.g., fetching logs, querying Prometheus)
- `tool_calling_result` — A tool returned data
- `ai_message` — Holmes is reasoning about the data
- `ai_answer_end` — Final investigation result

See the [SSE Reference](http-api.md#server-sent-events-sse-reference) for the full event format.

## Migration Cheat Sheet

| Old CLI / Endpoint | New `/api/chat` Equivalent |
|---|---|
| `holmes investigate alertmanager` | `ask` = alert JSON, `additional_system_prompt` = investigation instructions |
| Investigation result fields (title, root_cause, etc.) | Use `response_format` with a JSON schema |
| Batch investigation of all firing alerts | Loop over alerts, one `/api/chat` call per alert |
| Write-back to source (Slack, Jira) | Parse the structured `analysis` and post via your own integration |
| `--fast-mode` | `behavior_controls: {"todowrite_instructions": false, "todowrite_reminder": false}` |
| Follow-up investigation | Pass `conversation_history` from previous response |
