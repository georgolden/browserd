# BrowserD v2.0

Browser automation daemon with **parallel Chrome instances**, **persistent sessions**, and a **Unix socket CLI**.

Wraps [browser-use](https://github.com/browser-use/browser-use) (v0.12.6) with a port-pool architecture — each task gets its own Chrome process on a dedicated CDP port.

```bash
browser-cli run "search for European funding databases"
browser-cli run --keep-open "open gmail"
browser-cli run --session inbox "summarize this email"
browser-cli state system
```

## Architecture

```
┌─────────────┐     Unix socket      ┌──────────────┐
│ browser-cli │ ──── JSON-line ────→ │   browserd   │
└─────────────┘     ~/.browserd/sock │  (daemon)    │
                                     │              │
                                     │ PortPool     │
                                     │  :9222 :9223 │
                                     │  :9224 :9225 │
                                     │              │
                                     │ TaskManager  │
                                     │  Semaphore(N)│
                                     │  Sessions    │
                                     └──────┬───────┘
                                            │ CDP
                                     ┌──────┴───────┐
                                     │  Chrome 1..N │
                                     │  (isolated   │
                                     │  profiles)   │
                                     └──────────────┘
```

| Layer | What |
|-------|------|
| **Port Pool** | 4 parallel Chrome instances (ports 9222–9225), each with isolated `--user-data-dir` |
| **Sessions** | Persistent task chains that survive browser crashes |
| **Tabs** | Three statuses: active, detached (browser died), closed |
| **Daemon** | Unix socket JSON-line protocol at `~/.browserd/sock` |
| **CLI** | `browser-cli run|state|resume|cancel|...` |
| **Storage** | SQLite WAL at `~/.browserd/tasks.db` |

## Install

```bash
# Clone + install
python3 -m venv ~/.local/share/browserd/venv
~/.local/share/browserd/venv/bin/pip install -e ~/projects/browserd

# Link to PATH (~/.local/bin must be in $PATH)
ln -sf ~/.local/share/browserd/venv/bin/browserd ~/.local/bin/browserd
ln -sf ~/.local/share/browserd/venv/bin/browser-cli ~/.local/bin/browser-cli

# API key (DeepSeek)
echo 'DEEPSEEK_API_KEY=sk-...' > ~/.browserd/.env

# Auto-start daemon
systemctl --user enable --now browserd
```

## Commands

### Tasks

| Command | What |
|---------|------|
| `browser-cli run "prompt"` | New task, tab closes after |
| `browser-cli run --keep-open "prompt"` | New task, auto-creates session, tab stays |
| `browser-cli run --session <id> "prompt"` | Task in existing session |
| `browser-cli run --session <id> --tab <tid> "prompt"` | Task on specific tab |
| `browser-cli run --session <id> --new-tab "prompt"` | Task in new tab within session |
| `browser-cli run --wait "prompt"` | Block until done (no timeout = infinite) |
| `browser-cli run --wait --timeout 120 "prompt"` | Block with timeout |

### Monitoring

| Command | What |
|---------|------|
| `browser-cli list` | All tasks |
| `browser-cli status <id>` | Task detail (steps, URL, errors) |
| `browser-cli result <id>` | Task result text |
| `browser-cli logs <id>` | Step-by-step execution logs |

### State

| Command | What |
|---------|------|
| `browser-cli state tasks` | Queue + running tasks |
| `browser-cli state sessions` | All sessions with tab counts |
| `browser-cli state session <id>` | Full session detail (tabs, statuses, tasks) |
| `browser-cli state system` | Ports, sessions, task counts |

### Control

| Command | What |
|---------|------|
| `browser-cli resume <id>` | Resume blocked task |
| `browser-cli cancel <id>` | Cancel task |
| `browser-cli session-close <id>` | Close session, free resources |
| `browser-cli ping` | Daemon health check |

## Options

| Flag | Effect |
|------|--------|
| `--browser chrome\|chromium` | Browser binary (default: chrome) |
| `--max-steps N` | Max agent steps (default: 30) |
| `--wait` | Block until task completes |
| `--timeout N` | Wait timeout in seconds (0 = infinite) |
| `--json` | Machine-readable output |
| `--keep-open` | Auto-create session, keep tab open |
| `--session <id>` | Bind to existing session |
| `--tab <id>` | Target specific tab |
| `--new-tab` | Open new tab in session |
| `--follow-up` | Start from current browser state |

## Session workflow

```bash
# Create session with Gmail open
browser-cli run --keep-open "open gmail"

# Check what tabs are open
browser-cli state session auto-gmail-abc123
# → Shows tab DEF456 "Inbox" active

# User manually clicks an email...

# Continue in same session (agent sees the email)
browser-cli run --session auto-gmail-abc123 "summarize this email"

# Close browser, session survives
browser-cli state session auto-gmail-abc123
# → Tab shows as "detached" with URL preserved

# Continue after browser close (new Chrome spawns)
browser-cli run --session auto-gmail-abc123 "search for sender's company"
```

## Hermes Agent usage

```bash
# Hermes calls browser-cli from terminal tool:
browser-cli run --json --wait "go to google.com, find funding databases"
# → {"id": "abc123", "result": "...", "urls": [...]}

# Multi-step with state inspection:
browser-cli state sessions --json
browser-cli state session inbox --json  # find tab IDs
browser-cli run --session inbox --tab DEF456 --json --wait "reply to this email"
```

See `~/.hermes/skills/browser-cli/SKILL.md` for the full Hermes integration guide.

## Dependencies

- `browser-use` — git+https://github.com/browser-use/browser-use.git
- `cdp-use>=1.4.5` — typed Chrome DevTools Protocol client
- `aiohttp>=3.9` — async HTTP (health checks)
- `pydantic>=2.0` — data validation
