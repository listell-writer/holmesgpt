import pytest

from holmes.core.openai_formatting import format_tool_to_open_ai_standard, type_to_open_ai_schema
from holmes.core.tools import ToolParameter


@pytest.mark.parametrize(
    "toolset_type, open_ai_type",
    [
        (
            "int",
            {"type": "int"},
        ),
        (
            "string",
            {"type": "string"},
        ),
        (
            "array[int]",
            {"type": "array", "items": {"type": "int"}},
        ),
        (
            "array[string]",
            {"type": "array", "items": {"type": "string"}},
        ),
    ],
)
def test_type_to_open_ai_schema(toolset_type, open_ai_type):
    param = ToolParameter(type=toolset_type, required=True)
    result = type_to_open_ai_schema(param)
    assert result == open_ai_type


class TestJsonSchemaConstraints:
    """Verify that JSON Schema constraint fields from MCP are passed through to the LLM schema."""

    def test_format_on_string(self):
        param = ToolParameter(type="string", required=True, format="date-time")
        result = type_to_open_ai_schema(param)
        assert result == {"type": "string", "format": "date-time"}

    def test_pattern_on_string(self):
        param = ToolParameter(type="string", required=True, pattern=r"^\d{4}-\d{2}-\d{2}$")
        result = type_to_open_ai_schema(param)
        assert result["pattern"] == r"^\d{4}-\d{2}-\d{2}$"

    def test_min_max_on_number(self):
        param = ToolParameter(type="number", required=True, minimum=0.0, maximum=100.0)
        result = type_to_open_ai_schema(param)
        assert result == {"type": "number", "minimum": 0.0, "maximum": 100.0}

    def test_min_max_on_integer(self):
        param = ToolParameter(type="integer", required=True, minimum=1, maximum=50)
        result = type_to_open_ai_schema(param)
        assert result == {"type": "integer", "minimum": 1, "maximum": 50}

    def test_min_max_length_on_string(self):
        param = ToolParameter(type="string", required=True, min_length=1, max_length=255)
        result = type_to_open_ai_schema(param)
        assert result == {"type": "string", "minLength": 1, "maxLength": 255}

    def test_min_max_items_on_array(self):
        param = ToolParameter(
            type="array", required=True,
            items=ToolParameter(type="string", required=True),
            min_items=1, max_items=10,
        )
        result = type_to_open_ai_schema(param)
        assert result["minItems"] == 1
        assert result["maxItems"] == 10
        assert result["type"] == "array"

    def test_constraints_not_added_when_absent(self):
        param = ToolParameter(type="string", required=True)
        result = type_to_open_ai_schema(param)
        assert result == {"type": "string"}
        assert "format" not in result
        assert "pattern" not in result

    def test_numeric_constraints_ignored_on_string(self):
        """minimum/maximum should not appear on string types."""
        param = ToolParameter(type="string", required=True, minimum=0, maximum=100)
        result = type_to_open_ai_schema(param)
        assert "minimum" not in result
        assert "maximum" not in result

    def test_default_and_examples_on_property(self):
        params = {
            "city": ToolParameter(
                type="string", required=True,
                default="Berlin", examples=["Berlin", "Tokyo", "NYC"],
            ),
        }
        result = format_tool_to_open_ai_standard("test", "test tool", params)
        prop = result["function"]["parameters"]["properties"]["city"]
        assert prop["default"] == "Berlin"
        assert prop["examples"] == ["Berlin", "Tokyo", "NYC"]

    def test_no_default_or_examples_when_absent(self):
        params = {"x": ToolParameter(type="string", required=True)}
        result = format_tool_to_open_ai_standard("test", "test tool", params)
        prop = result["function"]["parameters"]["properties"]["x"]
        assert "default" not in prop
        assert "examples" not in prop


class TestNestedPropertyMetadata:
    """Verify that nested object properties get description, default, examples, enum, and format."""

    def test_nested_description_preserved(self):
        params = {
            "config": ToolParameter(
                type="object", required=True,
                properties={
                    "startDate": ToolParameter(
                        type="string", required=False,
                        description="ISO 8601 timestamp for the start of the range",
                        format="date-time",
                    ),
                },
            ),
        }
        result = format_tool_to_open_ai_standard("test", "test", params)
        nested = result["function"]["parameters"]["properties"]["config"]
        start = nested["properties"]["startDate"]
        assert start["description"] == "ISO 8601 timestamp for the start of the range"

    def test_nested_format_preserved(self):
        params = {
            "config": ToolParameter(
                type="object", required=True,
                properties={
                    "email": ToolParameter(type="string", required=True, format="email"),
                },
            ),
        }
        result = format_tool_to_open_ai_standard("test", "test", params)
        nested = result["function"]["parameters"]["properties"]["config"]
        # format is inside the type schema, which may be wrapped in anyOf
        email_prop = nested["properties"]["email"]
        # In non-strict mode, format is directly on the type object
        # In strict mode, it might be inside anyOf
        if "anyOf" in email_prop:
            type_schema = email_prop["anyOf"][0]
        else:
            type_schema = email_prop
        assert type_schema.get("format") == "email"

    def test_nested_default_and_examples_preserved(self):
        params = {
            "opts": ToolParameter(
                type="object", required=True,
                properties={
                    "limit": ToolParameter(
                        type="integer", required=False,
                        default=10, examples=[10, 50, 100],
                    ),
                },
            ),
        }
        result = format_tool_to_open_ai_standard("test", "test", params)
        limit = result["function"]["parameters"]["properties"]["opts"]["properties"]["limit"]
        assert limit["default"] == 10
        assert limit["examples"] == [10, 50, 100]

    def test_nested_enum_preserved(self):
        params = {
            "settings": ToolParameter(
                type="object", required=True,
                properties={
                    "mode": ToolParameter(
                        type="string", required=True,
                        enum=["fast", "balanced", "thorough"],
                    ),
                },
            ),
        }
        result = format_tool_to_open_ai_standard("test", "test", params)
        mode = result["function"]["parameters"]["properties"]["settings"]["properties"]["mode"]
        assert "fast" in mode["enum"]
        assert "balanced" in mode["enum"]
        assert "thorough" in mode["enum"]

    def test_deeply_nested_metadata_preserved(self):
        """Metadata survives two levels of nesting."""
        params = {
            "outer": ToolParameter(
                type="object", required=True,
                properties={
                    "inner": ToolParameter(
                        type="object", required=True,
                        properties={
                            "deep": ToolParameter(
                                type="string", required=True,
                                description="deeply nested field",
                                format="uri",
                            ),
                        },
                    ),
                },
            ),
        }
        result = format_tool_to_open_ai_standard("test", "test", params)
        deep = (
            result["function"]["parameters"]["properties"]["outer"]
            ["properties"]["inner"]["properties"]["deep"]
        )
        assert deep["description"] == "deeply nested field"
