"""
Integration tests for config merging functionality.
Tests the complete CLI --fast-model workflow via the class-level singleton default.
"""

from unittest.mock import patch

from holmes.core.tools import ToolsetTag, YAMLTool, YAMLToolset
from holmes.core.toolset_manager import ToolsetManager
from holmes.core.transformers import Transformer
from holmes.core.transformers.llm_summarize import LLMSummarizeTransformer


def _with_default_fast_model(model, fn):
    """Run ``fn`` with ``_default_fast_model`` set, restoring it afterwards."""
    original = LLMSummarizeTransformer._default_fast_model
    try:
        LLMSummarizeTransformer._default_fast_model = None  # reset before test
        fn(model)
    finally:
        LLMSummarizeTransformer._default_fast_model = original


def test_cli_fast_model_sets_class_default():
    """CLI --fast-model sets the class-level default on LLMSummarizeTransformer."""
    original = LLMSummarizeTransformer._default_fast_model
    try:
        LLMSummarizeTransformer._default_fast_model = None
        ToolsetManager(global_fast_model="azure/gpt-4.1")
        assert LLMSummarizeTransformer._default_fast_model == "azure/gpt-4.1"
    finally:
        LLMSummarizeTransformer._default_fast_model = original


def test_lazy_instances_pick_up_class_default():
    """Transformer instances created after set_default_fast_model use the default."""
    original = LLMSummarizeTransformer._default_fast_model
    try:
        LLMSummarizeTransformer._default_fast_model = None

        toolset = YAMLToolset(
            name="kubernetes/core",
            tags=[ToolsetTag.CORE],
            description="Kubernetes toolset",
            tools=[
                {
                    "name": "kubectl_describe",
                    "description": "Run kubectl describe",
                    "command": "kubectl describe {{ kind }} {{ name }}",
                    "transformers": [
                        Transformer(
                            name="llm_summarize",
                            config={
                                "input_threshold": 1000,
                                "prompt": "Summarize kubectl describe output...",
                            },
                        )
                    ],
                }
            ],
        )

        with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
            mock_load.return_value = [toolset]

            with patch("holmes.core.transformers.llm_summarize.DefaultLLM") as mock_llm:
                manager = ToolsetManager(global_fast_model="azure/gpt-4.1")
                toolsets = manager._list_all_toolsets(check_prerequisites=False)

                # Trigger lazy init
                k8s_tool = toolsets[0].tools[0]
                instances = k8s_tool.transformer_instances

                assert len(instances) == 1
                # DefaultLLM should have been called with the class default
                mock_llm.assert_called_with("azure/gpt-4.1", None)
    finally:
        LLMSummarizeTransformer._default_fast_model = original


def test_explicit_fast_model_wins_over_class_default():
    """Per-transformer fast_model takes precedence over class default."""
    original = LLMSummarizeTransformer._default_fast_model
    try:
        LLMSummarizeTransformer._default_fast_model = None

        toolset = YAMLToolset(
            name="test_toolset",
            tags=[ToolsetTag.CORE],
            description="Test toolset",
            tools=[
                {
                    "name": "tool_with_explicit",
                    "description": "Tool with explicit fast_model",
                    "command": "echo test",
                    "transformers": [
                        Transformer(
                            name="llm_summarize",
                            config={"input_threshold": 2000, "fast_model": "my-explicit-model"},
                        )
                    ],
                },
            ],
        )

        with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
            mock_load.return_value = [toolset]

            with patch("holmes.core.transformers.llm_summarize.DefaultLLM") as mock_llm:
                manager = ToolsetManager(global_fast_model="gpt-4.1")
                toolsets = manager._list_all_toolsets(check_prerequisites=False)

                # Trigger lazy init
                tool = toolsets[0].tools[0]
                tool.transformer_instances

                # Should use explicit, not global
                mock_llm.assert_called_with("my-explicit-model", None)
    finally:
        LLMSummarizeTransformer._default_fast_model = original


def test_backward_compatibility():
    """Toolsets without transformers still work correctly."""
    simple_toolset = YAMLToolset(
        name="simple_toolset",
        tags=[ToolsetTag.CORE],
        description="Simple toolset without transformers",
        tools=[YAMLTool(name="simple_tool", description="Simple", command="echo")],
    )

    with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
        mock_load.return_value = [simple_toolset]

        manager = ToolsetManager(global_fast_model="gpt-4o-mini")
        toolsets = manager._list_all_toolsets(check_prerequisites=False)

        result_toolset = toolsets[0]
        assert result_toolset.transformers is None
        assert result_toolset.tools[0].transformers is None


def test_no_global_configs_no_regression():
    """Existing behavior unchanged when no global configs provided."""
    original = LLMSummarizeTransformer._default_fast_model
    try:
        LLMSummarizeTransformer._default_fast_model = None

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

            assert toolsets[0].transformers == toolset_configs
            assert LLMSummarizeTransformer._default_fast_model is None
    finally:
        LLMSummarizeTransformer._default_fast_model = original


def test_toolset_transformer_inheritance_with_class_default():
    """Tools that inherit toolset-level transformers also pick up class default."""
    original = LLMSummarizeTransformer._default_fast_model
    try:
        LLMSummarizeTransformer._default_fast_model = None

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
                    "name": "generic_tool",
                    "description": "Generic tool (inherits toolset transformers)",
                    "command": "echo generic",
                },
            ],
        )

        with patch("holmes.core.toolset_registry._discover_builtin_toolsets") as mock_load:
            mock_load.return_value = [toolset]

            with patch("holmes.core.transformers.llm_summarize.DefaultLLM") as mock_llm:
                manager = ToolsetManager(global_fast_model="gpt-4.1")
                toolsets = manager._list_all_toolsets(check_prerequisites=False)

                # Tool inherited transformer from toolset
                tool = toolsets[0].tools[0]
                assert tool.transformers is not None

                # Trigger lazy init — should use class default
                tool.transformer_instances
                mock_llm.assert_called_with("gpt-4.1", None)
    finally:
        LLMSummarizeTransformer._default_fast_model = original
