# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

BrowserD is an async Python daemon that manages parallel Chrome instances for browser automation. It wraps browser-use with a port-pool architecture — each task claims a dedicated Chrome process via unique `--remote-debugging-port=N`.

## High-Level Architecture

- **PortPool** (`browserd/portpool.py`): Manages 4 CDP ports (9222–9225). Spawns/kills Chrome processes, health-checks via `/json/version`, one `asyncio.Lock` for port selection.
- **TaskManager** (`browserd/tasks.py`): Task queue + `asyncio.Semaphore(N)`. `_run_task()` handles the full lifecycle: acquire port → create Browser → run Agent → process result → session update → port release.
- **DaemonServer** (`browserd/daemon.py`): Unix socket server at `~/.browserd/sock`. JSON-line protocol. `_dispatch()` maps commands to TaskManager methods.
- **BrowserClient** (`browserd/client.py`): Async socket client. Methods: `run()`, `wait()`, `resume()`, `cancel()`, `state_*()`.
- **CLI** (`browserd/cli.py`): argparse wrapper. Commands: `run`, `list`, `status`, `result`, `wait`, `resume`, `cancel`, `logs`, `state-tasks`, `state-sessions`, `state-session`, `state-system`, `session-close`, `ping`.

## Development Commands

**Install:**
```bash
python3 -m venv ~/.local/share/browserd/venv
~/.local/share/browserd/venv/bin/pip install -e .
```

**Run daemon:**
```bash
browserd
# or via systemd:
systemctl --user start browserd
```

**Run CLI:**
```bash
browser-cli ping
browser-cli run --wait "go to example.com"
```

## Code Style

- Python >= 3.10 with `from __future__ import annotations`
- Pydantic v2 for all models (`ConfigDict`, `Field`, never `parse_obj`)
- `TYPE_CHECKING` guards for inter-module imports
- Heavy imports (browser_use, cdp_use) inside methods, not at module top
- Docstrings on all public methods
- Explicit parameters, no `**kwargs`

## Key Patterns

### Port lifecycle
```python
port, cdp_url = await self.pool.acquire(browser_kind, reuse=bool(session_id))
# ... run task ...
if task_status == "blocked" or keep_open:
    # DON'T release — port stays occupied for resume/session
elif has_active_tabs:
    # DON't release — session has active tabs
else:
    self.pool.release(port)
```

### Session continue (follow_up_task)
When resuming a session, query CDP for open pages, find the session's active tab, activate it, then start Agent with `follow_up_task=True` and `directly_open_url=False`.

### Tab activation fallback chain
1. Session's active tab (by target_id match in open pages)
2. First non-about:blank page in browser
3. Session's last_url (navigate)
4. Most recent detached tab URL (navigate)
5. Let LLM figure it out (directly_open_url=True)

## Strategy For Making Changes

1. Read relevant files in `browserd/` to understand current state
2. Check the use case list in AGENTS.md for expected behavior
3. Make changes, kill stale Chromes (`for port in 9222-9225; kill $(lsof -ti :$port)`)
4. Clear pycache (`find . -type d -name __pycache__ -exec rm -rf {} +`)
5. Restart daemon (`systemctl --user restart browserd`)
6. Test the affected use cases
