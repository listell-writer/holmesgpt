import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional

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
from holmes.core.models import ChatRequest
from holmes.core.tools_utils.filesystem_result_storage import tool_result_storage
from holmes.utils.stream import StreamEvents

if TYPE_CHECKING:
    from holmes.config import Config
    from holmes.core.supabase_dal import SupabaseDal

logger = logging.getLogger(__name__)


class ConversationWorker:
    """Actively picks up pending conversations from Supabase and processes
    them, writing results directly to ``ConversationEvents``.

    Architecture::

        ConversationWorker
          +-- RealtimeManager  (background async thread)
          |     +-- Cluster Presence channel
          |     +-- Postgres Changes subscription
          |     +-- Per-conversation Presence channels
          +-- Claim Loop  (main worker thread)
          |     +-- Triggered by RealtimeManager notifications OR polling fallback
          |     +-- Claims via dal.claim_conversations()
          +-- Processing Threads  (up to MAX_CONCURRENT)
                +-- Reconstructs conversation history from ConversationEvents
                +-- Builds ChatRequest
                +-- Calls ai.call_stream()
                +-- Wraps stream with ConversationEventPublisher
                +-- Calls dal.complete_conversation() on finish
    """

    def __init__(
        self,
        dal: "SupabaseDal",
        config: "Config",
    ):
        self._dal = dal
        self._config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._holmes_id = os.environ.get("HOSTNAME") or str(os.getpid())

        self._notification_queue: queue.Queue[str] = queue.Queue(maxsize=100)
        self._executor = ThreadPoolExecutor(
            max_workers=CONVERSATION_WORKER_MAX_CONCURRENT,
            thread_name_prefix="conv-worker",
        )
        self._active_futures: Dict[str, object] = {}  # conversation_id -> Future

        self._realtime_manager: Optional[RealtimeManager] = None

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start the conversation worker and its Realtime manager."""
        if not self._dal.enabled:
            logger.info(
                "ConversationWorker not started - Supabase DAL not enabled"
            )
            return

        if self._running:
            logger.warning("ConversationWorker is already running")
            return

        self._running = True

        # Start Realtime manager for Presence and Postgres Changes
        self._realtime_manager = RealtimeManager(
            account_id=self._dal.account_id,
            cluster_id=self._dal.cluster,
            holmes_id=self._holmes_id,
            access_token=self._get_access_token(),
            notification_queue=self._notification_queue,
        )
        self._realtime_manager.start()

        # Start the claim loop thread
        self._thread = threading.Thread(
            target=self._claim_loop, daemon=True, name="conv-claim-loop"
        )
        self._thread.start()
        logger.info("ConversationWorker started (holmes_id=%s)", self._holmes_id)

    def stop(self) -> None:
        """Gracefully stop the conversation worker."""
        self._running = False
        if self._realtime_manager:
            self._realtime_manager.stop()
        self._executor.shutdown(wait=False)
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("ConversationWorker stopped")

    # ── Claim Loop ────────────────────────────────────────────────

    def _claim_loop(self) -> None:
        """Main loop: wait for notifications or poll, then claim and process."""
        # Immediately claim on startup to catch any pending conversations
        self._do_claim()

        while self._running:
            try:
                # Wait for a notification or timeout (poll fallback)
                timeout = CONVERSATION_WORKER_POLL_INTERVAL_SECONDS
                if (
                    self._realtime_manager
                    and self._realtime_manager.polling_fallback_active
                ):
                    # When Realtime is down, poll more frequently
                    timeout = min(timeout, 30)

                try:
                    self._notification_queue.get(timeout=timeout)
                    # Drain any additional notifications that queued up
                    while not self._notification_queue.empty():
                        try:
                            self._notification_queue.get_nowait()
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass  # Timeout — poll anyway

                self._do_claim()
                self._cleanup_completed_futures()
            except Exception:
                logger.exception("Error in ConversationWorker claim loop")

    def _do_claim(self) -> None:
        """Claim all pending conversations and submit them for processing."""
        claimed = self._dal.claim_conversations(self._holmes_id)
        if not claimed:
            return

        logger.info("Claimed %d conversation(s)", len(claimed))
        for row in claimed:
            conversation_id = row["conversation_id"]
            if conversation_id in self._active_futures:
                logger.warning(
                    "Conversation %s already being processed, skipping",
                    conversation_id,
                )
                continue

            future = self._executor.submit(
                self._process_conversation, row
            )
            self._active_futures[conversation_id] = future

    def _cleanup_completed_futures(self) -> None:
        """Remove finished futures from the active set."""
        done = [
            cid
            for cid, future in self._active_futures.items()
            if future.done()
        ]
        for cid in done:
            future = self._active_futures.pop(cid)
            exc = future.exception()
            if exc:
                logger.error(
                    "Conversation %s processing raised: %s", cid, exc
                )

    # ── Conversation Processing ───────────────────────────────────

    def _process_conversation(self, row: dict) -> None:
        """Process a single claimed conversation end-to-end."""
        conversation_id = row["conversation_id"]
        request_sequence = row["request_sequence"]

        logger.info(
            "Processing conversation %s (request_sequence=%d)",
            conversation_id,
            request_sequence,
        )

        # Join conversation Presence (heartbeat)
        if self._realtime_manager:
            self._realtime_manager.join_conversation_presence(conversation_id)

        try:
            task = self._build_task(row)
            self._execute_task(task)
            self._dal.complete_conversation(
                conversation_id=conversation_id,
                request_sequence=request_sequence,
                holmes_id=self._holmes_id,
                status="completed",
            )
            logger.info("Conversation %s completed", conversation_id)
        except ConversationReassignedError:
            logger.warning(
                "Conversation %s was reassigned, aborting", conversation_id
            )
        except Exception:
            logger.exception(
                "Error processing conversation %s", conversation_id
            )
            try:
                self._dal.complete_conversation(
                    conversation_id=conversation_id,
                    request_sequence=request_sequence,
                    holmes_id=self._holmes_id,
                    status="failed",
                )
            except Exception:
                logger.exception(
                    "Failed to mark conversation %s as failed",
                    conversation_id,
                )
        finally:
            if self._realtime_manager:
                self._realtime_manager.leave_conversation_presence(
                    conversation_id
                )

    def _build_task(self, row: dict) -> ConversationTask:
        """Build a ``ConversationTask`` from a claimed Conversations row
        and its events."""
        conversation_id = row["conversation_id"]
        request_sequence = row["request_sequence"]
        metadata = row.get("metadata") or {}

        task = ConversationTask(
            conversation_id=conversation_id,
            account_id=row["account_id"],
            cluster_id=row["cluster_id"],
            origin=row.get("origin", "chat"),
            request_sequence=request_sequence,
            metadata=metadata,
            model=metadata.get("model_name"),
            additional_system_prompt=metadata.get("additional_system_prompt"),
            enable_tool_approval=metadata.get("enable_tool_approval", False),
        )

        # Reconstruct conversation history and extract the user question
        events = self._dal.get_conversation_events(
            conversation_id=conversation_id,
            holmes_id=self._holmes_id,
        )

        task.ask, task.conversation_history, task.tool_decisions = (
            self._extract_from_events(events, request_sequence)
        )

        return task

    def _extract_from_events(
        self,
        event_rows: List[dict],
        current_request_sequence: int,
    ) -> tuple:
        """Extract the user question, conversation history, and tool
        decisions from ``ConversationEvents`` rows.

        Returns ``(ask, conversation_history, tool_decisions)``.
        """
        ask: Optional[str] = None
        conversation_history: Optional[list] = None
        tool_decisions: Optional[list] = None

        # Find the latest conversation_history from a previous ai_answer_end
        # event (for follow-up conversations)
        for row in event_rows:
            row_events = row.get("events", [])
            row_seq = row.get("request_sequence", 1)

            for evt in row_events:
                event_type = evt.get("event")
                data = evt.get("data", {})

                if event_type == "ai_answer_end" and row_seq < current_request_sequence:
                    # Use conversation_history from the last completed turn
                    messages = data.get("messages")
                    if messages:
                        conversation_history = messages

                if row_seq == current_request_sequence:
                    if event_type == "user_message":
                        ask = data.get("content", "")
                        # Check for tool decisions in the same event
                        td = data.get("tool_decisions")
                        if td:
                            tool_decisions = td

        if not ask:
            logger.warning(
                "No user_message found for request_sequence=%d, "
                "falling back to empty ask",
                current_request_sequence,
            )
            ask = ""

        return ask, conversation_history, tool_decisions

    def _execute_task(self, task: ConversationTask) -> None:
        """Execute the LLM conversation and publish events."""
        storage = tool_result_storage()
        tool_results_dir = storage.__enter__()
        try:
            ai = self._config.create_toolcalling_llm(
                dal=self._dal,
                model=task.model,
                tool_results_dir=tool_results_dir,
            )

            global_instructions = (
                self._dal.get_global_instructions_for_account()
            )
            runbooks = self._config.get_runbook_catalog()

            messages = build_chat_messages(
                ask=task.ask or "",
                conversation_history=task.conversation_history,
                ai=ai,
                config=self._config,
                global_instructions=global_instructions,
                additional_system_prompt=task.additional_system_prompt,
                runbooks=runbooks,
            )

            # Create heartbeat span
            heartbeat = ConversationHeartbeat(
                conversation_id=task.conversation_id,
                realtime_manager=self._realtime_manager,
            )

            # Create the event publisher
            publisher = ConversationEventPublisher(
                dal=self._dal,
                conversation_id=task.conversation_id,
                holmes_id=self._holmes_id,
                request_sequence=task.request_sequence,
            )

            # Run the LLM stream and publish events
            stream = ai.call_stream(
                msgs=messages,
                enable_tool_approval=task.enable_tool_approval,
                tool_decisions=task.tool_decisions,
                trace_span=heartbeat,
            )

            publisher.consume_stream(stream)
        finally:
            storage.__exit__(None, None, None)

    # ── Helpers ───────────────────────────────────────────────────

    def _get_access_token(self) -> str:
        """Extract the current JWT access token from the DAL's Supabase
        client session."""
        try:
            session = self._dal.client.auth.get_session()
            if session:
                return session.access_token
        except Exception:
            logger.warning("Could not get session access token", exc_info=True)
        return ""
