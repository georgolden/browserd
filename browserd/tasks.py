"""Task lifecycle management — queue, execution, blocked detection, session persistence."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from browserd.models import DaemonConfig
from browserd.db import TaskDB
from browserd.portpool import PortPool

if TYPE_CHECKING:
    from browser_use import Browser

AUTH_PATTERNS = (
    "login", "signin", "accounts.google", "auth", "password",
    "captcha", "recaptcha", "verify", "2fa", "two-factor",
)


class TaskManager:
    """Manages task queue, execution, sessions, and browser CDP port pool.

    Each task claims a dedicated port from the PortPool. Sessions persist
    across tasks, surviving browser crashes via detached tab state.
    """

    def __init__(self, db: TaskDB, config: DaemonConfig):
        self.db = db
        self.config = config
        self.pool = PortPool(config)
        self.running: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(config.max_parallel)

    # ── Public API ──────────────────────────────────────────────────────────

    async def run(self, prompt: str, browser: str = "chrome",
                  keep_open: bool = False, close_tabs: bool = True,
                  max_steps: int = 30, model: str = "deepseek-chat",
                  session_id: str | None = None,
                  tab_target_id: str | None = None,
                  new_tab: bool = False,
                  follow_up_task: bool = False) -> str:
        """Run a task — unified entry point for all task variants.

        Args:
            prompt: The task instruction.
            browser: "chrome" or "chromium" — which binary to use.
            keep_open: Keep tab open after task; auto-creates session.
            close_tabs: Close tab after task (unless keep_open overrides).
            max_steps: Maximum agent steps.
            model: LLM model name.
            session_id: Bind to existing session (None = new or auto-create).
            tab_target_id: Target a specific tab in the session.
            new_tab: Open a new tab in the session instead of reusing.
            follow_up_task: Start from current browser state (no auto-navigate).
        """
        task_id = uuid.uuid4().hex[:12]

        # Resolve session
        actual_session_id = session_id
        if keep_open and not actual_session_id:
            # Auto-create session from task prompt
            actual_session_id = f"auto-{prompt[:20].replace(' ', '-').lower()}-{task_id[:6]}"
            self.db.create_session(actual_session_id)
            self.db.add_log(task_id, None, "info", f"Auto-created session: {actual_session_id}")

        is_keep_open = keep_open
        is_close_tabs = close_tabs if not keep_open else False

        self.db.create_task(
            task_id, prompt, browser, is_close_tabs, max_steps, model,
            session_id=actual_session_id,
        )
        self.db.add_log(task_id, None, "info", f"Queued: {prompt[:100]}")

        # Store extra params in task record for runner
        self._task_extras: dict[str, dict] = getattr(self, '_task_extras', {})
        self._task_extras[task_id] = {
            "keep_open": keep_open,
            "session_id": actual_session_id,
            "tab_target_id": tab_target_id,
            "new_tab": new_tab,
            "follow_up_task": follow_up_task,
            "browser": browser,
        }

        await self._dequeue()
        return task_id

    async def resume(self, task_id: str) -> bool:
        """Resume a blocked/failed task, preserving its session."""
        t = self.db.get_task(task_id)
        if not t or t["status"] not in ("blocked", "failed"):
            return False
        self.db.update_task(task_id, status="queued", blocked_reason=None, error=None)
        self.db.add_log(task_id, None, "info", "Resumed by user")
        await self._dequeue()
        return True

    async def cancel(self, task_id: str) -> bool:
        """Cancel a task. Closes tab if close_tabs. Frees port."""
        t = self.db.get_task(task_id)
        if not t:
            return False
        if task_id in self.running:
            self.running[task_id].cancel()
            try:
                await self.running[task_id]
            except asyncio.CancelledError:
                pass
        if t["status"] in ("queued", "running", "blocked"):
            status = "cancelled"
            self.db.update_task(task_id, status=status, finished_at=self._now())
            self.db.add_log(task_id, None, "info", "Cancelled by user")
            # Free port if claimed
            port = t.get("port")
            if port:
                self.pool.release(port)
            return True
        return False

    async def shutdown(self) -> None:
        """Cancel all running tasks, kill all browsers."""
        for tid, task in list(self.running.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.pool.shutdown_all()

    # ── State queries ───────────────────────────────────────────────────────

    def get_tasks_state(self) -> dict:
        """Return summary of task queue and running tasks."""
        q = self.db.list_tasks("queued")
        r = self.db.list_tasks("running")
        b = self.db.list_tasks("blocked")
        return {
            "running": [t for t in r],
            "queued": [t for t in q],
            "blocked": [t for t in b],
        }

    def get_sessions_state(self) -> list[dict]:
        """Return all sessions with tab summaries."""
        sessions = self.db.list_sessions()
        for s in sessions:
            tabs = s.get("tabs", [])
            s["tab_summary"] = {
                "active": sum(1 for t in tabs if t.get("status") == "active"),
                "detached": sum(1 for t in tabs if t.get("status") == "detached"),
                "closed": sum(1 for t in tabs if t.get("status") == "closed"),
            }
        return sessions

    def get_session_detail(self, session_id: str) -> dict | None:
        """Get full session detail with tabs and associated tasks."""
        s = self.db.get_session(session_id)
        if not s:
            return None
        tasks = self.db.list_tasks()
        s["task_ids"] = [
            t["id"] for t in tasks
            if t.get("session_id") == session_id
        ]
        s["tab_count"] = {
            "active": sum(1 for t in s.get("tabs", []) if t.get("status") == "active"),
            "detached": sum(1 for t in s.get("tabs", []) if t.get("status") == "detached"),
            "closed": sum(1 for t in s.get("tabs", []) if t.get("status") == "closed"),
        }
        return s

    def get_system_state(self) -> dict:
        """Full system overview."""
        return {
            "ports": [
                {"port": p, **info}
                for p, info in self.pool.all_ports().items()
            ],
            "sessions": self.get_sessions_state(),
            "tasks": {
                "running": len(self.running),
                "queued": len([t for t in self.db.list_tasks("queued")]),
                "blocked": len([t for t in self.db.list_tasks("blocked")]),
            },
        }

    async def close_session(self, session_id: str) -> bool:
        """Close a session: detach all tabs, free port, mark closed."""
        s = self.db.get_session(session_id)
        if not s:
            return False

        port = s.get("browser_port")
        # Detach all active tabs
        for tab in s.get("tabs", []):
            if tab.get("status") == "active":
                self.db.update_tab_status(session_id, tab["target_id"], "detached")

        # Free port
        if port:
            self.pool.release(port)
            # Mark sessions on this port as detached if no active tabs remain
            tabs = self.db.get_session_tabs(session_id)
            has_active = any(t.get("status") == "active" for t in tabs)
            if not has_active:
                self.db.update_session(session_id, status="detached", browser_port=None)

        self.db.update_session(session_id, status="closed")
        return True

    # ── Queue management ────────────────────────────────────────────────────

    async def _dequeue(self) -> None:
        # Pick the first queued task not already being processed
        queued = self.db.list_tasks("queued")
        for t in queued:
            tid = t["id"]
            if tid not in self.running:
                print(f"[DEQUEUE] Spawning {tid} at {self._now()[:19]}", flush=True)
                task = asyncio.create_task(self._run_with_semaphore(tid))
                self.running[tid] = task
                return

    async def _run_with_semaphore(self, task_id: str) -> None:
        print(f"[SEMAPHORE] {task_id[:8]} entering import at {self._now()[11:19]}", flush=True)
        import browser_use  # noqa: F401
        print(f"[SEMAPHORE] {task_id[:8]} import done, acquiring semaphore (avail={self.semaphore._value})", flush=True)
        async with self.semaphore:
            print(f"[SEMAPHORE] {task_id[:8]} acquired semaphore, entering _run_task", flush=True)
            await self._run_task(task_id)

    # ── State reconciliation ────────────────────────────────────────────────

    async def _reconcile_state(self) -> None:
        """Check all occupied ports and active tabs. Detach dead ones."""
        for port, info in self.pool.all_ports().items():
            if info["status"] != "occupied":
                continue
            alive = await self.pool.health_check(port)
            if not alive:
                # Browser died — detach all active tabs on this port
                self.db.detach_all_tabs_on_port(port)
                # Mark sessions on this port as detached
                sessions = self.db.find_sessions_by_port(port)
                for s in sessions:
                    tabs = self.db.get_session_tabs(s["id"])
                    has_active = any(t.get("status") == "active" for t in tabs)
                    if not has_active:
                        self.db.update_session(s["id"], status="detached")
                self.pool.ports[port] = "dead"
                self.pool.release(port)

    # ── Task runner ─────────────────────────────────────────────────────────

    async def _run_task(self, task_id: str) -> None:
        """Execute one task with session-aware lifecycle."""
        t = self.db.get_task(task_id)
        if not t:
            return

        # IMMEDIATELY mark as running to prevent double-dequeue race
        if t["status"] == "queued":
            self.db.update_task(task_id, status="running")

        extras = getattr(self, '_task_extras', {}).pop(task_id, {})
        keep_open = extras.get("keep_open", False)
        session_id = extras.get("session_id") or t.get("session_id")
        tab_target_id = extras.get("tab_target_id")
        new_tab = extras.get("new_tab", False)
        follow_up = extras.get("follow_up_task", False) or bool(session_id)
        browser_kind = extras.get("browser", t.get("browser", "chrome"))

        # State reconciliation before running (moved to periodic daemon loop)
        # await self._reconcile_state()  # REMOVED — races with concurrent port acquisition

        # Acquire port
        try:
            reuse = bool(session_id)  # reuse browser for session continuations
            print(f"[TASK] {task_id[:8]} acquiring port (reuse={reuse}) at {self._now()[11:19]}", flush=True)
            port, cdp_url = await self.pool.acquire(browser_kind, reuse=reuse)
            print(f"[TASK] {task_id[:8]} got port {port} at {self._now()[11:19]}", flush=True)
            self.db.update_task(task_id, status="running", port=port, started_at=self._now())
            self.db.add_log(task_id, 0, "info",
                          f"Started ({browser_kind} on :{port}): {t['prompt'][:100]}")
        except RuntimeError as e:
            self.db.update_task(task_id, status="failed", error=str(e), started_at=None)
            self.db.add_log(task_id, None, "error", f"Failed to acquire port: {e}")
            if task_id in self.running:
                del self.running[task_id]
            await self._dequeue()
            return

        session: Browser | None = None
        created_targets: list[str] = []

        try:
            from browser_use import Agent, Browser
            from browser_use.llm import ChatDeepSeek

            # Resolve session state
            if session_id:
                s = self.db.get_session(session_id)
                if s:
                    self.db.update_session(session_id, browser_port=port)

            # Create or reuse BrowserSession
            session = Browser(cdp_url=cdp_url)

            # Determine starting tab
            if follow_up and tab_target_id:
                # Focus on specific tab
                try:
                    from cdp_use.cdp.target.commands import ActivateTargetParameters
                    await session.cdp_client.send.Target.activateTarget(
                        ActivateTargetParameters(targetId=tab_target_id)
                    )
                    self.db.add_log(task_id, 0, "info", f"Focused tab {tab_target_id}")
                except Exception:
                    # Tab doesn't exist — navigate to its saved URL instead
                    tabs = self.db.get_session_tabs(session_id) if session_id else []
                    tab_url = next((t["url"] for t in tabs if t["target_id"] == tab_target_id), None)
                    if tab_url:
                        follow_up = False  # need to navigate
                        from cdp_use.cdp.target.commands import CreateTargetParameters
                        result = await session.cdp_client.send.Target.createTarget(
                            CreateTargetParameters(url=tab_url)
                        )
                        created_targets.append(result['targetId'])
            elif follow_up and not new_tab:
                # Continue from current browser state — find and activate the right tab
                try:
                    from cdp_use.cdp.target.commands import GetTargetsParameters, ActivateTargetParameters

                    targets_result = await session.cdp_client.send.Target.getTargets()
                    pages = [
                        t for t in targets_result.get('targetInfos', [])
                        if t.get('type') == 'page'
                    ]

                    # 1. Try to find the session's active tab still open in browser
                    target_to_activate = None
                    if session_id:
                        tabs = self.db.get_session_tabs(session_id)
                        active_tab = next((t for t in tabs if t.get('status') == 'active'), None)
                        if active_tab:
                            for page in pages:
                                if page.get('targetId') == active_tab['target_id']:
                                    target_to_activate = active_tab['target_id']
                                    break

                    # 2. If not found, pick the first non-about:blank page
                    if not target_to_activate:
                        for page in pages:
                            if page.get('url') and page['url'] != 'about:blank':
                                target_to_activate = page['targetId']
                                break

                    # 3. Activate the chosen tab or navigate
                    if target_to_activate:
                        await session.cdp_client.send.Target.activateTarget(
                            ActivateTargetParameters(targetId=target_to_activate)
                        )
                        self.db.add_log(task_id, 0, "info",
                                      f"Activated tab {target_to_activate[:12]}")
                    else:
                        # No tabs to activate — navigate to saved URL
                        navigate_url = None
                        if session_id:
                            s = self.db.get_session(session_id)
                            tabs = self.db.get_session_tabs(session_id)
                            if s and s.get('last_url'):
                                navigate_url = s['last_url']
                            elif tabs:
                                detached = [t for t in tabs if t.get('status') == 'detached']
                                best = detached[-1] if detached else tabs[-1]
                                navigate_url = best.get('url')

                        if navigate_url:
                            from cdp_use.cdp.target.commands import CreateTargetParameters
                            result = await session.cdp_client.send.Target.createTarget(
                                CreateTargetParameters(url=navigate_url)
                            )
                            created_targets.append(result['targetId'])
                            follow_up = False  # We navigated, not continuing from state
                        else:
                            follow_up = False  # Let LLM navigate from blank
                except Exception as e:
                    self.db.add_log(task_id, 0, "warn",
                                  f"Tab activation failed: {e}, falling back to navigation")
                    follow_up = False

            elif session_id and new_tab:
                # Open new tab for this task
                from cdp_use.cdp.target.commands import CreateTargetParameters
                result = await session.cdp_client.send.Target.createTarget(
                    CreateTargetParameters(url='about:blank')
                )
                created_targets.append(result['targetId'])

            # LLM
            llm = ChatDeepSeek(
                model=t["model"],
                api_key=self.config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", ""),
            )

            extend = (
                "You are a thorough web research agent.\n"
                "1. Open URLs directly from search results\n"
                "2. Read page content carefully before concluding\n"
                "3. If stuck on login/auth you cannot bypass, call done(success=False)\n"
                "4. Summarize clearly in final response with website names and URLs"
            )

            step_state: dict[str, Any] = {"step": 0, "url": "", "login_hits": 0}

            async def on_step(state: Any, output: Any, step: int) -> None:
                step_state["step"] = step
                url = getattr(state, "url", "") or ""
                step_state["url"] = url
                if url and any(p in url.lower() for p in AUTH_PATTERNS):
                    step_state["login_hits"] += 1
                self.db.update_task(task_id, step_count=step, current_url=url)

            agent = Agent(
                task=t["prompt"],
                llm=llm,
                browser=session,
                use_vision=False,
                max_failures=5,
                extend_system_message=extend,
                flash_mode=False,
                use_thinking=True,
                step_timeout=self.config.step_timeout,
                register_new_step_callback=on_step,
                directly_open_url=not follow_up,
            )

            history = await agent.run(max_steps=t["max_steps"])

            # ── Process results ──
            is_done = history.is_done()
            result_text = history.final_result() or "(no result)"
            urls = history.urls() or []
            steps = history.number_of_steps()
            errors = [str(e) for e in (history.errors() or []) if e]
            last_url = urls[-1] if urls else step_state["url"]

            blocked, reason = False, None
            if not is_done:
                if step_state["login_hits"] >= 3 or any(
                    p in (last_url or "").lower() for p in AUTH_PATTERNS
                ):
                    blocked, reason = True, f"Auth required at {(last_url or '')[:120]}"
                elif steps >= t["max_steps"]:
                    blocked, reason = True, f"Max steps ({t['max_steps']}) reached"
            elif (result_text and
                  last_url and
                  any(p in (last_url or "").lower() for p in AUTH_PATTERNS) and
                  any(p in (result_text or "").lower()
                      for p in ("login", "sign in", "credentials", "cannot access",
                                "unable to access", "without logging", "not logged",
                                "need to log"))):
                # Agent called done(success=False) on auth page — treat as blocked
                blocked, reason = True, f"Auth blocked at {(last_url or '')[:120]}"

            result_json = json.dumps({
                "success": bool(is_done),
                "steps": steps,
                "result": result_text,
                "urls": urls[-20:],
                "errors": errors[:5],
                "blocked": blocked,
                "blocked_reason": reason,
            })

            status = "blocked" if blocked else ("done" if is_done else "failed")
            self.db.update_task(
                task_id,
                status=status,
                finished_at=self._now(),
                result=result_json,
                error="; ".join(errors) if errors else None,
                blocked_reason=reason,
                step_count=steps,
            )

            # ── Session update ──
            if session_id:
                self.db.update_session(session_id, last_url=last_url)
                # Track tab
                try:
                    current_target_id = session.agent_focus_target_id
                    if current_target_id:
                        current_url = await session.get_current_page_url()
                        current_title = ""
                        try:
                            # Get page title via evaluate
                            cdp_sess = await session.get_or_create_cdp_session()
                            result = await cdp_sess.cdp_client.send.Runtime.evaluate(
                                params={'expression': 'document.title'},
                                session_id=cdp_sess.session_id,
                            )
                            current_title = result.get('result', {}).get('value', '')
                        except Exception:
                            pass

                        self.db.add_tab_to_session(
                            session_id, current_target_id,
                            url=current_url, title=current_title,
                            port=port, status="active"
                        )
                        created_targets.append(current_target_id)
                except Exception:
                    pass

            # ── Cleanup ──
            if t["close_tabs"] and not blocked:
                await self._close_tabs(session, created_targets, session_id)
            elif keep_open and not blocked:
                self.db.add_log(task_id, None, "info",
                              f"Tab kept open in session {session_id}")
            elif blocked:
                self.db.add_log(task_id, None, "info",
                              f"Tab kept open — resolve then: browser-cli resume {task_id}")

            self.db.add_log(
                task_id, steps,
                "info" if is_done else ("warn" if blocked else "error"),
                f"Task {status}{': ' + reason if reason else ''}",
            )

        except asyncio.CancelledError:
            self.db.update_task(task_id, status="cancelled", finished_at=self._now())
            self.db.add_log(task_id, None, "info", "Task cancelled by user")
            await self._close_tabs(session, created_targets, session_id)
        except Exception as e:
            msg = str(e)
            st = (
                "blocked"
                if any(p in msg.lower() for p in ("login", "auth", "captcha", "permission"))
                else "failed"
            )
            self.db.update_task(task_id, status=st, finished_at=self._now(), error=msg)
            self.db.add_log(task_id, None, "error", f"Task {st}: {msg}")
            await self._close_tabs(session, created_targets, session_id)
        finally:
            # Stop browser session (doesn't kill Chrome, just disconnects CDP)
            if session:
                try:
                    await session.stop()
                except Exception:
                    pass

            # Handle port: keep occupied if session has active tabs, else free
            t2 = self.db.get_task(task_id)
            port_to_check = t2.get("port") if t2 else port
            should_release = True
            if port_to_check and session_id:
                tabs = self.db.get_session_tabs(session_id)
                has_active = any(ta.get("status") == "active" for ta in tabs)
                task_status = t2.get("status", "") if t2 else ""
                # Don't release port on blocked/keep-open — browser must stay alive
                if task_status == "blocked" or keep_open:
                    should_release = False
                    self.db.add_log(task_id, None, "info",
                                  f"Port {port_to_check} held (blocked/keep-open)")
                elif has_active:
                    should_release = False
                if should_release:
                    self.pool.release(port_to_check)
            elif port_to_check and should_release:
                # No session — always free the port
                self.pool.release(port_to_check)

            if task_id in self.running:
                del self.running[task_id]
            await self._dequeue()

    async def _close_tabs(self, session: Browser | None, target_ids: list[str],
                          session_id: str | None) -> None:
        """Close specified tabs and update session state."""
        if not session or not target_ids:
            return
        for tid in target_ids:
            try:
                from cdp_use.cdp.target.commands import CloseTargetParameters
                await session.cdp_client.send.Target.closeTarget(
                    CloseTargetParameters(targetId=tid)
                )
                if session_id:
                    self.db.update_tab_status(session_id, tid, "closed")
            except Exception:
                if session_id:
                    self.db.update_tab_status(session_id, tid, "detached")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
