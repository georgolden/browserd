"""Profile manager — named, persistent Chrome identities with own cookies/sessions."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from browserd.models import DaemonConfig, ProfileRecord

if TYPE_CHECKING:
    from browserd.db import TaskDB


class ProfileManager:
    """Manages named browser profiles backed by filesystem + SQLite.

    Each profile is a persistent Chrome user-data-dir. Cookies, logins,
    and sessions survive across browser restarts. Profiles get CDP ports
    at runtime via the existing PortPool — they don't own ports.

    Usage:
        pm = ProfileManager(db, config)
        await pm.ensure_default()                    # auto-create 'default'
        pm.resolve("work")                           # → data_dir path
        pm.resolve(None)                             # → 'default' data_dir
        await pm.create("personal", "chromium")      # new profile
        pm.list()                                    # all profiles
        await pm.delete("old")                       # removes data dir + DB row
    """

    DEFAULT_PROFILE = "default"

    def __init__(self, db: TaskDB, config: DaemonConfig):
        self.db = db
        self.config = config

    def _profile_dir(self, name: str) -> Path:
        return self.config.profiles_dir / name

    async def create(self, name: str, browser: str = "chrome") -> ProfileRecord:
        """Create a new named profile. Fails if it already exists."""
        if self.db.get_profile(name):
            raise ValueError(f"Profile '{name}' already exists")
        data_dir = str(self._profile_dir(name))
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db.create_profile(name, browser, data_dir)
        return ProfileRecord(
            name=name, browser=browser, data_dir=data_dir, status="idle",
        )

    async def ensure_default(self) -> ProfileRecord:
        """Auto-create the 'default' profile if it doesn't exist yet."""
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
