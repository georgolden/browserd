#!/usr/bin/env python3
"""browser-cli v2.0.0 — control browserd daemon."""

import argparse
import asyncio
import json
import sys

from browserd.client import BrowserClient, die, icon, trunc


async def _wait_result(client, tid, timeout, json_out):
    import time
    t0 = time.monotonic()
    resp = await client.wait(tid, timeout)
    t = resp.get("task", {})
    elapsed = round(time.monotonic() - t0, 1)
    if json_out:
        print(json.dumps(t, indent=2, default=str))
    elif resp["type"] == "task_timeout":
        print(f"⏳ Still running after {elapsed}s — status: {t.get('status','?')}")
    elif t.get("status") == "blocked":
        print(f"🚧 BLOCKED after {elapsed}s")
        print(f"   Reason: {t.get('blocked_reason','?')}")
        print(f"   Resolve in browser, then: browser-cli resume {tid}")
    elif t.get("status") == "done":
        print(f"✅ Done in {elapsed}s")
        if t.get("result"):
            try:
                r = json.loads(t["result"])
                if r.get("result"):
                    print(f"\n{r['result']}")
            except Exception:
                pass
    else:
        print(f"{icon(t.get('status','?'))} {t.get('status')} — {elapsed}s")


# ── Command handlers ────────────────────────────────────────────────────────

async def cmd_run(client, args):
    resp = await client.run(
        prompt=args.prompt,
        browser=args.browser,
        keep_open=args.keep_open,
        close_tabs=not args.keep_open,
        max_steps=args.max_steps,
        session_id=args.session,
        tab_target_id=args.tab,
        new_tab=args.new_tab,
        follow_up_task=args.follow_up or bool(args.session),
    )
    tid = resp["id"]
    sid = resp.get("session_id", "")
    tab = resp.get("tab_target_id", "")

    if args.json and not args.wait:
        print(json.dumps({"id": tid, "session_id": sid, "tab_target_id": tab, "status": "queued"}))
    elif args.wait:
        if args.json:
            await _wait_result(client, tid, args.timeout, json_out=True)
        else:
            context = f" (session: {sid})" if sid else ""
            if tab:
                context += f" (tab: {tab})"
            print(f"Running: {tid}{context} — waiting...")
            await _wait_result(client, tid, args.timeout, json_out=False)
    else:
        context = ""
        if sid:
            context = f" (session: {sid}"
            if tab:
                context += f", tab: {tab}"
            context += ")"
        print(f"Running: {tid}{context}")


async def cmd_list(client, args):
    resp = await client.list_tasks(args.status or "all")
    tasks = resp.get("tasks", [])
    if args.json:
        print(json.dumps(tasks, indent=2))
    elif not tasks:
        print("No tasks.")
    else:
        print(f"{'ID':<14} {'STATUS':<10} {'BROWSER':<10} {'S':<4} {'SESSION':<16} PROMPT")
        print("-" * 90)
        for t in tasks:
            sid = t.get("session_id") or "—"
            print(f"{t['id']:<14} {icon(t['status'])} {t['status']:<6} {t.get('browser','?'):<9} "
                  f"{t.get('step_count',0):<4} {sid:<16} {trunc(t.get('prompt',''))}")


async def cmd_status(client, args):
    resp = await client.status(args.id)
    if resp.get("type") == "error":
        die(resp["message"])
    t = resp["task"]
    if args.json:
        print(json.dumps(t, indent=2, default=str))
    else:
        print(f"Task:      {t['id']}")
        print(f"Status:    {icon(t['status'])} {t['status']}")
        if t.get("session_id"):
            print(f"Session:   {t['session_id']}")
        print(f"Browser:   {t.get('browser','?')}  Steps: {t.get('step_count',0)}/{t.get('max_steps','?')}")
        if t.get("current_url"):
            print(f"URL:       {t['current_url']}")
        if t.get("blocked_reason"):
            print(f"Blocked:   {t['blocked_reason']}")
        if t.get("error"):
            print(f"Error:     {trunc(t['error'], 200)}")


async def cmd_result(client, args):
    resp = await client.result(args.id)
    if resp.get("type") == "error":
        die(resp["message"])
    t = resp["task"]
    if args.json:
        print(json.dumps(t, indent=2, default=str))
    else:
        print(f"Task: {t['id']}  |  {icon(t['status'])} {t['status']}")
        if t.get("blocked_reason"):
            print(f"\n🚧 BLOCKED: {t['blocked_reason']}")
            print(f"   Resolve in browser, then: browser-cli resume {t['id']}")
        if t.get("result"):
            try:
                r = json.loads(t["result"])
                if r.get("result"):
                    print(f"\n{r['result']}")
            except Exception:
                pass


async def cmd_wait(client, args):
    await _wait_result(client, args.id, args.timeout, args.json)


async def cmd_resume(client, args):
    resp = await client.resume(args.id)
    if resp.get("type") == "error":
        die(resp["message"])
    print(f"Resumed: {args.id}")
    if args.wait:
        await _wait_result(client, args.id, args.timeout, args.json)


async def cmd_cancel(client, args):
    resp = await client.cancel(args.id)
    if resp.get("type") == "error":
        die(resp["message"])
    print(f"Cancelled: {args.id}")


async def cmd_steps(client, args):
    """Show agent step data — thinking, actions, page state for real-time awareness."""
    resp = await client.steps(args.id, args.tail)
    steps = resp.get("steps", [])
    if not steps:
        print("No step data yet (task may not have started)")
        return
    for s in steps:
        step = s.get("step", "?")
        url = s.get("url", "")[:70]
        # Show what the agent was thinking
        if "thinking" in s:
            print(f"\n── Step {step} @ {url} ──")
            print(f"  💭 {s['thinking'][:200]}")
        elif "evaluation" in s:
            print(f"\n── Step {step} @ {url} ──")
            print(f"  📋 Eval: {s['evaluation'][:200]}")
        # Show actions
        actions = s.get("actions", [])
        if actions:
            for a in actions:
                if isinstance(a, dict):
                    action_type = a.get("type", a.get("action", "?"))
                    details = {k: str(v)[:60] for k, v in a.items() if k != "type" and v}
                    print(f"  ▶ {action_type}: {details}")
        elif "error" in s:
            print(f"  ⚠ {s['error']}")
        else:
            print(f"\n── Step {step} @ {url} ── (no detail)")

async def cmd_logs(client, args):
    resp = await client.logs(args.id, args.tail)
    for e in resp.get("logs", []):
        s = f"[{e.get('step','?')}]" if e.get('step') is not None else "[*]"
        print(f"{e['timestamp'][:19]} {s} {e['level']:>5}: {e['message']}")


async def cmd_ping(client, args):
    resp = await client.ping()
    print(f"browserd v{resp.get('version','?')} — {resp.get('running',0)} tasks running")


async def cmd_setenv(args):
    """Set an environment variable in ~/.browserd/.env — prompts for value."""
    import getpass
    from pathlib import Path

    env_path = Path.home() / ".browserd" / ".env"
    value = getpass.getpass(f"Value for {args.key}: ")
    if not value:
        die("No value entered — aborting.")

    # Read existing lines so we can update in-place
    lines: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                lines[k.strip()] = line

    # Add or update
    lines[args.key] = f"{args.key}={value}"

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines.values()) + "\n")
    print(f"✅ Set {args.key} in {env_path}")
    print(f"   Restart browserd for changes to take effect: browser-cli daemon restart")


def cmd_setup() -> None:
    """Interactive setup wizard: choose LLM provider, model, and API key."""
    import getpass
    from pathlib import Path
    from browserd.providers import PROVIDERS, list_providers

    print("\n  ═══ BrowserD Setup ═══\n")
    print("  Choose your LLM provider:\n")

    plist = list_providers()
    for i, p in enumerate(plist, 1):
        print(f"  {i:>2}. {p['name']:<20}  (default: {p['default_model']})")

    # Provider selection
    try:
        choice = input(f"\n  Provider [1-{len(plist)}] (default: 1): ").strip()
        idx = int(choice) - 1 if choice else 0
        if idx < 0 or idx >= len(plist):
            die("Invalid selection.")
    except (ValueError, EOFError):
        die("Cancelled.")

    pid = plist[idx]["id"]
    provider = PROVIDERS[pid]
    print(f"\n  ✅ Provider: {provider['name']}")

    # Model selection
    models = provider.get("models", [])
    if models:
        print(f"\n  Available models for {provider['name']}:")
        for i, m in enumerate(models, 1):
            marker = " (default)" if i == 1 else ""
            print(f"  {i:>2}. {m}{marker}")
        print(f"  {'':>2}  (or type any model name)")
        try:
            model_choice = input(f"\n  Model [1-{len(models)}] (default: 1): ").strip()
            if not model_choice:
                selected_model = models[0]
            else:
                try:
                    idx2 = int(model_choice) - 1
                    if 0 <= idx2 < len(models):
                        selected_model = models[idx2]
                    else:
                        selected_model = model_choice
                except ValueError:
                    selected_model = model_choice
        except EOFError:
            die("Cancelled.")
    else:
        selected_model = input(
            f"\n  Model name (default: {provider['default_model']}): "
        ).strip()
        if not selected_model:
            selected_model = provider["default_model"]

    print(f"  ✅ Model: {selected_model}")

    # API key
    env_lines: dict[str, str] = {}

    if not provider.get("no_api_key"):
        key_env = provider["env_vars"][0]
        try:
            api_key = getpass.getpass(f"\n  {key_env}: ").strip()
            if not api_key:
                die("API key is required — aborting.")
        except EOFError:
            die("Cancelled.")
        env_lines[key_env] = f"{key_env}={api_key}"
        print(f"  ✅ {key_env} set")
    else:
        print(f"\n  (no API key needed for {provider['name']})")

    # Extra prompts (Azure endpoint, Ollama URL, etc.)
    for ev_key, label, default in provider.get("extra_prompts", []):
        try:
            val = input(f"\n  {label} (default: {default}): ").strip()
        except EOFError:
            die("Cancelled.")
        env_lines[ev_key] = f"{ev_key}={val or default}"
        print(f"  ✅ {ev_key} set")

    # Provider + model env vars
    env_lines["LLM_PROVIDER"] = f"LLM_PROVIDER={pid}"
    env_lines["LLM_MODEL"] = f"LLM_MODEL={selected_model}"

    # Merge with existing .env
    env_path = Path.home() / ".browserd" / ".env"
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k = line.partition("=")[0].strip()
                existing[k] = line

    existing.update(env_lines)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(existing.values()) + "\n")

    print(f"\n  ═══ Configuration saved to {env_path} ═══")
    print(f"  LLM_PROVIDER={pid}")
    print(f"  LLM_MODEL={selected_model}")
    if not provider.get("no_api_key"):
        print(f"  {provider['env_vars'][0]}=<set>")
    for ev_key, _, _ in provider.get("extra_prompts", []):
        print(f"  {ev_key}={env_lines[ev_key].split('=',1)[1]}")

    print(f"\n  Run this to apply changes:")
    print(f"    browser-cli daemon restart")


def cmd_daemon(action: str) -> None:
    """Run systemctl --user <action> browserd."""
    import subprocess

    cmd = ["systemctl", "--user", action, "browserd"]
    if action == "status":
        result = subprocess.run(cmd, capture_output=False)
        sys.exit(result.returncode)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ browserd {action} successful")
            if result.stdout.strip():
                print(result.stdout.strip())
        else:
            die(f"Failed to {action} browserd:\n{result.stderr.strip()}")


# ── State commands ──────────────────────────────────────────────────────────

async def cmd_state_tasks(client, args):
    resp = await client.state_tasks()
    t = resp.get("running", [])
    q = resp.get("queued", [])
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        print(f"Tasks running: {len(t)}  queued: {len(q)}")
        for task in t:
            sid = task.get("session_id") or "—"
            print(f"  🔄 {task['id']}  {trunc(task.get('prompt',''))}  [session: {sid}]")
        for task in q:
            sid = task.get("session_id") or "—"
            print(f"  ⏳ {task['id']}  {trunc(task.get('prompt',''))}")


async def cmd_state_sessions(client, args):
    resp = await client.state_sessions()
    sessions = resp.get("sessions", [])
    if args.json:
        print(json.dumps(sessions, indent=2))
    elif not sessions:
        print("No sessions.")
    else:
        print(f"{'SESSION':<24} {'STATUS':<10} {'PORT':<6} {'TABS (active/detached)':<22} LAST URL")
        print("-" * 90)
        for s in sessions:
            ts = s.get("tab_summary", {})
            tab_str = f"{ts.get('active',0)}/{ts.get('detached',0)}"
            print(f"{s['id']:<24} {s.get('status','?'):<10} {s.get('browser_port') or '—':<6} "
                  f"{tab_str:<22} {trunc(s.get('last_url',''), 40)}")


async def cmd_state_session(client, args):
    resp = await client.state_session(args.id)
    if resp.get("type") == "error":
        die(resp["message"])
    s = resp["session"]
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        print(f"Session: {s['id']}")
        print(f"Status:  {s.get('status','?')}  |  Port: {s.get('browser_port') or '—'}")
        tc = s.get("tab_count", {})
        print(f"Tabs ({tc.get('active',0)} active, {tc.get('detached',0)} detached, "
              f"{tc.get('closed',0)} closed):")
        status_icons = {"active": "🟢", "detached": "🟡", "closed": "⚫"}
        for tab in s.get("tabs", []):
            si = status_icons.get(tab.get("status", ""), "?")
            print(f"  [{tab['target_id']}] {si} {tab.get('status',''):<8} "
                  f"{trunc(tab.get('url','no url'), 60)} — \"{tab.get('title','?')}\"")
        if s.get("task_ids"):
            print(f"Tasks ({len(s['task_ids'])}):")
            # Show last 5
            for tid in s["task_ids"][-5:]:
                print(f"  • {tid}")


async def cmd_state_system(client, args):
    resp = await client.state_system()
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        ports = resp.get("ports", [])
        occ = [p for p in ports if p["status"] == "occupied"]
        free = [p for p in ports if p["status"] == "free"]
        sessions = resp.get("sessions", [])
        tasks = resp.get("tasks", {})
        print(f"=== BrowserD System ===")
        print(f"Port pool: {ports[0]['port'] if ports else '?'}-{ports[-1]['port'] if ports else '?'} "
              f"({len(occ)} occupied, {len(free)} free)")
        for p in ports:
            sid = ""
            for s2 in sessions:
                if s2.get("browser_port") == p["port"]:
                    sid = f'  session: "{s2["id"]}"'
                    break
            print(f"  {p['port']}: {p['status']:<10} {p.get('browser','?'):<10}{sid}")
        print(f"Sessions ({len(sessions)}): " +
              ", ".join(f"{s['id']} ({s.get('status','?')})" for s in sessions) if sessions else "none")
        print(f"Tasks: {tasks.get('running',0)} running, {tasks.get('queued',0)} queued, "
              f"{tasks.get('blocked',0)} blocked")


async def cmd_session_close(client, args):
    resp = await client.session_close(args.id)
    if resp.get("type") == "error":
        die(resp["message"])
    print(resp.get("message", f"Session '{args.id}' closed"))


# ── Parser ──────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="browser-cli", description="Control browserd daemon v2",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="browser-cli 2.0.0")
    s = p.add_subparsers(dest="cmd")

    # run
    sub = s.add_parser("run", aliases=["r"]); sub.add_argument("prompt")
    sub.add_argument("--browser", "-b", choices=["chrome", "chromium"], default="chrome")
    sub.add_argument("--keep-open", "-k", action="store_true")
    sub.add_argument("--session", "-s", default=None, help="Session ID")
    sub.add_argument("--tab", default=None, help="Target tab ID")
    sub.add_argument("--new-tab", action="store_true")
    sub.add_argument("--follow-up", action="store_true")
    sub.add_argument("--max-steps", "-m", type=int, default=30)
    sub.add_argument("--wait", "-w", action="store_true")
    sub.add_argument("--timeout", "-t", type=int, default=0)
    sub.add_argument("--json", "-j", action="store_true")

    # list
    ls = s.add_parser("list", aliases=["ls"])
    ls.add_argument("--status", "-s", choices=["queued","running","blocked","done","failed","cancelled"])
    ls.add_argument("--json", "-j", action="store_true")

    # status
    st = s.add_parser("status", aliases=["st", "info"]); st.add_argument("id")
    st.add_argument("--json", "-j", action="store_true")

    # result
    r = s.add_parser("result", aliases=["res", "output"]); r.add_argument("id")
    r.add_argument("--json", "-j", action="store_true")

    # wait
    w = s.add_parser("wait", aliases=["w"]); w.add_argument("id")
    w.add_argument("--timeout", "-t", type=int, default=0)
    w.add_argument("--json", "-j", action="store_true")

    # resume
    rs = s.add_parser("resume", aliases=["unblock"]); rs.add_argument("id")
    rs.add_argument("--wait", "-w", action="store_true")
    rs.add_argument("--timeout", "-t", type=int, default=0)
    rs.add_argument("--json", "-j", action="store_true")

    # cancel
    s.add_parser("cancel", aliases=["kill", "stop"]).add_argument("id")

    # logs
    lg = s.add_parser("logs", aliases=["log"]); lg.add_argument("id")
    lg.add_argument("--tail", "-n", type=int, default=50)

    # steps — real-time agent step awareness
    sp = s.add_parser("steps", aliases=["step"], help="View agent step data (thinking, actions, page state)")
    sp.add_argument("id")
    sp.add_argument("--tail", "-n", type=int, default=10)

    # state subcommands
    s.add_parser("state-tasks", aliases=["st-t"]).add_argument("--json", "-j", action="store_true")
    s.add_parser("state-sessions", aliases=["st-s"]).add_argument("--json", "-j", action="store_true")
    sts = s.add_parser("state-session", aliases=["st-si", "session-info"]); sts.add_argument("id")
    sts.add_argument("--json", "-j", action="store_true")
    s.add_parser("state-system", aliases=["st-sy", "sys"]).add_argument("--json", "-j", action="store_true")

    # session close
    sc = s.add_parser("session-close", aliases=["sc"]); sc.add_argument("id")

    # ping
    s.add_parser("ping", aliases=["p"])

    # set — manage daemon config/env
    set_sub = s.add_parser("set", help="Manage daemon configuration")
    set_cmds = set_sub.add_subparsers(dest="set_cmd")

    setenv_p = set_cmds.add_parser("setenv", help="Set environment variable in ~/.browserd/.env")
    setenv_p.add_argument("key", help="Environment variable name (e.g., MAX_PARALLEL_TASKS)")

    # setup — interactive provider/model/key wizard
    s.add_parser("setup", help="Interactive setup: choose LLM provider, model, and API key")

    # daemon — service lifecycle control
    daemon_sub = s.add_parser("daemon", help="Control the browserd systemd service")
    daemon_cmds = daemon_sub.add_subparsers(dest="daemon_cmd")

    daemon_cmds.add_parser("restart", help="Restart browserd service")
    daemon_cmds.add_parser("stop", help="Stop browserd service")
    daemon_cmds.add_parser("start", help="Start browserd service")
    daemon_cmds.add_parser("enable", help="Enable browserd to start on boot")
    daemon_cmds.add_parser("disable", help="Disable browserd auto-start")
    daemon_cmds.add_parser("status", help="Show browserd service status")

    return p


def main():
    p = build_parser(); args = p.parse_args()
    if not args.cmd: p.print_help(); sys.exit(1)

    # "set" subcommands that don't need a daemon connection
    if args.cmd == "set" and args.set_cmd == "setenv":
        asyncio.run(cmd_setenv(args))
        return

    # "setup" — interactive wizard, no daemon connection needed
    if args.cmd == "setup":
        cmd_setup()
        return

    # "daemon" subcommands — systemctl wrapper, no daemon connection needed
    if args.cmd == "daemon" and args.daemon_cmd:
        cmd_daemon(args.daemon_cmd)
        return

    handlers = {
        "run": "cmd_run", "r": "cmd_run",
        "list": "cmd_list", "ls": "cmd_list",
        "status": "cmd_status", "st": "cmd_status", "info": "cmd_status",
        "result": "cmd_result", "res": "cmd_result", "output": "cmd_result",
        "wait": "cmd_wait", "w": "cmd_wait",
        "resume": "cmd_resume", "unblock": "cmd_resume",
        "cancel": "cmd_cancel", "kill": "cmd_cancel", "stop": "cmd_cancel",
        "logs": "cmd_logs", "log": "cmd_logs",
        "steps": "cmd_steps", "step": "cmd_steps",
        "state-tasks": "cmd_state_tasks", "st-t": "cmd_state_tasks",
        "state-sessions": "cmd_state_sessions", "st-s": "cmd_state_sessions",
        "state-session": "cmd_state_session", "st-si": "cmd_state_session", "session-info": "cmd_state_session",
        "state-system": "cmd_state_system", "st-sy": "cmd_state_system", "sys": "cmd_state_system",
        "session-close": "cmd_session_close", "sc": "cmd_session_close",
        "ping": "cmd_ping", "p": "cmd_ping",
    }
    fn = globals().get(handlers.get(args.cmd, ""))
    if not fn: p.print_help(); sys.exit(1)

    client = BrowserClient()
    try:
        asyncio.run(fn(client, args))
    except FileNotFoundError:
        die("browserd not running. Start: browserd", 3)
    except ConnectionRefusedError:
        die("browserd crashed — check logs", 3)
    except KeyboardInterrupt:
        print(); sys.exit(130)


if __name__ == "__main__":
    main()
