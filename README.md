# BrowserD v2.0.0

Browser automation daemon that manages parallel Chrome instances for AI agents. Wraps [browser-use](https://github.com/browser-use/browser-use) with a port-pool architecture — each task claims a dedicated Chrome process (not a tab) via unique `--remote-debugging-port=N` + `--user-data-dir`.

## Architecture

```
PortPool → N Chrome instances (ports 9222–9222+N-1), isolated user-data-dirs
TaskManager → asyncio.Semaphore(N), session-aware task lifecycle
DaemonServer → Unix socket JSON-line protocol (~/.browserd/sock)
BrowserClient → async socket client
CLI → argparse wrapper (browser-cli)
```

## Quick Start

```bash
# Install
git clone <repo-url> && cd browserd
python3 -m venv ~/.local/share/browserd/venv
~/.local/share/browserd/venv/bin/pip install -e .

# Configure
mkdir -p ~/.browserd
echo 'DEEPSEEK_API_KEY=sk-...' > ~/.browserd/.env

# Start
systemctl --user enable --now browserd

# Verify
browser-cli ping
```

## Configuration

BrowserD reads environment variables from `~/.browserd/.env` at startup.

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_API_KEY` | (required) | DeepSeek API key for the LLM agent |
| `MAX_PARALLEL_TASKS` | `4` | Max parallel Chrome instances (1–16). Controls how many tasks can run simultaneously. |
| `BROWSERD_PORT_BASE` | (hardcoded: 9222) | First port in the CDP port range |

### Setting environment variables

Use the CLI to set variables securely (prompts for value — never appears in shell history):

```bash
browser-cli set setenv MAX_PARALLEL_TASKS
# → Value for MAX_PARALLEL_TASKS: [type value, press Enter]
# → ✅ Set MAX_PARALLEL_TASKS in ~/.browserd/.env
# → Restart browserd for changes to take effect: browser-cli daemon restart
```

Or edit `~/.browserd/.env` directly:

```bash
echo 'MAX_PARALLEL_TASKS=8' >> ~/.browserd/.env
```

## CLI Commands

### Task Management

```bash
browser-cli run "your task prompt"          # Fire-and-forget
browser-cli run "prompt" --keep-open         # Keep browser open (auto-creates session)
browser-cli run --session <id> "prompt"     # Continue in existing session
browser-cli run "prompt" --wait              # Wait for completion
browser-cli run "prompt" --json              # JSON output

browser-cli list                             # List all tasks
browser-cli list --status running            # Filter by status

browser-cli status <task-id>                 # Task details
browser-cli result <task-id>                 # Task result output
browser-cli logs <task-id>                   # Task logs (--tail N)
browser-cli wait <task-id>                   # Wait for completion (--timeout N)

browser-cli resume <task-id>                 # Resume blocked task
browser-cli cancel <task-id>                 # Cancel task
```

### State Inspection

```bash
browser-cli state-system                     # Full overview (ports, sessions, tasks)
browser-cli state-tasks                      # Running + queued tasks
browser-cli state-sessions                   # All sessions with tab counts
browser-cli state-session <session-id>       # Session detail with tabs
browser-cli session-close <session-id>        # Close session
```

### Configuration

```bash
browser-cli set setenv <KEY>                 # Set env var (prompts for value)
```

### Service Control

```bash
browser-cli daemon restart                   # Restart browserd
browser-cli daemon stop                      # Stop browserd
browser-cli daemon start                     # Start browserd
browser-cli daemon enable                    # Enable auto-start on boot
browser-cli daemon disable                   # Disable auto-start
browser-cli daemon status                    # Show service status
```

### Utility

```bash
browser-cli ping                             # Health check
```

## Development

- Python >= 3.10, async/await
- Pydantic v2 models, `from __future__ import annotations`
- Heavy imports (browser_use) inside methods, not module top
- `TYPE_CHECKING` guards for inter-module references

```bash
pip install -e .
pytest
```
