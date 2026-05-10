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
| `LLM_PROVIDER` | `deepseek` | LLM provider ID (see table below) |
| `LLM_MODEL` | provider default | Model name override (optional) |
| `DEEPSEEK_API_KEY` | — | API key for your chosen provider |
| `MAX_PARALLEL_TASKS` | `4` | How many Chrome instances can run simultaneously (1–16) |

### LLM Providers

BrowserD supports all providers that browser-use supports. Set `LLM_PROVIDER` and the corresponding API key:

| Provider ID | API Key Env Var | Default Model |
|-------------|-----------------|---------------|
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `openai` | `OPENAI_API_KEY` | `gpt-4.1-mini` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-0` |
| `google` | `GOOGLE_API_KEY` | `gemini-2.5-flash` |
| `browser-use` | `BROWSER_USE_API_KEY` | `bu-latest` |
| `groq` | `GROQ_API_KEY` | `meta-llama/llama-4-maverick` |
| `mistral` | `MISTRAL_API_KEY` | `mistral-large-latest` |
| `azure` | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` | `gpt-4o` |
| `cerebras` | `CEREBRAS_API_KEY` | `llama-3.3-70b` |
| `ollama` | none (local) | `llama3` |
| `openrouter` | `OPENROUTER_API_KEY` | `openai/gpt-4o` |
| `vercel` | `AI_GATEWAY_API_KEY` | `anthropic/claude-3.5-sonnet` |
| `litellm` | none (local proxy) | `openai/gpt-4o` |

### Interactive Setup

The easiest way to configure everything:

```bash
browser-cli setup
```

Walks through:
1. Pick a provider from the list
2. Pick a model (or type your own)
3. Enter your API key (hidden input)
4. Saved to `~/.browserd/.env`

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
browser-cli setup                      # Interactive wizard: pick provider → model → API key
browser-cli set setenv <KEY>           # Set env var (prompts securely for value)
browser-cli daemon restart             # Restart the service
browser-cli daemon stop               # Stop the service
browser-cli daemon start              # Start the service
browser-cli daemon enable             # Auto-start on boot
browser-cli daemon disable            # Disable auto-start
browser-cli daemon status             # Show service status
browser-cli ping                      # Health check
```
