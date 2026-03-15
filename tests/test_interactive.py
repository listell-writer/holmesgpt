import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from rich.console import Console

from holmes.core.feedback import FeedbackMetadata
from holmes.core.tool_calling_llm import ToolCallingLLM
from holmes.interactive import (
    AgenticProgressRenderer,
    Feedback,
    SlashCommandCompleter,
    SlashCommands,
    UserFeedback,
    handle_feedback_command,
    run_interactive_loop,
)
from holmes.utils.stream import StreamEvents, StreamMessage
from tests.mocks.toolset_mocks import SampleToolset


class TestAgenticProgressRendererSummary(unittest.TestCase):
    """Test that tasks and tools panels persist after flush()."""

    def _get_printed_panels(self, console):
        """Extract Panel objects from console.print calls."""
        from rich.panel import Panel
        panels = []
        for call in console.print.call_args_list:
            args = call[0] if call[0] else []
            for arg in args:
                if isinstance(arg, Panel):
                    panels.append(arg)
        return panels

    def test_flush_prints_tools_summary(self):
        """flush() should print the tools panel even when AI_MESSAGE never fired."""
        console = Mock(spec=Console)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)

        # Simulate tool completion (what TOOL_RESULT handler does)
        renderer._tool_history.append(("kubectl_get_pods", "get pods in namespace default", "kubernetes", 1.2, 500, False))
        renderer._tool_history.append(("kubectl_top_pods", "get resource usage for pods", "kubernetes", 0.8, 300, False))
        renderer._total_bytes = 800
        renderer._total_queries = 2

        renderer.flush()

        # Should have printed panels (tools) and stats
        panels = self._get_printed_panels(console)
        assert len(panels) >= 1, f"Expected at least 1 panel, got {len(panels)}"
        assert console.print.call_count >= 2  # tools panel + stats line

    def test_flush_prints_tasks_summary(self):
        """flush() should print task panel when tasks exist."""
        console = Mock(spec=Console)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)

        renderer._live_tasks = [
            {"content": "Check pods", "status": "completed"},
            {"content": "Check logs", "status": "in_progress"},
        ]
        renderer._tool_history.append(("kubectl_get_pods", "get pods in namespace default", "kubernetes", 1.0, 100, False))

        renderer.flush()

        panels = self._get_printed_panels(console)
        assert len(panels) >= 2, f"Expected tasks + tools panels, got {len(panels)}"

    def test_flush_no_double_print_after_ai_message(self):
        """Summary should print only once even if AI_MESSAGE already triggered it."""
        console = Mock(spec=Console)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)

        renderer._tool_history.append(("kubectl_get_pods", "get pods in namespace default", "kubernetes", 1.0, 100, False))

        # Simulate AI_MESSAGE calling _print_investigation_summary
        renderer._print_investigation_summary()
        first_print_count = console.print.call_count

        # Now flush - should NOT re-print
        renderer.flush()
        assert console.print.call_count == first_print_count, (
            "Summary was printed twice"
        )

    def test_flush_no_output_when_no_tools(self):
        """flush() should not print anything when no tools ran."""
        console = Mock(spec=Console)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)

        renderer.flush()

        console.print.assert_not_called()


class TestSlashCommandCompleter(unittest.TestCase):
    def test_init_without_unsupported_commands(self):
        """Test SlashCommandCompleter initialization without unsupported commands."""
        completer = SlashCommandCompleter()
        expected_commands = {cmd.command: cmd.description for cmd in SlashCommands}
        self.assertEqual(completer.commands, expected_commands)

    def test_init_with_unsupported_commands(self):
        """Test SlashCommandCompleter initialization with unsupported commands."""
        unsupported = [SlashCommands.FEEDBACK.command]
        completer = SlashCommandCompleter(unsupported)

        expected_commands = {cmd.command: cmd.description for cmd in SlashCommands}
        expected_commands.pop(SlashCommands.FEEDBACK.command)

        self.assertEqual(completer.commands, expected_commands)

    def test_get_completions_with_slash_prefix(self):
        """Test completion suggestions for slash commands."""
        completer = SlashCommandCompleter()
        document = Mock()
        document.text_before_cursor = "/ex"

        completions = list(completer.get_completions(document, None))

        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].text, SlashCommands.EXIT.command)

    def test_get_completions_without_slash_prefix(self):
        """Test no completions for non-slash input."""
        completer = SlashCommandCompleter()
        document = Mock()
        document.text_before_cursor = "regular input"

        completions = list(completer.get_completions(document, None))

        self.assertEqual(len(completions), 0)

    def test_get_completions_filters_unsupported_commands(self):
        """Test that unsupported commands are filtered out of completions."""
        unsupported = [SlashCommands.FEEDBACK.command]
        completer = SlashCommandCompleter(unsupported)
        document = Mock()
        document.text_before_cursor = "/feed"

        completions = list(completer.get_completions(document, None))

        self.assertEqual(len(completions), 0)


class TestHandleFeedbackCommand(unittest.TestCase):
    @patch("holmes.interactive.PromptSession")
    def test_handle_feedback_command_positive(self, mock_prompt_session_class):
        """Test feedback command with positive rating."""
        mock_prompt_session_class.return_value.prompt.side_effect = [
            "y",
            "Great response!",
            "Y",  # Final confirmation
        ]

        console = Mock()
        style = Mock()
        feedback = Feedback()
        feedback_callback = Mock()

        handle_feedback_command(style, console, feedback, feedback_callback)

        # Verify feedback object was populated
        self.assertIsNotNone(feedback.user_feedback)
        self.assertTrue(feedback.user_feedback.is_positive)
        self.assertEqual(feedback.user_feedback.comment, "Great response!")

        # Verify callback was called with the feedback object
        feedback_callback.assert_called_once_with(feedback)

        # Verify thank you message was printed
        console.print.assert_any_call(
            "[bold green]Thank you for your feedback! 🙏[/bold green]"
        )

    @patch("holmes.interactive.PromptSession")
    def test_handle_feedback_command_negative(self, mock_prompt_session_class):
        """Test feedback command with negative rating."""
        mock_prompt_session_class.return_value.prompt.side_effect = [
            "n",
            "Could be better",
            "Y",  # Final confirmation
        ]

        console = Mock()
        style = Mock()
        feedback = Feedback()
        feedback_callback = Mock()

        handle_feedback_command(style, console, feedback, feedback_callback)

        # Verify feedback object was populated
        self.assertIsNotNone(feedback.user_feedback)
        self.assertFalse(feedback.user_feedback.is_positive)
        self.assertEqual(feedback.user_feedback.comment, "Could be better")

        # Verify callback was called with the feedback object
        feedback_callback.assert_called_once_with(feedback)

        # Verify thank you message was printed
        console.print.assert_any_call(
            "[bold green]Thank you for your feedback! 🙏[/bold green]"
        )

    @patch("holmes.interactive.PromptSession")
    def test_handle_feedback_command_no_comment(self, mock_prompt_session_class):
        """Test feedback command without comment."""
        mock_prompt_session_class.return_value.prompt.side_effect = [
            "y",
            "",  # No comment
            "Y",  # Final confirmation
        ]

        console = Mock()
        style = Mock()
        feedback = Feedback()
        feedback_callback = Mock()

        handle_feedback_command(style, console, feedback, feedback_callback)

        # Verify feedback object was populated
        self.assertIsNotNone(feedback.user_feedback)
        self.assertTrue(feedback.user_feedback.is_positive)
        self.assertIsNone(feedback.user_feedback.comment)

        # Verify callback was called with the feedback object
        feedback_callback.assert_called_once_with(feedback)

        # Verify thank you message was printed
        console.print.assert_any_call(
            "[bold green]Thank you for your feedback! 🙏[/bold green]"
        )

    @patch("holmes.interactive.PromptSession")
    def test_handle_feedback_command_invalid_then_valid_rating(
        self, mock_prompt_session_class
    ):
        """Test feedback command with invalid rating first, then valid."""
        mock_prompt_session_class.return_value.prompt.side_effect = [
            "x",
            "y",
            "",  # No comment
            "Y",  # Final confirmation
        ]

        console = Mock()
        style = Mock()
        feedback = Feedback()
        feedback_callback = Mock()

        handle_feedback_command(style, console, feedback, feedback_callback)

        # Verify feedback object was populated
        self.assertIsNotNone(feedback.user_feedback)
        self.assertTrue(feedback.user_feedback.is_positive)
        self.assertIsNone(feedback.user_feedback.comment)

        # Verify callback was called with the feedback object
        feedback_callback.assert_called_once_with(feedback)

        # Verify error message was printed for invalid input
        console.print.assert_any_call(
            "[bold red]Please enter only 'y' for yes or 'n' for no.[/bold red]"
        )

        # Verify feedback recorded message was printed
        console.print.assert_any_call(
            "[bold green]✓ Feedback recorded (rating=👍, no comment)[/bold green]"
        )

        # Verify thank you message was printed
        console.print.assert_any_call(
            "[bold green]Thank you for your feedback! 🙏[/bold green]"
        )

    @patch("holmes.interactive.PromptSession")
    def test_handle_feedback_command_confirmation_cancelled(
        self, mock_prompt_session_class
    ):
        """Test feedback command when final confirmation is cancelled."""
        mock_prompt_session_class.return_value.prompt.side_effect = [
            "y",
            "Great response!",
            "n",  # Final confirmation cancelled
        ]

        console = Mock()
        style = Mock()
        feedback = Feedback()
        feedback_callback = Mock()

        handle_feedback_command(style, console, feedback, feedback_callback)

        # Verify feedback object was NOT populated and callback was NOT called
        # because final confirmation was cancelled
        self.assertIsNone(feedback.user_feedback)

        # Verify callback was NOT called since confirmation was cancelled
        feedback_callback.assert_not_called()

        # Verify cancellation message was printed, not thank you message
        console.print.assert_any_call("[dim]Feedback cancelled.[/dim]")

        # Ensure thank you message was NOT printed
        thank_you_calls = [
            call
            for call in console.print.call_args_list
            if "[bold green]Thank you for your feedback! 🙏[/bold green]" in str(call)
        ]
        self.assertEqual(len(thank_you_calls), 0)

    @patch("holmes.interactive.PromptSession")
    def test_handle_feedback_command_keyboard_interrupt(
        self, mock_prompt_session_class
    ):
        """Test feedback command when KeyboardInterrupt is raised."""
        mock_prompt_session_class.return_value.prompt.side_effect = KeyboardInterrupt()

        console = Mock()
        style = Mock()
        feedback = Feedback()
        feedback_callback = Mock()

        handle_feedback_command(style, console, feedback, feedback_callback)

        # Verify feedback object was not populated and callback was not called
        self.assertIsNone(feedback.user_feedback)
        feedback_callback.assert_not_called()

        # Verify cancellation message was printed
        console.print.assert_any_call("[dim]Feedback cancelled.[/dim]")

    @patch("holmes.interactive.PromptSession")
    def test_handle_feedback_command_with_comment_containing_markup(
        self, mock_prompt_session_class
    ):
        """Test feedback command with comment containing markup characters that need escaping."""
        mock_prompt_session_class.return_value.prompt.side_effect = [
            "y",
            "Great [bold]response[/bold] & nice <work>!",  # Comment with markup
            "Y",  # Final confirmation
        ]

        console = Mock()
        style = Mock()
        feedback = Feedback()
        feedback_callback = Mock()

        handle_feedback_command(style, console, feedback, feedback_callback)

        # Verify feedback object was populated
        self.assertIsNotNone(feedback.user_feedback)
        self.assertTrue(feedback.user_feedback.is_positive)
        self.assertEqual(
            feedback.user_feedback.comment, "Great [bold]response[/bold] & nice <work>!"
        )

        # Verify callback was called with the feedback object
        feedback_callback.assert_called_once_with(feedback)

        # The feedback recorded message should have escaped markup
        expected_msg = (
            "[bold green]✓ Feedback recorded (rating=👍, "
            '"Great \\[bold]response\\[/bold] & nice <work>!")[/bold green]'
        )
        console.print.assert_any_call(expected_msg)


class TestRunInteractiveLoop(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        self.mock_ai = Mock(spec=ToolCallingLLM)
        self.mock_ai.llm = Mock()
        self.mock_ai.llm.model = "test-model"
        self.mock_ai.llm.get_context_window_size.return_value = 4096
        self.mock_ai.tool_executor = Mock()
        self.mock_ai.tool_executor.toolsets = [SampleToolset()]

        # Mock AI response
        self.mock_response = Mock()
        self.mock_response.result = "Test response"
        self.mock_response.messages = []
        self.mock_response.tool_calls = []
        self.mock_ai.call.return_value = self.mock_response

        # Mock call_stream to yield an ANSWER_END event (used by interactive loop)
        def _mock_call_stream(**kwargs):
            yield StreamMessage(
                event=StreamEvents.ANSWER_END,
                data={
                    "content": "Test response",
                    "messages": [],
                    "tool_calls": [],
                    "num_llm_calls": 1,
                    "costs": {},
                },
            )
        self.mock_ai.call_stream = Mock(side_effect=_mock_call_stream)

        self.mock_console = Mock(spec=Console)

        # Create a temporary directory for history file
        self.temp_dir = tempfile.mkdtemp()
        self.history_file = os.path.join(self.temp_dir, "history")

    def tearDown(self):
        """Clean up test fixtures."""

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    @patch("holmes.interactive.handle_feedback_command")
    def test_run_interactive_loop_feedback_command_positive_with_callback(
        self,
        mock_handle_feedback,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test interactive loop with /feedback command - positive feedback."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        mock_session.prompt.side_effect = ["/feedback", "/exit"]

        mock_build_messages.return_value = []
        mock_callback = Mock()

        # Mock the feedback handler to simulate feedback collection and callback invocation
        def mock_feedback_handler(_style, _console, feedback, feedback_callback):
            # Simulate what the real function does
            user_feedback = UserFeedback(is_positive=True, comment="Great response!")
            feedback.user_feedback = user_feedback
            feedback_callback(feedback)

        mock_handle_feedback.side_effect = mock_feedback_handler

        # Run the interactive loop
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=None,
            include_files=None,
            show_tool_output=False,
            check_version=False,
            feedback_callback=mock_callback,
        )

        # Verify feedback handler was called
        mock_handle_feedback.assert_called_once()

        # Verify callback was called with complete Feedback object
        mock_callback.assert_called_once()
        call_args = mock_callback.call_args[0][0]

        # Test complete Feedback structure
        self.assertIsInstance(call_args, Feedback)

        # Test UserFeedback component
        self.assertIsNotNone(call_args.user_feedback)
        self.assertIsInstance(call_args.user_feedback, UserFeedback)
        self.assertEqual(call_args.user_feedback.is_positive, True)
        self.assertEqual(call_args.user_feedback.comment, "Great response!")

        # Test FeedbackMetadata component
        self.assertIsNotNone(call_args.metadata)
        self.assertIsInstance(call_args.metadata, FeedbackMetadata)

        # Test LLM information in metadata
        self.assertIsNotNone(call_args.metadata.llm)
        self.assertEqual(call_args.metadata.llm.model, "test-model")
        self.assertEqual(call_args.metadata.llm.max_context_size, 4096)

        # Test LLM responses list (should be empty initially but list should exist)
        self.assertIsInstance(call_args.metadata.llm_responses, list)

        # Test to_dict() functionality
        feedback_dict = call_args.to_dict()
        self.assertIn("user_feedback", feedback_dict)
        self.assertIn("metadata", feedback_dict)
        self.assertEqual(feedback_dict["user_feedback"]["is_positive"], True)
        self.assertEqual(feedback_dict["user_feedback"]["comment"], "Great response!")
        self.assertEqual(feedback_dict["metadata"]["llm"]["model"], "test-model")
        self.assertEqual(feedback_dict["metadata"]["llm"]["max_context_size"], 4096)

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    @patch("holmes.interactive.handle_feedback_command")
    def test_run_interactive_loop_feedback_command_negative_with_callback(
        self,
        mock_handle_feedback,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test interactive loop with /feedback command - negative feedback."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        mock_session.prompt.side_effect = ["/feedback", "/exit"]

        mock_build_messages.return_value = []
        mock_callback = Mock()

        # Mock the feedback handler to simulate feedback collection and callback invocation
        def mock_feedback_handler(_style, _console, feedback, feedback_callback):
            # Simulate what the real function does
            user_feedback = UserFeedback(is_positive=False, comment="Could be better")
            feedback.user_feedback = user_feedback
            feedback_callback(feedback)

        mock_handle_feedback.side_effect = mock_feedback_handler

        # Run the interactive loop
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=None,
            include_files=None,
            show_tool_output=False,
            check_version=False,
            feedback_callback=mock_callback,
        )

        # Verify callback was called with complete Feedback object containing negative feedback
        mock_callback.assert_called_once()
        call_args = mock_callback.call_args[0][0]

        # Test complete Feedback structure
        self.assertIsInstance(call_args, Feedback)

        # Test UserFeedback component
        self.assertIsNotNone(call_args.user_feedback)
        self.assertIsInstance(call_args.user_feedback, UserFeedback)
        self.assertEqual(call_args.user_feedback.is_positive, False)
        self.assertEqual(call_args.user_feedback.comment, "Could be better")

        # Test FeedbackMetadata component
        self.assertIsNotNone(call_args.metadata)
        self.assertIsInstance(call_args.metadata, FeedbackMetadata)

        # Test LLM information in metadata
        self.assertIsNotNone(call_args.metadata.llm)
        self.assertEqual(call_args.metadata.llm.model, "test-model")
        self.assertEqual(call_args.metadata.llm.max_context_size, 4096)

        # Test LLM responses list
        self.assertIsInstance(call_args.metadata.llm_responses, list)

        # Test to_dict() functionality for negative feedback
        feedback_dict = call_args.to_dict()
        self.assertIn("user_feedback", feedback_dict)
        self.assertIn("metadata", feedback_dict)
        self.assertEqual(feedback_dict["user_feedback"]["is_positive"], False)
        self.assertEqual(feedback_dict["user_feedback"]["comment"], "Could be better")
        self.assertEqual(feedback_dict["metadata"]["llm"]["model"], "test-model")
        self.assertEqual(feedback_dict["metadata"]["llm"]["max_context_size"], 4096)
        self.assertIsInstance(feedback_dict["metadata"]["llm_responses"], list)

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    @patch("holmes.interactive.handle_feedback_command")
    def test_run_interactive_loop_feedback_with_conversation_history(
        self,
        mock_handle_feedback,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test feedback system with conversation history (LLM responses)."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        mock_session.prompt.side_effect = ["What is Kubernetes?", "/feedback", "/exit"]

        mock_build_messages.return_value = [
            {"role": "user", "content": "What is Kubernetes?"}
        ]
        mock_callback = Mock()

        # Mock the feedback handler to simulate feedback collection and callback invocation
        def mock_feedback_handler(_style, _console, feedback, feedback_callback):
            # Simulate what the real function does
            user_feedback = UserFeedback(is_positive=True, comment="Very helpful!")
            feedback.user_feedback = user_feedback
            feedback_callback(feedback)

        mock_handle_feedback.side_effect = mock_feedback_handler

        # Mock tracer for the normal query
        mock_tracer = Mock()
        mock_trace_span = Mock()
        mock_tracer.start_trace.return_value.__enter__ = Mock(
            return_value=mock_trace_span
        )
        mock_tracer.start_trace.return_value.__exit__ = Mock(return_value=None)
        mock_tracer.get_trace_url.return_value = None

        # Run the interactive loop with a conversation
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=None,
            include_files=None,
            show_tool_output=False,
            check_version=False,
            feedback_callback=mock_callback,
            tracer=mock_tracer,
        )

        # Verify callback was called with Feedback containing conversation history
        mock_callback.assert_called_once()
        call_args = mock_callback.call_args[0][0]

        # Test complete Feedback structure with conversation history
        self.assertIsInstance(call_args, Feedback)

        # Test UserFeedback component
        self.assertIsNotNone(call_args.user_feedback)
        self.assertEqual(call_args.user_feedback.is_positive, True)
        self.assertEqual(call_args.user_feedback.comment, "Very helpful!")

        # Test FeedbackMetadata with LLM responses
        self.assertIsNotNone(call_args.metadata)
        self.assertIsInstance(call_args.metadata, FeedbackMetadata)

        # Test LLM information
        self.assertEqual(call_args.metadata.llm.model, "test-model")
        self.assertEqual(call_args.metadata.llm.max_context_size, 4096)

        # Test LLM responses list contains the conversation
        self.assertIsInstance(call_args.metadata.llm_responses, list)
        self.assertGreaterEqual(
            len(call_args.metadata.llm_responses), 1
        )  # Should have at least one exchange

        # Test to_dict() functionality with conversation history
        feedback_dict = call_args.to_dict()
        self.assertIn("metadata", feedback_dict)
        self.assertIn("llm_responses", feedback_dict["metadata"])
        self.assertIsInstance(feedback_dict["metadata"]["llm_responses"], list)

        # If there are responses, verify their structure
        if feedback_dict["metadata"]["llm_responses"]:
            first_response = feedback_dict["metadata"]["llm_responses"][0]
            self.assertIn("user_ask", first_response)
            self.assertIn("response", first_response)
            self.assertIsInstance(first_response["user_ask"], str)
            self.assertIsInstance(first_response["response"], str)

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    def test_run_interactive_loop_feedback_command_without_callback(
        self,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test interactive loop with /feedback command when no callback is provided."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        mock_session.prompt.side_effect = ["/feedback", "/exit"]

        mock_build_messages.return_value = []

        # Run the interactive loop without feedback callback
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=None,
            include_files=None,
            show_tool_output=False,
            check_version=False,
            feedback_callback=None,  # No callback
        )

        # Verify "Unknown command" message was displayed
        unknown_calls = [
            call_args
            for call_args in self.mock_console.print.call_args_list
            if "Unknown command" in str(call_args)
        ]
        self.assertTrue(len(unknown_calls) > 0)

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    def test_run_interactive_loop_feedback_help_filtering(
        self,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test that help command filters out feedback when callback is None."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        mock_session.prompt.side_effect = ["/help", "/exit"]

        mock_build_messages.return_value = []

        # Run without feedback callback
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=None,
            include_files=None,
            show_tool_output=False,
            check_version=False,
            feedback_callback=None,
        )

        # Check all printed messages
        all_prints = [
            str(call_args) for call_args in self.mock_console.print.call_args_list
        ]

        # Should contain help for other commands but not feedback
        has_help_command = any("/help" in print_msg for print_msg in all_prints)
        has_exit_command = any("/exit" in print_msg for print_msg in all_prints)
        has_feedback_command = any("/feedback" in print_msg for print_msg in all_prints)

        self.assertTrue(has_help_command)
        self.assertTrue(has_exit_command)
        self.assertFalse(has_feedback_command)  # Should be filtered out

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    def test_run_interactive_loop_feedback_help_not_filtering_with_callback(
        self,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test that help command shows feedback when callback is provided."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        mock_session.prompt.side_effect = ["/help", "/exit"]

        mock_build_messages.return_value = []
        mock_callback = Mock()

        # Run with feedback callback
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=None,
            include_files=None,
            show_tool_output=False,
            check_version=False,
            feedback_callback=mock_callback,
        )

        # Check all printed messages
        all_prints = [
            str(call_args) for call_args in self.mock_console.print.call_args_list
        ]

        # Should contain help for feedback command
        has_feedback_command = any("/feedback" in print_msg for print_msg in all_prints)
        self.assertTrue(has_feedback_command)  # Should be shown

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    def test_run_interactive_loop_with_initial_input(
        self,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test interactive loop with initial user input."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        mock_session.prompt.side_effect = [
            "/exit"
        ]  # Only need exit after initial input

        initial_input = "What is kubernetes?"
        mock_build_messages.return_value = [{"role": "user", "content": initial_input}]

        # Mock tracer
        mock_tracer = Mock()
        mock_trace_span = Mock()
        mock_tracer.start_trace.return_value.__enter__ = Mock(
            return_value=mock_trace_span
        )
        mock_tracer.start_trace.return_value.__exit__ = Mock(return_value=None)
        mock_tracer.get_trace_url.return_value = None

        # Run the interactive loop
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=initial_input,
            include_files=None,
            show_tool_output=False,
            check_version=False,
            tracer=mock_tracer,
        )

        # Verify initial input was displayed
        initial_calls = [
            call_args
            for call_args in self.mock_console.print.call_args_list
            if initial_input in str(call_args)
        ]
        self.assertTrue(len(initial_calls) > 0)

        # Verify AI was called with initial input
        self.mock_ai.call_stream.assert_called_once()

    @patch("holmes.interactive.check_version_async")
    @patch("holmes.interactive.PromptSession")
    @patch("holmes.interactive.build_initial_ask_messages")
    @patch(
        "holmes.interactive.config_path_dir", new_callable=lambda: tempfile.gettempdir()
    )
    def test_run_interactive_loop_exception_handling(
        self,
        mock_config_dir,
        mock_build_messages,
        mock_prompt_session_class,
        mock_check_version,
    ):
        """Test interactive loop exception handling."""
        mock_session = Mock()
        mock_prompt_session_class.return_value = mock_session
        # First call raises exception, second call exits
        mock_session.prompt.side_effect = [Exception("Test error"), "/exit"]

        mock_build_messages.return_value = []

        # Run the interactive loop
        run_interactive_loop(
            ai=self.mock_ai,
            console=self.mock_console,
            initial_user_input=None,
            include_files=None,
            show_tool_output=False,
            check_version=False,
        )

        # Verify error was displayed
        error_calls = [
            call_args
            for call_args in self.mock_console.print.call_args_list
            if "Error:" in str(call_args)
        ]
        self.assertTrue(len(error_calls) > 0)

    def test_run_interactive_loop_unsupported_commands_without_callback(self):
        """Test that feedback command is not available when callback is None."""
        with patch("holmes.interactive.check_version_async"), patch(
            "holmes.interactive.PromptSession"
        ) as mock_prompt_session_class, patch(
            "holmes.interactive.build_initial_ask_messages"
        ), patch("holmes.interactive.config_path_dir", new=tempfile.gettempdir()):
            mock_session = Mock()
            mock_prompt_session_class.return_value = mock_session
            mock_session.prompt.side_effect = ["/help", "/exit"]

            # Run the interactive loop without feedback callback
            run_interactive_loop(
                ai=self.mock_ai,
                console=self.mock_console,
                initial_user_input=None,
                include_files=None,
                show_tool_output=False,
                check_version=False,
                feedback_callback=None,  # No callback
            )

            # Verify feedback command is not shown in help
            help_calls = [
                str(call_args) for call_args in self.mock_console.print.call_args_list
            ]

            # The feedback command should not be shown since callback is None
            has_feedback_in_help = any(
                "/feedback" in call_str for call_str in help_calls
            )
            self.assertFalse(has_feedback_in_help)


class TestRendererEndToEnd(unittest.TestCase):
    """End-to-end tests for AgenticProgressRenderer with real Rich Console.

    These tests exercise the full lifecycle: start() → handle_event() → flush(),
    using a real Console(record=True) to capture actual rendered output.
    """

    def _make_console(self):
        return Console(width=100, record=True, force_terminal=True, color_system=None)

    def _make_event(self, event_type, data=None):
        return StreamMessage(event=event_type, data=data or {})

    def test_full_lifecycle_with_tools(self):
        """Full start → tools → AI message → flush lifecycle renders correctly."""
        console = self._make_console()
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)

        renderer.start()
        all_tool_calls = []
        history = []

        # Tool 1: start
        renderer.handle_event(
            self._make_event(StreamEvents.START_TOOL, {"tool_name": "kubectl_get"}),
            all_tool_calls, history,
        )

        # Tool 1: complete with output
        renderer.handle_event(
            self._make_event(StreamEvents.TOOL_RESULT, {
                "tool_name": "kubectl_get",
                "description": "kubectl get pods --all-namespaces",
                "toolset_name": "kubernetes/core",
                "result": {
                    "data": "NAMESPACE  NAME       READY  STATUS\ndefault    nginx-abc  1/1    Running",
                    "elapsed_seconds": 1.5,
                },
            }),
            all_tool_calls, history,
        )

        # Tool 2: start + complete with empty output (error)
        renderer.handle_event(
            self._make_event(StreamEvents.START_TOOL, {"tool_name": "Fetch Runbook"}),
            all_tool_calls, history,
        )
        renderer.handle_event(
            self._make_event(StreamEvents.TOOL_RESULT, {
                "tool_name": "Fetch Runbook",
                "description": "Fetch Runbook cluster-problems.md",
                "toolset_name": "runbook",
                "result": {"data": "", "elapsed_seconds": 0.0},
            }),
            all_tool_calls, history,
        )

        # AI message triggers summary
        renderer.handle_event(
            self._make_event(StreamEvents.AI_MESSAGE, {
                "content": "All pods are running normally.",
            }),
            all_tool_calls, history,
        )

        renderer.flush()

        output = console.export_text()

        # Verify tools summary is printed
        assert "kubectl get pods --all-namespaces" in output, f"Tool description not in output:\n{output}"
        assert "Fetch Runbook cluster-problems.md" in output, f"Error tool not in output:\n{output}"
        assert "(error)" in output, f"Error marker not in output:\n{output}"

        # Verify AI message content is printed
        assert "All pods are running normally." in output, f"AI message not in output:\n{output}"

        # Verify stats line
        assert "tokens" in output.lower(), f"Stats line not in output:\n{output}"

    def test_no_data_pane_before_tool_output(self):
        """Data pane should not appear until tools produce output."""
        console = Console(width=100, force_terminal=True, color_system=None)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        renderer._thinking = True
        renderer._start_time = time.time()

        # Initial state: no data pane
        display_text = self._render_to_text(renderer)
        assert "Data" not in display_text, f"Data pane appeared too early:\n{display_text}"

        # Add tasks — still no data pane
        renderer._live_tasks = [
            {"content": "Check pods", "status": "pending"},
        ]
        display_text = self._render_to_text(renderer)
        assert "Data" not in display_text, f"Data pane appeared with only tasks:\n{display_text}"
        assert "Check pods" in display_text, f"Tasks not shown:\n{display_text}"

        # Add in-flight tool — still no data pane
        renderer._in_flight[1] = ("kubectl_get", time.time())
        renderer._thinking = False
        display_text = self._render_to_text(renderer)
        assert "Data" not in display_text, f"Data pane appeared during in-flight tool:\n{display_text}"
        assert "kubectl_get" in display_text, f"In-flight tool not shown:\n{display_text}"

        # Now add output — data pane should appear
        del renderer._in_flight[1]
        renderer._thinking = True
        renderer._tool_history.append(("kubectl_get", "get pods", "k8s", 1.0, 100, False))
        renderer._ingest_output("kubectl_get", "some output data", description="get pods")
        display_text = self._render_to_text(renderer)
        assert "Data" in display_text, f"Data pane did not appear after output:\n{display_text}"
        assert "some output data" in display_text, f"Output not in data pane:\n{display_text}"

    def test_data_pane_fixed_width(self):
        """Data pane should take 50% of terminal width regardless of content."""
        console = Console(width=100, force_terminal=True, color_system=None)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        renderer._thinking = True
        renderer._start_time = time.time()

        # Add a tool with short output — data pane should still be ~50% wide
        renderer._tool_history.append(("tool1", "desc", "ts", 1.0, 10, False))
        renderer._ingest_output("tool1", "short", description="desc")

        display_text = self._render_to_text(renderer)

        # The data panel border should be ~50 chars (50% of 100)
        data_lines = [l for l in display_text.split("\n") if "Data" in l]
        assert data_lines, f"No Data header line found:\n{display_text}"
        data_header = data_lines[0]
        # With ratio=1:1, data pane should be close to 50 chars, not shrunk
        assert len(data_header.rstrip()) >= 40, (
            f"Data pane header too narrow ({len(data_header.rstrip())} chars), "
            f"expected ~50% width:\n{display_text}"
        )

    def test_error_tool_shows_token_count(self):
        """Error tools with output should show both token count and (error)."""
        console = Console(width=100, force_terminal=True, color_system=None)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        all_tool_calls = []

        renderer.handle_event(
            self._make_event(StreamEvents.START_TOOL, {"tool_name": "bad_query"}),
            all_tool_calls, [],
        )
        renderer.handle_event(
            self._make_event(StreamEvents.TOOL_RESULT, {
                "tool_name": "bad_query",
                "description": "bad query that returned error",
                "toolset_name": "test",
                "result": {
                    "data": "Error: connection refused to database server",
                    "elapsed_seconds": 0.5,
                    "error": True,
                },
            }),
            all_tool_calls, [],
        )

        # Tool should have output_len > 0 AND is_error
        assert len(renderer._tool_history) == 1
        _name, _desc, _ts, _elapsed, output_len, is_error = renderer._tool_history[0]
        assert is_error, "Tool should be marked as error"
        assert output_len > 0, "Tool should have output length despite error"

        # Render the left pane and verify both token count and (error) appear
        display_text = self._render_to_text(renderer)
        assert "tokens" in display_text.lower() or "token" in display_text.lower(), (
            f"Token count not shown for error tool:\n{display_text}"
        )
        assert "(error)" in display_text, f"Error marker not shown:\n{display_text}"

    def test_empty_output_shows_red_marker(self):
        """Empty tool output should show a visible red marker, not dim text."""
        console = Console(width=100, force_terminal=True, color_system=None)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        renderer._thinking = True
        renderer._start_time = time.time()

        renderer._tool_history.append(("bad_tool", "bad tool call", "test", 0.0, 0, True))
        renderer._ingest_output("bad_tool", "", description="bad tool call")

        display_text = self._render_to_text(renderer)
        assert "no output" in display_text, f"Empty marker not found:\n{display_text}"

    def test_log_buffering_filter(self):
        """Log filter should capture records and prevent them from passing through."""
        console = Mock(spec=Console)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        root = logging.getLogger()

        # Install filter on all handlers (matches production behavior)
        for handler in root.handlers:
            handler.addFilter(renderer._log_filter)
        try:
            test_logger = logging.getLogger("test.interactive.buffering")
            test_logger.error("This should be buffered")

            assert len(renderer._log_buffer) >= 1, (
                f"Expected at least 1 buffered log record, got {len(renderer._log_buffer)}"
            )
            assert renderer._log_buffer[0].getMessage() == "This should be buffered"
        finally:
            for handler in root.handlers:
                handler.removeFilter(renderer._log_filter)
            renderer._log_buffer.clear()

    def test_start_installs_log_filter_on_handlers(self):
        """start() should install the log filter on root logger's handlers."""
        console = self._make_console()
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        root = logging.getLogger()

        renderer.start()
        try:
            # Filter should be on at least one handler
            has_filter = any(
                renderer._log_filter in h.filters for h in root.handlers
            )
            assert has_filter, "Log filter not installed on any handler after start()"
        finally:
            renderer.flush()

        # After flush, filter should be removed from all handlers
        has_filter = any(
            renderer._log_filter in h.filters for h in root.handlers
        )
        assert not has_filter, "Log filter still on handlers after flush"

    def test_handle_event_tool_result_populates_data(self):
        """TOOL_RESULT events should populate tool history and data buffer."""
        console = Mock(spec=Console)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        all_tool_calls = []

        # Start a tool
        renderer.handle_event(
            self._make_event(StreamEvents.START_TOOL, {"tool_name": "my_tool"}),
            all_tool_calls, [],
        )
        assert len(renderer._in_flight) == 1
        assert renderer._thinking is False

        # Complete the tool
        renderer.handle_event(
            self._make_event(StreamEvents.TOOL_RESULT, {
                "tool_name": "my_tool",
                "description": "do something useful",
                "toolset_name": "test_toolset",
                "result": {
                    "data": "line 1\nline 2\nline 3",
                    "elapsed_seconds": 2.0,
                },
            }),
            all_tool_calls, [],
        )

        assert len(renderer._in_flight) == 0, "Tool still in flight after completion"
        assert renderer._thinking is True, "Should be thinking between tools"
        assert len(renderer._tool_history) == 1
        assert renderer._tool_history[0][1] == "do something useful"
        assert len(renderer._data_lines) > 0, "Data buffer should have content"
        assert any("line 1" in l for l in renderer._data_lines)

    def test_ai_message_stops_live_and_prints_summary(self):
        """AI_MESSAGE event should stop Live and print the summary."""
        console = self._make_console()
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)

        renderer.start()
        all_tool_calls = []

        # Run a tool through the full cycle
        renderer.handle_event(
            self._make_event(StreamEvents.START_TOOL, {"tool_name": "test_tool"}),
            all_tool_calls, [],
        )
        renderer.handle_event(
            self._make_event(StreamEvents.TOOL_RESULT, {
                "tool_name": "test_tool",
                "description": "test tool description",
                "toolset_name": "testing",
                "result": {"data": "some output", "elapsed_seconds": 0.5},
            }),
            all_tool_calls, [],
        )

        # AI message should stop Live and print summary
        renderer.handle_event(
            self._make_event(StreamEvents.AI_MESSAGE, {
                "content": "Here is my analysis.",
            }),
            all_tool_calls, [],
        )

        assert renderer._live is None, "Live display not stopped after AI_MESSAGE"
        assert renderer._summary_printed is True

        output = console.export_text()
        assert "test tool description" in output, f"Tool not in summary:\n{output}"
        assert "Here is my analysis." in output, f"AI message not printed:\n{output}"

    def test_multiple_tool_rounds_no_duplicate_summary(self):
        """Multiple tool rounds followed by AI message should print summary once."""
        console = self._make_console()
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)

        renderer.start()
        all_tool_calls = []

        # Round 1
        for tool_name in ["tool_a", "tool_b"]:
            renderer.handle_event(
                self._make_event(StreamEvents.START_TOOL, {"tool_name": tool_name}),
                all_tool_calls, [],
            )
            renderer.handle_event(
                self._make_event(StreamEvents.TOOL_RESULT, {
                    "tool_name": tool_name,
                    "description": f"run {tool_name}",
                    "toolset_name": "test",
                    "result": {"data": f"output from {tool_name}", "elapsed_seconds": 0.1},
                }),
                all_tool_calls, [],
            )

        # AI message
        renderer.handle_event(
            self._make_event(StreamEvents.AI_MESSAGE, {"content": "Done."}),
            all_tool_calls, [],
        )

        # flush should not duplicate
        renderer.flush()

        output = console.export_text()
        # Count occurrences of "Tools" panel header
        tools_count = output.count("Tools")
        assert tools_count <= 2, (  # Title + border
            f"Tools panel printed multiple times ({tools_count}):\n{output}"
        )

    def test_todo_write_updates_tasks(self):
        """TodoWrite tool results should update live tasks, not appear in tool history."""
        console = Mock(spec=Console)
        renderer = AgenticProgressRenderer(console, tool_number_offset=0)
        all_tool_calls = []

        renderer.handle_event(
            self._make_event(StreamEvents.START_TOOL, {"tool_name": "TodoWrite"}),
            all_tool_calls, [],
        )
        renderer.handle_event(
            self._make_event(StreamEvents.TOOL_RESULT, {
                "tool_name": "TodoWrite",
                "description": "TodoWrite",
                "toolset_name": "",
                "result": {
                    "data": "Tasks updated",
                    "elapsed_seconds": 0.0,
                    "params": {
                        "todos": [
                            {"content": "Check pods", "status": "in_progress"},
                            {"content": "Check logs", "status": "pending"},
                        ]
                    },
                },
            }),
            all_tool_calls, [],
        )

        assert renderer._live_tasks is not None, "Tasks not set"
        assert len(renderer._live_tasks) == 2
        assert renderer._live_tasks[0]["content"] == "Check pods"
        # TodoWrite should NOT appear in tool history
        assert len(renderer._tool_history) == 0, "TodoWrite should not be in tool history"
        # TodoWrite should NOT be in data buffer
        assert not any("TodoWrite" in l for l in renderer._data_lines), "TodoWrite in data buffer"

    def _render_to_text(self, renderer):
        """Render the display to plain text using a recording console."""
        capture = Console(width=100, record=True, force_terminal=True, color_system=None)
        display = renderer._build_display()
        capture.print(display)
        return capture.export_text()
