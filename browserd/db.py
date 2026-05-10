"""SQLite database layer for browserd — WAL mode, typed operations, no ORM."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from browserd.models import TaskRecord, TaskSummary, TaskLogEntry


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskDB:
    """SQLite-backed task store with WAL mode for zero reader/writer contention."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        # Migration: add columns that may be missing from v1 schema (MUST run before CREATE INDEX)
        try:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN port INTEGER")
        except sqlite3.OperationalError:
            pass

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id              TEXT PRIMARY KEY,
                prompt          TEXT NOT NULL,
                browser         TEXT DEFAULT 'chrome',
                close_tabs      INTEGER DEFAULT 1,
                status          TEXT DEFAULT 'queued',
                max_steps       INTEGER DEFAULT 30,
                model           TEXT DEFAULT 'deepseek-chat',
                session_id      TEXT,
                port            INTEGER,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                started_at      TEXT,
                finished_at     TEXT,
                result          TEXT,
                error           TEXT,
                blocked_reason  TEXT,
                step_count      INTEGER DEFAULT 0,
                current_url     TEXT
            );
            CREATE TABLE IF NOT EXISTS task_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL REFERENCES tasks(id),
                step        INTEGER,
                level       TEXT DEFAULT 'info',
                message     TEXT NOT NULL,
                timestamp   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id                      TEXT PRIMARY KEY,
                browser_port            INTEGER,
                last_url                TEXT,
                last_focused_target_id  TEXT,
                status                  TEXT DEFAULT 'active',
                created_at              TEXT NOT NULL,
                updated_at              TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_tabs (
                session_id  TEXT NOT NULL REFERENCES sessions(id),
                target_id   TEXT NOT NULL,
                url         TEXT,
                title       TEXT,
                port        INTEGER,
                status      TEXT DEFAULT 'active',
                PRIMARY KEY (session_id, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_logs_task ON task_logs(task_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tabs_session ON session_tabs(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_port ON sessions(browser_port);
        """)
        self._conn.commit()

    # ── Tasks CRUD ──────────────────────────────────────────────────────────
    def create_task(self, task_id: str, prompt: str, browser: str = "chrome",
                    close_tabs: bool = True, max_steps: int = 30,
                    model: str = "deepseek-chat", session_id: str | None = None) -> None:
        ts = now()
        self._conn.execute(
            """INSERT INTO tasks (id, prompt, browser, close_tabs, status,
               max_steps, model, session_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task_id, prompt, browser, int(close_tabs), "queued", max_steps, model, session_id, ts, ts),
        )
        self._conn.commit()

    def update_task(self, task_id: str, **kwargs) -> None:
        kwargs["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [task_id]
        self._conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", vals)
        self._conn.commit()

    def get_task(self, task_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, status_filter: str = "all") -> list[dict]:
        q = """SELECT id, prompt, browser, close_tabs, status, max_steps,
               session_id, created_at, step_count, current_url, error, blocked_reason
               FROM tasks"""
        if status_filter != "all":
            q += " WHERE status=?"
            rows = self._conn.execute(q + " ORDER BY created_at DESC", (status_filter,)).fetchall()
        else:
            rows = self._conn.execute(q + " ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def next_queued(self) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    # ── Logs ────────────────────────────────────────────────────────────────
    def add_log(self, task_id: str, step: int | None, level: str, message: str) -> None:
        self._conn.execute(
            "INSERT INTO task_logs (task_id, step, level, message, timestamp) VALUES (?,?,?,?,?)",
            (task_id, step, level, message, now()),
        )
        self._conn.commit()

    def get_logs(self, task_id: str, tail: int = 50, level_filter: str | None = None) -> list[dict]:
        if level_filter:
            rows = self._conn.execute(
                "SELECT * FROM task_logs WHERE task_id=? AND level=? ORDER BY id DESC LIMIT ?",
                (task_id, level_filter, tail),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, tail),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Sessions CRUD ──────────────────────────────────────────────────────
    def create_session(self, session_id: str, port: int | None = None,
                       last_url: str | None = None) -> None:
        ts = now()
        self._conn.execute(
            """INSERT INTO sessions (id, browser_port, last_url, status, created_at, updated_at)
               VALUES (?,?,?,'active',?,?)""",
            (session_id, port, last_url, ts, ts),
        )
        self._conn.commit()

    def update_session(self, session_id: str, **kwargs) -> None:
        kwargs["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [session_id]
        self._conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", vals)
        self._conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return None
        s = dict(row)
        s["tabs"] = self.get_session_tabs(session_id)
        return s

    def delete_session(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM session_tabs WHERE session_id=?", (session_id,))
        self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self._conn.commit()

    def list_sessions(self, status_filter: str | None = None) -> list[dict]:
        q = "SELECT * FROM sessions"
        params: tuple = ()
        if status_filter:
            q += " WHERE status=?"
            params = (status_filter,)
        rows = self._conn.execute(q + " ORDER BY created_at DESC", params).fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            s["tabs"] = self.get_session_tabs(s["id"])
            sessions.append(s)
        return sessions

    def find_sessions_by_port(self, port: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE browser_port=? AND status='active'", (port,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Session tabs ───────────────────────────────────────────────────────
    def add_tab_to_session(self, session_id: str, target_id: str,
                           url: str | None = None, title: str | None = None,
                           port: int | None = None, status: str = "active") -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO session_tabs (session_id, target_id, url, title, port, status)
               VALUES (?,?,?,?,?,?)""",
            (session_id, target_id, url, title, port, status),
        )
        self._conn.commit()

    def update_tab_status(self, session_id: str, target_id: str, status: str,
                          url: str | None = None, title: str | None = None) -> None:
        if url or title:
            self._conn.execute(
                "UPDATE session_tabs SET status=?, url=COALESCE(?,url), title=COALESCE(?,title) WHERE session_id=? AND target_id=?",
                (status, url, title, session_id, target_id),
            )
        else:
            self._conn.execute(
                "UPDATE session_tabs SET status=? WHERE session_id=? AND target_id=?",
                (status, session_id, target_id),
            )
        self._conn.commit()

    def remove_tab_from_session(self, session_id: str, target_id: str) -> None:
        self._conn.execute(
            "DELETE FROM session_tabs WHERE session_id=? AND target_id=?",
            (session_id, target_id),
        )
        self._conn.commit()

    def get_session_tabs(self, session_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM session_tabs WHERE session_id=? ORDER BY target_id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_tabs_on_port(self, port: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM session_tabs WHERE port=? AND status='active'", (port,)
        ).fetchall()
        return [dict(r) for r in rows]

    def detach_all_tabs_on_port(self, port: int) -> None:
        """Mark all active tabs on a port as detached (browser died/closed)."""
        self._conn.execute(
            "UPDATE session_tabs SET status='detached' WHERE port=? AND status='active'",
            (port,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
