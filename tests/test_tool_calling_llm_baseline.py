"""
Baseline tests for ToolCallingLLM.call() and call_stream() loop mechanics.

These tests establish behavioral contracts BEFORE the refactor that makes
call() a thin wrapper around call_stream(). They should pass on the current
code and continue to pass after the refactor.

Mocking strategy:
- Patch `limit_input_context_window` to avoid its internal LLM/token counting
- Mock `self.llm.completion` to control LLM responses
- Mock `self._invoke_llm_tool_call` to control tool execution
- Mock `self.llm.count_tokens` for token counting calls that happen outside
  of limit_input_context_window (e.g., after tool results, at final response)
"""

import json
import threading
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from holmes.core.llm import LLM, TokenCountMetadata
from holmes.core.models import ToolCallResult
from holmes.core.tool_calling_llm import LLMInterruptedError, ToolCallingLLM
from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus
from holmes.core.tools_utils.tool_executor import ToolExecutor
from holmes.core.truncation.compaction import CompactionUsage
from holmes.core.truncation.input_context_window_limiter import (
    ContextWindowLimiterOutput,
)
from holmes.utils.stream import StreamEvents, StreamMessage

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

SIMPLE_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "kubectl_get",
        "description": "Get Kubernetes resources",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
        },
    },
}

DEFAULT_TOKEN_COUNT = TokenCountMetadata(
    total_tokens=100,
    system_tokens=0,
    tools_to_call_tokens=0,
    tools_tokens=0,
    user_tokens=0,
    assistant_tokens=0,
    other_tokens=0,
)


def _make_context_limiter_passthrough(messages, **_kwargs):
    """Returns a ContextWindowLimiterOutput that passes messages through unchanged."""
    return ContextWindowLimiterOutput(
        metadata={},
        messages=list(messages),
        events=[],
        max_context_size=128000,
        maximum_output_token=4096,
        tokens=DEFAULT_TOKEN_COUNT,
        conversation_history_compacted=False,
        compaction_usage=CompactionUsage(),
    )


def _make_mock_tool_call(tool_call_id="tc_1", tool_name="kubectl_get", arguments=None):
    tc = MagicMock()
    tc.id = tool_call_id
    tc.function = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(arguments or {"command": "kubectl get pods"})
    return tc


def _make_llm_response(content="done", tool_calls=None, cost=0.001, prompt_tokens=50, completion_tokens=20):
    """Create a mock LLM response matching litellm ModelResponse shape."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    msg.reasoning_content = None

    # model_dump must return a dict matching what gets appended to messages
    dump = {"role": "assistant", "content": content}
    if tool_calls:
        dump["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    msg.model_dump.return_value = dump

    resp.choices[0].message = msg
    resp.to_json.return_value = json.dumps({"choices": [{"message": dump}]})

    # Cost/usage info
    resp._hidden_params = {"response_cost": cost}
    usage = MagicMock()
    usage.get = lambda key, default=0: {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": None,
        "completion_tokens_details": None,
    }.get(key, default)
    resp.usage = usage

    return resp


def _make_tool_call_result(tool_call_id="tc_1", tool_name="kubectl_get", data="pod1 Running"):
    return ToolCallResult(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        description=f"Ran {tool_name}",
        result=StructuredToolResult(
            status=StructuredToolResultStatus.SUCCESS,
            data=data,
            params={"command": "kubectl get pods"},
        ),
    )


def _make_tool_call_result_error(tool_call_id="tc_1", tool_name="kubectl_get", error="command not found"):
    return ToolCallResult(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        description=f"Ran {tool_name}",
        result=StructuredToolResult(
            status=StructuredToolResultStatus.ERROR,
            error=error,
            params={"command": "kubectl get pods"},
        ),
    )


def _make_tool_call_result_approval(tool_call_id="tc_1", tool_name="kubectl_delete",
                                     invocation="kubectl delete pod foo"):
    return ToolCallResult(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        description=f"Run {tool_name}",
        result=StructuredToolResult(
            status=StructuredToolResultStatus.APPROVAL_REQUIRED,
            invocation=invocation,
            params={"command": "kubectl delete pod foo", "suggested_prefixes": ["kubectl delete"]},
        ),
    )


@pytest.fixture
def mock_llm():
    llm = MagicMock(spec=LLM)
    llm.count_tokens.return_value = DEFAULT_TOKEN_COUNT
    llm.get_context_window_size.return_value = 128000
    llm.get_maximum_output_token.return_value = 4096
    llm.get_max_token_count_for_single_tool.return_value = 10000
    llm.model = "gpt-4o"
    return llm


@pytest.fixture
def mock_tool_executor():
    te = MagicMock(spec=ToolExecutor)
    te.get_all_tools_openai_format.return_value = [SIMPLE_TOOL_OPENAI]
    te.ensure_toolset_initialized.return_value = None
    mock_toolset = MagicMock()
    mock_toolset.name = "kubectl"
    te.toolsets = [mock_toolset]
    te.enabled_toolsets = [mock_toolset]
    return te


@pytest.fixture
def make_ai(mock_llm, mock_tool_executor):
    """Factory that returns a ToolCallingLLM with default mocks."""
    def _make(max_steps=10, approval_callback=None):
        ai = ToolCallingLLM(
            tool_executor=mock_tool_executor,
            max_steps=max_steps,
            llm=mock_llm,
            tool_results_dir=None,
        )
        if approval_callback:
            ai.approval_callback = approval_callback
        return ai
    return _make


LIMIT_PATCH = "holmes.core.tool_calling_llm.limit_input_context_window"


def _collect_stream_events(stream) -> List[StreamMessage]:
    return list(stream)


def _events_of_type(events: List[StreamMessage], event_type: StreamEvents) -> List[StreamMessage]:
    return [e for e in events if e.event == event_type]


# ---------------------------------------------------------------------------
# Test 1: Multi-iteration happy path
# ---------------------------------------------------------------------------


class TestMultiIterationHappyPath:
    """Mock LLM returns a tool call on first response, then a text answer on second."""

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_call_happy_path(self, _mock_limit, make_ai, mock_llm):
        tc = _make_mock_tool_call()
        resp_with_tool = _make_llm_response(content="Let me check", tool_calls=[tc])
        resp_final = _make_llm_response(content="All pods are running", tool_calls=None)
        mock_llm.completion.side_effect = [resp_with_tool, resp_final]

        ai = make_ai()
        tool_result = _make_tool_call_result()
        ai._invoke_llm_tool_call = MagicMock(return_value=tool_result)

        messages = [{"role": "user", "content": "What pods are running?"}]
        result = ai.call(messages)

        # Verify result fields
        assert result.result == "All pods are running"
        assert result.num_llm_calls == 2
        assert len(result.tool_calls) == 1
        # LLMResult.tool_calls contains ToolCallResult objects (Pydantic coerces
        # the dicts from as_tool_result_response() back into ToolCallResult)
        assert result.tool_calls[0].tool_name == "kubectl_get"
        assert result.prompt is not None
        json.loads(result.prompt)  # must be valid JSON

        # Messages should contain: original + assistant(tool_calls) + tool + assistant(answer)
        assert result.messages is not None
        assert len(result.messages) >= 4

        # Cost fields populated
        assert result.prompt_tokens > 0
        assert result.completion_tokens > 0
        assert result.total_cost > 0

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_call_stream_happy_path(self, _mock_limit, make_ai, mock_llm):
        tc = _make_mock_tool_call()
        resp_with_tool = _make_llm_response(content="Let me check", tool_calls=[tc])
        resp_final = _make_llm_response(content="All pods are running", tool_calls=None)
        mock_llm.completion.side_effect = [resp_with_tool, resp_final]

        ai = make_ai()
        tool_result = _make_tool_call_result()
        ai._invoke_llm_tool_call = MagicMock(return_value=tool_result)

        messages = [{"role": "user", "content": "What pods are running?"}]
        events = _collect_stream_events(ai.call_stream(msgs=messages))

        # Should have START_TOOL, TOOL_RESULT, TOKEN_COUNT, AI_MESSAGE, ANSWER_END
        start_tools = _events_of_type(events, StreamEvents.START_TOOL)
        tool_results = _events_of_type(events, StreamEvents.TOOL_RESULT)
        answer_ends = _events_of_type(events, StreamEvents.ANSWER_END)
        token_counts = _events_of_type(events, StreamEvents.TOKEN_COUNT)

        assert len(start_tools) == 1
        assert start_tools[0].data["tool_name"] == "kubectl_get"
        assert len(tool_results) == 1
        assert len(answer_ends) == 1
        assert answer_ends[0].data["content"] == "All pods are running"
        assert "messages" in answer_ends[0].data
        assert "metadata" in answer_ends[0].data
        assert len(token_counts) >= 1  # at least one TOKEN_COUNT event


# ---------------------------------------------------------------------------
# Test 3: Approval callback flow (pre-refactor: _handle_tool_call_approval)
# ---------------------------------------------------------------------------


class TestApprovalCallbackFlow:
    """Mock tool returns APPROVAL_REQUIRED, callback approves, tool re-executes."""

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_approval_approved(self, _mock_limit, make_ai, mock_llm):
        tc = _make_mock_tool_call(tool_call_id="tc_del", tool_name="kubectl_delete")
        resp_with_tool = _make_llm_response(content="Deleting pod", tool_calls=[tc])
        resp_final = _make_llm_response(content="Pod deleted", tool_calls=None)
        mock_llm.completion.side_effect = [resp_with_tool, resp_final]

        callback = MagicMock(return_value=(True, None))
        ai = make_ai(approval_callback=callback)

        # First call returns APPROVAL_REQUIRED, second (after approval) returns SUCCESS
        approval_result = _make_tool_call_result_approval(
            tool_call_id="tc_del", tool_name="kubectl_delete"
        )
        approved_result = StructuredToolResult(
            status=StructuredToolResultStatus.SUCCESS,
            data="pod deleted",
            params={"command": "kubectl delete pod foo"},
        )

        # _invoke_llm_tool_call returns the approval-required result
        ai._invoke_llm_tool_call = MagicMock(return_value=approval_result)
        # _directly_invoke_tool_call returns the success result after approval
        ai._directly_invoke_tool_call = MagicMock(return_value=approved_result)
        # _is_tool_call_already_approved returns False so callback is invoked
        ai._is_tool_call_already_approved = MagicMock(return_value=False)

        messages = [{"role": "user", "content": "Delete the pod"}]
        result = ai.call(messages)

        # Callback was invoked with the StructuredToolResult
        callback.assert_called_once()
        callback_arg = callback.call_args[0][0]
        assert callback_arg.status == StructuredToolResultStatus.APPROVAL_REQUIRED

        # Tool was re-executed after approval
        ai._directly_invoke_tool_call.assert_called_once()

        # Final result includes the approved tool
        assert result.result == "Pod deleted"
        assert len(result.tool_calls) == 1

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_approval_denied_with_feedback(self, _mock_limit, make_ai, mock_llm):
        tc = _make_mock_tool_call(tool_call_id="tc_del", tool_name="kubectl_delete")
        resp_with_tool = _make_llm_response(content="Deleting pod", tool_calls=[tc])
        resp_final = _make_llm_response(content="OK I won't delete it", tool_calls=None)
        mock_llm.completion.side_effect = [resp_with_tool, resp_final]

        callback = MagicMock(return_value=(False, "try using namespace kube-system instead"))
        ai = make_ai(approval_callback=callback)

        approval_result = _make_tool_call_result_approval(
            tool_call_id="tc_del", tool_name="kubectl_delete"
        )
        ai._invoke_llm_tool_call = MagicMock(return_value=approval_result)
        ai._is_tool_call_already_approved = MagicMock(return_value=False)

        messages = [{"role": "user", "content": "Delete the pod"}]
        result = ai.call(messages)

        # Callback invoked
        callback.assert_called_once()

        # The tool result in messages should contain the feedback
        tool_messages = [m for m in result.messages if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        tool_content = tool_messages[0]["content"]
        assert "User feedback: try using namespace kube-system instead" in tool_content

        # Final answer from LLM
        assert result.result == "OK I won't delete it"


# ---------------------------------------------------------------------------
# Test 4: Cost accumulation across multiple iterations
# ---------------------------------------------------------------------------


class TestCostAccumulation:
    """3 iterations: 2 tool rounds + final answer. Verify costs sum correctly."""

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_costs_summed_across_iterations(self, _mock_limit, make_ai, mock_llm):
        tc1 = _make_mock_tool_call(tool_call_id="tc_1")
        tc2 = _make_mock_tool_call(tool_call_id="tc_2")

        resp1 = _make_llm_response(content="step 1", tool_calls=[tc1], cost=0.01, prompt_tokens=100, completion_tokens=50)
        resp2 = _make_llm_response(content="step 2", tool_calls=[tc2], cost=0.02, prompt_tokens=200, completion_tokens=80)
        resp3 = _make_llm_response(content="final answer", tool_calls=None, cost=0.03, prompt_tokens=300, completion_tokens=100)
        mock_llm.completion.side_effect = [resp1, resp2, resp3]

        ai = make_ai()
        ai._invoke_llm_tool_call = MagicMock(
            side_effect=[
                _make_tool_call_result(tool_call_id="tc_1"),
                _make_tool_call_result(tool_call_id="tc_2"),
            ]
        )

        result = ai.call([{"role": "user", "content": "analyze"}])

        assert result.num_llm_calls == 3
        assert len(result.tool_calls) == 2

        # Costs should be summed
        assert result.total_cost == pytest.approx(0.06, abs=1e-9)
        assert result.prompt_tokens == 600  # 100 + 200 + 300
        assert result.completion_tokens == 230  # 50 + 80 + 100
        assert result.total_tokens == 830
        assert result.max_prompt_tokens_per_call == 300
        assert result.max_completion_tokens_per_call == 100


# ---------------------------------------------------------------------------
# Test 5: Cancellation via cancel_event
# ---------------------------------------------------------------------------


class TestCancellation:
    """cancel_event is set during tool execution, raises LLMInterruptedError."""

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_cancel_during_tool_execution(self, _mock_limit, make_ai, mock_llm):
        cancel_event = threading.Event()
        tc = _make_mock_tool_call()
        resp = _make_llm_response(content="running", tool_calls=[tc])
        mock_llm.completion.return_value = resp

        def tool_side_effect(*args, **kwargs):
            # Set cancel during tool execution
            cancel_event.set()
            return _make_tool_call_result()

        ai = make_ai()
        ai._invoke_llm_tool_call = MagicMock(side_effect=tool_side_effect)

        with pytest.raises(LLMInterruptedError):
            ai.call(
                [{"role": "user", "content": "check pods"}],
                cancel_event=cancel_event,
            )

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_cancel_before_llm_call(self, _mock_limit, make_ai, mock_llm):
        cancel_event = threading.Event()
        cancel_event.set()  # Already cancelled

        ai = make_ai()

        with pytest.raises(LLMInterruptedError):
            ai.call(
                [{"role": "user", "content": "check pods"}],
                cancel_event=cancel_event,
            )

        # LLM should never be called
        mock_llm.completion.assert_not_called()

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_cancel_after_llm_response(self, _mock_limit, make_ai, mock_llm):
        cancel_event = threading.Event()
        tc = _make_mock_tool_call()
        resp = _make_llm_response(content="running", tool_calls=[tc])

        def completion_side_effect(*args, **kwargs):
            cancel_event.set()  # Set cancel after LLM responds
            return resp

        mock_llm.completion.side_effect = completion_side_effect

        ai = make_ai()

        with pytest.raises(LLMInterruptedError):
            ai.call(
                [{"role": "user", "content": "check pods"}],
                cancel_event=cancel_event,
            )


# ---------------------------------------------------------------------------
# Test 7: Tool returning ERROR status
# ---------------------------------------------------------------------------


class TestToolError:
    """Tool returns ERROR, LLM receives error and continues to give final answer."""

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_call_continues_after_tool_error(self, _mock_limit, make_ai, mock_llm):
        tc = _make_mock_tool_call()
        resp_with_tool = _make_llm_response(content="checking", tool_calls=[tc])
        resp_final = _make_llm_response(content="The command failed, here is why...", tool_calls=None)
        mock_llm.completion.side_effect = [resp_with_tool, resp_final]

        ai = make_ai()
        ai._invoke_llm_tool_call = MagicMock(
            return_value=_make_tool_call_result_error(error="permission denied")
        )

        result = ai.call([{"role": "user", "content": "check pods"}])

        assert result.result == "The command failed, here is why..."
        assert result.num_llm_calls == 2
        assert len(result.tool_calls) == 1

        # Verify the error tool result was included in messages for LLM
        tool_messages = [m for m in result.messages if m.get("role") == "tool"]
        assert len(tool_messages) == 1

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_call_stream_yields_error_tool_result(self, _mock_limit, make_ai, mock_llm):
        tc = _make_mock_tool_call()
        resp_with_tool = _make_llm_response(content="checking", tool_calls=[tc])
        resp_final = _make_llm_response(content="error occurred", tool_calls=None)
        mock_llm.completion.side_effect = [resp_with_tool, resp_final]

        ai = make_ai()
        ai._invoke_llm_tool_call = MagicMock(
            return_value=_make_tool_call_result_error(error="permission denied")
        )

        events = _collect_stream_events(
            ai.call_stream(msgs=[{"role": "user", "content": "check pods"}])
        )

        tool_results = _events_of_type(events, StreamEvents.TOOL_RESULT)
        assert len(tool_results) == 1
        assert tool_results[0].data["result"]["status"] == "error"

        answer_ends = _events_of_type(events, StreamEvents.ANSWER_END)
        assert len(answer_ends) == 1
        assert answer_ends[0].data["content"] == "error occurred"


# ---------------------------------------------------------------------------
# Test 8: max_steps boundary
# ---------------------------------------------------------------------------


class TestMaxSteps:
    """max_steps=2, LLM always returns tool calls. Loop terminates."""

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_max_steps_forces_termination(self, _mock_limit, make_ai, mock_llm):
        tc = _make_mock_tool_call()
        # First response: tool call (iteration 1)
        resp1 = _make_llm_response(content="step 1", tool_calls=[tc])
        # Second response: tools=None forced by max_steps, so LLM must give text
        # But we'll mock it to return text since tools will be set to None
        resp2 = _make_llm_response(content="forced final answer", tool_calls=None)
        mock_llm.completion.side_effect = [resp1, resp2]

        ai = make_ai(max_steps=2)
        ai._invoke_llm_tool_call = MagicMock(return_value=_make_tool_call_result())

        result = ai.call([{"role": "user", "content": "check"}])

        assert result.result == "forced final answer"
        assert result.num_llm_calls == 2

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_max_steps_exceeded_raises(self, _mock_limit, make_ai, mock_llm):
        """If LLM keeps returning tool calls even on last iteration (shouldn't happen
        with tools=None, but test the safety net)."""
        tc = _make_mock_tool_call()
        # Both iterations return tool calls
        resp = _make_llm_response(content="still going", tool_calls=[tc])
        mock_llm.completion.return_value = resp

        ai = make_ai(max_steps=2)
        ai._invoke_llm_tool_call = MagicMock(return_value=_make_tool_call_result())

        with pytest.raises(Exception, match="Too many LLM calls"):
            ai.call([{"role": "user", "content": "check"}])


# ---------------------------------------------------------------------------
# Test 9: response_format passthrough
# ---------------------------------------------------------------------------


class TestResponseFormatPassthrough:
    """response_format is forwarded to litellm.completion."""

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_call_passes_response_format(self, _mock_limit, make_ai, mock_llm):
        resp = _make_llm_response(content='{"key": "value"}', tool_calls=None)
        mock_llm.completion.return_value = resp

        ai = make_ai()
        fmt = {"type": "json_object"}
        ai.call([{"role": "user", "content": "give me json"}], response_format=fmt)

        # Verify response_format was passed through
        call_kwargs = mock_llm.completion.call_args
        assert call_kwargs.kwargs.get("response_format") == fmt or call_kwargs[1].get("response_format") == fmt

    @patch(LIMIT_PATCH, side_effect=_make_context_limiter_passthrough)
    def test_call_stream_passes_response_format(self, _mock_limit, make_ai, mock_llm):
        resp = _make_llm_response(content='{"key": "value"}', tool_calls=None)
        mock_llm.completion.return_value = resp

        ai = make_ai()
        fmt = {"type": "json_object"}
        events = _collect_stream_events(
            ai.call_stream(msgs=[{"role": "user", "content": "give me json"}], response_format=fmt)
        )

        call_kwargs = mock_llm.completion.call_args
        assert call_kwargs.kwargs.get("response_format") == fmt or call_kwargs[1].get("response_format") == fmt

        # Should still get ANSWER_END
        answer_ends = _events_of_type(events, StreamEvents.ANSWER_END)
        assert len(answer_ends) == 1
