"""Unix socket server for browserd — JSON-line protocol, delegates to TaskManager."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from browserd.models import DaemonConfig, SocketRequest, SocketResponse

if TYPE_CHECKING:
    from browserd.tasks import TaskManager


class DaemonServer:
    """Listens on Unix socket, dispatches commands to TaskManager."""

    def __init__(self, manager: TaskManager, config: DaemonConfig):
        self.manager = manager
        self.config = config
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        socket_path = Path(self.config.socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(socket_path)
        )
        os.chmod(str(socket_path), 0o600)
        print(f"[browserd] Listening on {socket_path}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        socket_path = Path(self.config.socket_path)
        if socket_path.exists():
            socket_path.unlink()
        await self.manager.shutdown()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=30)
            if not data:
                return
            cmd = json.loads(data.decode())
            resp = await self._dispatch(cmd)
            if resp is not None:
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except asyncio.TimeoutError:
            pass
        except json.JSONDecodeError:
            writer.write(b'{"type":"error","message":"Invalid JSON"}\n')
            await writer.drain()
        except Exception as e:
            try:
                writer.write(
                    json.dumps({"type": "error", "message": str(e)}).encode() + b"\n"
                )
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, cmd: dict) -> dict | None:
        action = cmd.get("cmd", "")
        tid = cmd.get("id")

        if action == "ping":
            return {
                "type": "pong",
                "version": "2.0.0",
                "running": len(self.manager.running),
            }

        elif action == "run":
            tid = await self.manager.run(
                prompt=cmd["prompt"],
                browser=cmd.get("browser", "chrome"),
                keep_open=cmd.get("keep_open", False),
                close_tabs=cmd.get("close_tabs", True),
                max_steps=cmd.get("max_steps", self.config.default_max_steps),
                model=cmd.get("model", self.config.default_model),
                session_id=cmd.get("session_id"),
                tab_target_id=cmd.get("tab_target_id"),
                new_tab=cmd.get("new_tab", False),
                follow_up_task=cmd.get("follow_up_task", False),
            )
            return {
                "type": "submitted",
                "id": tid,
                "session_id": cmd.get("session_id"),
                "tab_target_id": cmd.get("tab_target_id"),
            }

        elif action == "list":
            tasks = self.manager.db.list_tasks(cmd.get("filter", "all"))
            return {"type": "task_list", "tasks": tasks}

        elif action == "status":
            t = self.manager.db.get_task(tid) if tid else None
            if not t:
                return {"type": "error", "message": f"Task {tid} not found"}
            return {"type": "task_status", "task": {k: v for k, v in t.items() if k != "result"}}

        elif action == "result":
            t = self.manager.db.get_task(tid) if tid else None
            if not t:
                return {"type": "error", "message": f"Task {tid} not found"}
            return {"type": "task_result", "task": t}

        elif action == "wait":
            return await self._wait(tid or "", cmd.get("timeout", 0))

        elif action == "resume":
            ok = await self.manager.resume(tid or "")
            return {
                "type": "resumed" if ok else "error",
                "id": tid,
                "message": "Resumed" if ok else "Not in blockable state",
            }

        elif action == "cancel":
            ok = await self.manager.cancel(tid or "")
            return {
                "type": "cancelled" if ok else "error",
                "id": tid,
                "message": "Cancelled" if ok else "Not cancellable",
            }

        elif action == "logs":
            logs = self.manager.db.get_logs(tid or "", tail=cmd.get("tail", 50))
            return {"type": "task_logs", "id": tid, "logs": logs}

        elif action == "state":
            target = cmd.get("target", "system")
            if target == "tasks":
                state = self.manager.get_tasks_state()
                return {"type": "state_tasks", **state}
            elif target == "sessions":
                sessions = self.manager.get_sessions_state()
                return {"type": "state_sessions", "sessions": sessions}
            elif target == "session":
                sid = cmd.get("session_id") or tid
                if not sid:
                    return {"type": "error", "message": "Missing session_id"}
                session = self.manager.get_session_detail(sid)
                if not session:
                    return {"type": "error", "message": f"Session {sid} not found"}
                return {"type": "state_session", "session": session}
            else:  # system
                state = self.manager.get_system_state()
                return {"type": "state_system", **state}

        elif action == "session_close":
            sid = cmd.get("session_id") or tid
            if not sid:
                return {"type": "error", "message": "Missing session_id"}
            ok = await self.manager.close_session(sid)
            return {
                "type": "session_closed" if ok else "error",
                "session_id": sid,
                "message": f"Session '{sid}' closed" if ok else f"Session '{sid}' not found",
            }

        else:
            return {"type": "error", "message": f"Unknown command: {action}"}

    async def _wait(self, tid: str, timeout: int) -> dict:
        """Wait until task reaches terminal state. timeout=0 means forever."""
        import time

        deadline = float("inf") if timeout <= 0 else time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.manager.db.get_task(tid)
            if not t:
                return {"type": "error", "message": f"Task {tid} not found"}
            if t["status"] in ("done", "failed", "blocked", "cancelled"):
                return {"type": "task_complete", "task": t}
            await asyncio.sleep(1)
        t = self.manager.db.get_task(tid)
        return {"type": "task_timeout", "task": t}

# ── Entry point ────────────────────────────────────────────────────────────

async def _main_async() -> None:
    """Entry point for `browserd` console script."""
    import os
    import signal
    from pathlib import Path

    from browserd.models import DaemonConfig
    from browserd.db import TaskDB
    from browserd.tasks import TaskManager

    # Load .env
    for env_path in [Path.home() / ".browserd" / ".env", Path.cwd() / ".env"]:
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k not in os.environ:
                            os.environ[k] = v
            break

    config = DaemonConfig(
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
    )
    db = TaskDB(config.db_path)
    manager = TaskManager(db, config)
    server = DaemonServer(manager, config)

    loop = asyncio.get_event_loop()

    async def stop() -> None:
        print("\n[browserd] Shutting down...")
        await server.stop()
        db.close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(stop()))

    await server.start()
    print(f"[browserd] — SQLite at {config.db_path}")
    print(f"[browserd] Port pool: {min(config.port_range)}-{max(config.port_range)} ({config.max_parallel} ports)")
    print(f"[browserd] Ready. Control: browser-cli <cmd>")

    # Resolve effective model for logging
    actual_model = config.llm_model
    if not actual_model:
        try:
            from browserd.providers import PROVIDERS
            actual_model = PROVIDERS.get(config.llm_provider, {}).get("default_model", "?")
        except Exception:
            actual_model = "?"
    print(f"[browserd] LLM: {config.llm_provider} / {actual_model}")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass


def main() -> None:
    """Console script entry point."""
    asyncio.run(_main_async())
