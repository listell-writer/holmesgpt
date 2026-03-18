"""
Tests that read-only bash commands (like kubectl describe) do NOT require
approval when an MCP toolset with approval_required_tools: ['*'] is configured.

Reproduces the issue reported where adding a kubernetes-remediation MCP server
with approval_required_tools: ['*'] caused read-only kubectl operations via the
bash toolset to show confirmation dialogs.

Two root causes are tested:
1. Toolset-level approval_required_tools must only affect tools within that
   toolset - the bash tool's approval logic must be independent.
2. The _is_tool_call_already_approved pre-check must be consistent with
   _get_approval_requirement (the actual invocation check).
"""

import pytest

from tests.conftest import create_mock_tool_invoke_context

from holmes.core.tools import (
    ApprovalRequirement,
    StructuredToolResult,
    StructuredToolResultStatus,
    Tool,
    ToolInvokeContext,
    ToolParameter,
    Toolset,
    ToolsetStatusEnum,
    ToolsetTag,
)
from holmes.core.tools_utils.tool_executor import ToolExecutor
from holmes.plugins.toolsets.bash.bash_toolset import BashExecutorToolset, RunBashCommand


class FakeMCPTool(Tool):
    """Simulates a tool from a kubernetes-remediation MCP server."""

    toolset: "FakeMCPToolset"

    def get_parameterized_one_liner(self, params) -> str:
        return f"{self.name} {params}"

    def _invoke(self, params: dict, context: ToolInvokeContext) -> StructuredToolResult:
        return StructuredToolResult(
            status=StructuredToolResultStatus.SUCCESS,
            data="fake mcp result",
            params=params,
        )


class FakeMCPToolset(Toolset):
    """Simulates an MCP toolset with approval_required_tools: ['*']."""

    pass


def _create_mcp_toolset_with_approval() -> FakeMCPToolset:
    """Create a fake MCP toolset that requires approval for all tools."""
    toolset = FakeMCPToolset(
        name="kubernetes-remediation",
        enabled=True,
        description="Kubernetes remediation MCP server",
        tools=[],
        tags=[ToolsetTag.CORE],
        approval_required_tools=["*"],
    )
    kubectl_tool = FakeMCPTool(
        name="kubectl_execute",
        description="Execute kubectl commands for remediation",
        parameters={
            "args": ToolParameter(
                description="kubectl arguments",
                type="array",
                items=ToolParameter(type="string"),
            ),
        },
        toolset=toolset,
    )
    toolset.tools = [kubectl_tool]
    return toolset


class TestReadOnlyBashApprovalWithMCP:
    """Verify that read-only bash commands don't require approval when MCP is configured."""

    def test_bash_tool_not_affected_by_mcp_approval_required_tools(self):
        """
        Core test: adding an MCP toolset with approval_required_tools: ['*']
        must NOT cause the bash tool to require approval for read-only commands.
        """
        bash_toolset = BashExecutorToolset()
        bash_tool = bash_toolset.tools[0]
        assert isinstance(bash_tool, RunBashCommand)

        # Verify bash toolset has no approval_required_tools
        assert bash_toolset.approval_required_tools == []

        context = create_mock_tool_invoke_context(tool_name="bash")

        # kubectl describe should be allowed without approval
        params = {
            "command": "kubectl describe deployment my-app",
            "suggested_prefixes": ["kubectl describe"],
        }
        approval = bash_tool.requires_approval(params, context)
        assert approval is None or not approval.needs_approval, (
            "kubectl describe should not require approval via the bash tool"
        )

    def test_mcp_tool_requires_approval_with_wildcard(self):
        """MCP tools should require approval when approval_required_tools: ['*']."""
        mcp_toolset = _create_mcp_toolset_with_approval()
        mcp_tool = mcp_toolset.tools[0]

        # _check_approval_config should trigger for the MCP tool
        approval = mcp_tool._check_approval_config()
        assert approval is not None
        assert approval.needs_approval is True
        assert "kubectl_execute" in approval.reason

    def test_bash_tool_approval_config_not_affected_by_coexisting_mcp(self):
        """
        When both bash and MCP toolsets are loaded, the bash tool's
        _check_approval_config must return None (no toolset-level approval).
        """
        bash_toolset = BashExecutorToolset()
        mcp_toolset = _create_mcp_toolset_with_approval()

        bash_tool = bash_toolset.tools[0]
        mcp_tool = mcp_toolset.tools[0]

        # The bash tool's toolset-level check should return None
        bash_approval = bash_tool._check_approval_config()
        assert bash_approval is None, (
            "Bash tool should not be affected by MCP toolset's approval_required_tools"
        )

        # The MCP tool's toolset-level check should require approval
        mcp_approval = mcp_tool._check_approval_config()
        assert mcp_approval is not None
        assert mcp_approval.needs_approval is True

    def test_bash_get_approval_requirement_for_readonly_kubectl(self):
        """
        Full approval check path (_get_approval_requirement) for bash tool
        with a read-only kubectl command should not require approval.
        """
        bash_toolset = BashExecutorToolset()
        bash_tool = bash_toolset.tools[0]

        context = create_mock_tool_invoke_context(tool_name="bash")
        params = {
            "command": "kubectl describe deployment my-app",
            "suggested_prefixes": ["kubectl describe"],
        }

        # This is the method called during actual tool invocation
        approval = bash_tool._get_approval_requirement(params, context)
        assert approval is None or not approval.needs_approval, (
            "kubectl describe via bash should not require approval"
        )

    def test_tool_executor_keeps_tools_separate(self):
        """
        When both bash and MCP toolsets are registered in ToolExecutor,
        their tools should be separate and not affect each other's approval.
        """
        bash_toolset = BashExecutorToolset()
        bash_toolset.status = ToolsetStatusEnum.ENABLED
        mcp_toolset = _create_mcp_toolset_with_approval()
        mcp_toolset.status = ToolsetStatusEnum.ENABLED

        executor = ToolExecutor([bash_toolset, mcp_toolset])

        # Both tools should be registered
        bash_tool = executor.get_tool_by_name("bash")
        mcp_tool = executor.get_tool_by_name("kubectl_execute")
        assert bash_tool is not None
        assert mcp_tool is not None

        # bash tool should belong to bash toolset, not MCP
        assert bash_tool.toolset.name == "bash"
        assert mcp_tool.toolset.name == "kubernetes-remediation"

    def test_is_tool_call_already_approved_consistent_with_invoke(self):
        """
        _is_tool_call_already_approved (pre-check) must agree with
        _get_approval_requirement (invocation check) for bash read-only commands.

        If _is_tool_call_already_approved says "approved" but invoke disagrees,
        the UI would not show a dialog but the tool would still block.
        """
        bash_toolset = BashExecutorToolset()
        bash_tool = bash_toolset.tools[0]

        context = create_mock_tool_invoke_context(tool_name="bash")
        params = {
            "command": "kubectl describe deployment my-app",
            "suggested_prefixes": ["kubectl describe"],
        }

        # Pre-check (used by UI): calls requires_approval directly
        precheck_approval = bash_tool.requires_approval(params, context)
        precheck_passes = precheck_approval is None or not precheck_approval.needs_approval

        # Invocation check: calls _get_approval_requirement (includes toolset-level check)
        invoke_approval = bash_tool._get_approval_requirement(params, context)
        invoke_passes = invoke_approval is None or not invoke_approval.needs_approval

        assert precheck_passes == invoke_passes, (
            f"Pre-check and invocation approval must agree. "
            f"Pre-check: passes={precheck_passes}, Invoke: passes={invoke_passes}"
        )

    def test_mcp_tool_name_collision_overrides_bash(self):
        """
        If an MCP tool has the same name as the bash tool ('bash'),
        ToolExecutor should log a warning and the last-registered tool wins.
        This test documents the behavior to prevent silent tool shadowing.
        """
        bash_toolset = BashExecutorToolset()
        bash_toolset.status = ToolsetStatusEnum.ENABLED

        # Create MCP toolset with a tool named "bash" (name collision!)
        mcp_toolset = FakeMCPToolset(
            name="kubernetes-remediation",
            enabled=True,
            description="MCP with name collision",
            tools=[],
            tags=[ToolsetTag.CORE],
            approval_required_tools=["*"],
        )
        colliding_tool = FakeMCPTool(
            name="bash",  # Same name as the bash toolset's tool!
            description="MCP tool that collides with bash",
            parameters={},
            toolset=mcp_toolset,
        )
        mcp_toolset.tools = [colliding_tool]
        mcp_toolset.status = ToolsetStatusEnum.ENABLED

        # MCP toolset registered after bash - its tool should override
        executor = ToolExecutor([bash_toolset, mcp_toolset])
        bash_tool = executor.get_tool_by_name("bash")

        # The MCP tool wins due to registration order
        assert bash_tool is not None
        assert bash_tool.toolset.name == "kubernetes-remediation", (
            "MCP tool should override bash tool when name collides"
        )

        # The MCP tool now requires approval for everything via toolset config
        approval = bash_tool._check_approval_config()
        assert approval is not None
        assert approval.needs_approval is True, (
            "If MCP tool overrides bash tool, it inherits MCP approval settings"
        )


class TestReadOnlyKubectlCommands:
    """Verify specific read-only kubectl commands don't require approval via bash."""

    @pytest.mark.parametrize(
        "command,prefixes",
        [
            ("kubectl describe deployment my-app", ["kubectl describe"]),
            ("kubectl get pods -n default", ["kubectl get"]),
            ("kubectl logs my-pod", ["kubectl logs"]),
            ("kubectl get nodes", ["kubectl get"]),
            ("kubectl describe pod my-pod -n kube-system", ["kubectl describe"]),
            ("kubectl get deployment -o yaml", ["kubectl get"]),
            ("kubectl top pods", ["kubectl top"]),
            ("kubectl get events --sort-by=.lastTimestamp", ["kubectl get"]),
        ],
    )
    def test_readonly_kubectl_no_approval(self, command: str, prefixes: list):
        """Read-only kubectl commands should not require approval via bash tool."""
        bash_toolset = BashExecutorToolset()
        bash_tool = bash_toolset.tools[0]

        context = create_mock_tool_invoke_context(tool_name="bash")
        params = {
            "command": command,
            "suggested_prefixes": prefixes,
        }

        approval = bash_tool._get_approval_requirement(params, context)
        assert approval is None or not approval.needs_approval, (
            f"Read-only command '{command}' should not require approval"
        )
