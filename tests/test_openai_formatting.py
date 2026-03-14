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
