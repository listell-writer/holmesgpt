"""
MCP Elicitation handler for HolmesGPT.

When an MCP server sends an elicitation/create request, it asks the user
for structured information (e.g., "Enter your GitHub username"). This module
provides callbacks that present elicitation requests to the user.

Per the MCP spec, elicitation MUST NOT be used for sensitive information
like passwords. It's for non-secret configuration like usernames, project
names, preferences, etc.
"""

import logging
from typing import Any

from mcp.shared.context import RequestContext
from mcp.types import (
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    ErrorData,
    INVALID_REQUEST,
)

logger = logging.getLogger(__name__)


async def cli_elicitation_callback(
    context: RequestContext[Any, Any],
    params: ElicitRequestURLParams | ElicitRequestFormParams,
) -> ElicitResult | ErrorData:
    """Handle elicitation requests in CLI mode by prompting the user in the terminal.

    Supports form-mode elicitation (structured fields) and URL-mode elicitation
    (opening a URL for the user).
    """
    # URL mode: server wants user to visit a URL (e.g., for upstream OAuth)
    if isinstance(params, ElicitRequestURLParams):
        return await _handle_url_elicitation(params)

    # Form mode: server wants structured input
    return await _handle_form_elicitation(params)


async def _handle_url_elicitation(params: ElicitRequestURLParams) -> ElicitResult | ErrorData:
    """Handle URL-mode elicitation by opening a browser."""
    import webbrowser

    print(f"\n{'='*60}")
    print(f"MCP server requests action: {params.message}")
    print(f"Opening: {params.url}")
    print(f"{'='*60}")

    try:
        webbrowser.open(params.url)
    except Exception:
        print(f"Could not open browser. Please visit: {params.url}")

    response = input("\nPress Enter when done, or type 'cancel' to abort: ").strip()
    if response.lower() == "cancel":
        return ElicitResult(action="cancel")

    return ElicitResult(action="accept")


async def _handle_form_elicitation(params: ElicitRequestFormParams) -> ElicitResult | ErrorData:
    """Handle form-mode elicitation by prompting for each field."""
    schema = params.requestedSchema
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])

    if not properties:
        return ErrorData(code=INVALID_REQUEST, message="Empty elicitation schema")

    print(f"\n{'='*60}")
    print(f"MCP server requests information: {params.message}")
    print(f"{'='*60}\n")

    content: dict[str, str | int | float | bool | list[str] | None] = {}

    for field_name, field_schema in properties.items():
        field_type = field_schema.get("type", "string")
        description = field_schema.get("description", "")
        is_required = field_name in required_fields
        enum_values = field_schema.get("enum")

        # Build prompt
        prompt_parts = [f"  {field_name}"]
        if description:
            prompt_parts.append(f" ({description})")
        if enum_values:
            prompt_parts.append(f" [{'/'.join(str(v) for v in enum_values)}]")
        if not is_required:
            prompt_parts.append(" [optional]")
        prompt_parts.append(": ")
        prompt = "".join(prompt_parts)

        value = input(prompt).strip()

        if not value:
            if is_required:
                print(f"  '{field_name}' is required. Cancelling.")
                return ElicitResult(action="decline")
            continue

        # Coerce to the declared type
        try:
            if field_type == "boolean":
                content[field_name] = value.lower() in ("true", "yes", "1", "y")
            elif field_type == "integer":
                content[field_name] = int(value)
            elif field_type == "number":
                content[field_name] = float(value)
            else:
                content[field_name] = value
        except (ValueError, TypeError):
            content[field_name] = value

    return ElicitResult(action="accept", content=content)
