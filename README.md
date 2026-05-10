# BrowserD

A daemon that makes [browser-use](https://github.com/browser-use/browser-use) more comfortable for AI agents to use. Instead of agents having to manage browser processes, ports, sessions, and task queues themselves, BrowserD runs as a background service that handles all of that — so agents just send tasks and get results.

## What it does

BrowserD runs as a background service. AI agents (or any client) send tasks over a Unix socket, and BrowserD handles everything else:

- **Parallel execution** — spawns isolated Chrome instances, one per task. Configure how many run at once with `MAX_PARALLEL_TASKS`.
- **Persistent sessions** — keep a browser alive across multiple tasks. Agents can start a session, run several follow-up tasks, and close it when done.
- **Crash recovery** — if Chrome dies, the session remembers which tabs were open and restores them automatically.
- **Task queuing** — submitting 10 tasks when only 4 ports are available? No problem — the extras queue up and run when a port frees.
- **Simple CLI** — designed for both humans and agents. Every command has JSON output mode so agent toolchains can parse responses.

## Quick Start

```bash
git clone https://github.com/georgolden/browserd && cd browserd
python3 -m venv ~/.local/share/browserd/venv
~/.local/share/browserd/venv/bin/pip install -e .

mkdir -p ~/.browserd
echo 'DEEPSEEK_API_KEY=sk-...' > ~/.browserd/.env

systemctl --user enable --now browserd
browser-cli ping
```

## Configuration

BrowserD reads environment variables from `~/.browserd/.env` at startup.

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_API_KEY` | (required) | DeepSeek API key for the LLM agent |
| `MAX_PARALLEL_TASKS` | `4` | How many Chrome instances can run simultaneously (1–16) |

### Setting environment variables

```bash
browser-cli set setenv MAX_PARALLEL_TASKS   # prompts for value (hidden input)
browser-cli daemon restart                  # apply changes
```

Or edit the file directly:

```bash
echo 'MAX_PARALLEL_TASKS=8' >> ~/.browserd/.env
```

## CLI Commands

### Running tasks

```bash
browser-cli run "find the top HN post today"      # Fire-and-forget
browser-cli run "log in to my account" --keep-open  # Keep browser open (creates session)
browser-cli run --session <id> "do next thing"      # Continue in existing session
browser-cli run "scrape products" --wait             # Wait for result before returning
browser-cli run "scrape products" --json             # JSON output for agent consumption
```

### Managing tasks

```bash
browser-cli list                     # All tasks
browser-cli list --status running    # Filter by status
browser-cli status <task-id>         # Current state + step count + URL
browser-cli result <task-id>         # Final output (parsed result)
browser-cli logs <task-id>           # Step-by-step logs (--tail N)
browser-cli wait <task-id>           # Block until done (--timeout N)
browser-cli resume <task-id>         # Retry a blocked login/auth task
browser-cli cancel <task-id>         # Kill a running task, free its port
```

### Inspecting state

```bash
browser-cli state-system              # Everything: ports, sessions, task counts
browser-cli state-tasks               # Running + queued tasks
browser-cli state-sessions            # All sessions with tab counts
browser-cli state-session <id>        # Session tabs, URLs, associated tasks
browser-cli session-close <id>        # Close session, free its port
```

### Configuration and service

```bash
browser-cli set setenv <KEY>          # Set env var (prompts securely for value)
browser-cli daemon restart            # Restart the service
browser-cli daemon stop               # Stop the service
browser-cli daemon start              # Start the service
browser-cli daemon enable             # Auto-start on boot
browser-cli daemon disable            # Disable auto-start
browser-cli daemon status             # Show service status
browser-cli ping                      # Health check
```
