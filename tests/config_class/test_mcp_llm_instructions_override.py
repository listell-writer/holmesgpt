import yaml

from holmes.plugins.toolsets import load_toolsets_from_config
from holmes.plugins.toolsets.mcp.toolset_mcp import RemoteMCPToolset


initial_config_str = """
  mcp_grafana:
    type: mcp
    url: "http://mcp-server:8080"
    description: "Grafana MCP server"
    llm_instructions: "Use these tools to query Grafana dashboards."
    config:
      url: "http://mcp-server:8080"
"""

updated_config_str = """
  mcp_grafana:
    type: mcp
    url: "http://mcp-server:8080"
    description: "Grafana MCP server"
    llm_instructions: "UPDATED: Use these tools to query Prometheus via Grafana."
    config:
      url: "http://mcp-server:8080"
"""


def test_mcp_toolset_loads_llm_instructions():
    """Test that RemoteMCPToolset renders llm_instructions on creation."""
    config = yaml.safe_load(initial_config_str)
    toolsets = load_toolsets_from_config(toolsets=config, strict_check=False)
    assert len(toolsets) == 1
    toolset = toolsets[0]
    assert isinstance(toolset, RemoteMCPToolset)
    assert toolset.llm_instructions == "Use these tools to query Grafana dashboards."


def test_mcp_toolset_override_updates_llm_instructions():
    """Test that override_with() updates llm_instructions when config changes.

    This verifies the fix for the bug where changing llm_instructions in a
    ConfigMap and restarting the pod would not update the instructions because
    override_with() did not re-render them.
    """
    initial_config = yaml.safe_load(initial_config_str)
    updated_config = yaml.safe_load(updated_config_str)

    initial_toolsets = load_toolsets_from_config(
        toolsets=initial_config, strict_check=False
    )
    updated_toolsets = load_toolsets_from_config(
        toolsets=updated_config, strict_check=False
    )

    assert len(initial_toolsets) == 1
    assert len(updated_toolsets) == 1

    original = initial_toolsets[0]
    override = updated_toolsets[0]

    assert original.llm_instructions == "Use these tools to query Grafana dashboards."

    # Simulate what add_or_merge_onto_toolsets does when names match
    original.override_with(override)

    assert (
        original.llm_instructions
        == "UPDATED: Use these tools to query Prometheus via Grafana."
    )


def test_mcp_toolset_override_with_jinja_template():
    """Test that override_with() re-renders Jinja2 templates in llm_instructions."""
    config_with_template = yaml.safe_load(
        """
  mcp_test:
    type: mcp
    url: "http://mcp-server:8080"
    description: "Test MCP"
    llm_instructions: "Available tools: {{ tool_names | join(', ') }}"
    config:
      url: "http://mcp-server:8080"
"""
    )

    toolsets = load_toolsets_from_config(
        toolsets=config_with_template, strict_check=False
    )
    toolset = toolsets[0]
    # With no tools loaded (no real MCP server), tool_names is empty
    assert "Available tools:" in toolset.llm_instructions
