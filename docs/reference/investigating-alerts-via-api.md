# Investigating Alerts via the API

The `/api/investigate` and `/api/stream/investigate` endpoints were removed in favor of the more flexible `/api/chat` endpoint. This guide shows how to replicate — and improve upon — the old investigation workflow using `/api/chat`.

If you're a new user who wants an alert-oriented investigation view (structured sections like "Root Cause", "Key Findings", "Next Steps"), this guide is also for you.

## Migrating from `/api/investigate`

The old `/api/investigate` endpoint accepted an `InvestigateRequest` with these fields:

```json
{
  "source": "prometheus",
  "title": "KubePodCrashLooping",
  "description": "Pod has been crash-looping for 15 minutes",
  "subject": {"namespace": "production", "pod": "payment-service-7b4d6f8c9-x2k5m"},
  "context": {"robusta_issue_id": "..."},
  "source_instance_id": "ApiRequest",
  "sections": {
    "Alert Explanation": "1-2 sentences explaining the alert",
    "Key Findings": "What you checked and found",
    "Conclusions and Possible Root causes": "...",
    "Next Steps": "...",
    "Related logs": "...",
    "App or Infra?": "...",
    "External links": "..."
  },
  "model": "fast-model"
}
```

The equivalent `/api/chat` call combines three features: `ask` (alert data), `additional_system_prompt` (investigation instructions), and `response_format` (structured sections):

```bash
curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate this alert:\n\nSource: prometheus\nTitle: KubePodCrashLooping\nDescription: Pod has been crash-looping for 15 minutes\nSubject: {\"namespace\": \"production\", \"pod\": \"payment-service-7b4d6f8c9-x2k5m\"}",
    "additional_system_prompt": "Provide a terse analysis of the following alert and why it is firing. Focus on root cause, not symptoms.",
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "AlertInvestigation",
        "strict": true,
        "schema": {
          "type": "object",
          "properties": {
            "alert_explanation": {
              "type": "string",
              "description": "1-2 sentences explaining what the alert means in plain language"
            },
            "key_findings": {
              "type": "string",
              "description": "What you checked and found during investigation"
            },
            "root_causes": {
              "type": "string",
              "description": "Possible root causes based on the evidence. Distinguish between what is known for certain and what is a possible explanation"
            },
            "next_steps": {
              "type": "string",
              "description": "What to do next to troubleshoot or fix the issue. Prefer precise bash commands when possible"
            },
            "related_logs": {
              "type": "string",
              "description": "The most relevant log lines with surrounding context"
            },
            "app_or_infra": {
              "type": "string",
              "description": "Whether this is an infrastructure or application issue and why"
            }
          },
          "required": ["alert_explanation", "key_findings", "root_causes", "next_steps", "related_logs", "app_or_infra"],
          "additionalProperties": false
        }
      }
    }
  }'
```

**What changed:**

| Old (`/api/investigate`) | New (`/api/chat`) |
|---|---|
| `source`, `title`, `description`, `subject`, `context` fields | All passed as text in `ask` |
| `sections` dict defining output structure | `response_format` with JSON schema |
| `prompt_template` for investigation prompt | `additional_system_prompt` for custom instructions |
| Built-in markdown-to-sections parsing | LLM returns structured JSON directly |
| Separate `/api/stream/investigate` for SSE | Same `/api/chat` with `stream: true` |

**What you gain:**

- Follow-up questions via `conversation_history` (the old endpoint was single-shot)
- Custom sections — define any schema you want, not just the 7 built-in ones
- `behavior_controls` to tune speed vs. thoroughness
- Image analysis support
- Frontend tools for UI integrations

## Basic Alert Investigation

For a quick investigation without structured output:

```bash
curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate this alert:\n\nAlert: KubePodCrashLooping\nNamespace: production\nPod: payment-service-7b4d6f8c9-x2k5m\nSeverity: critical\nSummary: Pod has been crash-looping for 15 minutes\nLabels: {\"app\": \"payment-service\", \"team\": \"payments\"}",
    "additional_system_prompt": "Provide a terse analysis of the following alert and why it is firing. Focus on root cause, not symptoms."
  }'
```

Holmes will use its available tools (kubectl, Prometheus, logs, etc.) to investigate and return a free-form analysis in the `analysis` field.

## Reproducing the Old Default Sections

The old `/api/investigate` returned results split into 7 sections by default. Here's the exact `response_format` to replicate that:

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "InvestigationResult",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {
        "alert_explanation": {
          "type": "string",
          "description": "1-2 sentences explaining the alert itself. Don't say 'The alert indicates a warning event related to...' — just say what happened (e.g., 'The pod XYZ did blah')."
        },
        "key_findings": {
          "type": "string",
          "description": "What you checked and found during investigation."
        },
        "conclusions_and_possible_root_causes": {
          "type": "string",
          "description": "What conclusions can you reach based on the data you found? What are possible root causes or what uncertainty remains? Distinguish between what you know for certain and what is a possible explanation."
        },
        "next_steps": {
          "type": "string",
          "description": "What to do next to troubleshoot this issue, any commands to fix it, or other ways to solve it. Prefer giving precise bash commands when possible."
        },
        "related_logs": {
          "type": "string",
          "description": "Truncated most relevant logs, especially if they explain the root cause. Include the surrounding +/- 5 log lines."
        },
        "app_or_infra": {
          "type": "string",
          "description": "Whether the issue is more likely infrastructure or application level and why."
        },
        "external_links": {
          "type": "string",
          "description": "Links to external sources (runbooks, docs) with a short sentence describing each link. Markdown formatted."
        }
      },
      "required": ["alert_explanation", "key_findings", "conclusions_and_possible_root_causes", "next_steps", "related_logs", "app_or_infra", "external_links"],
      "additionalProperties": false
    }
  }
}
```

## Custom Sections

You can define any sections you want. For example, a minimal schema for a Slack notification:

```bash
curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate this alert:\n\nAlert: HighErrorRate\nService: api-gateway\nNamespace: production\nError rate: 12% (threshold: 5%)",
    "additional_system_prompt": "Investigate this alert. Be concise.",
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "SlackAlert",
        "strict": true,
        "schema": {
          "type": "object",
          "properties": {
            "title": {"type": "string", "description": "One-line summary"},
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "root_cause": {"type": "string", "description": "Root cause in 1-2 sentences"},
            "action_required": {"type": "boolean", "description": "Whether immediate human action is needed"}
          },
          "required": ["title", "severity", "root_cause", "action_required"],
          "additionalProperties": false
        }
      }
    }
  }'
```

## Batch Investigation (Multiple Alerts)

To investigate multiple alerts (replacing `holmes investigate alertmanager`), loop over your alerts:

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

for r in results:
    print(f"[{r['investigation']['severity'].upper()}] {r['alert_name']}: {r['investigation']['root_cause']}")
```

## Follow-Up Questions

The old `/api/investigate` was single-shot — one request, one response. With `/api/chat`, you can ask follow-up questions using `conversation_history`:

```bash
# Step 1: Initial investigation
RESPONSE=$(curl -s -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate: Alert KubePodCrashLooping on pod checkout-service-6d9f8c7b5-p4m2n in namespace production",
    "additional_system_prompt": "Investigate this alert. Focus on root cause."
  }')

# Step 2: Follow up using the conversation history from step 1
HISTORY=$(echo "$RESPONSE" | jq '.conversation_history')

curl -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"ask\": \"Show me the relevant logs from the last crash\",
    \"conversation_history\": $HISTORY
  }"
```

## Streaming Investigations

The old `/api/stream/investigate` is now just `/api/chat` with `stream: true`:

```bash
curl -N -X POST http://<HOLMES-URL>/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "ask": "Investigate: HighErrorRate on api-gateway in production, error rate 12% (threshold 5%)",
    "additional_system_prompt": "Investigate this alert and provide root cause analysis.",
    "stream": true
  }'
```

SSE events show investigation progress in real-time:

- `start_tool_calling` — Holmes is running a tool (e.g., fetching logs, querying Prometheus)
- `tool_calling_result` — A tool returned data
- `ai_message` — Holmes is reasoning about the data
- `ai_answer_end` — Final investigation result

See the [SSE Reference](http-api.md#server-sent-events-sse-reference) for the full event format.

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

This skips the TodoWrite planning phase and style guide, reducing token usage and latency.

## Migration Cheat Sheet

| Old `/api/investigate` | New `/api/chat` Equivalent |
|---|---|
| `source`, `title`, `description`, `subject` | Combine into `ask` as text |
| `context.robusta_issue_id` | Not needed (Holmes discovers context via tools) |
| `sections` dict | `response_format` with JSON schema |
| `prompt_template` | `additional_system_prompt` |
| `include_tool_calls: true` | Tool calls always returned in `tool_calls` field |
| `/api/stream/investigate` | `/api/chat` with `stream: true` |
| `/api/issue_chat` (follow-up) | `/api/chat` with `conversation_history` |
| Single-shot investigation | Multi-turn with `conversation_history` |
