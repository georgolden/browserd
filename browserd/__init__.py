"""browserd — Browser automation daemon with parallel Chrome instances.

Netflix-style lazy loading: heavy imports deferred until attribute access.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from browserd.models import (
        DaemonConfig, TaskCreate, TaskRecord, TaskResult, TaskSummary,
        SessionRecord, TabRecord, TabStatus, TaskLogEntry,
        SocketRequest, SocketResponse, TaskStatus, BrowserKind, LogLevel,
    )
    from browserd.db import TaskDB
    from browserd.portpool import PortPool
    from browserd.tasks import TaskManager
    from browserd.daemon import DaemonServer
    from browserd.client import BrowserClient

__all__ = [
    # models
    "DaemonConfig", "TaskCreate", "TaskRecord", "TaskResult", "TaskSummary",
    "SessionRecord", "TabRecord", "TabStatus", "TaskStatus", "BrowserKind",
    "TaskLogEntry", "LogLevel", "SocketRequest", "SocketResponse",
    # core
    "TaskDB", "PortPool", "TaskManager",
    # io
    "DaemonServer", "BrowserClient",
]
