# SPDX-License-Identifier: BUSL-1.1
"""skua list — list projects and running containers."""

import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from skua.config import ConfigStore
from skua.docker import (
    get_running_skua_containers,
    image_exists,
    image_matches_build_context,
    image_name_for_project,
    resolve_project_image_inputs,
)
from skua.project_adapt import image_request_path, load_image_request, request_changes_project


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


def _format_host(project) -> str:
    """Return the host label: SSH:<host> for remote, LOCAL for local."""
    host = getattr(project, "host", "") or ""
    if host:
        return f"SSH:{host}"
    return "LOCAL"


def _format_source(project) -> str:
    """Return the source label: DIR:... or GITHUB:... or REPO:... ."""
    if project.directory:
        return f"DIR:{_shorten_home_path(project.directory)}"
    if project.repo:
        github = _github_source(project.repo)
        return github or f"REPO:{project.repo}"
    return "(none)"


def _has_pending_adapt_request(project) -> bool:
    """Return True when project has unapplied image-request changes."""
    directory = str(getattr(project, "directory", "") or "").strip()
    if not directory:
        return False
    project_dir = Path(directory).expanduser()
    if not project_dir.is_dir():
        return False
    req_path = image_request_path(project_dir)
    if not req_path.is_file():
        return False
    request = load_image_request(req_path)
    return request_changes_project(project, request)


def _git_status(project, store: ConfigStore) -> str:
    """Return git status for repo projects: BEHIND/AHEAD/UNCLEAN/CURRENT."""
    if not project or not getattr(project, "repo", ""):
        return ""
    if getattr(project, "host", ""):
        return ""

    repo_dir = None
    if project.directory:
        candidate = Path(project.directory).expanduser()
        if candidate.is_dir():
            repo_dir = candidate
    if repo_dir is None:
        candidate = store.repo_dir(project.name)
        if candidate.is_dir():
            repo_dir = candidate
    if repo_dir is None:
        return ""
    if not (repo_dir / ".git").exists():
        return ""

    try:
        dirty = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if dirty.stdout.strip():
            return "UNCLEAN"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "--quiet", "--prune"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

    try:
        ahead_behind = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

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


def _container_image_id(container_name: str, host: str = "") -> str:
    """Return image ID used by a container, or empty string."""
    cmd = ["docker", "inspect", "--format", "{{.Image}}", container_name]
    if host:
        cmd = ["ssh", host, *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _container_image_name(container_name: str, host: str = "") -> str:
    """Return image name recorded on the container, or empty string."""
    cmd = ["docker", "inspect", "--format", "{{.Config.Image}}", container_name]
    if host:
        cmd = ["ssh", host, *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()

def _container_image_name(container_name: str, host: str = "") -> str:
    """Return image name recorded on the container, or empty string."""
    cmd = ["docker", "inspect", "--format", "{{.Config.Image}}", container_name]
    if host:
        cmd = ["ssh", host, *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()

def _image_id(image_name: str, host: str = "") -> str:
    """Return image ID for an image name, or empty string."""
    cmd = ["docker", "image", "inspect", "--format", "{{.Id}}", image_name]
    if host:
        cmd = ["ssh", host, *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _image_suffix(project, store: ConfigStore) -> tuple:
    """Return (suffix, flags) for image status."""
    if not project:
        return "", set()

    flags = set()
    if _has_pending_adapt_request(project):
        flags.add("(A)")

    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    image_name = image_name_for_project(image_name_base, project)
    if image_exists(image_name):
        container_dir = store.get_container_dir()
        if container_dir is None:
            return "".join(flags)
        defaults = g.get("defaults", {})
        security_name = defaults.get("security", "open")
        security = store.load_security(security_name)
        agent = store.load_agent(project.agent)
        if agent is None or security is None:
            return "".join(flags)
        image_config = g.get("image", {})
        global_packages = image_config.get("extraPackages", [])
        global_commands = image_config.get("extraCommands", [])
        base_image = g.get("baseImage", "debian:bookworm-slim")
        resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
            default_base_image=base_image,
            agent=agent,
            project=project,
            global_extra_packages=global_packages,
            global_extra_commands=global_commands,
        )
        if not image_matches_build_context(
            image_name=image_name,
            container_dir=container_dir,
            security=security,
            agent=agent,
            base_image=resolved_base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
        ):
            flags.add("(B)")

    ordered = [flag for flag in ("(A)", "(B)") if flag in flags]
    return "".join(ordered), flags


def cmd_list(args):
    store = ConfigStore()
    project_names = store.list_resources("Project")
    running_by_host = {"": set(get_running_skua_containers())}
    show_agent = bool(getattr(args, "agent", False))
    show_security = bool(getattr(args, "security", False))
    show_git = bool(getattr(args, "git", False))
    local_only = bool(getattr(args, "local", False))
    show_image = bool(getattr(args, "image", False))
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")

    if not project_names:
        print("No projects configured. Add one with: skua add <name> --dir <path> or --repo <url>")
        return

    projects = [(name, store.resolve_project(name)) for name in project_names]
    projects = [(name, p) for name, p in projects if p is not None]
    if local_only:
        projects = [(name, p) for name, p in projects if not getattr(p, "host", "")]

    def _running_for_host(host: str) -> set:
        normalized = host or ""
        if normalized not in running_by_host:
            running_by_host[normalized] = set(get_running_skua_containers(host=normalized))
        return running_by_host[normalized]

    show_host = any(getattr(p, "host", "") for _, p in projects)
    needs_running_image = False
    running_image_values = {}
    if show_image:
        for name, project in projects:
            container_name = f"skua-{name}"
            host = getattr(project, "host", "") or ""
            if container_name not in _running_for_host(host):
                running_image_values[name] = "-"
                continue
            img_name = image_name_for_project(image_name_base, project)
            project_id = _image_id(img_name, host=host)
            container_id = _container_image_id(container_name, host=host)
            container_name_value = _container_image_name(container_name, host=host)
            if container_name_value == img_name:
                if not project_id or not container_id or project_id == container_id:
                    running_image_values[name] = "-"
                    continue
            running_name = container_name_value or container_id or "-"
            running_image_values[name] = running_name
            if running_name != "-":
                needs_running_image = True

    columns = [("NAME", 16)]
    if show_host:
        columns.append(("HOST", 14))
    columns.append(("SOURCE", 38))
    if show_git:
        columns.append(("GIT", 9))
    if show_image:
        columns.append(("IMAGE", 36))
        if needs_running_image:
            columns.append(("RUNNING-IMAGE", 36))
    if show_agent:
        columns.extend([("AGENT", 10), ("CREDENTIAL", 20)])
    if show_security:
        columns.extend([("SECURITY", 12), ("NETWORK", 10)])
    columns.append(("STATUS", 10))

    print(" ".join(f"{title:<{width}}" for title, width in columns))
    print("-" * (sum(width for _, width in columns) + (len(columns) - 1)))

    pending_count = 0
    needs_adapt = False
    needs_build = False
    for name, project in projects:
        container_name = f"skua-{name}"
        host = getattr(project, "host", "") or ""
        running = _running_for_host(host)
        pending_adapt = _has_pending_adapt_request(project)
        img_name = image_name_for_project(image_name_base, project)
        if container_name in running:
            status = "running"
        else:
            status = "built" if image_exists(img_name) else "missing"
        if pending_adapt:
            status += "*"
            pending_count += 1
        row = [f"{name:<16}"]

        if show_host:
            row.append(f"{_format_host(project):<14}")
        row.append(f"{_format_source(project):<38}")
        if show_git:
            git_status = _git_status(project, store) or "-"
            row.append(f"{git_status:<9}")
        if show_image:
            suffix, flags = _image_suffix(project, store)
            if "(A)" in flags:
                needs_adapt = True
            if "(B)" in flags:
                needs_build = True
            sep = " " if suffix else ""
            row.append(f"{(img_name + sep + suffix):<36}")
            if needs_running_image:
                row.append(f"{running_image_values.get(name, '-'):<36}")

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
    running_count = 0
    for name, project in projects:
        host = getattr(project, "host", "") or ""
        if f"skua-{name}" in _running_for_host(host):
            running_count += 1
    print(f"{len(project_names)} project(s), {running_count} running, {pending_count} pending adapt")
    if pending_count:
        print("  * pending image-request changes")
    if show_image and (needs_adapt or needs_build):
        if needs_adapt:
            print("  (A) image-request changes pending; run 'skua adapt'")
        if needs_build:
            print("  (B) image out of date; run 'skua build' or 'skua adapt --build'")
    if show_image and needs_running_image:
        print("  RUNNING-IMAGE indicates a restart is needed to use the latest image")
