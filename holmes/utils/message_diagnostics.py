"""Diagnostic helpers for inspecting the OpenAI-format message array.

Used to root-cause Bedrock errors like:
    "Expected toolResult blocks at messages.18.content for the following Ids: tooluse_..."
which fire when an assistant message has tool_use blocks that aren't followed by
matching tool_result blocks.
"""

from typing import Any, Dict, List


def summarize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a content-free structural summary suitable for log output."""
    summary: List[Dict[str, Any]] = []
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            summary.append({"index": idx, "type": type(msg).__name__})
            continue
        entry: Dict[str, Any] = {"index": idx, "role": msg.get("role")}
        content = msg.get("content")
        if isinstance(content, str):
            entry["content_len"] = len(content)
        elif isinstance(content, list):
            entry["content_blocks"] = [
                b.get("type") if isinstance(b, dict) else type(b).__name__
                for b in content
            ]
        elif content is None:
            entry["content"] = None
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            entry["tool_call_ids"] = [
                tc.get("id") for tc in tool_calls if isinstance(tc, dict)
            ]
            entry["pending_approval_ids"] = [
                tc.get("id")
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("pending_approval")
            ]
            entry["pending_frontend_ids"] = [
                tc.get("id")
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("pending_frontend")
            ]
        if msg.get("tool_call_id"):
            entry["tool_call_id"] = msg.get("tool_call_id")
        summary.append(entry)
    return summary


def find_orphan_tool_uses(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return assistant tool_call ids that lack a corresponding tool-role result.

    Each entry is {"assistant_index": i, "missing_tool_call_ids": [...]}.
    """
    if not messages:
        return []
    orphans: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            continue
        expected_ids = {
            tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")
        }
        seen: set = set()
        for j in range(i + 1, len(messages)):
            nxt = messages[j]
            if not isinstance(nxt, dict):
                break
            if nxt.get("role") == "tool":
                tcid = nxt.get("tool_call_id")
                if tcid:
                    seen.add(tcid)
                continue
            if nxt.get("role") == "user" and isinstance(nxt.get("content"), list):
                for block in nxt["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tcid = block.get("tool_use_id") or block.get("tool_call_id")
                        if tcid:
                            seen.add(tcid)
                continue
            break
        missing = expected_ids - seen
        if missing:
            orphans.append(
                {
                    "assistant_index": i,
                    "missing_tool_call_ids": sorted(m for m in missing if m),
                }
            )
    return orphans
