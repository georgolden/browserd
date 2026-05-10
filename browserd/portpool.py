"""Port pool with per-port browser process management.

DELIBERATELY simple: no lazy imports, no complex locking.
One asyncio.Lock for port selection, tracked subprocesses per port.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from browserd.models import DaemonConfig

DEBUG = Path("/tmp/browserd-debug.log")


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()[:19]
    with open(DEBUG, "a") as f:
        f.write(f"{ts} {msg}\n")
    print(msg, flush=True)


class PortPool:
    """Manages CDP ports with one browser process per port."""

    def __init__(self, config: DaemonConfig):
        self.config = config
        self.ports: dict[int, str] = {}       # port -> "free" | "occupied"
        self.procs: dict[int, asyncio.subprocess.Process] = {}
        self.browser_kinds: dict[int, str] = {}
        self._lock = asyncio.Lock()

        for port in config.port_range:
            self.ports[port] = "free"

    async def acquire(self, browser: str = "chrome", reuse: bool = False,
                      profile_data_dir: str | None = None) -> tuple[int, str]:
        """Get a free port with a fresh (or reused) browser. Returns (port, cdp_url).

        If profile_data_dir is provided, Chrome uses that as its user-data-dir
        (persistent profile). Otherwise uses a temp dir (anonymous session).
        """
        _log(f"[POOL] acquire({browser}, reuse={reuse}, profile={profile_data_dir}) ENTER ports={list(self.ports.values())}")

        async with self._lock:
            free = [p for p, s in self.ports.items() if s == "free"]
            if not free:
                raise RuntimeError(f"No free ports (range {min(self.ports)}-{max(self.ports)})")
            port = free[0]
            self.ports[port] = "occupied"
            self.browser_kinds[port] = browser
            _log(f"[POOL] acquire claimed port={port} ports_now={list(self.ports.values())}")

        cdp_url = f"http://localhost:{port}"

        if reuse and await self._is_alive(port):
            _log(f"[POOL] acquire REUSING existing browser on port {port}")
            return port, cdp_url

        # Kill any stale browser on this port
        await self._kill_port(port)

        # Launch fresh
        _log(f"[POOL] acquire LAUNCHING {browser} on port {port}")
        await self._launch(port, browser, profile_data_dir=profile_data_dir)
        _log(f"[POOL] acquire DONE port={port}")
        return port, cdp_url

    def release(self, port: int) -> None:
        """Free port without killing browser."""
        if port in self.ports:
            self.ports[port] = "free"
            _log(f"[POOL] release port={port}")

    async def kill(self, port: int) -> None:
        """Kill browser and free port."""
        await self._kill_port(port)
        if port in self.ports:
            self.ports[port] = "free"
            _log(f"[POOL] kill+free port={port}")

    def all_ports(self) -> dict[int, dict[str, Any]]:
        return {
            port: {"status": status, "browser": self.browser_kinds.get(port, "?")}
            for port, status in self.ports.items()
        }

    def free_count(self) -> int:
        return sum(1 for s in self.ports.values() if s == "free")

    async def health_check(self, port: int) -> bool:
        alive = await self._is_alive(port)
        if not alive and self.ports.get(port) == "occupied":
            self.ports[port] = "dead"
        return alive

    async def shutdown_all(self) -> None:
        for port in list(self.procs.keys()):
            await self._kill_port(port)
            self.ports[port] = "free"

    # ── internals ────────────────────────────────────────────────────────

    async def _is_alive(self, port: int) -> bool:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"http://localhost:{port}/json/version",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as r:
                    return r.status == 200
        except Exception:
            return False

    async def _kill_port(self, port: int) -> None:
        """Kill everything on this port — our subprocess AND any external Chrome."""
        _log(f"[POOL] _kill_port port={port}")

        # 0. Try CDP Browser.close first (graceful — flushes cookies to disk)
        await self._graceful_close(port)

        # 1. Kill our tracked process
        if port in self.procs:
            proc = self.procs.pop(port)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
                _log(f"[POOL] _kill_port terminated tracked proc on {port}")
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except Exception:
                    pass

        # 2. Gracefully terminate ANY process on this port (gives Chrome time to flush)
        try:
            # First, SIGTERM for graceful shutdown (cookies flush to disk)
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                f"lsof -ti :{port} 2>/dev/null | xargs -r kill -TERM 2>/dev/null; true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=2)
            await asyncio.sleep(2)  # give Chrome time to flush cookies/state
            # Then SIGKILL any stragglers
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                f"lsof -ti :{port} 2>/dev/null | xargs -r kill -9 2>/dev/null; true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass

        # 3. Also kill chrome processes with matching user-data-dir (graceful first)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                f"pkill -TERM -f 'user-data-dir=.*browserd-port{port}' 2>/dev/null; "
                f"pkill -TERM -f 'user-data-dir=.*browserd/profiles/' 2>/dev/null; "
                f"sleep 2; "
                f"pkill -9 -f 'user-data-dir=.*browserd-port{port}' 2>/dev/null; "
                f"pkill -9 -f 'user-data-dir=.*browserd/profiles/' 2>/dev/null; true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

        await asyncio.sleep(1)  # let OS release the port

    async def _graceful_close(self, port: int) -> bool:
        """Try to gracefully close Chrome via CDP Browser.close.

        This gives Chrome time to flush cookies, localStorage, etc. to disk.
        Returns True if CDP close succeeded, False if it failed.
        """
        import aiohttp
        try:
            # Connect to CDP and send Browser.close
            ws_url = f"http://localhost:{port}/json/version"
            async with aiohttp.ClientSession() as session:
                async with session.get(ws_url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status != 200:
                        return False
                    data = await r.json()
                    ws_endpoint = data.get("webSocketDebuggerUrl", "")

                if not ws_endpoint:
                    return False

                # Connect via WebSocket and send Browser.close
                async with session.ws_connect(
                    ws_endpoint,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as ws:
                    # Send Browser.close command
                    await ws.send_json({
                        "id": 1,
                        "method": "Browser.close",
                    })
                    # Wait for response
                    try:
                        resp = await asyncio.wait_for(ws.receive_json(), timeout=3)
                        _log(f"[POOL] _graceful_close Browser.close response: {resp}")
                    except asyncio.TimeoutError:
                        pass  # Chrome may close before sending response

            # Give Chrome time to flush
            await asyncio.sleep(2)
            _log(f"[POOL] _graceful_close succeeded on :{port}")
            return True
        except Exception as e:
            _log(f"[POOL] _graceful_close failed on :{port}: {e}")
            return False

    async def _launch(self, port: int, browser: str,
                       profile_data_dir: str | None = None) -> None:
        path = self.config.browser_path(browser)
        if not os.path.exists(path):
            if browser == "chrome" and os.path.exists(self.config.chromium_path):
                path = self.config.chromium_path
                self.browser_kinds[port] = "chromium"
            else:
                raise RuntimeError(f"Browser not found at {path}")

        if profile_data_dir:
            user_data_dir = profile_data_dir
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        else:
            user_data_dir = f"/tmp/browserd-port{port}"
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)

        _log(f"[POOL] _launch spawning {self.browser_kinds[port]} on :{port}"
             f" profile={user_data_dir}")
        env = os.environ.copy()
        if not env.get("DISPLAY"):
            env["DISPLAY"] = ":0"  # default X display
        proc = await asyncio.create_subprocess_exec(
            str(path),
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={user_data_dir}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        self.procs[port] = proc

        # Wait for CDP to come up
        for i in range(self.config.cdp_launch_timeout * 2):
            await asyncio.sleep(0.5)
            if await self._is_alive(port):
                _log(f"[POOL] _launch CDP ready on :{port}")
                return

        raise RuntimeError(f"Timeout waiting for browser CDP on :{port}")
