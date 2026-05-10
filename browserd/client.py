"""browser-cli client — talks to browserd via Unix socket JSON-line protocol."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SOCKET_PATH = Path.home() / ".browserd" / "sock"
STATUS_ICONS = {"queued": "⏳", "running": "🔄", "blocked": "🚧", "done": "✅", "failed": "❌", "cancelled": "⛔"}


class BrowserClient:
    """Thin client for the browserd daemon."""

    def __init__(self, socket_path: Path = SOCKET_PATH):
        self.socket_path = socket_path

    async def send(self, cmd: dict, timeout: int = 30) -> dict:
        if not self.socket_path.exists():
            raise FileNotFoundError(f"browserd not running (no socket at {self.socket_path})")

        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path)), timeout=5
        )
        try:
            writer.write((json.dumps(cmd) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                raise ConnectionError("browserd closed connection")
            return json.loads(line.decode())
        finally:
            writer.close()
            await writer.wait_closed()

    # ── Command methods ─────────────────────────────────────────────────────
    async def ping(self) -> dict:
        return await self.send({"cmd": "ping"}, timeout=5)

    async def run(self, prompt: str, browser: str = "chrome",
                  keep_open: bool = False, close_tabs: bool = True,
                  max_steps: int = 30, session_id: str | None = None,
                  tab_target_id: str | None = None, new_tab: bool = False,
                  follow_up_task: bool = False) -> dict:
        return await self.send({
            "cmd": "run", "prompt": prompt, "browser": browser,
            "keep_open": keep_open, "close_tabs": close_tabs,
            "max_steps": max_steps,
            "session_id": session_id, "tab_target_id": tab_target_id,
            "new_tab": new_tab, "follow_up_task": follow_up_task,
        })

    async def list_tasks(self, status_filter: str = "all") -> dict:
        return await self.send({"cmd": "list", "filter": status_filter})

    async def status(self, task_id: str) -> dict:
        return await self.send({"cmd": "status", "id": task_id})

    async def result(self, task_id: str) -> dict:
        return await self.send({"cmd": "result", "id": task_id})

    async def wait(self, task_id: str, timeout: int = 0) -> dict:
        return await self.send(
            {"cmd": "wait", "id": task_id, "timeout": timeout},
            timeout=max(timeout + 10, 30) if timeout > 0 else 86400,
        )

    async def resume(self, task_id: str) -> dict:
        return await self.send({"cmd": "resume", "id": task_id})

    async def cancel(self, task_id: str) -> dict:
        return await self.send({"cmd": "cancel", "id": task_id})

    async def logs(self, task_id: str, tail: int = 50) -> dict:
        return await self.send({"cmd": "logs", "id": task_id, "tail": tail})

    async def steps(self, task_id: str, tail: int = 20) -> dict:
        return await self.send({"cmd": "steps", "id": task_id, "tail": tail})

    # State queries
    async def state_tasks(self) -> dict:
        return await self.send({"cmd": "state", "target": "tasks"})

    async def state_sessions(self) -> dict:
        return await self.send({"cmd": "state", "target": "sessions"})

    async def state_session(self, session_id: str) -> dict:
        return await self.send({"cmd": "state", "target": "session", "session_id": session_id})

    async def state_system(self) -> dict:
        return await self.send({"cmd": "state", "target": "system"})

    async def session_close(self, session_id: str) -> dict:
        return await self.send({"cmd": "session_close", "session_id": session_id})


# ── CLI formatting helpers ──────────────────────────────────────────────────
def icon(status: str) -> str:
    return STATUS_ICONS.get(status, "?")


def trunc(s: str | None, n: int = 80) -> str:
    if not s:
        return ""
    return s[:n] + "..." if len(s) > n else s


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)
