# SPDX-License-Identifier: BUSL-1.1
"""skua list — list projects and running containers."""

from pathlib import Path
from urllib.parse import urlsplit

from skua.config import ConfigStore
from skua.docker import get_running_skua_containers


def _shorten_home_path(path: str) -> str:
    """Shorten an absolute path under $HOME to ~/... for display."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except (ValueError, TypeError):
        return path


def _github_source(repo_url: str) -> str:
    """Return GITHUB:/owner/repo for GitHub URLs, or empty string if not GitHub."""
    if not repo_url:
        return ""

    path = ""
    if repo_url.startswith("git@github.com:"):
        path = repo_url.split(":", 1)[1]
    else:
        parsed = urlsplit(repo_url)
        if parsed.hostname == "github.com":
            path = parsed.path.lstrip("/")

    if not path:
        return ""

    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"GITHUB:/{parts[0]}/{parts[1]}"
    return ""


def _format_project_source(project) -> str:
    """Render a clear source label for list output."""
    if project.directory:
        return f"LOCAL:{_shorten_home_path(project.directory)}"
    if project.repo:
        github = _github_source(project.repo)
        return github or f"REPO:{project.repo}"
    return "(none)"


def cmd_list(args):
    store = ConfigStore()
    project_names = store.list_resources("Project")
    running = get_running_skua_containers()

    if not project_names:
        print("No projects configured. Add one with: skua add <name> --dir <path> or --repo <url>")
        return

    # Header
    print(f"{'NAME':<16} {'SOURCE':<42} {'AGENT':<10} {'SECURITY':<12} {'NETWORK':<10} {'STATUS':<10}")
    print("-" * 106)

    for name in project_names:
        project = store.resolve_project(name)
        if project is None:
            continue
        container_name = f"skua-{name}"
        status = "running" if container_name in running else "stopped"
        source = _format_project_source(project)

        env = store.load_environment(project.environment)
        network = env.network.mode if env else "?"

        print(f"{name:<16} {source:<42} {project.agent:<10} {project.security:<12} {network:<10} {status:<10}")

    print()
    running_count = sum(1 for n in project_names if f"skua-{n}" in running)
    print(f"{len(project_names)} project(s), {running_count} running")
