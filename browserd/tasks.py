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
from browserd.portpool import PortPool, _log

if TYPE_CHECKING:
    from browser_use import Browser

AUTH_PATTERNS = (
    "login", "signin", "accounts.google", "auth", "password",
    "captcha", "recaptcha", "verify", "2fa", "two-factor",
)


def _build_llm(config: DaemonConfig, model_override: str | None = None):
    """Build an LLM instance from provider config.

    Resolves the provider from config.llm_provider, loads the chat class
    dynamically, and constructs it with the appropriate API key and model.
    Falls back to ChatDeepSeek if the provider is unknown.
    """
    from browserd.providers import PROVIDERS

    provider = PROVIDERS.get(config.llm_provider)
    if provider is None:
        # Unknown provider — fall back to DeepSeek
        from browser_use.llm.deepseek.chat import ChatDeepSeek
        return ChatDeepSeek(
            model=model_override or config.llm_model or "deepseek-chat",
            api_key=config.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", ""),
        )

    # Dynamic import of chat class
    import importlib
    module = importlib.import_module(provider["import_path"])
    chat_cls = getattr(module, provider["attr"])

    # Resolve model
    model = model_override or config.llm_model or provider["default_model"]

    # Build kwargs based on provider type
    kwargs: dict[str, Any] = {"model": model}

    env_vars = provider.get("env_vars", [])

    if provider.get("no_api_key"):
        # Local providers (Ollama, LiteLLM) — use base_url if configured
        for ev in provider.get("extra_prompts", []):
            env_key = ev[0]
            val = os.environ.get(env_key)
            if val:
                if env_key == "OLLAMA_BASE_URL" or env_key == "LITELLM_BASE_URL":
                    kwargs["base_url"] = val.rstrip("/")
    elif provider["attr"] == "ChatAzureOpenAI":
        # Azure needs azure_endpoint instead of api_key
        kwargs["azure_endpoint"] = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        kwargs["api_version"] = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
        api_key = os.environ.get(env_vars[0], "") if env_vars else ""
        if not kwargs["azure_endpoint"]:
            # Fall back to api_key-only if no endpoint configured
            kwargs["api_key"] = api_key or config.deepseek_api_key or ""
            del kwargs["azure_endpoint"]
        else:
            kwargs["api_key"] = api_key or ""
    else:
        # Standard API key providers
        api_key = ""
        for ev in env_vars:
            api_key = os.environ.get(ev, "")
            if api_key:
                break
        if not api_key and config.deepseek_api_key:
            api_key = config.deepseek_api_key

        # ChatOpenRouter needs explicit api_key param (doesn't auto-detect env)
        if provider["attr"] == "ChatOpenRouter":
            kwargs["api_key"] = api_key or ""
        else:
            # Most classes auto-detect from env, but passing explicitly is fine
            if api_key:
                kwargs["api_key"] = api_key

    return chat_cls(**kwargs)


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
        self._agents: dict[str, Any] = {}   # agent refs for pause/inject
        self.semaphore = asyncio.Semaphore(config.max_parallel)
        from browserd.profile_manager import ProfileManager
        self.profiles = ProfileManager(db, config)

    # ── Public API ──────────────────────────────────────────────────────────

    async def run(self, prompt: str, browser: str = "chrome",
                  max_steps: int = 30, model: str = "deepseek-chat",
                  session_id: str | None = None,
                  tab_target_id: str | None = None,
                  new_tab: bool = False,
                  follow_up_task: bool = False,
                  profile: str | None = None) -> str:
        """Run a task — unified entry point for all task variants.

        Args:
            prompt: The task instruction.
            browser: "chrome" or "chromium" — which binary to use.
            max_steps: Maximum agent steps.
            model: LLM model name.
            session_id: Bind to existing session (None = auto-create).
            tab_target_id: Target a specific tab in the session.
            new_tab: Open a new tab in the session instead of reusing.
            follow_up_task: Start from current browser state (no auto-navigate).
            profile: Named profile to use (None = 'default').
        """
        task_id = uuid.uuid4().hex[:12]

        # Resolve session — always auto-create if no explicit session
        actual_session_id = session_id
        if not actual_session_id:
            actual_session_id = f"auto-{prompt[:20].replace(' ', '-').lower()}-{task_id[:6]}"
            self.db.create_session(actual_session_id)
            self.db.add_log(task_id, None, "info", f"Auto-created session: {actual_session_id}")

        self.db.create_task(
            task_id, prompt, browser, max_steps, model,
            session_id=actual_session_id,
        )
        self.db.add_log(task_id, None, "info", f"Queued: {prompt[:100]}")

        # Store extra params in task record for runner
        self._task_extras: dict[str, dict] = getattr(self, '_task_extras', {})
        self._task_extras[task_id] = {
            "session_id": actual_session_id,
            "tab_target_id": tab_target_id,
            "new_tab": new_tab,
            "follow_up_task": follow_up_task,
            "browser": browser,
            "profile": profile,
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
        """Cancel a task. Marks as cancelled and frees port via session_close."""
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

    async def pause(self, task_id: str) -> bool:
        """Pause a running agent — freezes mid-step, browser stays open."""
        agent = self._agents.get(task_id)
        if agent is None:
            return False
        if agent.state.paused:
            return True  # already paused
        agent.pause()
        self.db.add_log(task_id, None, "warn", "Agent paused — waiting for user to log in")
        return True

    async def resume_agent(self, task_id: str) -> bool:
        """Resume a paused agent — continues from current browser state."""
        agent = self._agents.get(task_id)
        if agent is None:
            return False
        if not agent.state.paused:
            return True  # already running
        agent.resume()
        self.db.add_log(task_id, None, "info", "Agent resumed by user")
        return True

    async def inject(self, task_id: str, new_prompt: str) -> bool:
        """Inject a corrected/follow-up prompt into a paused or running agent.
        
        Calls agent.add_new_task() which resets paused state and continues.
        """
        agent = self._agents.get(task_id)
        if agent is None:
            return False
        agent.add_new_task(new_prompt)
        self.db.add_log(task_id, None, "info", f"Prompt injected: {new_prompt[:120]}")
        self.db.update_task(task_id, prompt=f"{self.db.get_task(task_id)['prompt']} | CORRECTION: {new_prompt}")
        return True

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
        _log(f"[DEQUEUE] checking {len(queued)} queued tasks, {len(self.running)} running, sem avail={self.semaphore._value}")
        for t in queued:
            tid = t["id"]
            if tid not in self.running:
                _log(f"[DEQUEUE] Spawning {tid} at {self._now()[:19]}")
                task = asyncio.create_task(self._run_with_semaphore(tid))
                self.running[tid] = task
                return
        _log(f"[DEQUEUE] nothing to spawn (all queued tasks already in running)")

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
        session_id = extras.get("session_id") or t.get("session_id")
        tab_target_id = extras.get("tab_target_id")
        new_tab = extras.get("new_tab", False)
        follow_up = extras.get("follow_up_task", False) or bool(session_id)
        browser_kind = extras.get("browser", t.get("browser", "chrome"))
        profile_name = extras.get("profile")

        # Resolve profile → data_dir (None → 'default')
        profile_data_dir = self.profiles.resolve(profile_name)
        resolved_profile = profile_name or "default"

        # State reconciliation before running (moved to periodic daemon loop)
        # await self._reconcile_state()  # REMOVED — races with concurrent port acquisition

        # Acquire port
        try:
            reuse = bool(session_id)  # reuse browser for session continuations
            print(f"[TASK] {task_id[:8]} acquiring port (reuse={reuse}) at {self._now()[11:19]}", flush=True)
            port, cdp_url = await self.pool.acquire(browser_kind, reuse=reuse, profile_data_dir=profile_data_dir)
            print(f"[TASK] {task_id[:8]} got port {port} at {self._now()[11:19]}", flush=True)
            self.db.update_task(task_id, status="running", port=port, started_at=self._now())
            self.db.add_log(task_id, 0, "info",
                          f"Started ({browser_kind} on :{port}): {t['prompt'][:100]}")
            self.profiles.set_running(resolved_profile)
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
            llm = _build_llm(self.config, t.get("model"))

            extend = (
                "You are a thorough web research agent.\n"
                "1. Open URLs directly from search results\n"
                "2. Read page content carefully before concluding\n"
                "3. If stuck on login/auth you cannot bypass, call done(success=False)\n"
                "4. Summarize clearly in final response with website names and URLs"
            )

            step_state: dict[str, Any] = {
                "step": 0, "url": "", "login_hits": 0,
                "url_history": [],     # last 10 URLs for loop detection
                "loop_warned": False,
            }

            async def on_step(state: Any, output: Any, step: int) -> None:
                step_state["step"] = step
                url = getattr(state, "url", "") or ""
                step_state["url"] = url
                if url and any(p in url.lower() for p in AUTH_PATTERNS):
                    step_state["login_hits"] += 1
                    if step_state["login_hits"] >= 2 and task_id in self._agents:
                        self._agents[task_id].pause()
                        self.db.add_log(task_id, step, "warn",
                                        f"Auto-paused: login wall at {url[:120]}")
                        self.db.update_task(task_id, blocked_reason=f"Login wall — paused at {url[:120]}")
                        print(f"[PAUSE] {task_id[:8]} Login wall detected, agent paused for manual login", flush=True)

                # ── Loop detection ──
                history = step_state["url_history"]
                history.append(url)
                if len(history) > 10:
                    history.pop(0)

                if len(history) >= 4:
                    url_counts = {}
                    for u in history:
                        url_counts[u] = url_counts.get(u, 0) + 1
                    most_repeated = max(url_counts.values())
                    if most_repeated >= 3 and not step_state["loop_warned"]:
                        step_state["loop_warned"] = True
                        repeated_url = max(url_counts, key=url_counts.get)
                        msg = (f"LOOP DETECTED: '{repeated_url[:80]}' visited "
                               f"{most_repeated}x in last {len(history)} steps")
                        self.db.add_log(task_id, step, "warn", msg)
                        print(f"[LOOP] {task_id[:8]} {msg}", flush=True)

                self.db.update_task(task_id, step_count=step, current_url=url)

                # ── Store full step data for real-time awareness ──
                try:
                    step_data = {
                        "step": step,
                        "url": url,
                        "title": getattr(state, "title", "") or "",
                    }
                    # Extract model's thinking from output
                    if output is not None:
                        if hasattr(output, "thinking") and output.thinking:
                            step_data["thinking"] = str(output.thinking)[:500]
                        if hasattr(output, "evaluation") and output.evaluation:
                            step_data["evaluation"] = str(output.evaluation)[:200]
                        # Extract action type
                        if hasattr(output, "action") and output.action:
                            action_list = output.action if isinstance(output.action, list) else [output.action]
                            step_data["actions"] = [
                                a.model_dump() if hasattr(a, "model_dump") else str(a)
                                for a in action_list[:3]
                            ]
                    # Extract page content snapshot
                    if hasattr(state, "dom_state") and state.dom_state:
                        try:
                            text = state.dom_state.get_llm_representation() if hasattr(state.dom_state, "get_llm_representation") else ""
                            step_data["page_snapshot"] = text[:800] if text else ""
                        except Exception:
                            pass
                    self.db.add_log(task_id, step, "step_data", json.dumps(step_data, default=str))
                except Exception as e:
                    self.db.add_log(task_id, step, "step_data", json.dumps({"error": str(e)[:200]}))

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
            self._agents[task_id] = agent

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

            # ── Cleanup: browser always stays open ──
            if blocked:
                self.db.add_log(task_id, None, "info",
                              f"Task blocked — browser stays open. Resume: browser-cli resume {task_id}")
            else:
                self.db.add_log(task_id, None, "info",
                              f"Task done — browser stays open in session {session_id}")

            self.db.add_log(
                task_id, steps,
                "info" if is_done else ("warn" if blocked else "error"),
                f"Task {status}{': ' + reason if reason else ''}",
            )

        except asyncio.CancelledError:
            self.db.update_task(task_id, status="cancelled", finished_at=self._now())
            self.db.add_log(task_id, None, "info", "Task cancelled — browser stays open")
        except Exception as e:
            msg = str(e)
            st = (
                "blocked"
                if any(p in msg.lower() for p in ("login", "auth", "captcha", "permission"))
                else "failed"
            )
            self.db.update_task(task_id, status=st, finished_at=self._now(), error=msg)
            self.db.add_log(task_id, None, "error", f"Task {st} — browser stays open: {msg}")
        finally:
            # Browser ALWAYS stays alive — never close via CDP, never session.stop()
            # Ports are only released by explicit session-close command
            self.db.add_log(task_id, None, "info",
                          f"Browser kept alive on port {port}, session {session_id}")

            # Port stays held — only session_close releases it
            if port and session_id:
                self.db.add_log(task_id, None, "info",
                              f"Port {port} held for session {session_id}")

            if task_id in self.running:
                del self.running[task_id]
            self._agents.pop(task_id, None)
            self.profiles.set_idle(resolved_profile)
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

    async def _close_blank_tabs(self, session: Browser | None, port: int) -> None:
        """Close leftover about:blank tabs created during agent startup, via direct HTTP."""
        try:
            import aiohttp
            cdp_url = f"http://localhost:{port}"
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{cdp_url}/json", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status != 200:
                        return
                    targets = await resp.json()

                for t in targets:
                    if t.get('type') != 'page':
                        continue
                    url = t.get('url', '')
                    title = t.get('title', '')
                    tid = t.get('id', '')
                    if (url == 'about:blank' and 'Starting agent' in title) or \
                       (url == 'about:blank' and not title):
                        try:
                            async with http.get(
                                f"{cdp_url}/json/close/{tid}",
                                timeout=aiohttp.ClientTimeout(total=2)
                            ) as cr:
                                if cr.status == 200:
                                    print(f"[CLEANUP] Closed leftover blank tab {tid[:12]} on port {port}", flush=True)
                        except Exception:
                            pass
        except Exception as e:
            print(f"[CLEANUP] _close_blank_tabs error: {e}", flush=True)

    async def _close_all_tabs(self, session: Browser | None, port: int) -> None:
        """Close all page tabs on a browser via direct CDP HTTP (bypasses agent session)."""
        try:
            import aiohttp
            cdp_url = f"http://localhost:{port}"
            async with aiohttp.ClientSession() as http:
                # Get all targets
                async with http.get(f"{cdp_url}/json", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status != 200:
                        return
                    targets = await resp.json()

                closed = 0
                for t in targets:
                    if t.get('type') != 'page':
                        continue
                    tid = t.get('id', '')
                    if not tid:
                        continue
                    try:
                        async with http.get(
                            f"{cdp_url}/json/close/{tid}",
                            timeout=aiohttp.ClientTimeout(total=2)
                        ) as cr:
                            if cr.status == 200:
                                closed += 1
                    except Exception:
                        pass

                if closed:
                    print(f"[CLEANUP] Closed {closed} tabs on port {port}", flush=True)
        except Exception as e:
            print(f"[CLEANUP] _close_all_tabs error: {e}", flush=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
