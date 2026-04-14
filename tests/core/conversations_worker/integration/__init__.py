"""Shared fixtures for conversation worker integration tests.

These tests require a running Holmes server with ENABLE_CONVERSATION_WORKER=true
and the following environment variables:

    ROBUSTA_UI_TOKEN     – base64-encoded JSON with store_url, api_key, email,
                           password, account_id
    CLUSTER_NAME         – cluster to target (must match Holmes's config)

Run with:
    poetry run pytest tests/core/conversations_worker/integration/ -m conversation_worker --no-cov -v
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions


def _decode_token() -> dict:
    raw = os.environ.get("ROBUSTA_UI_TOKEN")
    if not raw:
        pytest.skip("ROBUSTA_UI_TOKEN not set")
    return json.loads(base64.b64decode(raw))


@dataclass
class SupabaseFixture:
    """Thin wrapper around a logged-in Supabase client with helper methods."""

    client: Client
    account_id: str
    cluster_id: str
    user_id: str

    # Track conversation IDs for cleanup
    _created_conversations: list = field(default_factory=list)

    # ---- conversation helpers ----

    def create_conversation(
        self,
        ask: str,
        title: str = "integration test",
        enable_tool_approval: bool = False,
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        user_msg_data: Dict[str, Any] = {"ask": ask}
        if enable_tool_approval:
            user_msg_data["enable_tool_approval"] = True
        conv = self.client.rpc(
            "post_new_conversation",
            {
                "_account_id": self.account_id,
                "_cluster_id": self.cluster_id,
                "_origin": "chat",
                "_user_id": self.user_id,
                "_title": title,
                "_initial_events": [
                    {"event": "user_message", "data": user_msg_data, "ts": now_iso}
                ],
            },
        ).execute().data
        self._created_conversations.append(conv["conversation_id"])
        return conv

    def post_followup(
        self,
        conversation_id: str,
        events: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.client.rpc(
            "post_conversation_followup",
            {
                "_account_id": self.account_id,
                "_conversation_id": conversation_id,
                "_events": events,
                "_metadata": metadata or {},
            },
        ).execute().data

    def stop_conversation(self, conversation_id: str) -> None:
        self.client.rpc(
            "stop_conversation",
            {
                "_conversation_id": conversation_id,
                "_account_id": self.account_id,
            },
        ).execute()

    def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        return (
            self.client.table("Conversations")
            .select("*")
            .eq("conversation_id", conversation_id)
            .single()
            .execute()
        ).data

    def get_events(self, conversation_id: str) -> List[Dict[str, Any]]:
        return (
            self.client.table("ConversationEvents")
            .select("*")
            .eq("conversation_id", conversation_id)
            .order("seq")
            .execute()
        ).data or []

    def flat_event_types(self, conversation_id: str) -> List[str]:
        """Return a flat list of event type strings across all rows."""
        types = []
        for row in self.get_events(conversation_id):
            for ev in row.get("events") or []:
                types.append(ev.get("event"))
        return types

    def wait_for_status(
        self,
        conversation_id: str,
        target_statuses: set,
        timeout: float = 120,
        poll_interval: float = 1.0,
    ) -> Dict[str, Any]:
        """Poll until the conversation reaches one of the target statuses."""
        start = time.time()
        while time.time() - start < timeout:
            conv = self.get_conversation(conversation_id)
            if conv["status"] in target_statuses:
                return conv
            time.sleep(poll_interval)
        conv = self.get_conversation(conversation_id)
        raise TimeoutError(
            f"Conversation {conversation_id} did not reach {target_statuses} "
            f"within {timeout}s (current: {conv['status']})"
        )

    def wait_for_terminal(
        self,
        conversation_id: str,
        request_sequence: int,
        timeout: float = 120,
    ) -> Dict[str, Any]:
        """Wait until conversation is terminal for the given request_sequence."""
        start = time.time()
        while time.time() - start < timeout:
            conv = self.get_conversation(conversation_id)
            if (
                conv["request_sequence"] == request_sequence
                and conv["status"] in ("completed", "failed", "stopped")
            ):
                return conv
            time.sleep(1.0)
        conv = self.get_conversation(conversation_id)
        raise TimeoutError(
            f"Conversation {conversation_id} not terminal for seq={request_sequence} "
            f"within {timeout}s (status={conv['status']}, seq={conv['request_sequence']})"
        )

    def find_terminal_event(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Find the last terminal event (ai_answer_end / approval_required / error)."""
        for row in reversed(self.get_events(conversation_id)):
            for ev in reversed(row.get("events") or []):
                if ev.get("event") in ("ai_answer_end", "approval_required", "error"):
                    return ev
        return None


@pytest.fixture(scope="session")
def supabase_fx() -> SupabaseFixture:
    """Session-scoped Supabase client fixture.

    Requires ROBUSTA_UI_TOKEN and CLUSTER_NAME environment variables.
    """
    decoded = _decode_token()
    cluster_id = os.environ.get("CLUSTER_NAME")
    if not cluster_id:
        pytest.skip("CLUSTER_NAME not set")

    options = ClientOptions(postgrest_client_timeout=60)
    client = create_client(decoded["store_url"], decoded["api_key"], options)
    res = client.auth.sign_in_with_password(
        {"email": decoded["email"], "password": decoded["password"]}
    )
    client.auth.set_session(res.session.access_token, res.session.refresh_token)
    client.postgrest.auth(res.session.access_token)

    return SupabaseFixture(
        client=client,
        account_id=decoded["account_id"],
        cluster_id=cluster_id,
        user_id=res.user.id,
    )
