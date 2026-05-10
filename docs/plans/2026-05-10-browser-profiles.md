# Browser Profiles Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the anonymous-only browser model with named, persistent profiles. Each profile is a reusable Chrome identity with its own cookies, sessions, and logins. "Just like UFW profiles — pick one or use the default."

**Architecture:** A `ProfileManager` manages named profiles (backed by SQLite + filesystem). Each profile maps to a persistent `~/.browserd/profiles/{name}/` user-data-dir. The existing port-pool/task/session system stays intact — profiles just change WHICH user-data-dir Chrome launches with. CLI gets `--profile <name>` on `run`; omission uses `default`.

**Tech Stack:** Python 3.10+, Pydantic v2, SQLite (existing DB), asyncio

**Files to touch:**
- `browserd/models.py` — ProfileRecord, wire into DaemonConfig/SocketRequest
- `browserd/db.py` — profiles table + CRUD
- `browserd/profile_manager.py` — **NEW** — ProfileManager class
- `browserd/portpool.py` — `_launch()` accepts profile dir instead of `persistent` bool
- `browserd/tasks.py` — `run()` accepts `profile` name, resolved to dir for `acquire()`
- `browserd/daemon.py` — new `profile_create/list/delete` commands + `profile` passthrough on `run`
- `browserd/client.py` — `profile_create/list/delete/run` methods
- `browserd/cli.py` — `profile` subcommand group + `--profile` on `run`

**Deprecate:** `--persistent` flag (half-implemented, replaced by `--profile`)

---

### Task 1: Add Profile Pydantic model and wire into existing models

**Objective:** Define the Profile model and add `profile` fields to relevant request/config models.

**Files:**
- Modify: `browserd/models.py`

**Implementation:**

```python
class ProfileRecord(BaseModel):
    """A named, persistent Chrome profile."""
    model_config = ConfigDict(from_attributes=True)

    name: str                          # unique, e.g. "work", "default"
    browser: str = "chrome"            # "chrome" or "chromium"
    data_dir: str                      # ~/.browserd/profiles/{name}/
    status: str = "idle"              # idle | running
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
```

Add to `DaemonConfig`:
```python
profiles_dir: Path = Field(
    default_factory=lambda: Path(os.environ.get(
        "BROWSERD_PROFILES_DIR",
        str(Path.home() / ".browserd" / "profiles")
    )),
    description="Base directory for named browser profiles",
)
```

Add to `SocketRequest`:
```python
profile: str | None = None   # profile name for "run" command
```

Add to `TaskCreate`:
```python
profile: str | None = Field(default=None, description="Named profile to use (defaults to 'default')")
```

**Remove** the `persistent: bool` fields from `DaemonConfig`, `TaskCreate`, `SocketRequest` (they're being replaced).

**Verification:** Python imports cleanly: `python -c "from browserd.models import ProfileRecord, DaemonConfig; print('OK')"`

---

### Task 2: Add profiles table to SQLite (TaskDB)

**Objective:** Create the `profiles` table in the existing SQLite database with auto-migration.

**Files:**
- Modify: `browserd/db.py`

**Implementation:**
Add to `TaskDB._ensure_tables()` (or equivalent init):
```sql
CREATE TABLE IF NOT EXISTS profiles (
    name        TEXT PRIMARY KEY,
    browser     TEXT NOT NULL DEFAULT 'chrome',
    data_dir    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'idle',
    created_at  TEXT NOT NULL
);
```

Add methods:
```python
def create_profile(self, name: str, browser: str, data_dir: str) -> None: ...
def get_profile(self, name: str) -> dict | None: ...
def list_profiles(self) -> list[dict]: ...
def update_profile_status(self, name: str, status: str) -> None: ...
def delete_profile(self, name: str) -> None: ...
```

All using existing `self.conn.execute()` pattern with WAL mode.

**Verification:** Run `browser-cli ping` — daemon starts without errors. Check `sqlite3 ~/.browserd/tasks.db ".schema profiles"` shows the table.

---

### Task 3: Create ProfileManager

**Objective:** New module that manages profile lifecycle — create, list, delete, resolve to data_dir.

**Files:**
- Create: `browserd/profile_manager.py`

**Implementation:**

```python
"""Profile manager — named, persistent Chrome identities."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from browserd.models import DaemonConfig, ProfileRecord

if TYPE_CHECKING:
    from browserd.db import TaskDB


class ProfileManager:
    """Manages named browser profiles backed by filesystem + SQLite."""

    DEFAULT_PROFILE = "default"

    def __init__(self, db: TaskDB, config: DaemonConfig):
        self.db = db
        self.config = config

    def _profile_dir(self, name: str) -> Path:
        return self.config.profiles_dir / name

    async def create(self, name: str, browser: str = "chrome") -> ProfileRecord:
        """Create a new profile. Fails if already exists."""
        if self.db.get_profile(name):
            raise ValueError(f"Profile '{name}' already exists")
        data_dir = str(self._profile_dir(name))
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db.create_profile(name, browser, data_dir)
        return ProfileRecord(
            name=name, browser=browser, data_dir=data_dir,
            status="idle",
        )

    async def ensure_default(self) -> ProfileRecord:
        """Auto-create 'default' profile if it doesn't exist."""
        existing = self.db.get_profile(self.DEFAULT_PROFILE)
        if existing:
            return ProfileRecord(**existing)
        return await self.create(self.DEFAULT_PROFILE)

    def resolve(self, name: str | None) -> str:
        """Resolve profile name → data_dir. None → 'default'."""
        resolved = name or self.DEFAULT_PROFILE
        p = self.db.get_profile(resolved)
        if not p:
            raise ValueError(
                f"Profile '{resolved}' not found. "
                f"Create it with: browser-cli profile create {resolved}"
            )
        return p["data_dir"]

    def list(self) -> list[ProfileRecord]:
        return [ProfileRecord(**p) for p in self.db.list_profiles()]

    async def delete(self, name: str) -> None:
        """Delete profile and its data directory. Cannot delete 'default'."""
        if name == self.DEFAULT_PROFILE:
            raise ValueError("Cannot delete the default profile")
        p = self.db.get_profile(name)
        if not p:
            raise ValueError(f"Profile '{name}' not found")
        self.db.delete_profile(name)
        data_path = Path(p["data_dir"])
        if data_path.exists():
            shutil.rmtree(data_path)

    def set_running(self, name: str) -> None:
        self.db.update_profile_status(name, "running")

    def set_idle(self, name: str) -> None:
        self.db.update_profile_status(name, "idle")
```

**Verification:** `python -c "from browserd.profile_manager import ProfileManager; print('OK')"`

---

### Task 4: Wire ProfileManager into TaskManager and daemon startup

**Objective:** Instantiate ProfileManager in TaskManager, ensure default profile exists at startup.

**Files:**
- Modify: `browserd/tasks.py` (TaskManager.__init__)
- Modify: `browserd/daemon.py` (`_main_async`)

**Implementation:**

In `TaskManager.__init__`:
```python
from browserd.profile_manager import ProfileManager

self.profiles = ProfileManager(db, config)
```

In `daemon.py` `_main_async()`, after creating TaskManager:
```python
await manager.profiles.ensure_default()
```

**Verification:** `browser-cli daemon restart && browser-cli ping` — starts clean. Check SQLite: `sqlite3 ~/.browserd/tasks.db "SELECT * FROM profiles"` shows `default` row.

---

### Task 5: Refactor portpool._launch to accept profile_data_dir

**Objective:** Change `_launch()` signature: replace `persistent: bool` with `profile_data_dir: str`. Chrome always launches with the given directory.

**Files:**
- Modify: `browserd/portpool.py`

**Implementation:**

Change `acquire()`:
```python
async def acquire(self, browser: str = "chrome", reuse: bool = False,
                  profile_data_dir: str | None = None) -> tuple[int, str]:
```

Change `_launch()`:
```python
async def _launch(self, port: int, browser: str,
                   profile_data_dir: str | None = None) -> None:
```

Inside `_launch`:
```python
if profile_data_dir:
    user_data_dir = profile_data_dir
else:
    user_data_dir = f"/tmp/browserd-port{port}"
```

Update `_kill_port` regex to match both `/tmp/browserd-port{N}` and `~/.browserd/profiles/*` paths.

Remove the `persistent` parameter entirely.

**Verification:** Existing tasks without `--profile` still work — they use `/tmp/browserd-port{N}` as before.

---

### Task 6: Thread profile through tasks.py pipeline

**Objective:** `TaskManager.run()` accepts `profile: str | None`, resolves to data_dir, passes to `pool.acquire()`.

**Files:**
- Modify: `browserd/tasks.py`

**Implementation:**

Change `run()`:
```python
async def run(self, ..., profile: str | None = None) -> str:
```

Store in `_task_extras`:
```python
"profile": profile,
```

In `_run_task()`, extract and resolve:
```python
profile_name = extras.get("profile")
profile_data_dir = self.profiles.resolve(profile_name)  # None → "default"
```

Pass to `pool.acquire()`:
```python
port, cdp_url = await self.pool.acquire(
    browser_kind, reuse=reuse, profile_data_dir=profile_data_dir
)
```

Track profile status:
```python
self.profiles.set_running(profile_name or "default")
# ... run agent ...
self.profiles.set_idle(profile_name or "default")
```

**Verification:** `browser-cli run "go to example.com"` uses default profile, creates persistent data dir automatically.

---

### Task 7: Add profile commands to daemon.py

**Objective:** Handle `profile_create`, `profile_list`, `profile_delete` commands. Pass `profile` through on `run`.

**Files:**
- Modify: `browserd/daemon.py`

**Implementation:**

Add dispatch cases:
```python
elif action == "profile_create":
    name = cmd["name"]
    browser = cmd.get("browser", "chrome")
    p = await self.manager.profiles.create(name, browser)
    return {"type": "profile_created", "profile": p.model_dump()}

elif action == "profile_list":
    profiles = self.manager.profiles.list()
    return {"type": "profile_list", "profiles": [p.model_dump() for p in profiles]}

elif action == "profile_delete":
    name = cmd["name"]
    await self.manager.profiles.delete(name)
    return {"type": "profile_deleted", "name": name}
```

In `run` handler, add:
```python
profile=cmd.get("profile"),
```

**Verification:** After implementing client.py and cli.py, `browser-cli profile list` shows `default`.

---

### Task 8: Add profile methods to client.py

**Objective:** `BrowserClient` gets `profile_create/list/delete` methods. `run()` accepts `profile`.

**Files:**
- Modify: `browserd/client.py`

**Implementation:**

```python
async def profile_create(self, name: str, browser: str = "chrome") -> dict:
    return await self.send({"cmd": "profile_create", "name": name, "browser": browser})

async def profile_list(self) -> dict:
    return await self.send({"cmd": "profile_list"})

async def profile_delete(self, name: str) -> dict:
    return await self.send({"cmd": "profile_delete", "name": name})
```

Add to `run()`:
```python
async def run(self, ..., profile: str | None = None) -> dict:
    return await self.send({
        ...
        "profile": profile,
    })
```

**Verification:** `python -c "from browserd.client import BrowserClient; c = BrowserClient(); print('OK')"`

---

### Task 9: Add `profile` subcommand group and `--profile` flag to CLI

**Objective:** `browser-cli profile create|list|delete` commands. `browser-cli run --profile work "task"`.

**Files:**
- Modify: `browserd/cli.py`

**Implementation:**

Add profile subparsers:
```python
# profile create
pc = s.add_parser("profile-create", help="Create a named browser profile")
pc.add_argument("name")
pc.add_argument("--browser", "-b", choices=["chrome", "chromium"], default="chrome")

# profile list
s.add_parser("profile-list", aliases=["profiles"], help="List all profiles")

# profile delete
pd = s.add_parser("profile-delete", help="Delete a profile")
pd.add_argument("name")
```

Add to `run` subparser:
```python
sub.add_argument("--profile", "-P", default=None, help="Named profile (default: 'default')")
```

Command handlers:
```python
async def cmd_profile_create(client, args):
    resp = await client.profile_create(args.name, args.browser)
    p = resp["profile"]
    print(f"Created profile '{p['name']}' ({p['browser']}) → {p['data_dir']}")

async def cmd_profile_list(client, args):
    resp = await client.profile_list()
    for p in resp["profiles"]:
        status_icon = "🟢" if p["status"] == "idle" else "🔵"
        print(f"  {status_icon} {p['name']:20s} {p['browser']:10s} created {p['created_at'][:10]}")

async def cmd_profile_delete(client, args):
    await client.profile_delete(args.name)
    print(f"Deleted profile '{args.name}'")
```

Update `cmd_run()`:
```python
resp = await client.run(
    ...,
    profile=args.profile,  # None = "default"
)
```

**Verification:** `browser-cli profile list` → shows `default`. `browser-cli profile create work --browser chromium` → creates profile. `browser-cli profile delete work` → removes it.

---

### Task 10: End-to-end verification — persistent cookies test

**Objective:** Prove that profile-based persistence works: log in once, cookies survive next task.

**Test flow:**
```bash
# 1. Create a work profile
browser-cli profile create work --browser chromium

# 2. Task 1: navigate to a site that sets cookies
browser-cli run --profile work --wait "go to httpbin.org/cookies/set?session=abc123 then read the page"

# 3. Task 2: verify the cookie persists
browser-cli run --profile work --wait "go to httpbin.org/cookies and read what cookies are set"
# Should show session=abc123

# 4. Task 3 (no profile): anonymous — should have no cookies
browser-cli run --wait "go to httpbin.org/cookies and read what cookies are set"
# Should show empty cookies
```

**Verification:** Task 2 shows the cookie from Task 1. Task 3 shows no cookies (different profile).

---

### Task 11: Update documentation and skill files

**Objective:** Update README/AGENTS/skills to reflect profile system. Remove `--persistent` references.

**Files:**
- Modify: `~/projects/browserd/README.md`
- Modify: `~/projects/browserd/AGENTS.md`
- Modify: `browserd` skill (via skill_manage)
- Modify: `browser-cli` skill (via skill_manage)
- Update: memory entry about browserd capabilities

**Verification:** All docs reference `--profile` not `--persistent`. Quickstart shows profile workflow.

---

### Task 12: Final commit + rebuild + restart

**Commands:**
```bash
cd ~/projects/browserd
git add -A
git commit -m "feat: named browser profiles — persistent Chrome identities with own cookies/sessions

- ProfileManager: create/list/delete named profiles
- Each profile = persistent user-data-dir at ~/.browserd/profiles/{name}/
- --profile <name> on 'browser-cli run' (omit for 'default')
- Port pool unchanged — profiles get CDP ports at runtime
- Replaces --persistent flag with proper profile system
- Auto-creates 'default' profile on daemon startup
- Profile lifecycle tracked in SQLite + filesystem"

# Rebuild
source ~/.local/share/browserd/venv/bin/activate && pip install -e ~/projects/browserd

# Restart
systemctl --user daemon-reload && systemctl --user restart browserd

# Verify
browser-cli ping
browser-cli profile list
```

**Verification:** Daemon running, default profile exists, `browser-cli run "go to example.com" --wait` works.
