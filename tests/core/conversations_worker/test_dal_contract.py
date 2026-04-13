"""Unit tests for the SupabaseDal methods used by the M2 worker.

These tests use a minimal fake of the supabase client so we can verify:
 - post_conversation_events forwards ``_compact`` to the RPC correctly
 - get_conversation_events applies the compacted filter by default
"""
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from holmes.core.supabase_dal import SupabaseDal


def _build_dal(table_rows: Optional[List[Dict[str, Any]]] = None) -> SupabaseDal:
    dal = SupabaseDal.__new__(SupabaseDal)
    dal.enabled = True
    dal.account_id = "acc-1"
    dal.cluster = "cluster-1"
    dal.client = MagicMock()
    # capture RPC invocations
    dal.client.rpc = MagicMock()
    dal._last_rpc_args = None

    # Simulate table().select().eq().eq().order().order().execute()
    def _table_select_eq_order(rows):
        builder = MagicMock()
        state = {"rows": rows, "filters": []}

        def select(*args, **kwargs):
            return builder

        def eq(col, val):
            state["filters"].append((col, val))
            return builder

        def order(*args, **kwargs):
            return builder

        def execute():
            filtered = state["rows"]
            for col, val in state["filters"]:
                filtered = [r for r in filtered if r.get(col) == val]
            return MagicMock(data=filtered)

        builder.select = select
        builder.eq = eq
        builder.order = order
        builder.execute = execute
        return builder

    dal.client.table = lambda _name: _table_select_eq_order(table_rows or [])
    return dal


def test_post_conversation_events_forwards_compact_flag():
    dal = _build_dal()
    dal.client.rpc.return_value = MagicMock(execute=MagicMock(return_value=MagicMock(data=7)))

    dal.post_conversation_events(
        conversation_id="c",
        holmes_id="h",
        request_sequence=3,
        events=[{"event": "x", "data": {}, "ts": "t"}],
        compact=True,
    )
    dal.client.rpc.assert_called_once()
    args, _ = dal.client.rpc.call_args
    assert args[0] == "post_conversation_events"
    params = args[1]
    assert params["_compact"] is True
    assert params["_conversation_id"] == "c"
    assert params["_holmes_id"] == "h"
    assert params["_request_sequence"] == 3


def test_post_conversation_events_default_compact_false():
    dal = _build_dal()
    dal.client.rpc.return_value = MagicMock(execute=MagicMock(return_value=MagicMock(data=1)))
    dal.post_conversation_events(
        conversation_id="c",
        holmes_id="h",
        request_sequence=1,
        events=[{"event": "ai_message", "data": {}, "ts": "t"}],
    )
    _, _ = dal.client.rpc.call_args
    params = dal.client.rpc.call_args[0][1]
    assert params["_compact"] is False


def test_get_conversation_events_filters_compacted_by_default():
    rows = [
        {"seq": 1, "compacted": True, "conversation_id": "c"},
        {"seq": 2, "compacted": True, "conversation_id": "c"},
        {"seq": 3, "compacted": False, "conversation_id": "c"},
        {"seq": 4, "compacted": False, "conversation_id": "c"},
    ]
    dal = _build_dal(rows)
    out = dal.get_conversation_events(conversation_id="c")
    assert [r["seq"] for r in out] == [3, 4]


def test_get_conversation_events_include_compacted_returns_all():
    rows = [
        {"seq": 1, "compacted": True, "conversation_id": "c"},
        {"seq": 2, "compacted": True, "conversation_id": "c"},
        {"seq": 3, "compacted": False, "conversation_id": "c"},
    ]
    dal = _build_dal(rows)
    out = dal.get_conversation_events(conversation_id="c", include_compacted=True)
    assert [r["seq"] for r in out] == [1, 2, 3]
