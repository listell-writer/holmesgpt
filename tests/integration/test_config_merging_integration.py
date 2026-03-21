"""
Integration tests for config merging functionality.
Tests the complete CLI --fast-model workflow with real-world toolset configurations.
"""

from unittest.mock import patch

from holmes.core.tools import ToolsetTag, YAMLTool, YAMLToolset
from holmes.core.toolset_manager import ToolsetManager
from holmes.core.transformers import Transformer


def create_kubernetes_toolset():
    """Create a Kubernetes toolset similar to the real one with transformers."""
    kubectl_describe = YAMLTool(
        name="kubectl_describe",
        description="Run kubectl describe",
        command="kubectl describe {{ kind }} {{ name }}",
        transformers=[
            Transformer(
                name="llm_summarize",
                config={
                    "input_threshold": 1000,
                    "prompt": "Summarize kubectl describe output...",
                },
            )
        ],
    )

    kubectl_get = YAMLTool(
        name="kubectl_get_by_kind_in_namespace",
        description="Run kubectl get",
        command="kubectl get {{ kind }} -n {{ namespace }}",
        transformers=[
            Transformer(
                name="llm_summarize",
                config={
                    "input_threshold": 1000,
                    "prompt": "Summarize kubectl output...",
                },
            )
        ],
    )

    return YAMLToolset(
        name="kubernetes/core",
        tags=[ToolsetTag.CORE],
        description="Kubernetes toolset",
        tools=[kubectl_describe, kubectl_get],
    )


def test_cli_fast_model_integration_with_kubernetes():
    """
    Integration test: CLI --fast-model should reach tool transformer instances.
    """
    global_fast_model = "azure/gpt-4.1"
    kubernetes_toolset = create_kubernetes_toolset()

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [kubernetes_toolset]

        manager = ToolsetManager(global_fast_model=global_fast_model)
        with patch("holmes.core.transformers.llm_summarize.DefaultLLM"):
            toolsets = manager._list_all_toolsets(check_prerequisites=False)

        k8s_toolset = next(t for t in toolsets if t.name == "kubernetes/core")

        # Verify transformer instances received the global_fast_model
        kubectl_describe = next(
            t for t in k8s_toolset.tools if t.name == "kubectl_describe"
        )
        instance = kubectl_describe._transformer_instances[0]
        assert instance.global_fast_model == "azure/gpt-4.1"
        assert instance.input_threshold == 1000


def test_fast_model_injection_chain():
    """
    Test that global_fast_model reaches tool instances (both tool-level and inherited).
    """
    global_fast_model = "gpt-4.1"

    # Pass tools as dicts to exercise the normal YAML parsing flow
    # (preprocess_tools merges toolset transformers into tools as dicts)
    toolset = YAMLToolset(
        name="test_toolset",
        tags=[ToolsetTag.CORE],
        description="Test toolset",
        transformers=[
            Transformer(
                name="llm_summarize",
                config={"input_threshold": 1000, "prompt": "Toolset prompt"},
            )
        ],
        tools=[
            {
                "name": "specific_tool",
                "description": "Tool with transformer",
                "command": "echo test",
                "transformers": [
                    Transformer(name="llm_summarize", config={"input_threshold": 2000})
                ],
            },
            {
                "name": "generic_tool",
                "description": "Generic tool",
                "command": "echo generic",
            },
        ],
    )

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [toolset]

        manager = ToolsetManager(global_fast_model=global_fast_model)
        with patch("holmes.core.transformers.llm_summarize.DefaultLLM"):
            toolsets = manager._list_all_toolsets(check_prerequisites=False)

        test_toolset = toolsets[0]

        # Tool with explicit transformer should have its threshold preserved
        specific_tool = next(t for t in test_toolset.tools if t.name == "specific_tool")
        instance = specific_tool._transformer_instances[0]
        assert instance.global_fast_model == "gpt-4.1"
        assert instance.input_threshold == 2000

        # Tool that inherited from toolset should also get global_fast_model
        generic_tool = next(t for t in test_toolset.tools if t.name == "generic_tool")
        instance = generic_tool._transformer_instances[0]
        assert instance.global_fast_model == "gpt-4.1"
        assert instance.input_threshold == 1000  # From toolset


def test_fast_model_injection_with_different_transformers():
    """
    Test that fast model injection reaches tool instances correctly.
    """
    global_fast_model = "gpt-4o-mini"

    tool = YAMLTool(
        name="multi_transformer_tool",
        description="Tool with transformer",
        command="echo test",
        transformers=[
            Transformer(name="llm_summarize", config={"prompt": "Custom prompt"})
        ],
    )

    toolset = YAMLToolset(
        name="multi_transformer_toolset",
        tags=[ToolsetTag.CORE],
        description="Multi transformer toolset",
        transformers=[
            Transformer(
                name="llm_summarize",
                config={"input_threshold": 1000, "prompt": "Toolset prompt"},
            )
        ],
        tools=[tool],
    )

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [toolset]

        manager = ToolsetManager(global_fast_model=global_fast_model)
        with patch("holmes.core.transformers.llm_summarize.DefaultLLM"):
            toolsets = manager._list_all_toolsets(check_prerequisites=False)

        result_tool = toolsets[0].tools[0]
        instance = result_tool._transformer_instances[0]
        assert instance.global_fast_model == "gpt-4o-mini"
        assert "Custom prompt" in instance.prompt


def test_backward_compatibility():
    """
    Test that toolsets without transformers still work correctly (no injection occurs).
    """
    global_fast_model = "gpt-4o-mini"

    simple_toolset = YAMLToolset(
        name="simple_toolset",
        tags=[ToolsetTag.CORE],
        description="Simple toolset without transformers",
        tools=[YAMLTool(name="simple_tool", description="Simple", command="echo")],
    )

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [simple_toolset]

        manager = ToolsetManager(global_fast_model=global_fast_model)
        toolsets = manager._list_all_toolsets(check_prerequisites=False)

        result_toolset = toolsets[0]
        assert result_toolset.transformers is None

        tool = result_toolset.tools[0]
        assert tool.transformers is None


def test_no_global_configs_no_regression():
    """
    Test that existing behavior is unchanged when no global configs are provided.
    """
    toolset_configs = [
        Transformer(name="llm_summarize", config={"input_threshold": 1000})
    ]

    toolset = YAMLToolset(
        name="existing_toolset",
        tags=[ToolsetTag.CORE],
        description="Existing toolset",
        transformers=toolset_configs,
    )

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [toolset]

        manager = ToolsetManager()
        toolsets = manager._list_all_toolsets(check_prerequisites=False)

        result_toolset = toolsets[0]
        assert result_toolset.transformers == toolset_configs


def test_toolset_with_only_tool_level_transformers_gets_fast_model():
    """
    Test that toolsets with ONLY tool-level transformers (no toolset-level transformers)
    DO receive global fast-model settings via instance injection.
    """
    global_fast_model = "gpt-4o-mini"

    jq_query_tool = YAMLTool(
        name="kubernetes_jq_query",
        description="Query Kubernetes Resources with jq",
        command="kubectl get {{ kind }} --all-namespaces -o json | jq -r {{ jq_expr }}",
        transformers=[
            Transformer(
                name="llm_summarize",
                config={
                    "input_threshold": 1000,
                    "prompt": "Summarize jq query output focusing on patterns...",
                },
            )
        ],
    )

    toolset = YAMLToolset(
        name="kubernetes/core",
        tags=[ToolsetTag.CORE],
        description="Kubernetes toolset with only tool-level transformers",
        tools=[jq_query_tool],
    )

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [toolset]

        manager = ToolsetManager(global_fast_model=global_fast_model)
        with patch("holmes.core.transformers.llm_summarize.DefaultLLM"):
            toolsets = manager._list_all_toolsets(check_prerequisites=False)

        jq_tool = next(
            t for t in toolsets[0].tools if t.name == "kubernetes_jq_query"
        )
        instance = jq_tool._transformer_instances[0]
        assert instance.global_fast_model == "gpt-4o-mini"
        assert instance.input_threshold == 1000


def test_toolset_with_toolset_level_transformers_works():
    """
    Contrast test: Verify that toolsets WITH toolset-level transformers
    DO receive global fast-model injection on tool instances.
    """
    global_fast_model = "gpt-4o-mini"

    kubectl_describe = YAMLTool(
        name="kubectl_describe",
        description="Run kubectl describe",
        command="kubectl describe {{ kind }} {{ name }}",
        transformers=[
            Transformer(
                name="llm_summarize",
                config={
                    "input_threshold": 1000,
                    "prompt": "Summarize kubectl describe output...",
                },
            )
        ],
    )

    toolset = YAMLToolset(
        name="kubernetes/core",
        tags=[ToolsetTag.CORE],
        description="Kubernetes toolset with toolset-level transformers",
        tools=[kubectl_describe],
        transformers=[
            Transformer(
                name="llm_summarize",
                config={"input_threshold": 800},
            )
        ],
    )

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [toolset]

        manager = ToolsetManager(global_fast_model=global_fast_model)
        with patch("holmes.core.transformers.llm_summarize.DefaultLLM"):
            toolsets = manager._list_all_toolsets(check_prerequisites=False)

        describe_tool = next(
            t for t in toolsets[0].tools if t.name == "kubectl_describe"
        )
        instance = describe_tool._transformer_instances[0]
        assert instance.global_fast_model == "gpt-4o-mini"
        assert instance.input_threshold == 1000
        assert "Summarize kubectl describe output" in instance.prompt
