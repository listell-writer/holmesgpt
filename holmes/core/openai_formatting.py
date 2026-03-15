import re
from typing import Any, Optional

from holmes.common.env_vars import (
    DISABLE_STRICT_TOOL_CALLS,
    TOOL_SCHEMA_NO_PARAM_OBJECT_IF_NO_PARAMS,
)

# parses both simple types: "int", "array", "string"
# but also arrays of those simpler types: "array[int]", "array[string]", etc.
pattern = r"^(array\[(?P<inner_type>\w+)\])|(?P<simple_type>\w+)$"

STRICT_MODE = not DISABLE_STRICT_TOOL_CALLS


def type_to_open_ai_schema(param_attributes: Any) -> dict[str, Any]:
    # Normalize schema types: MCP servers may emit nullable lists (e.g., ["string", "null"])
    # per JSON Schema spec, while OpenAI expects a primary type with explicit nullability via anyOf.
    raw_type = param_attributes.type
    is_nullable_from_schema = False

    if isinstance(raw_type, list):
        non_null_types = [t.strip() if isinstance(t, str) else t for t in raw_type if t != "null"]
        is_nullable_from_schema = "null" in raw_type
        param_type = non_null_types[0] if non_null_types else "string"
    else:
        param_type = raw_type.strip()

    type_obj: Optional[dict[str, Any]] = None

    if param_type == "object":
        type_obj = {"type": "object"}
        if STRICT_MODE:
            type_obj["additionalProperties"] = False

        # Use explicit properties if provided
        if hasattr(param_attributes, "properties") and param_attributes.properties:
            type_obj["properties"] = {
                name: _build_property_schema(prop)
                for name, prop in param_attributes.properties.items()
            }
            if STRICT_MODE:
                type_obj["required"] = list(param_attributes.properties.keys())

    elif param_type == "array":
        # Handle arrays with explicit item schemas
        if hasattr(param_attributes, "items") and param_attributes.items:
            items_schema = _build_property_schema(param_attributes.items)
            type_obj = {"type": "array", "items": items_schema}
        else:
            # Fallback for arrays without explicit item schema
            type_obj = {"type": "array", "items": {"type": "object"}}
            if STRICT_MODE:
                type_obj["items"]["additionalProperties"] = False
    else:
        match = re.match(pattern, param_type)

        if not match:
            raise ValueError(f"Invalid type format: {param_type}")

        if match.group("inner_type"):
            inner_type = match.group("inner_type")
            if inner_type == "object":
                raise ValueError(
                    "object inner type must have schema. Use ToolParameter.items"
                )
            else:
                type_obj = {"type": "array", "items": {"type": inner_type}}
        else:
            type_obj = {"type": match.group("simple_type")}

    # Pass through JSON Schema constraint fields from MCP so the LLM sees them
    if type_obj:
        _add_json_schema_constraints(type_obj, param_attributes, param_type)

    # Add nullability using anyOf per the OpenAI Structured Outputs spec when strict mode
    # requires optional params to accept null, or when the source schema explicitly marks
    # the field as nullable (e.g., MCP ["string", "null"]).
    if type_obj and (is_nullable_from_schema or (STRICT_MODE and not param_attributes.required)):
        type_obj = {"anyOf": [type_obj, {"type": "null"}]}

    return type_obj


def _add_json_schema_constraints(
    type_obj: dict[str, Any], param_attributes: Any, param_type: str
) -> None:
    """Add JSON Schema constraint fields (format, pattern, min/max, etc.) to a type object."""
    if getattr(param_attributes, "format", None):
        type_obj["format"] = param_attributes.format
    if getattr(param_attributes, "pattern", None):
        type_obj["pattern"] = param_attributes.pattern
    if param_type in ("number", "integer"):
        if getattr(param_attributes, "minimum", None) is not None:
            type_obj["minimum"] = param_attributes.minimum
        if getattr(param_attributes, "maximum", None) is not None:
            type_obj["maximum"] = param_attributes.maximum
    if param_type == "string":
        if getattr(param_attributes, "min_length", None) is not None:
            type_obj["minLength"] = param_attributes.min_length
        if getattr(param_attributes, "max_length", None) is not None:
            type_obj["maxLength"] = param_attributes.max_length
    if param_type == "array":
        if getattr(param_attributes, "min_items", None) is not None:
            type_obj["minItems"] = param_attributes.min_items
        if getattr(param_attributes, "max_items", None) is not None:
            type_obj["maxItems"] = param_attributes.max_items


def _build_property_schema(param_attributes: Any) -> dict[str, Any]:
    """Build a complete property schema including type, description, default, examples, and enum.

    Used for both top-level and nested properties so metadata is never lost.
    """
    schema = type_to_open_ai_schema(param_attributes)
    if param_attributes.description is not None:
        schema["description"] = param_attributes.description
    if getattr(param_attributes, "default", None) is not None:
        schema["default"] = param_attributes.default
    if getattr(param_attributes, "examples", None):
        schema["examples"] = param_attributes.examples
    if hasattr(param_attributes, "enum") and param_attributes.enum:
        enum_values = list(param_attributes.enum)
        if STRICT_MODE and not param_attributes.required and None not in enum_values:
            enum_values.append(None)
        schema["enum"] = enum_values
    return schema


def format_tool_to_open_ai_standard(
    tool_name: str, tool_description: str, tool_parameters: dict
):
    tool_properties = {}

    for param_name, param_attributes in tool_parameters.items():
        tool_properties[param_name] = _build_property_schema(param_attributes)

    result: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": tool_description,
            "parameters": {
                "properties": tool_properties,
                "required": [
                    param_name
                    for param_name, param_attributes in tool_parameters.items()
                    if param_attributes.required or STRICT_MODE
                ],
                "type": "object",
            },
        },
    }

    if STRICT_MODE and result["function"]:
        result["function"]["strict"] = True
        result["function"]["parameters"]["additionalProperties"] = False
        # Also set strict inside parameters for providers like Anthropic where
        # LiteLLM reads it from input_schema rather than function.strict
        result["function"]["parameters"]["strict"] = True

    # gemini doesnt have parameters object if it is without params
    if TOOL_SCHEMA_NO_PARAM_OBJECT_IF_NO_PARAMS and (
        tool_properties is None or tool_properties == {}
    ):
        result["function"].pop("parameters")  # type: ignore

    return result
