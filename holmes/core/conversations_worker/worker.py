import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

from starlette.requests import Request

from holmes.common.env_vars import (
    CONVERSATION_WORKER_MAX_CONCURRENT,
    CONVERSATION_WORKER_POLL_INTERVAL_SECONDS,
)
from holmes.core.conversations import build_chat_messages
from holmes.core.conversations_worker.event_publisher import (
    ConversationEventPublisher,
)
from holmes.core.conversations_worker.heartbeat import ConversationHeartbeat
from holmes.core.conversations_worker.models import (
    ConversationReassignedError,
    ConversationTask,
)
from holmes.core.conversations_worker.realtime_manager import RealtimeManager
from holmes.core.models import ChatRequest, ChatResponse
from holmes.core.tools import PrerequisiteCacheMode, ToolsetTag
from holmes.core.tools_utils.filesystem_result_storage import tool_result_storage
from holmes.core.tracing import TracingFactory
from holmes.utils.stream import StreamEvents

if TYPE_CHECKING:
    from fastapi.responses import StreamingResponse

    from holmes.config import Config
    from holmes.core.supabase_dal import SupabaseDal

ChatFunction = Callable[
    [ChatRequest, Request], Union["ChatResponse", "StreamingResponse"]
]

logger = logging.getLogger(__name__)


class ConversationWorker:
    """Active conversation processor.

    Advertises itself via Supabase Realtime Presence, picks up pending
    conversations from the database, and writes results directly to
    ``ConversationEvents`` — removing the dependency on a persistent
    SSE stream to Relay/Frontend.

    Architecture::

        ConversationWorker
          ├── RealtimeManager (background async thread)
          │     ├── Cluster Presence channel
          │     ├── Postgres Changes subscription
          │     └── Per-conversation Presence channels
          ├── Claim Loop (main worker thread)
          │     ├── Triggered by Realtime notifications OR polling fallback
          │     └── Claims via dal.claim_conversations()
          └── Processing Threads (up to MAX_CONCURRENT)
                ├── Reconstructs conversation history from ConversationEvents
                ├── Builds ChatRequest
                ├── Calls ai.call_stream()
                ├── Wraps stream with ConversationEventPublisher
                └── Calls dal.complete_conversation() on finish
    """

    def __init__(
        self,
        dal: "SupabaseDal",
        config: "Config",
        chat_function: ChatFunction,
    ):
        self._dal = dal
        self._config = config
        self._chat_function = chat_function
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Pod name in Kubernetes, or PID
        self._holmes_id = os.environ.get("HOSTNAME") or str(os.getpid())

        # Notification queue: RealtimeManager pushes here when pending
        # conversations are detected; the claim loop reads from it.
        self._notification_queue: queue.Queue = queue.Queue(maxsize=100)

        self._realtime_manager: Optional[RealtimeManager] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._active_count = 0
        self._active_count_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the conversation worker."""
        if not self._dal.enabled:
            logger.info(
                "ConversationWorker not started — Supabase DAL not enabled"
            )
            return
        if self._running:
            logger.warning("ConversationWorker is already running")
            return

        self._running = True

        # Thread pool for concurrent conversation processing
        self._executor = ThreadPoolExecutor(
            max_workers=CONVERSATION_WORKER_MAX_CONCURRENT,
            thread_name_prefix="conv-proc",
        )

        # Start Realtime manager (Presence + Postgres Changes)
        self._realtime_manager = RealtimeManager(
            supabase_url=self._dal.url,
            supabase_key=self._dal.api_key,
            account_id=self._dal.account_id,
            cluster_id=self._dal.cluster,
            holmes_id=self._holmes_id,
            notification_queue=self._notification_queue,
        )
        self._realtime_manager.start()

        # Start the claim loop
        self._thread = threading.Thread(
            target=self._claim_loop, daemon=True, name="conv-worker"
        )
        self._thread.start()
        logger.info("ConversationWorker started (holmes_id=%s)", self._holmes_id)

    def stop(self) -> None:
        """Stop the conversation worker gracefully."""
        self._running = False

        if self._realtime_manager:
            self._realtime_manager.stop()

        if self._executor:
            self._executor.shutdown(wait=False)

        if self._thread:
            self._thread.join(timeout=10)

        logger.info("ConversationWorker stopped")

    # ── Claim Loop ───────────────────────────────────────────────────

    def _claim_loop(self) -> None:
        """Main loop: wait for notifications or poll, then claim and dispatch."""
        # Immediately claim on startup to catch pending work from downtime
        self._try_claim_and_dispatch()

        while self._running:
            try:
                # Wait for a Realtime notification or timeout (polling fallback)
                timeout = CONVERSATION_WORKER_POLL_INTERVAL_SECONDS
                try:
                    self._notification_queue.get(timeout=timeout)
                    # Drain any additional notifications that arrived
                    while not self._notification_queue.empty():
                        try:
                            self._notification_queue.get_nowait()
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass  # Timeout — poll anyway

                self._try_claim_and_dispatch()
            except Exception:
                logger.exception("Error in ConversationWorker claim loop")

    def _try_claim_and_dispatch(self) -> None:
        """Claim pending conversations and dispatch them for processing."""
        with self._active_count_lock:
            if self._active_count >= CONVERSATION_WORKER_MAX_CONCURRENT:
                logger.debug(
                    "At capacity (%d/%d), skipping claim",
                    self._active_count,
                    CONVERSATION_WORKER_MAX_CONCURRENT,
                )
                return

        conversations = self._dal.claim_conversations(self._holmes_id)
        if not conversations:
            return

        logger.info("Claimed %d conversation(s)", len(conversations))
        for conv in conversations:
            with self._active_count_lock:
                if self._active_count >= CONVERSATION_WORKER_MAX_CONCURRENT:
                    logger.warning(
                        "Reached capacity after partial claim, remaining conversations stay pending"
                    )
                    break
                self._active_count += 1

            self._executor.submit(self._process_conversation_safe, conv)  # type: ignore[union-attr]

    # ── Conversation Processing ──────────────────────────────────────

    def _process_conversation_safe(self, conv: Dict[str, Any]) -> None:
        """Wrapper that catches all exceptions and always cleans up."""
        conversation_id = conv.get("conversation_id", "unknown")
        try:
            self._process_conversation(conv)
        except ConversationReassignedError:
            logger.info(
                "Conversation %s was reassigned, stopping processing",
                conversation_id,
            )
        except Exception:
            logger.exception(
                "Error processing conversation %s", conversation_id
            )
            try:
                self._dal.complete_conversation(
                    conversation_id=conversation_id,
                    request_sequence=conv.get("request_sequence", 1),
                    holmes_id=self._holmes_id,
                    status="failed",
                )
            except Exception:
                logger.exception(
                    "Failed to mark conversation %s as failed", conversation_id
                )
        finally:
            # Leave Presence and decrement active count
            if self._realtime_manager:
                self._realtime_manager.leave_conversation_presence(
                    conversation_id
                )
            with self._active_count_lock:
                self._active_count -= 1

    def _process_conversation(self, conv: Dict[str, Any]) -> None:
        """Process a single claimed conversation end-to-end."""
        conversation_id = conv["conversation_id"]
        request_sequence = conv["request_sequence"]
        metadata = conv.get("metadata") or {}

        logger.info(
            "Processing conversation %s (seq=%d, origin=%s)",
            conversation_id,
            request_sequence,
            conv.get("origin"),
        )

        # 1. Join conversation Presence (heartbeat)
        if self._realtime_manager:
            self._realtime_manager.join_conversation_presence(conversation_id)

        # 2. Reconstruct conversation history and extract user question
        task = self._build_task(conv)

        if not task.ask:
            logger.warning(
                "No user question found for conversation %s, marking as failed",
                conversation_id,
            )
            self._dal.complete_conversation(
                conversation_id=conversation_id,
                request_sequence=request_sequence,
                holmes_id=self._holmes_id,
                status="failed",
            )
            return

        # 3. Build LLM and messages
        storage = tool_result_storage()
        tool_results_dir = storage.__enter__()
        try:
            ai = self._config.create_toolcalling_llm(
                dal=self._dal,
                toolset_tag_filter=[ToolsetTag.CORE, ToolsetTag.CLUSTER],
                enable_all_toolsets_possible=False,
                prerequisite_cache=PrerequisiteCacheMode.DISABLED,
                reuse_executor=True,
                model=task.model,
                tracer=TracingFactory.get_active_tracer(),
                tool_results_dir=tool_results_dir,
            )

            runbooks = self._config.get_runbook_catalog()
            global_instructions = self._dal.get_global_instructions_for_account()

            messages = build_chat_messages(
                ask=task.ask,
                conversation_history=task.conversation_history,
                ai=ai,
                config=self._config,
                global_instructions=global_instructions,
                additional_system_prompt=task.additional_system_prompt,
                runbooks=runbooks,
                images=task.images,
            )

            # 4. Create heartbeat span
            heartbeat_span = ConversationHeartbeat(
                conversation_id=conversation_id,
                realtime_manager=self._realtime_manager,  # type: ignore[arg-type]
            ) if self._realtime_manager else None

            # 5. Create event publisher
            publisher = ConversationEventPublisher(
                dal=self._dal,
                conversation_id=conversation_id,
                holmes_id=self._holmes_id,
                request_sequence=request_sequence,
            )

            # 6. Run the LLM stream and publish events
            stream = ai.call_stream(
                msgs=messages,
                enable_tool_approval=task.enable_tool_approval,
                tool_decisions=task.tool_decisions,
                trace_span=heartbeat_span,
            )
            publisher.consume_stream(stream)

            # 7. Mark conversation as completed
            self._dal.complete_conversation(
                conversation_id=conversation_id,
                request_sequence=request_sequence,
                holmes_id=self._holmes_id,
                status="completed",
            )
            logger.info("Conversation %s completed successfully", conversation_id)
        finally:
            storage.__exit__(None, None, None)

    # ── History Reconstruction ───────────────────────────────────────

    def _build_task(self, conv: Dict[str, Any]) -> ConversationTask:
        """Build a :class:`ConversationTask` from a claimed conversation row
        and its ``ConversationEvents``.
        """
        conversation_id = conv["conversation_id"]
        request_sequence = conv["request_sequence"]
        metadata = conv.get("metadata") or {}

        task = ConversationTask(
            conversation_id=conversation_id,
            account_id=conv["account_id"],
            cluster_id=conv["cluster_id"],
            origin=conv.get("origin", "chat"),
            request_sequence=request_sequence,
            metadata=metadata,
            model=metadata.get("model"),
            additional_system_prompt=metadata.get("additional_system_prompt"),
            enable_tool_approval=metadata.get("enable_tool_approval", False),
        )

        # Fetch all events for this conversation
        events_rows = self._dal.get_conversation_events(conversation_id)
        if not events_rows:
            return task

        # For follow-ups (request_sequence > 1), reconstruct conversation
        # history from the last ai_answer_end event
        task.conversation_history = self._extract_conversation_history(events_rows)

        # Extract the user question from the latest request_sequence
        task.ask = self._extract_latest_ask(events_rows, request_sequence)

        # Extract tool decisions (for approval continuation)
        task.tool_decisions = self._extract_tool_decisions(
            events_rows, request_sequence
        )

        return task

    def _extract_conversation_history(
        self, events_rows: List[Dict[str, Any]]
    ) -> Optional[List[dict]]:
        """Find the last ``ai_answer_end`` event and extract its
        ``conversation_history`` payload.
        """
        for row in reversed(events_rows):
            events = row.get("events") or []
            for event in reversed(events):
                if event.get("event") == StreamEvents.ANSWER_END.value:
                    data = event.get("data") or {}
                    history = data.get("messages") or data.get("conversation_history")
                    if history:
                        return history
        return None

    def _extract_latest_ask(
        self, events_rows: List[Dict[str, Any]], request_sequence: int
    ) -> Optional[str]:
        """Extract the user question from events matching the current
        ``request_sequence``.
        """
        for row in events_rows:
            if row.get("request_sequence") != request_sequence:
                continue
            events = row.get("events") or []
            for event in events:
                if event.get("event") == "user_message":
                    data = event.get("data") or {}
                    return data.get("content") or data.get("ask")
        return None

    def _extract_tool_decisions(
        self, events_rows: List[Dict[str, Any]], request_sequence: int
    ) -> Optional[list]:
        """Extract tool approval decisions from events matching the current
        ``request_sequence``.
        """
        for row in events_rows:
            if row.get("request_sequence") != request_sequence:
                continue
            events = row.get("events") or []
            for event in events:
                if event.get("event") == "tool_decisions":
                    data = event.get("data") or {}
                    return data.get("decisions")
        return None
