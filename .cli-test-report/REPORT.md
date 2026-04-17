# HolmesGPT CLI Interactive-Mode TUI Test Report

Date: 2026-04-17
Model: `openrouter/anthropic/claude-haiku-4.5`
(Note: `haiku-4.6` was requested but OpenRouter returns `"anthropic/claude-haiku-4.6 is not a valid model ID"` — the latest Haiku available via OpenRouter is 4.5.)
Classifier: `openrouter/openai/gpt-4.1`
Harness: tmux puppeteering (`tui-start/send/assert/capture/stop`) on isolated socket `nori-agent-sock`.
Terminal geometry: 120x40.

## Covered screens / flows
Each has a text dump under `screens/` — these are the raw screen captures from the TUI.

| # | File                        | Flow                                          |
|---|-----------------------------|-----------------------------------------------|
| 1 | `01_welcome.txt`            | Banner + toolset loader + sample-questions menu |
| 2 | `02_after_enter.txt`        | After selecting "Ask my own question…"        |
| 3 | `03_help.txt`               | `/help` output                                |
| 4 | `04_tools.txt`              | `/tools` table                                |
| 5 | `05_context.txt`            | `/context` output                             |
| 6 | `06_asking.txt`             | Round-trip "What is 2+2?" → `4`               |
| 7 | `07_clear.txt`              | `/clear`                                      |
| 8 | `08_completer.txt`          | Slash completer popup (typed `/`)             |
| 9 | `10_exit_clean.txt`         | Clean `/exit` (exit code 0)                   |
|10 | `11_run.txt`                | `/run echo hi-from-run-123` + share prompt    |
|11 | `12_auto.txt`               | `/auto` toggle                                |
|12 | `13_last.txt`               | `/last` with no prior tool calls              |
|13 | `14_uparrow.txt`            | Up-arrow history recall                       |
|14 | `15_config.txt`             | `/config` toolset editor                      |

## Bugs Found

### Bug 1 — Completer dropdown not erased on submit (reproducible on every slash command)

**Severity:** Medium (cosmetic, but every command is affected → noisy scrollback).

**Repro:** Any slash command. Every capture in `03_help.txt`, `04_tools.txt`, `05_context.txt`, `07_clear.txt`, `11_run.txt`, `12_auto.txt`, `13_last.txt`, `15_config.txt` shows the same artefact — the row above the `User: <cmd>` echo still contains the final contents of the completion menu row (the selected command, indented to the menu column).

Example from `05_context.txt`:
```
      /contextUser: /context
No conversation context yet.
User:
```

Example from `03_help.txt`:
```
      /helpUser: /help
Available commands:
  ...
```

**Root cause hypothesis:** `holmes/interactive.py:2391` constructs a `PromptSession` with `reserve_space_for_menu=12` and `complete_style=CompleteStyle.COLUMN`. When Enter is pressed, prompt_toolkit releases the reserved rows but the last-rendered menu cell (the matched command) is not cleared — the Rich `console.print(...)` of the echo (`User: /help`) then lands one row below the ghost. A `session.prompt(..., refresh_interval=0, mouse_support=False)` is unlikely to help; passing `erase_when_done=True` isn't available on PromptSession, so the fix is to either issue an explicit screen-control sequence (clear-to-end-of-screen) before printing, or not to rely on `reserve_space_for_menu` when we never want a persistent menu.

### Bug 2 — `/clear` does not clear the terminal screen

**Severity:** Medium (UX — the command claims to clear but doesn't).

**Repro:** See `07_clear.txt`. After asking "What is 2+2?" and then running `/clear`, the prior `AI Response` box for `4` is still visible, with the status line below:

```
╭─ AI Response ─────...─╮
│  4                    │
╰───...─────────────────╯
User:
      /clearUser: /clear
Screen cleared and context reset. You can now ask a new question.
User:
```

Expected: the terminal contents should be erased (e.g. `console.clear()` / ANSI `\x1b[2J\x1b[H`) when `/clear` runs, not just a message + conversation-history reset. Right now the documented "Clear screen and reset conversation context" does only the latter.

### Bug 3 — Welcome-menu selection leaves `User:` overlapping the last menu row

**Severity:** Low (cosmetic).

**Repro:** Launch `holmes ask -i`, press Enter on the default-highlighted "Ask my own question…" entry. See `02_after_enter.txt`:

```
...
    4. Scan my cluster for resource issues (high CPU, memory, disk pressure)
    5. What's rUser:
```

The fifth sample question is truncated mid-word because the `User:` prompt is drawn over it. `_show_sample_questions_menu` in `holmes/interactive.py` uses `erase_when_done=True` to tear down its `Application` (see 1804) but the Rich header it drew above the menu (`"Try one of these questions to get started:"` box) is left on screen, and the per-line renderer doesn't repaint to the exact number of lines the menu occupied.

### Bug 4 — Stray vertical border character from `/config` menu layout

**Severity:** Low (cosmetic).

**Repro:** Run `/config`. See `15_config.txt`. A lone `r` appears on the far right of line 3, likely the right edge of a box border that is being drawn one column past the 120-col width, so only the last character leaks in.

## Non-bugs observed / things that worked

- **`/help`, `/tools`, `/context`, `/auto`, `/last`, `/run`, `/config`, `/exit`** all functionally succeeded.
- LLM round-trip worked via OpenRouter + Haiku-4.5 (`06_asking.txt`).
- `/run` correctly captured the shell command output and presented the "Share with LLM?" prompt (`11_run.txt`).
- Up-arrow history recall works (`14_uparrow.txt`).
- `/exit` terminates cleanly with exit code 0 (`10_exit_clean.txt`, stderr log shows `EXIT=0`).
- Environment variable `MODEL=openrouter/...` was surfaced correctly in the status line: `Model: openrouter/anthropic/claude-haiku-4.5, 200K context, 40K max response (configured via $MODEL)`.
