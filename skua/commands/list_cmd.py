# SPDX-License-Identifier: BUSL-1.1
"""skua list — list projects and running containers."""

import json
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
from skua.project_lock import project_operation_state
from skua.commands.run import _credential_refresh_reason


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


def _col(value: str, width: int) -> str:
    """Left-justify value in exactly width chars, truncating with … if needed."""
    if len(value) > width:
        value = value[:width - 1] + "\u2026"
    return f"{value:<{width}}"


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


def _format_project_source(project) -> str:
    """Backward-compatible source formatter used by older tests/callers."""
    if project.directory:
        return f"LOCAL:{_shorten_home_path(project.directory)}"
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


def _credential_state(store: ConfigStore, project) -> tuple:
    """Return (state, reason, display_label) for project credential health."""
    if project is None:
        return "unknown", "", "(none)"

    label = project.credential or "(none)"
    agent = store.load_agent(project.agent)
    if agent is None:
        return "unknown", "", label

    cred = None
    if project.credential:
        cred = store.load_credential(project.credential)
    try:
        reason = _credential_refresh_reason(cred, agent)
    except Exception:
        return "unknown", "", label
    if not reason:
        return "ok", "", label

    state = "missing" if "no local credential files" in reason.lower() else "stale"
    if project.credential:
        display = f"{project.credential} !{state}"
    else:
        display = f"(default) !{state}"
    return state, reason, display


def _git_status(project, store: ConfigStore) -> str:
    """Return git status for local git projects: BEHIND/AHEAD/UNCLEAN/CURRENT."""
    if not project or getattr(project, "host", ""):
        return ""

    repo_dir = None
    if project.directory:
        candidate = Path(project.directory).expanduser()
        if candidate.is_dir() and (candidate / ".git").exists():
            repo_dir = candidate
    if repo_dir is None and getattr(project, "repo", ""):
        candidate = store.repo_dir(project.name)
        if candidate.is_dir() and (candidate / ".git").exists():
            repo_dir = candidate
    if repo_dir is None:
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


def _short_image_id(image_id: str) -> str:
    """Return a 12-char image ID without the sha256: prefix."""
    if not image_id:
        return ""
    cleaned = image_id.strip()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[7:]
    return cleaned[:12]

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


def _base_project_status(
    project,
    running: set,
    unreachable_hosts: set,
    image_name: str,
) -> str:
    """Return base project status before adapt/credential suffix markers."""
    operation = project_operation_state(project)
    if operation:
        return operation

    container_name = f"skua-{project.name}"
    host = getattr(project, "host", "") or ""
    if container_name in running:
        return "running"
    if host and host in unreachable_hosts:
        return "unreachable"
    return "built" if image_exists(image_name) else "missing"


def _agent_activity(container_name: str, host: str = "") -> str:
    """Return agent activity state by reading /tmp/skua-agent-status inside the container.

    States written by the hook scripts:
        thinking  — agent is actively executing a tool
        idle      — agent is between tool calls
        api_activity — agent is actively using the API without a subprocess
        done      — agent finished its task (Stop hook fired)

    Returns "-" when the container is unreachable or the status file is absent
    (e.g. the image pre-dates monitoring support or no agent has been started).
    Returns "?" when the file exists but cannot be parsed.
    """
    cmd = ["docker", "exec", container_name, "cat", "/tmp/skua-agent-status"]
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", host, *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "-"
    if result.returncode != 0 or not result.stdout.strip():
        return "-"
    try:
        data = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return "?"
    state = data.get("state", "?")
    if state == "thinking":
        tool = data.get("tool", "")
        if tool:
            tool = tool[:10] if len(tool) > 10 else tool
            return f"think:{tool}"
        return "thinking"
    if state == "processing":
        return "processing"
    if state == "api_activity":
        hits = data.get("hits")
        if isinstance(hits, int) and hits >= 0:
            if hits < 100:
                return "idle"
            if hits < 250:
                return "X"
            if hits < 400:
                return "XX"
            if hits < 550:
                return "XXX"
            if hits < 700:
                return "XXXX"
            if hits < 850:
                return "XXXXX"
            return "XXXXXX"
        return "?"
    return state


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

    unreachable_hosts: set = set()

    def _running_for_host(host: str) -> set:
        normalized = host or ""
        if normalized not in running_by_host:
            result = get_running_skua_containers(host=normalized)
            if result is None:
                unreachable_hosts.add(normalized)
                running_by_host[normalized] = set()
            else:
                running_by_host[normalized] = set(result)
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
            if project_id and container_id and project_id == container_id:
                running_image_values[name] = "-"
                continue
            display_id = _short_image_id(container_id)
            running_name = display_id or _container_image_name(container_name, host=host) or "-"
            running_image_values[name] = running_name
            if running_name != "-":
                needs_running_image = True

    activity_values = {}
    for name, project in projects:
        container_name = f"skua-{name}"
        host = getattr(project, "host", "") or ""
        if container_name in _running_for_host(host):
            activity_values[name] = _agent_activity(container_name, host=host)
        else:
            activity_values[name] = "-"

    columns = [("NAME", 16)]
    columns.append(("ACTIVITY", 14))
    columns.append(("STATUS", 12))
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

    print(" ".join(f"{title:<{width}}" for title, width in columns))
    print("-" * (sum(width for _, width in columns) + (len(columns) - 1)))

    pending_count = 0
    needs_adapt = False
    needs_build = False
    stale_credential_count = 0
    for name, project in projects:
        host = getattr(project, "host", "") or ""
        running = _running_for_host(host)
        pending_adapt = _has_pending_adapt_request(project)
        img_name = image_name_for_project(image_name_base, project)
        status = _base_project_status(project, running, unreachable_hosts, img_name)
        if pending_adapt:
            status += "*"
            pending_count += 1
        cred_state, _cred_reason, cred_display = _credential_state(store, project)
        if cred_state in {"missing", "stale"}:
            status += "!"
            stale_credential_count += 1
        row = [_col(name, 16)]
        activity = activity_values.get(name, "-")
        row.append(_col(activity, 14))
        row.append(_col(status, 12))

        if show_host:
            row.append(_col(_format_host(project), 14))
        row.append(_col(_format_source(project), 38))
        if show_git:
            git_status = _git_status(project, store) or "-"
            row.append(_col(git_status, 9))
        if show_image:
            suffix, flags = _image_suffix(project, store)
            if "(A)" in flags:
                needs_adapt = True
            if "(B)" in flags:
                needs_build = True
            sep = " " if suffix else ""
            row.append(_col(img_name + sep + suffix, 36))
            if needs_running_image:
                row.append(_col(running_image_values.get(name, "-"), 36))

        if show_agent:
            row.extend([_col(project.agent, 10), _col(cred_display, 20)])
        if show_security:
            env = store.load_environment(project.environment)
            network = env.network.mode if env else "?"
            row.extend([_col(project.security, 12), _col(network, 10)])
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
    if stale_credential_count:
        print(f"  ! stale/missing local credentials for {stale_credential_count} project(s)")
        print("    run 'skua run <name>' and complete agent login to refresh")
    if show_image and (needs_adapt or needs_build):
        if needs_adapt:
            print("  (A) image-request changes pending; run 'skua adapt'")
        if needs_build:
            print("  (B) image out of date; run 'skua build <name>' or 'skua adapt --build'")
    if show_image and needs_running_image:
        print("  RUNNING-IMAGE indicates a restart is needed to use the latest image")
