# HolmesGPT Interactive Mode - Bug Report

**Test Setup**: OpenRouter API with `openrouter/anthropic/claude-haiku-4.5`, tested via tmux TUI puppeteering.

## Screens Tested

1. **Startup/Welcome Screen** - Shows 6 predefined questions + free-text option. Works correctly.
2. **Question/Response Flow** - LLM responses render in bordered panels with markdown. Works correctly.
3. **Slash Commands** (`/help`, `/tools`, `/context`, `/clear`, `/auto`, `/last`, `/show`, `/run`, `/shell`, `/config`, `/exit`) - All functional.
4. **Tool Use with Auto-display** - Tools execute correctly, `/show N` opens scrollable viewer.
5. **Config TUI** (`/config`) - Toolset selector renders properly with up/down navigation.

---

## Bugs Found

### Bug 1: Ctrl+C Kills the Entire Process (Severity: Medium)

**Location**: `holmes/interactive.py:2810-2822`

**Steps to reproduce**:
1. Start interactive mode: `holmes ask -i`
2. Ask a question
3. While the LLM is generating a response, press Ctrl+C

**Expected**: The LLM response is interrupted and the user returns to the prompt (similar to how Escape works).

**Actual**: The entire holmesgpt process terminates immediately, dropping the user back to the shell.

**Root cause**: The main `while True` loop's `try/except` block catches `typer.Abort`, `EOFError`, and `Exception`, but NOT `KeyboardInterrupt`. In Python, `KeyboardInterrupt` does not inherit from `Exception`, so it propagates up and kills the process.

**Fix**: Add a `KeyboardInterrupt` handler alongside the existing `except` blocks:
```python
except KeyboardInterrupt:
    # Treat Ctrl+C same as Escape interrupt during LLM call
    if cancel_event is not None:
        cancel_event.set()
    else:
        console.print("Exiting interactive mode.")
        break
```

---

### Bug 2: Interrupted User Message Persists in Conversation Context (Severity: Medium)

**Location**: `holmes/interactive.py:2562-2572`

**Steps to reproduce**:
1. Start interactive mode
2. Ask: "Tell me about SECRET_KEYWORD_ALPHA_123"
3. Press Escape to interrupt
4. See "Interrupted." message
5. Ask: "What was the last thing I asked about?"
6. The LLM references SECRET_KEYWORD_ALPHA_123

**Expected**: After pressing Escape to interrupt, the cancelled question should be removed from conversation context.

**Actual**: The interrupted user message stays in context and influences subsequent responses. Verified with `/context` showing 241 user tokens after interrupt.

**Root cause**: At line 2562, the user message is appended to `messages`. At line 2572, `messages_snapshot = list(messages)` takes the snapshot AFTER the append. On interrupt (line 2751), `messages = messages_snapshot` restores to the snapshot which still contains the user message.

**Fix**: Take the snapshot BEFORE appending the user message:
```python
# Line 2572 should be moved before line 2562:
messages_snapshot = list(messages)  # snapshot WITHOUT new user message
messages.append({"role": "user", "content": user_input})
```

---

### Bug 3: `/run` Command Has No Safety Checks for Dangerous Commands (Severity: Low-Medium)

**Location**: `holmes/interactive.py` - `/run` command handler

**Steps to reproduce**:
1. Start interactive mode
2. Type: `/run rm -rf /`
3. The command is executed (only OS failsafe prevents damage)

**Expected**: Dangerous commands like `rm -rf /` should be blocked or require confirmation before execution.

**Actual**: `/run` executes any command without safety validation. The LLM's bash tool has safety checks, but `/run` bypasses them entirely. Commands like `rm -rf /home` would succeed silently.

**Note**: This is by design for power users, but a warning for destructive commands would be prudent.

---

### Bug 4: Tool Calls from Interrupted Requests are Lost from `/show` History (Severity: Low)

**Location**: `holmes/interactive.py:2750-2755, 2784-2787`

**Steps to reproduce**:
1. Ask a question that triggers tool calls (e.g., Kubernetes investigation)
2. Wait for some tool calls to execute (visible in progress display)
3. Press Escape to interrupt
4. Try `/show 1` to view the tool output

**Expected**: Tool calls that were executed should still be viewable via `/show`.

**Actual**: `/show` reports "No tool calls available in the conversation."

**Root cause**: On interrupt, the code at line 2750 does `messages = messages_snapshot; continue`, skipping line 2784-2787 which adds tool calls to `all_tool_calls_history`. The tool calls were displayed in the progress renderer but never persisted to the history.

---

## What Works Well

- **Escape interrupt** works correctly during LLM streaming (aside from Bug #2)
- **Startup menu** with numbered options is intuitive
- **Slash command completion** with prefix matching (e.g., `/con` matches `/config` and `/context`)
- **Tool output display** with numbered tool calls and `/show N` viewer
- **Shell integration** (`/shell`) with session recording and optional LLM sharing
- **Context tracking** (`/context`) shows detailed token breakdown
- **Error handling** for invalid commands, empty input, and edge cases
- **Config TUI** (`/config`) for enabling/disabling toolsets
- **Auto-display toggle** (`/auto`) for tool outputs
- **Clean exit** via `/exit`, Ctrl+D, or Escape on startup menu
