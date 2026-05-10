# AGENTS.md

BrowserD is a browser automation daemon that manages parallel Chrome instances for AI agents. It wraps browser-use with a port-pool architecture — each task claims a dedicated Chrome process (not a tab) via unique `--remote-debugging-port=N` + `--user-data-dir`.

## Architecture

```
PortPool  → N Chrome instances (ports 9222–9222+N-1), isolated user-data-dirs
TaskManager → asyncio.Semaphore(N), session-aware task lifecycle
DaemonServer → Unix socket JSON-line protocol (~/.browserd/sock)
BrowserClient → async socket client
Providers → dynamic LLM class resolution (browserd/providers.py)
CLI → argparse wrapper (browser-cli)
```

### Key files

| File | What |
|------|------|
| `browserd/models.py` | Pydantic v2: DaemonConfig, TaskRecord, SessionRecord, TabRecord |
| `browserd/providers.py` | Provider registry: maps provider IDs to browser-use chat classes, env vars, models |
| `browserd/db.py` | SQLite WAL: tasks, sessions, session_tabs tables |
| `browserd/portpool.py` | Chrome process spawn/kill, port lifecycle with asyncio.Lock |
| `browserd/tasks.py` | TaskManager: queue, dequeue, _run_task, session handling, pause/inject control |
| `browserd/daemon.py` | DaemonServer: Unix socket, _dispatch, _wait + main() entry point |
| `browserd/client.py` | BrowserClient: send(), run(), wait(), resume(), pause(), inject(), state_*() |
| `browserd/cli.py` | argparse CLI: browser-cli run|pause|inject|resume-agent|state|cancel|... |

### Data flow

```
browser-cli run "prompt"
  → Unix socket → DaemonServer._dispatch("run")
  → TaskManager.run() → create_task() → _dequeue()
  → _run_with_semaphore() → import browser_use (heavy, serialized)
  → asyncio.Semaphore.acquire() → _run_task()
  → PortPool.acquire() → spawn Chrome → Browser(cdp_url)
  → Agent(task, llm, browser) → agent.run()
  → result → session update → port release (unless blocked/keep_open)
```

## Development rules

- **Python >= 3.10**, async/await everywhere
- **Pydantic v2** for all models (`ConfigDict`, `Field`, never `parse_obj`)
- **`from __future__ import annotations`** in every file
- **`TYPE_CHECKING` guards** for inter-module type references — never import heavy things at module level just for types
- **Heavy imports in methods**: `from browser_use import Agent` goes inside `_run_task()`, not at module top
- **Docstrings everywhere**: public methods require triple-quoted docstrings
- **No `**kwargs`**: explicit parameters always

### Netflix module pattern

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from browserd.models import DaemonConfig
```

### Status priority

```
blocked > done > failed
```
Blocked takes priority because it determines whether a port is held for resume.

### Port release rules

Port is NOT released when:
- `task_status == "blocked"` (user may resume)
- `keep_open == True` (session persists)
- `has_active` tabs exist in the session

## Use cases (tested)

| UC | Command | Expected |
|----|---------|----------|
| UC-01 | `run "prompt"` | Tab closes, port freed |
| UC-02 | `run --keep-open "prompt"` | Session auto-created, tab stays |
| UC-03 | `run --session <id> "prompt"` | Agent starts from active tab |
| UC-04 | Session after browser kill | New Chrome spawns, navigates to saved URL |
| UC-05 | Two `run` simultaneously | Two Chromes on different ports |
| UC-06 | 5 tasks on 4-port pool | 5th queues, runs when port freed |
| UC-07 | `resume <blocked_id>` | Re-runs task, activates existing tab |
| UC-08 | `cancel <id>` | Task cancelled, port freed |
| UC-09 | `pause <id>` | Agent freezes mid-step, browser stays open |
| UC-10 | `resume-agent <id>` | Unfreezes agent, continues from current state |
| UC-11 | `inject <id> "prompt"` | Replaces task mid-execution, auto-resumes |
| UC-12 | Auto-pause on login | Agent detects auth URLs, pauses after 2 hits |

## Setup

```bash
python3 -m venv ~/.local/share/browserd/venv
~/.local/share/browserd/venv/bin/pip install -e .
echo 'DEEPSEEK_API_KEY=sk-...' > ~/.browserd/.env
systemctl --user enable --now browserd
```
