import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from holmes.core.llm import LLM, ContextWindowUsage
from holmes.core.models import (
    FrontendToolDefinition,
    FrontendToolResult,
    PendingFrontendToolCall,
    StructuredToolResult,
    StructuredToolResultStatus,
)
from holmes.core.tool_calling_llm import ToolCallingLLM
from holmes.core.tools_utils.tool_executor import ToolExecutor
from holmes.utils.stream import StreamEvents
from server import app


@pytest.fixture
def client():
    return TestClient(app)


def create_mock_llm_response(content="Test response", tool_calls=None):
    """Create a mock LLM response."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message = MagicMock()
    mock_response.choices[0].message.content = content
    mock_response.choices[0].message.tool_calls = tool_calls
    mock_response.choices[0].message.reasoning_content = None
    mock_response.choices[0].message.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in (tool_calls or [])
        ]
        if tool_calls
        else None,
    }
    mock_response.to_json.return_value = json.dumps(
        {"choices": [{"message": {"content": content}}]}
    )
    return mock_response


def create_mock_tool_call(
    tool_call_id="call_xyz9", tool_name="ask_user_for_name", arguments=None
):
    """Create a mock tool call object."""
    mock_tool_call = MagicMock()
    mock_tool_call.id = tool_call_id
    mock_tool_call.function = MagicMock()
    mock_tool_call.function.name = tool_name
    mock_tool_call.function.arguments = json.dumps(
        arguments
        or {"title": "On-Call Engineer", "prompt_text": "What is your name?"}
    )
    return mock_tool_call


def parse_sse_events(response_text: str) -> list[dict]:
    """Parse SSE events from response text."""
    events = []
    event_type = None
    for line in response_text.split("\n"):
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                events.append({"event": event_type, "data": data})
            except json.JSONDecodeError:
                pass
    return events


def setup_mock_ai(mock_tool_executor, mock_llm, tool_calls_response, final_response):
    """Create a ToolCallingLLM with mocked dependencies."""
    ai = ToolCallingLLM(
        tool_executor=mock_tool_executor,
        max_steps=5,
        llm=mock_llm,
        tool_results_dir=None,
    )

    mock_llm.count_tokens.return_value = ContextWindowUsage(
        total_tokens=100,
        system_tokens=0,
        tools_to_call_tokens=0,
        tools_tokens=0,
        user_tokens=0,
        assistant_tokens=0,
        other_tokens=0,
    )
    mock_llm.get_context_window_size.return_value = 128000
    mock_llm.get_maximum_output_token.return_value = 4096
    mock_llm.model = "gpt-4o"
    mock_llm.completion.side_effect = [tool_calls_response, final_response]

    mock_tool_executor.get_all_tools_openai_format.return_value = []
    mock_tool_executor.tools_by_name = {}

    mock_toolset = MagicMock()
    mock_toolset.name = "test"
    mock_toolset.status = MagicMock()
    mock_toolset.status.value = "enabled"
    mock_tool_executor.toolsets = [mock_toolset]

    return ai


class TestFrontendToolModels:
    """Test the new frontend tool models."""

    def test_frontend_tool_definition(self):
        tool = FrontendToolDefinition(
            name="ask_user_for_name",
            description="Opens a modal dialog asking the user for their name.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "prompt_text": {"type": "string"},
                },
                "required": ["title", "prompt_text"],
            },
        )
        assert tool.name == "ask_user_for_name"
        assert "modal" in tool.description

    def test_frontend_tool_result(self):
        result = FrontendToolResult(
            tool_call_id="call_xyz9",
            tool_name="ask_user_for_name",
            result="Alice Chen",
        )
        assert result.result == "Alice Chen"

    def test_pending_frontend_tool_call(self):
        pending = PendingFrontendToolCall(
            tool_call_id="call_xyz9",
            tool_name="ask_user_for_name",
            arguments={"title": "On-Call Engineer", "prompt_text": "What is your name?"},
        )
        assert pending.arguments["title"] == "On-Call Engineer"


class TestFrontendToolValidation:
    """Test server-side validation of frontend tools."""

    @patch("holmes.core.supabase_dal.SupabaseDal._SupabaseDal__load_robusta_config")
    @patch("holmes.config.Config.create_toolcalling_llm")
    @patch(
        "holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account"
    )
    def test_frontend_tools_require_streaming(
        self,
        mock_get_global_instructions,
        mock_create_toolcalling_llm,
        mock_load_robusta_config,
        client,
    ):
        """Frontend tools with stream=false should return 400."""
        mock_load_robusta_config.return_value = None
        mock_get_global_instructions.return_value = []

        mock_ai = MagicMock()
        mock_ai.tool_executor = MagicMock()
        mock_ai.tool_executor.tools_by_name = {}
        mock_create_toolcalling_llm.return_value = mock_ai

        payload = {
            "ask": "Hello",
            "conversation_history": [
                {"role": "system", "content": "You are a helpful assistant."}
            ],
            "stream": False,
            "frontend_tools": [
                {
                    "name": "ask_user_for_name",
                    "description": "Ask user for name",
                    "parameters": {},
                }
            ],
        }

        response = client.post("/api/chat", json=payload)
        assert response.status_code == 400
        assert "stream=true" in response.json()["detail"]

    @patch("holmes.core.supabase_dal.SupabaseDal._SupabaseDal__load_robusta_config")
    @patch("holmes.config.Config.create_toolcalling_llm")
    @patch(
        "holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account"
    )
    def test_frontend_tool_name_collision(
        self,
        mock_get_global_instructions,
        mock_create_toolcalling_llm,
        mock_load_robusta_config,
        client,
    ):
        """Frontend tool name colliding with built-in tool should return 400."""
        mock_load_robusta_config.return_value = None
        mock_get_global_instructions.return_value = []

        mock_ai = MagicMock()
        mock_ai.tool_executor = MagicMock()
        mock_ai.tool_executor.tools_by_name = {"kubectl_get": MagicMock()}
        mock_create_toolcalling_llm.return_value = mock_ai

        payload = {
            "ask": "Hello",
            "conversation_history": [
                {"role": "system", "content": "You are a helpful assistant."}
            ],
            "stream": True,
            "frontend_tools": [
                {
                    "name": "kubectl_get",
                    "description": "This conflicts",
                    "parameters": {},
                }
            ],
        }

        response = client.post("/api/chat", json=payload)
        assert response.status_code == 400
        assert "conflicts" in response.json()["detail"]


class TestFrontendToolCallStream:
    """Test the frontend tool call flow in call_stream."""

    def test_frontend_tool_pauses_stream(self):
        """When LLM calls a frontend tool, the stream should pause with pending_frontend_tool_calls."""
        mock_llm = MagicMock(spec=LLM)
        mock_tool_executor = MagicMock(spec=ToolExecutor)

        frontend_tool_call = create_mock_tool_call()
        tool_calls_response = create_mock_llm_response(
            content="Let me get the on-call engineer's name.",
            tool_calls=[frontend_tool_call],
        )
        final_response = create_mock_llm_response(
            content="Done.", tool_calls=None
        )

        ai = setup_mock_ai(
            mock_tool_executor, mock_llm, tool_calls_response, final_response
        )

        frontend_tool_names = {"ask_user_for_name"}
        frontend_openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "ask_user_for_name",
                    "description": "Opens a modal dialog.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "prompt_text": {"type": "string"},
                        },
                        "required": ["title", "prompt_text"],
                    },
                },
            }
        ]

        events = list(
            ai.call_stream(
                msgs=[
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Ask for name"},
                ],
                frontend_tool_names=frontend_tool_names,
                frontend_openai_tools=frontend_openai_tools,
            )
        )

        # Find the approval_required event
        approval_events = [
            e for e in events if e.event == StreamEvents.APPROVAL_REQUIRED
        ]
        assert len(approval_events) == 1

        approval_data = approval_events[0].data
        assert approval_data["requires_approval"] is True
        assert len(approval_data["pending_frontend_tool_calls"]) == 1
        assert approval_data["pending_frontend_tool_calls"][0]["tool_name"] == "ask_user_for_name"
        assert approval_data["pending_frontend_tool_calls"][0]["tool_call_id"] == "call_xyz9"
        assert approval_data["pending_frontend_tool_calls"][0]["arguments"]["title"] == "On-Call Engineer"

        # Should also have the start_tool event with frontend=True
        start_events = [e for e in events if e.event == StreamEvents.START_TOOL]
        frontend_starts = [e for e in start_events if e.data.get("frontend")]
        assert len(frontend_starts) == 1
        assert frontend_starts[0].data["tool_name"] == "ask_user_for_name"

    def test_frontend_tool_results_resume_stream(self):
        """After receiving frontend tool results, the stream should resume."""
        mock_llm = MagicMock(spec=LLM)
        mock_tool_executor = MagicMock(spec=ToolExecutor)

        # Only need the final response since we're resuming
        final_response = create_mock_llm_response(
            content="The on-call engineer is Alice Chen.", tool_calls=None
        )

        ai = ToolCallingLLM(
            tool_executor=mock_tool_executor,
            max_steps=5,
            llm=mock_llm,
            tool_results_dir=None,
        )

        mock_llm.count_tokens.return_value = ContextWindowUsage(
            total_tokens=100,
            system_tokens=0,
            tools_to_call_tokens=0,
            tools_tokens=0,
            user_tokens=0,
            assistant_tokens=0,
            other_tokens=0,
        )
        mock_llm.get_context_window_size.return_value = 128000
        mock_llm.get_maximum_output_token.return_value = 4096
        mock_llm.model = "gpt-4o"
        mock_llm.completion.return_value = final_response

        mock_tool_executor.get_all_tools_openai_format.return_value = []
        mock_tool_executor.tools_by_name = {}
        mock_toolset = MagicMock()
        mock_toolset.name = "test"
        mock_toolset.status = MagicMock()
        mock_toolset.status.value = "enabled"
        mock_tool_executor.toolsets = [mock_toolset]

        # Simulate the conversation history from a paused stream
        conversation_history = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Ask for name"},
            {
                "role": "assistant",
                "content": "Let me get the name.",
                "tool_calls": [
                    {
                        "id": "call_xyz9",
                        "type": "function",
                        "function": {
                            "name": "ask_user_for_name",
                            "arguments": json.dumps(
                                {
                                    "title": "On-Call Engineer",
                                    "prompt_text": "What is your name?",
                                }
                            ),
                        },
                        "pending_frontend": True,
                    }
                ],
            },
        ]

        frontend_tool_results = [
            FrontendToolResult(
                tool_call_id="call_xyz9",
                tool_name="ask_user_for_name",
                result="Alice Chen",
            )
        ]

        events = list(
            ai.call_stream(
                msgs=conversation_history,
                frontend_tool_names={"ask_user_for_name"},
                frontend_tool_results=frontend_tool_results,
            )
        )

        # Should have tool_result events from the frontend tool result injection
        tool_result_events = [
            e for e in events if e.event == StreamEvents.TOOL_RESULT
        ]
        assert len(tool_result_events) >= 1
        assert tool_result_events[0].data["tool_name"] == "ask_user_for_name"

        # Should end with ANSWER_END
        answer_events = [
            e for e in events if e.event == StreamEvents.ANSWER_END
        ]
        assert len(answer_events) == 1
        assert "Alice Chen" in answer_events[0].data["content"]


class TestFrontendToolE2E:
    """End-to-end test of frontend tools via the HTTP API."""

    @patch("holmes.core.supabase_dal.SupabaseDal._SupabaseDal__load_robusta_config")
    @patch("holmes.config.Config.create_toolcalling_llm")
    @patch(
        "holmes.core.supabase_dal.SupabaseDal.get_global_instructions_for_account"
    )
    def test_streaming_frontend_tool_flow(
        self,
        mock_get_global_instructions,
        mock_create_toolcalling_llm,
        mock_load_robusta_config,
        client,
    ):
        """Test the full SSE flow: request -> pause with frontend tool -> resume with result."""
        mock_load_robusta_config.return_value = None
        mock_get_global_instructions.return_value = []

        mock_llm = MagicMock(spec=LLM)
        mock_tool_executor = MagicMock(spec=ToolExecutor)

        frontend_tool_call = create_mock_tool_call()
        tool_calls_response = create_mock_llm_response(
            content="Let me get the name.",
            tool_calls=[frontend_tool_call],
        )
        final_response = create_mock_llm_response(
            content="Done.", tool_calls=None
        )

        ai = setup_mock_ai(
            mock_tool_executor, mock_llm, tool_calls_response, final_response
        )
        mock_create_toolcalling_llm.return_value = ai

        payload = {
            "ask": "Ask the user for their on-call name",
            "conversation_history": [
                {"role": "system", "content": "You are a helpful assistant."}
            ],
            "stream": True,
            "frontend_tools": [
                {
                    "name": "ask_user_for_name",
                    "description": "Opens a modal dialog asking the user for their name.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "The title shown in the modal dialog",
                            },
                            "prompt_text": {
                                "type": "string",
                                "description": "The question to ask the user",
                            },
                        },
                        "required": ["title", "prompt_text"],
                    },
                }
            ],
        }

        response = client.post("/api/chat", json=payload)
        assert response.status_code == 200

        events = parse_sse_events(response.text)

        # Find the approval_required event
        approval_events = [
            e for e in events if e["event"] == "approval_required"
        ]
        assert len(approval_events) == 1

        approval_data = approval_events[0]["data"]
        assert approval_data["requires_approval"] is True
        assert len(approval_data.get("pending_frontend_tool_calls", [])) == 1
        assert (
            approval_data["pending_frontend_tool_calls"][0]["tool_name"]
            == "ask_user_for_name"
        )
