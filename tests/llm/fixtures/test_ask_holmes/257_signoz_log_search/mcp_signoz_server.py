"""
Mock SigNoz MCP server for eval testing.

Mirrors the public tool surface of the real signoz-mcp-server
(https://github.com/SigNoz/signoz-mcp-server) so an eval can verify
that HolmesGPT discovers and calls SigNoz tools correctly without
standing up the full SigNoz + ClickHouse stack.

The tool names, descriptions, and parameter shapes follow the real
server's published surface. This mock serves canned data for a
single namespace/service so an eval can inject a unique verification
code and confirm HolmesGPT retrieved it via the right tool call.

Transport: stdio (same pattern as tests/llm/fixtures/test_ask_holmes/253_*)
"""

import asyncio
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

VERIFICATION_CODE = "SIGNOZ-EVAL-7x9k2m4p"
SERVICE_NAME = "payment-api-257"
NAMESPACE = "app-257"

LOG_LINES = [
    {
        "timestamp": "2026-04-22T10:00:00Z",
        "severity": "INFO",
        "service": SERVICE_NAME,
        "body": "Service started successfully on port 8080",
    },
    {
        "timestamp": "2026-04-22T10:05:12Z",
        "severity": "INFO",
        "service": SERVICE_NAME,
        "body": "Processing 100 pending transactions",
    },
    {
        "timestamp": "2026-04-22T10:07:43Z",
        "severity": "WARN",
        "service": SERVICE_NAME,
        "body": "Database connection pool at 80% capacity",
    },
    {
        "timestamp": "2026-04-22T10:09:21Z",
        "severity": "ERROR",
        "service": SERVICE_NAME,
        "body": (
            f"Payment gateway rejected request "
            f"verification_code={VERIFICATION_CODE} "
            "reason=timeout after 30s"
        ),
    },
    {
        "timestamp": "2026-04-22T10:10:02Z",
        "severity": "ERROR",
        "service": SERVICE_NAME,
        "body": "Retry attempt 1 failed - downstream service unreachable",
    },
    {
        "timestamp": "2026-04-22T10:12:15Z",
        "severity": "INFO",
        "service": SERVICE_NAME,
        "body": "Circuit breaker opened for payment-gateway",
    },
]


def _filter_logs(query: Optional[str], service: Optional[str], severity: Optional[str]) -> List[Dict[str, Any]]:
    results = list(LOG_LINES)
    if service:
        results = [r for r in results if r["service"] == service]
    if severity:
        results = [r for r in results if r["severity"] == severity.upper()]
    if query:
        q = query.lower()
        results = [r for r in results if q in r["body"].lower()]
    return results


server = Server("signoz")


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="signoz_list_services",
            description="List all services currently reporting telemetry to SigNoz.",
            inputSchema={
                "type": "object",
                "properties": {
                    "time_range_minutes": {
                        "type": "integer",
                        "description": "Lookback window in minutes (default 60).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="signoz_search_logs",
            description=(
                "Search logs in SigNoz. Supports free-text query, service filter, "
                "and severity filter. Returns matching log lines with timestamps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search query matched against the log body.",
                    },
                    "service": {
                        "type": "string",
                        "description": "Filter by service name (e.g. 'payment-api-257').",
                    },
                    "severity": {
                        "type": "string",
                        "description": "Filter by severity (INFO, WARN, ERROR).",
                        "enum": ["INFO", "WARN", "ERROR"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum log lines to return (default 50).",
                    },
                    "start": {
                        "type": "string",
                        "description": "ISO-8601 start timestamp (optional).",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO-8601 end timestamp (optional).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="signoz_aggregate_logs",
            description=(
                "Aggregate log counts grouped by a field (service, severity) "
                "over a time range."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_by": {"type": "string"},
                    "time_range_minutes": {"type": "integer"},
                },
                "required": ["group_by"],
            },
        ),
        Tool(
            name="signoz_list_alerts",
            description="List configured SigNoz alerts and their current state.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="signoz_list_dashboards",
            description="List SigNoz dashboards.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    arguments = arguments or {}

    if name == "signoz_list_services":
        text = (
            "Services reporting to SigNoz:\n"
            f"  - {SERVICE_NAME} (namespace={NAMESPACE}) "
            "5m error rate: 33%, p95 latency: 2.4s"
        )
    elif name == "signoz_search_logs":
        matches = _filter_logs(
            arguments.get("query"),
            arguments.get("service"),
            arguments.get("severity"),
        )
        limit = arguments.get("limit", 50)
        matches = matches[:limit]
        if not matches:
            text = (
                f"0 log lines matched query={arguments.get('query')!r}, "
                f"service={arguments.get('service')!r}, "
                f"severity={arguments.get('severity')!r}. "
                "Try widening the time range or removing filters."
            )
        else:
            lines = [
                f"Found {len(matches)} log line(s):",
                f"(query={arguments.get('query')!r}, "
                f"service={arguments.get('service')!r}, "
                f"severity={arguments.get('severity')!r})",
                "",
            ]
            for log in matches:
                lines.append(
                    f"[{log['timestamp']}] {log['severity']} "
                    f"{log['service']}: {log['body']}"
                )
            text = "\n".join(lines)
    elif name == "signoz_aggregate_logs":
        if arguments.get("group_by") == "severity":
            counts: Dict[str, int] = {}
            for log in LOG_LINES:
                counts[log["severity"]] = counts.get(log["severity"], 0) + 1
            text = "Log counts by severity:\n" + "\n".join(
                f"  {k}: {v}" for k, v in sorted(counts.items())
            )
        else:
            text = f"Unsupported group_by: {arguments.get('group_by')}"
    elif name == "signoz_list_alerts":
        text = "No alerts configured."
    elif name == "signoz_list_dashboards":
        text = "No dashboards configured."
    else:
        text = f"Unknown tool: {name}"

    return [TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
