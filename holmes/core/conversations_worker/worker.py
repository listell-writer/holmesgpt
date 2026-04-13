import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING, Union

from starlette.requests import Request

from holmes.common.env_vars import (
    CONVERSATION_WORKER_EVENT_BATCH_INTERVAL_SECONDS,
    CONVERSATION_WORKER_MAX_CONCURRENT,
    CONVERSATION_WORKER_POLL_INTERVAL_SECONDS_WITHOUT_REALTIME,
    CONVERSATION_WORKER_REALTIME_ENABLED,
)

# When Realtime is connected we do not poll at all — Postgres Changes events
# drive claims. A large safety-net sleep value is used so the loop simply
# blocks until the realtime manager signals activity (e.g. reconnect).
_REALTIME_CONNECTED_IDLE_SECONDS = 3600
from holmes.core.conversations_worker.event_publisher import (
    ConversationEventPublisher,
)
from holmes.core.conversations_worker.models import (
    EVENT_USER_MESSAGE,
    ConversationReassignedError,
    ConversationTask,
)
from holmes.core.models import ChatRequest

if TYPE_CHECKING:
    from fastapi.responses import StreamingResponse
    from holmes.config import Config
    from holmes.core.models import ChatResponse
    from holmes.core.supabase_dal import SupabaseDal

ChatFunction = Callable[
    [ChatRequest, Request], Union["ChatResponse", "StreamingResponse"]
]


class ConversationWorker:
    """
    M2 Conversation Worker.

    Active participant that picks up pending Conversation rows from Supabase,
    runs them through the existing /api/chat pipeline (via chat_function),
    and writes results back as ConversationEvents in real-time.
    """

    def __init__(
        self,
        dal: "SupabaseDal",
        config: "Config",
        chat_function: ChatFunction,
    ):
        self.dal = dal
        self.config = config
        self.chat_function = chat_function
        self.holmes_id = os.environ.get("HOSTNAME") or str(os.getpid())

        self._running = False
        self._claim_thread: Optional[threading.Thread] = None
        self._notify_event = threading.Event()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._active_conversation_ids: set = set()
        self._active_lock = threading.Lock()

        self._realtime_manager = None  # optional

    def start(self) -> None:
        if not self.dal.enabled:
            logging.info(
                "ConversationWorker not started - Supabase DAL not enabled"
            )
            return
        if self._running:
            logging.warning("ConversationWorker is already running")
            return

        self._running = True
        self._executor = ThreadPoolExecutor(
            max_workers=CONVERSATION_WORKER_MAX_CONCURRENT,
            thread_name_prefix="conversation-worker",
        )

        if CONVERSATION_WORKER_REALTIME_ENABLED:
            try:
                from holmes.core.conversations_worker.realtime_manager import (
                    RealtimeManager,
                )

                self._realtime_manager = RealtimeManager(
                    dal=self.dal,
                    holmes_id=self.holmes_id,
                    on_new_pending=self._notify_event.set,
                )
                self._realtime_manager.start()
            except Exception:
                logging.exception(
                    "Failed to start Realtime manager; continuing with polling only",
                    exc_info=True,
                )
                self._realtime_manager = None

        self._claim_thread = threading.Thread(
            target=self._claim_loop,
            daemon=True,
            name="conversation-claim-loop",
        )
        self._claim_thread.start()
        logging.info(
            "ConversationWorker started (holmes_id=%s, account=%s, cluster=%s, realtime=%s)",
            self.holmes_id,
            self.dal.account_id,
            self.dal.cluster,
            self._realtime_manager is not None,
        )

    def stop(self) -> None:
        logging.info("Stopping ConversationWorker...")
        self._running = False
        self._notify_event.set()
        if self._realtime_manager:
            try:
                self._realtime_manager.stop()
            except Exception:
                logging.exception("Error stopping realtime manager", exc_info=True)
        if self._executor:
            self._executor.shutdown(wait=False)
        if self._claim_thread:
            self._claim_thread.join(timeout=5)
        logging.info("ConversationWorker stopped")

    # ---- claim loop ----

    def _claim_loop(self) -> None:
        # Initial claim on startup to pick up any pending missed during downtime
        self._try_claim_and_dispatch()

        while self._running:
            # Timeout depends on realtime state:
            #  - Realtime not enabled: poll at WITHOUT_REALTIME interval
            #  - Realtime enabled but not connected: poll at same interval as a
            #    fallback while the realtime manager retries its WebSocket
            #  - Realtime enabled and connected: don't poll; Postgres Changes
            #    notifications set the event. A very large safety sleep keeps
            #    the loop responsive to shutdown.
            if self._realtime_connected():
                timeout = _REALTIME_CONNECTED_IDLE_SECONDS
            else:
                timeout = CONVERSATION_WORKER_POLL_INTERVAL_SECONDS_WITHOUT_REALTIME

            triggered = self._notify_event.wait(timeout=timeout)
            if not self._running:
                break
            self._notify_event.clear()
            try:
                self._try_claim_and_dispatch()
            except Exception:
                logging.exception(
                    "Error in ConversationWorker claim loop (triggered=%s)",
                    triggered,
                    exc_info=True,
                )

    def _realtime_connected(self) -> bool:
        if self._realtime_manager is None:
            return False
        try:
            return bool(self._realtime_manager.is_connected())
        except Exception:
            return False

    def _try_claim_and_dispatch(self) -> None:
        # Respect max concurrency: if we're at capacity, skip claiming
        with self._active_lock:
            active = len(self._active_conversation_ids)
        if active >= CONVERSATION_WORKER_MAX_CONCURRENT:
            logging.debug(
                "At max concurrency (%d), skipping claim", active
            )
            return

        claimed = self.dal.claim_conversations(self.holmes_id)
        if not claimed:
            return
        logging.info("Claimed %d conversation(s)", len(claimed))
        for conv in claimed:
            task = self._build_task_from_conversation_row(conv)
            if task is None:
                continue
            with self._active_lock:
                self._active_conversation_ids.add(task.conversation_id)
            self._executor.submit(self._process_conversation_safe, task)

    def _build_task_from_conversation_row(
        self, conv: Dict[str, Any]
    ) -> Optional[ConversationTask]:
        try:
            return ConversationTask(
                conversation_id=conv["conversation_id"],
                account_id=conv["account_id"],
                cluster_id=conv["cluster_id"],
                origin=conv.get("origin", "chat"),
                request_sequence=int(conv.get("request_sequence", 1)),
                metadata=conv.get("metadata") or {},
                title=conv.get("title"),
            )
        except Exception:
            logging.exception(
                "Failed to build conversation task from row: %s", conv, exc_info=True
            )
            return None

    # ---- per-conversation processing ----

    def _process_conversation_safe(self, task: ConversationTask) -> None:
        try:
            self._process_conversation(task)
        except ConversationReassignedError as e:
            # Another worker claimed this conversation or the initiator bumped
            # request_sequence (e.g. stop_conversation) while we were working.
            # The DB already reflects the new state — do NOT call
            # complete_conversation, which would either fail (status guard) or
            # race with the new owner.
            logging.warning(
                "Conversation %s was reassigned mid-process: %s",
                task.conversation_id,
                e,
            )
        except Exception as e:
            logging.exception(
                "Error processing conversation %s: %s",
                task.conversation_id,
                e,
                exc_info=True,
            )
            # Attempt to mark as failed
            try:
                self.dal.complete_conversation(
                    conversation_id=task.conversation_id,
                    request_sequence=task.request_sequence,
                    assignee=self.holmes_id,
                    status="failed",
                )
            except Exception:
                logging.exception(
                    "Failed to mark conversation %s as failed",
                    task.conversation_id,
                    exc_info=True,
                )
        finally:
            # Leave the conversation Presence channel regardless of outcome
            if self._realtime_manager is not None:
                try:
                    self._realtime_manager.leave_conversation_presence(
                        task.conversation_id
                    )
                except Exception:
                    logging.exception(
                        "Failed to leave conversation presence for %s",
                        task.conversation_id,
                        exc_info=True,
                    )
            with self._active_lock:
                self._active_conversation_ids.discard(task.conversation_id)

    def _process_conversation(self, task: ConversationTask) -> None:
        # Join per-conversation Presence channel so external observers (Relay)
        # can see that this conversation has a live Holmes working on it.
        if self._realtime_manager is not None:
            try:
                self._realtime_manager.join_conversation_presence(
                    task.conversation_id
                )
            except Exception:
                logging.exception(
                    "Failed to join conversation presence for %s",
                    task.conversation_id,
                    exc_info=True,
                )

        # Load events and extract the user ask + conversation history
        events = self.dal.get_conversation_events(task.conversation_id)
        self._hydrate_task_from_events(task, events)

        # A follow-up may carry only tool_decisions / frontend_tool_results
        # (no new user question). In that case Holmes resumes the prior
        # assistant turn — we reuse the previous ask as a placeholder for
        # ChatRequest (which requires `ask: str`), but no new user message
        # is appended to the history (see _run_chat_and_publish).
        resume_only = bool(
            not task.ask and (task.tool_decisions or task.frontend_tool_results)
        )
        if resume_only:
            # Pull the last user text from the reconstructed history for the
            # ChatRequest field; not used to build a new prompt.
            task.ask = self._extract_last_user_ask(task.conversation_history) or "Continue"

        if not task.ask:
            logging.warning(
                "Conversation %s has no user question, marking as failed",
                task.conversation_id,
            )
            self.dal.complete_conversation(
                conversation_id=task.conversation_id,
                request_sequence=task.request_sequence,
                assignee=self.holmes_id,
                status="failed",
            )
            return

        publisher = ConversationEventPublisher(
            dal=self.dal,
            conversation_id=task.conversation_id,
            assignee=self.holmes_id,
            request_sequence=task.request_sequence,
            batch_interval_seconds=CONVERSATION_WORKER_EVENT_BATCH_INTERVAL_SECONDS,
        )

        # Build ChatRequest
        chat_request = ChatRequest(
            ask=task.ask,
            images=task.images,
            model=task.model,
            conversation_history=task.conversation_history,
            stream=True,
            additional_system_prompt=task.additional_system_prompt,
            enable_tool_approval=task.enable_tool_approval,
            tool_decisions=task.tool_decisions,  # type: ignore[arg-type]
            frontend_tool_results=task.frontend_tool_results,  # type: ignore[arg-type]
        )
        # Flag used later to skip build_chat_messages for pure resumes
        chat_request_is_resume_only = resume_only

        # Call the chat function to get a StreamingResponse — we need the raw
        # StreamMessage generator not the SSE-wrapped one, so we need a different
        # path. The cleanest way is to build the LLM call directly.
        self._run_chat_and_publish(
            task, chat_request, publisher, resume_only=chat_request_is_resume_only
        )

    def _hydrate_task_from_events(
        self, task: ConversationTask, events: List[Dict[str, Any]]
    ) -> None:
        """
        Extract the latest user ask + model/additional_system_prompt/etc.
        Also reconstruct conversation_history from the previous terminal event.

        ``events`` is the flat chronological event list returned by
        ``get_conversation_events`` RPC: ``[{event, data, ts}, ...]`` sorted by
        ``(seq, ord)``. Turn boundaries are detected by the ``user_message``
        event itself. Algorithm:
         1. Find the index of the LATEST ``user_message`` event — that's the
            current turn's request.
         2. Among events with index < that, find the latest terminal event
            (``ai_answer_end`` or ``approval_required``). Its ``messages``
            array is the conversation history the LLM should resume from.
         3. Extract the current turn's ask / tool_decisions / etc. from the
            latest ``user_message``'s data.
        """
        current_user_msg: Optional[Dict[str, Any]] = None
        current_user_idx: int = -1
        last_terminal_messages: Optional[list] = None
        last_terminal_idx: int = -1

        for idx, ev in enumerate(events):
            if ev.get("event") == EVENT_USER_MESSAGE:
                current_user_idx = idx
                current_user_msg = ev

        upper = current_user_idx if current_user_idx >= 0 else len(events)
        for idx in range(upper):
            ev = events[idx]
            if ev.get("event") in ("ai_answer_end", "approval_required"):
                messages = (ev.get("data") or {}).get("messages")
                if messages:
                    last_terminal_idx = idx
                    last_terminal_messages = messages

        if current_user_msg is not None:
            data = current_user_msg.get("data") or {}
            if data.get("ask"):
                task.ask = data["ask"]
            if data.get("images"):
                task.images = data["images"]
            if data.get("model"):
                task.model = data["model"]
            if data.get("additional_system_prompt"):
                task.additional_system_prompt = data["additional_system_prompt"]
            if data.get("tool_decisions"):
                task.tool_decisions = data["tool_decisions"]
                task.enable_tool_approval = True
            if data.get("frontend_tool_results"):
                task.frontend_tool_results = data["frontend_tool_results"]
            if "bash_enabled" in data:
                task.bash_enabled = data["bash_enabled"]
            if "fast_mode" in data:
                task.fast_mode = data["fast_mode"]
            if "enable_tool_approval" in data:
                task.enable_tool_approval = bool(data["enable_tool_approval"])

        if last_terminal_messages is not None:
            task.conversation_history = last_terminal_messages
            logging.debug(
                "Reconstructed conversation history from event index=%d for conv %s",
                last_terminal_idx,
                task.conversation_id,
            )

    @staticmethod
    def _extract_last_user_ask(history: Optional[list]) -> Optional[str]:
        """Pull the most recent user message text from an OpenAI-format history.

        Tolerates malformed (non-dict) entries by skipping them.
        """
        if not history:
            return None
        for msg in reversed(history):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                # Vision message: find the first text part
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            return text
        return None

    def _run_chat_and_publish(
        self,
        task: ConversationTask,
        chat_request: ChatRequest,
        publisher: ConversationEventPublisher,
        resume_only: bool = False,
    ) -> None:
        """
        Run Holmes on the chat_request and stream StreamMessages into the publisher.
        Mirrors server.py::chat() for the streaming path but hands raw StreamMessages
        to the publisher instead of SSE-wrapping.
        """
        # Imports here to avoid circular deps at module load time
        from holmes.core.prompt import PromptComponent
        from holmes.core.tools import PrerequisiteCacheMode, ToolsetTag
        from holmes.core.tools_utils.filesystem_result_storage import (
            tool_result_storage,
        )
        from holmes.core.conversations import build_chat_messages
        from holmes.core.tracing import TracingFactory

        server_tracer = TracingFactory.create_tracer(
            trace_type=os.environ.get("HOLMES_TRACE_BACKEND")
        )

        runbooks = self.config.get_runbook_catalog()

        prompt_component_overrides = None
        behavior_controls = {}
        if task.bash_enabled is not None:
            behavior_controls["bash_enabled"] = task.bash_enabled
        if task.fast_mode is not None:
            behavior_controls["fast_mode"] = task.fast_mode
        if behavior_controls:
            prompt_component_overrides = {}
            for k, v in behavior_controls.items():
                try:
                    prompt_component_overrides[PromptComponent(k.lower())] = v
                except ValueError:
                    pass

        storage = tool_result_storage()
        tool_results_dir = storage.__enter__()
        try:
            ai = self.config.create_toolcalling_llm(
                dal=self.dal,
                toolset_tag_filter=[ToolsetTag.CORE, ToolsetTag.CLUSTER],
                enable_all_toolsets_possible=False,
                prerequisite_cache=PrerequisiteCacheMode.DISABLED,
                reuse_executor=True,
                model=chat_request.model,
                tracer=server_tracer,
                tool_results_dir=tool_results_dir,
            )

            global_instructions = self.dal.get_global_instructions_for_account()
            if resume_only and chat_request.conversation_history:
                # Pure tool-decision / frontend-tool-result resume. Don't append
                # a new user message — call_stream consumes the existing history
                # plus tool_decisions to produce the next turn.
                messages = list(chat_request.conversation_history)
            else:
                messages = build_chat_messages(
                    chat_request.ask,
                    chat_request.conversation_history,
                    ai=ai,
                    config=self.config,
                    global_instructions=global_instructions,
                    additional_system_prompt=chat_request.additional_system_prompt,
                    runbooks=runbooks,
                    images=chat_request.images,
                    prompt_component_overrides=prompt_component_overrides,
                )

            # Write an initial ai_message event (optional) - skip; call_stream will emit events
            trace_span = server_tracer.start_trace("holmesgpt.investigation")
            trace_span.log(
                metadata={
                    "holmesgpt.investigation.question": chat_request.ask[:1024],
                    "holmesgpt.investigation.stream": True,
                    "holmesgpt.conversation_id": task.conversation_id,
                }
            )

            try:
                stream = ai.call_stream(
                    msgs=messages,
                    enable_tool_approval=chat_request.enable_tool_approval or False,
                    tool_decisions=chat_request.tool_decisions,
                    frontend_tool_results=chat_request.frontend_tool_results,
                    response_format=chat_request.response_format,
                    trace_span=trace_span,
                )

                terminal = publisher.consume(stream)
                status = self._terminal_to_status(terminal)
                if status == "completed" or status == "failed":
                    ok = self.dal.complete_conversation(
                        conversation_id=task.conversation_id,
                        request_sequence=task.request_sequence,
                        assignee=self.holmes_id,
                        status=status,
                    )
                    if not ok:
                        logging.warning(
                            "Failed to mark conversation %s complete (status=%s)",
                            task.conversation_id,
                            status,
                        )
                elif status == "awaiting_approval":
                    # Leave the conversation in 'running' state — actually approvals
                    # per spec should mark the current request as complete so the
                    # follow-up can re-pend it. We treat approval_required as completed
                    # for the current request_sequence.
                    self.dal.complete_conversation(
                        conversation_id=task.conversation_id,
                        request_sequence=task.request_sequence,
                        assignee=self.holmes_id,
                        status="completed",
                    )
                else:
                    logging.warning(
                        "Conversation %s ended without a terminal event",
                        task.conversation_id,
                    )
                    self.dal.complete_conversation(
                        conversation_id=task.conversation_id,
                        request_sequence=task.request_sequence,
                        assignee=self.holmes_id,
                        status="failed",
                    )
            finally:
                trace_span.end()
        except ConversationReassignedError as e:
            logging.warning(
                "Conversation %s was reassigned: %s", task.conversation_id, e
            )
        finally:
            storage.__exit__(None, None, None)

    @staticmethod
    def _terminal_to_status(terminal) -> str:
        from holmes.utils.stream import StreamEvents

        if terminal == StreamEvents.ANSWER_END:
            return "completed"
        if terminal == StreamEvents.ERROR:
            return "failed"
        if terminal == StreamEvents.APPROVAL_REQUIRED:
            return "awaiting_approval"
        return "unknown"
