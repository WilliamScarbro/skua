# SPDX-License-Identifier: BUSL-1.1
"""skua stop — stop a running project container."""

import subprocess
import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.docker import get_running_skua_containers
from skua.utils import confirm


def _repo_dir(project, store: ConfigStore) -> Path:
    if project.directory:
        candidate = Path(project.directory).expanduser()
        if candidate.is_dir():
            return candidate
    candidate = store.repo_dir(project.name)
    if candidate.is_dir():
        return candidate
    return Path()


def _git_status(repo_dir: Path) -> str:
    if not repo_dir or not repo_dir.is_dir() or not (repo_dir / ".git").exists():
        return ""

    try:
        dirty = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "UNKNOWN"

    if dirty.stdout.strip():
        return "UNCLEAN"

    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "--quiet", "--prune"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "UNKNOWN"

    try:
        ahead_behind = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "UNKNOWN"

    if ahead_behind.returncode != 0:
        return "CURRENT"

    parts = ahead_behind.stdout.strip().split()
    if len(parts) >= 2:
        behind = int(parts[0])
        ahead = int(parts[1])
        if behind > 0 and ahead > 0:
            return "DIVERGED"
        if behind > 0:
            return "BEHIND"
        if ahead > 0:
            return "AHEAD"
    return "CURRENT"


def _should_continue_for_git(project, store: ConfigStore) -> bool:
    if not project.repo:
        return True
    if project.host:
        print("Warning: Cannot check git status for remote projects.")
        return confirm("Stop container anyway?", default=False)
    repo_dir = _repo_dir(project, store)
    status = _git_status(repo_dir)
    if status in ("", "CURRENT"):
        return True
    print(f"Warning: git status is {status} for {repo_dir}")
    return confirm("Stop container anyway?", default=False)


def cmd_stop(args) -> bool:
    store = ConfigStore()
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        print("Error: Provide a project name.")
        sys.exit(1)

    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        sys.exit(1)

    container_name = f"skua-{name}"
    host = getattr(project, "host", "") or ""
    running = set(get_running_skua_containers(host=host))
    if container_name not in running:
        print(f"Container '{container_name}' is not running.")
        return True

    if not _should_continue_for_git(project, store):
        print("Stop cancelled.")
        return False

    cmd = ["docker", "stop", container_name]
    if host:
        cmd = ["ssh", host, *cmd]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"Error: Failed to stop container '{container_name}'.")
        sys.exit(1)
    print(f"Stopped '{container_name}'.")
    return True
