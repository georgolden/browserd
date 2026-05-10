"""Type definitions for the browserd CLI — follows browser-use Pydantic conventions."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    blocked = "blocked"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class BrowserKind(str, Enum):
    """Browser binary selection only — each port can run either binary."""

    chrome = "chrome"
    chromium = "chromium"


class TabStatus(str, Enum):
    """Tab lifecycle within a session."""

    active = "active"  # currently open, can be focused
    detached = "detached"  # browser/port died but URL preserved
    closed = "closed"  # task explicitly closed this tab


class LogLevel(str, Enum):
    info = "info"
    warn = "warn"
    error = "error"
    debug = "debug"


# ── Daemon configuration ────────────────────────────────────────────────────
class DaemonConfig(BaseModel):
    """Configuration for the browserd daemon."""

    model_config = ConfigDict(extra="forbid")

    socket_path: Path = Field(default=Path.home() / ".browserd" / "sock")
    db_path: Path = Field(default=Path.home() / ".browserd" / "tasks.db")

    chrome_path: Path = Field(default=Path("/usr/bin/google-chrome-stable"))
    chromium_path: Path = Field(default=Path("/usr/bin/chromium"))
    port_base: int = Field(default=9222, ge=1024, le=65535)

    max_parallel: int = Field(
        default_factory=lambda: int(os.environ.get("MAX_PARALLEL_TASKS", "4")),
        ge=1, le=16,
    )
    llm_provider: str = Field(
        default_factory=lambda: os.environ.get("LLM_PROVIDER", "deepseek"),
        description="Provider ID from provider registry (e.g. openai, anthropic, deepseek)",
    )
    llm_model: str | None = Field(
        default_factory=lambda: os.environ.get("LLM_MODEL") or None,
        description="Model override — uses provider default if unset",
    )
    default_max_steps: int = Field(default=30, ge=1)
    default_model: str = Field(default="deepseek-chat")
    step_timeout: int = Field(default=120, ge=10)
    cdp_launch_timeout: int = Field(default=30, ge=5)

    deepseek_api_key: str | None = Field(default=None)

    @property
    def port_range(self) -> range:
        """Range of CDP ports managed by the pool."""
        return range(self.port_base, self.port_base + self.max_parallel)

    def browser_path(self, kind: str) -> Path:
        """Resolve browser binary path."""
        if kind == "chromium":
            return self.chromium_path
        return self.chrome_path


# ── Task models ─────────────────────────────────────────────────────────────
class TaskCreate(BaseModel):
    """Request to create a new task."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(..., min_length=1, max_length=10000)
    browser: BrowserKind = Field(default=BrowserKind.chrome)
    close_tabs: bool = Field(default=True, description="Close tabs after task done")
    keep_open: bool = Field(default=False, description="Keep tab open; auto-create session")
    max_steps: int = Field(default=30, ge=1, le=200)
    model: str = Field(default="deepseek-chat")
    session_id: str | None = Field(default=None, description="Bind to existing session")
    tab_target_id: str | None = Field(default=None, description="Target specific tab")
    new_tab: bool = Field(default=False, description="Open new tab in session")
    follow_up_task: bool = Field(default=False, description="Start from current browser state")


class TaskResult(BaseModel):
    """Structured result from a completed task."""

    success: bool
    steps: int
    result: str = ""
    urls: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_reason: str | None = None


class TaskRecord(BaseModel):
    """Full task state as stored in SQLite."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    prompt: str
    browser: str = "chrome"
    close_tabs: bool = True
    status: TaskStatus = TaskStatus.queued
    max_steps: int = 30
    model: str = "deepseek-chat"
    session_id: str | None = None
    port: int | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    result: str | None = None
    error: str | None = None
    blocked_reason: str | None = None
    step_count: int = 0
    current_url: str | None = None

    @property
    def parsed_result(self) -> TaskResult | None:
        if not self.result:
            return None
        import json

        return TaskResult(**json.loads(self.result))


class TaskSummary(BaseModel):
    """Lightweight task view for list commands."""

    id: str
    prompt: str
    browser: str
    close_tabs: bool = True
    status: TaskStatus
    max_steps: int
    session_id: str | None = None
    created_at: str
    step_count: int = 0
    current_url: str | None = None
    error: str | None = None
    blocked_reason: str | None = None


# ── Session models ───────────────────────────────────────────────────────────
class TabRecord(BaseModel):
    """A tab within a session — persists across browser restarts."""

    target_id: str
    url: str | None = None
    title: str | None = None
    port: int | None = None
    status: TabStatus = TabStatus.active


class SessionRecord(BaseModel):
    """Long-lived sequence of tasks sharing browser state."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    browser_port: int | None = None
    last_url: str | None = None
    last_focused_target_id: str | None = None
    status: str = "active"  # active | detached | closed
    tabs: list[TabRecord] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TaskLogEntry(BaseModel):
    """Single log entry for a task."""

    id: int
    task_id: str
    step: int | None = None
    level: str = "info"
    message: str
    timestamp: str


# ── Protocol messages ───────────────────────────────────────────────────────
class SocketRequest(BaseModel):
    """Incoming command from browser-cli."""

    cmd: str
    id: str | None = None
    prompt: str | None = None
    browser: str | None = None
    keep_open: bool | None = None
    close_tabs: bool | None = None
    max_steps: int | None = None
    model: str | None = None
    session_id: str | None = None
    tab_target_id: str | None = None
    new_tab: bool | None = None
    follow_up_task: bool | None = None
    filter: str | None = None
    timeout: int | None = None
    tail: int | None = None
    target: str | None = None  # for state sub-commands: tasks|sessions|session|system


class SocketResponse(BaseModel):
    """Response sent back to browser-cli."""

    type: str
    id: str | None = None
    session_id: str | None = None
    tab_target_id: str | None = None
    version: str | None = None
    running: int | None = None
    message: str | None = None
    tasks: list[dict[str, Any]] | None = None
    task: dict[str, Any] | None = None
    logs: list[dict[str, Any]] | None = None
    ports: list[dict[str, Any]] | None = None
    sessions: list[dict[str, Any]] | None = None
    session: dict[str, Any] | None = None
