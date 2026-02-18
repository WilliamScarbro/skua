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
    host = getattr(project, "host", "") or ""
    if host:
        github = _github_source(project.repo) if project.repo else ""
        repo_label = github or (f"REPO:{project.repo}" if project.repo else "(none)")
        return f"SSH:{host} {repo_label}"
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
    show_agent = bool(getattr(args, "agent", False))
    show_security = bool(getattr(args, "security", False))

    if not project_names:
        print("No projects configured. Add one with: skua add <name> --dir <path> or --repo <url>")
        return

    columns = [("NAME", 16), ("SOURCE", 38)]
    if show_agent:
        columns.extend([("AGENT", 10), ("CREDENTIAL", 20)])
    if show_security:
        columns.extend([("SECURITY", 12), ("NETWORK", 10)])
    columns.append(("STATUS", 10))

    print(" ".join(f"{title:<{width}}" for title, width in columns))
    print("-" * (sum(width for _, width in columns) + (len(columns) - 1)))

    for name in project_names:
        project = store.resolve_project(name)
        if project is None:
            continue
        container_name = f"skua-{name}"
        status = "running" if container_name in running else "stopped"
        source = _format_project_source(project)
        row = [f"{name:<16}", f"{source:<38}"]

        if show_agent:
            credential = project.credential or "(none)"
            row.extend([f"{project.agent:<10}", f"{credential:<20}"])
        if show_security:
            env = store.load_environment(project.environment)
            network = env.network.mode if env else "?"
            row.extend([f"{project.security:<12}", f"{network:<10}"])

        row.append(f"{status:<10}")
        print(" ".join(row))

    print()
    running_count = sum(1 for n in project_names if f"skua-{n}" in running)
    print(f"{len(project_names)} project(s), {running_count} running")
