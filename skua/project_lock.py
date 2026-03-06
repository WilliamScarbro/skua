# SPDX-License-Identifier: BUSL-1.1
"""Project-level orchestration locks and persisted operation state."""

from __future__ import annotations

import fcntl
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from skua.config.loader import ConfigStore


class ProjectBusyError(RuntimeError):
    """Raised when a project operation lock is already held."""

    def __init__(
        self,
        project_name: str,
        operation: str = "",
        owner: str = "",
        acquired_at: str = "",
    ):
        self.project_name = project_name
        self.operation = operation
        self.owner = owner
        self.acquired_at = acquired_at
        super().__init__(project_name)


def project_operation_state(project) -> str:
    """Return persisted operation status for a project, or empty string."""
    state = getattr(project, "state", None)
    if state is None:
        return ""
    return str(getattr(state, "status", "") or "").strip()


def _project_state_details(project) -> tuple[str, str, str]:
    state = getattr(project, "state", None)
    if state is None:
        return "", "", ""
    return (
        str(getattr(state, "status", "") or "").strip(),
        str(getattr(state, "lock_owner", "") or "").strip(),
        str(getattr(state, "lock_acquired_at", "") or "").strip(),
    )


def format_project_busy_error(exc: ProjectBusyError, action: str) -> str:
    """Render a consistent lock-contention message."""
    parts = [f"Error: Project '{exc.project_name}' is busy"]
    if exc.operation:
        parts.append(f"({exc.operation})")
    if exc.owner:
        parts.append(f"by {exc.owner}")
    if exc.acquired_at:
        parts.append(f"since {exc.acquired_at}")
    return " ".join(parts) + f"; cannot {action}."


def _lock_file_path(store: ConfigStore, project_name: str) -> Path:
    return store.config_dir / "locks" / "projects" / f"{project_name}.lock"


def _owner_label() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _set_project_state(store: ConfigStore, project_name: str, operation: str, owner: str, acquired_at: str) -> None:
    project = store.load_project(project_name)
    if project is None:
        return
    if getattr(project, "state", None) is None:
        return
    project.state.status = operation
    project.state.lock_owner = owner
    project.state.lock_acquired_at = acquired_at
    store.save_resource(project)


def _clear_project_state(store: ConfigStore, project_name: str, owner: str) -> None:
    project = store.load_project(project_name)
    if project is None:
        return
    state = getattr(project, "state", None)
    if state is None:
        return
    current_owner = str(getattr(state, "lock_owner", "") or "").strip()
    if current_owner and current_owner != owner:
        return
    state.status = ""
    state.lock_owner = ""
    state.lock_acquired_at = ""
    store.save_resource(project)


def project_busy_error_if_locked(store: ConfigStore, project_name: str) -> ProjectBusyError | None:
    """Return ProjectBusyError when the project lock is currently held."""
    name = str(project_name or "").strip()
    if not name:
        return None

    lock_path = _lock_file_path(store, name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            project = store.load_project(name)
            state, owner, acquired_at = _project_state_details(project)
            return ProjectBusyError(
                project_name=name,
                operation=state,
                owner=owner,
                acquired_at=acquired_at,
            )
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_fd.close()
    return None


@contextmanager
def project_operation_lock(store: ConfigStore, project_name: str, operation: str) -> Iterator[None]:
    """Acquire a non-blocking lock for a project's mutating operation."""
    name = str(project_name or "").strip()
    op = str(operation or "").strip()
    if not name:
        raise ValueError("project name is required")
    if not op:
        raise ValueError("operation is required")

    lock_path = _lock_file_path(store, name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("a+")

    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            project = store.load_project(name)
            state, owner, acquired_at = _project_state_details(project)
            raise ProjectBusyError(
                project_name=name,
                operation=state,
                owner=owner,
                acquired_at=acquired_at,
            )

        owner = _owner_label()
        acquired_at = _now_iso()
        _set_project_state(store, name, op, owner, acquired_at)
        try:
            yield
        finally:
            _clear_project_state(store, name, owner)
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
    finally:
        lock_fd.close()
