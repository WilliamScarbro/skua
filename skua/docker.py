# SPDX-License-Identifier: BUSL-1.1
"""Docker operations — build images, run containers, query state."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from skua.config.resources import Environment, SecurityProfile, AgentConfig, Project, ssh_private_keys


def is_container_running(name: str) -> bool:
    """Check if a Docker container with the given name is running."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name=^{name}$"],
            capture_output=True, text=True
        )
        return bool(result.stdout.strip())
    except FileNotFoundError:
        return False


def get_running_skua_containers(host: str = "") -> list | None:
    """Return list of running skua container names for local or remote host.

    Returns None when the host is unreachable (SSH failure or timeout).
    Returns an empty list when connected but no skua containers are running.
    """
    cmd = ["docker", "ps", "--filter", "name=^skua-", "--format", "{{.Names}}"]
    if host:
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            host,
            *cmd,
        ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None if host else []

    if result.returncode != 0:
        return None if host else []
    if result.stdout.strip():
        return result.stdout.strip().split("\n")
    return []


def image_name_for_agent(base_image_name: str, agent_name: str) -> str:
    """Return an agent-specific image name, preserving an optional tag."""
    base = (base_image_name or "skua-base").strip().lower()
    agent = (agent_name or "claude").strip().lower()

    if not base:
        base = "skua-base"
    if not agent:
        agent = "claude"

    # Split tag only when ':' appears after the last '/'
    slash_idx = base.rfind("/")
    colon_idx = base.rfind(":")
    if colon_idx > slash_idx:
        repo = base[:colon_idx]
        tag = base[colon_idx:]
    else:
        repo = base
        tag = ""

    suffix = f"-{agent}"
    if repo.endswith(suffix):
        return base
    return f"{repo}{suffix}{tag}"


def _split_image_ref_tag(image_ref: str) -> tuple:
    """Split an image reference into (repository, optional_tag_with_colon)."""
    ref = (image_ref or "").strip()
    if not ref:
        return "skua-base", ""
    slash_idx = ref.rfind("/")
    colon_idx = ref.rfind(":")
    if colon_idx > slash_idx:
        return ref[:colon_idx], ref[colon_idx:]
    return ref, ""


def image_exists(name: str) -> bool:
    """Check if a Docker image exists locally."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", name],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def project_has_image_customizations(project: Project) -> bool:
    """Return True if project image config overrides agent/global defaults."""
    if not project or not getattr(project, "image", None):
        return False
    img = project.image
    return bool(
        str(getattr(img, "base_image", "") or "").strip()
        or str(getattr(img, "from_image", "") or "").strip()
        or list(getattr(img, "extra_packages", []) or [])
        or list(getattr(img, "extra_commands", []) or [])
    )


def project_uses_agent_base_layer(project: Project) -> bool:
    """Return True when a project should layer on top of the shared agent image."""
    if not project_has_image_customizations(project):
        return False
    img = getattr(project, "image", None)
    if not img:
        return False
    return not (
        str(getattr(img, "base_image", "") or "").strip()
        or str(getattr(img, "from_image", "") or "").strip()
    )


def image_name_for_project(base_image_name: str, project: Project) -> str:
    """Return the image tag to use for a specific project."""
    agent_name = (project.agent or "claude") if project else "claude"
    agent_image = image_name_for_agent(base_image_name, agent_name)
    if not project_has_image_customizations(project):
        return agent_image

    repo, tag = _split_image_ref_tag(agent_image)
    project_part = _sanitize_mount_name(project.name or "project").lower()
    version = int(getattr(project.image, "version", 0) or 0)
    if version < 1:
        version = 1
    return f"{repo}-{project_part}-v{version}{tag}"


def effective_project_image(
    image_name_base: str,
    project: Project,
    global_extra_packages: list = None,
    global_extra_commands: list = None,
) -> str:
    """Return the effective Docker image name for a project.

    When from_image is set and no extra packages/commands are applied (from
    project or global config), the from_image is used directly — the prebuilt
    default image is the final image and no project-specific build is needed.
    In all other cases this delegates to image_name_for_project.
    """
    from_image = ""
    if project and getattr(project, "image", None):
        from_image = str(getattr(project.image, "from_image", "") or "").strip()

    if from_image:
        project_packages = list(getattr(project.image, "extra_packages", []) or [])
        project_commands = list(getattr(project.image, "extra_commands", []) or [])
        merged_packages = _merge_unique(list(global_extra_packages or []) + project_packages)
        merged_commands = _merge_unique(list(global_extra_commands or []) + project_commands)
        if not merged_packages and not merged_commands:
            return from_image

    return image_name_for_project(image_name_base, project)


def _merge_unique(items: list) -> list:
    """Return unique non-empty strings in order."""
    out = []
    seen = set()
    for item in items or []:
        value = str(item).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def resolve_project_image_inputs(
    default_base_image: str,
    agent: AgentConfig,
    project: Project,
    global_extra_packages: list = None,
    global_extra_commands: list = None,
    image_name_base: str = "skua-base",
) -> tuple:
    """Resolve base image and build extras for a project image build."""
    agent_default_base = base_image_for_agent(default_base_image, agent)
    project_base_image = ""
    project_from_image = ""
    project_packages = []
    project_commands = []

    if project and getattr(project, "image", None):
        project_base_image = str(getattr(project.image, "base_image", "") or "").strip()
        project_from_image = str(getattr(project.image, "from_image", "") or "").strip()
        project_packages = list(getattr(project.image, "extra_packages", []) or [])
        project_commands = list(getattr(project.image, "extra_commands", []) or [])

    resolved_base_image = project_from_image or project_base_image or agent_default_base
    if project_uses_agent_base_layer(project):
        resolved_base_image = image_name_for_agent(image_name_base, agent.name if agent else "")
    extra_packages = _merge_unique(list(global_extra_packages or []) + project_packages)
    extra_commands = _merge_unique(list(global_extra_commands or []) + project_commands)
    return resolved_base_image, extra_packages, extra_commands


def _sanitize_mount_name(name: str) -> str:
    """Return a safe single path component for use inside the container."""
    cleaned = "".join(
        c if c.isalnum() or c in "._-" else "-"
        for c in (name or "").strip()
    ).strip(".-")
    if cleaned in ("", ".", ".."):
        return "project"
    return cleaned


def _repo_name_from_url(repo_url: str) -> str:
    """Extract repository name from common git URL formats."""
    repo = (repo_url or "").strip()
    if not repo:
        return ""

    path = repo
    if "://" in repo:
        path = urlparse(repo).path or ""
    elif "@" in repo and ":" in repo and repo.index(":") > repo.index("@"):
        # SCP-like syntax: git@github.com:owner/repo.git
        path = repo.split(":", 1)[1]

    repo_name = Path(path.rstrip("/")).name
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    return _sanitize_mount_name(repo_name)


def _project_mount_path(project: Project) -> str:
    """Return the in-container project mount path."""
    mount_name = ""
    if project.repo:
        mount_name = _repo_name_from_url(project.repo)
    elif project.directory:
        mount_name = _sanitize_mount_name(Path(project.directory).name)
    elif project.name:
        mount_name = _sanitize_mount_name(project.name)
    if not mount_name:
        mount_name = "project"
    return f"/home/dev/{mount_name}"


def _project_sources(project: Project) -> list:
    """Return explicit project sources, or a single implicit primary source."""
    sources = list(getattr(project, "sources", []) or [])
    if sources:
        return sources
    return [project]


def _source_mount_path(source, index: int = 0) -> str:
    """Return the in-container mount path for a source."""
    explicit = str(getattr(source, "mount_path", "") or "").strip()
    if explicit:
        return explicit
    if getattr(source, "repo", ""):
        mount_name = _repo_name_from_url(source.repo)
    elif getattr(source, "directory", ""):
        mount_name = _sanitize_mount_name(Path(source.directory).name)
    elif getattr(source, "name", ""):
        mount_name = _sanitize_mount_name(source.name)
    elif getattr(source, "project", ""):
        mount_name = _sanitize_mount_name(source.project)
    else:
        mount_name = f"project-{index + 1}"
    return f"/home/dev/{mount_name}"


# ── Dockerfile generation ────────────────────────────────────────────────

# Core packages always included (required for container operation)
CORE_PACKAGES = [
    "ca-certificates", "curl", "wget", "git", "openssh-client", "sudo", "vim",
]

# Default packages included unless explicitly removed
DEFAULT_PACKAGES = [
    "python3", "python3-pip",
    "procps", "coreutils", "findutils", "grep", "gawk", "sed",
    "less", "tree", "file", "htop", "jq", "tmux",
    "zip", "unzip", "tar", "gzip", "bzip2", "xz-utils",
    "diffutils", "patch", "man-db", "manpages",
    "net-tools", "iputils-ping", "dnsutils", "tcpdump", "libcap2-bin",
    "ripgrep",
]

# Agent install commands keyed by agent name (fallback when no AgentConfig found)
DEFAULT_AGENT_INSTALLS = {
    "claude": ["curl -fsSL https://claude.ai/install.sh | bash"],
    "codex": ["npm install -g --prefix /home/dev/.local @openai/codex"],
}

# Agent-required packages keyed by agent name.
DEFAULT_AGENT_REQUIRED_PACKAGES = {
    "codex": ["nodejs", "npm"],
}

LEGACY_CODEX_UNIVERSAL_IMAGE = "ghcr.io/openai/codex-universal:latest"
BUILD_CONTEXT_HASH_LABEL = "skua.build-context-hash"
MANAGED_IMAGE_LABEL = "skua.managed"
AGENT_VERSION_LABEL_PREFIX = "skua.agent.version."
AGENT_VERSION_NPM_PACKAGES = {
    "codex": "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
}
_AGENT_VERSION_CACHE = {}
AGENT_VERSION_CACHE_TTL_SECONDS = 300


def _agent_version_cache_path() -> Path:
    """Return the shared on-disk cache path for latest agent versions."""
    return Path.home() / ".config" / "skua" / "cache" / "agent-versions.json"


def _read_agent_version_disk_cache() -> dict:
    """Load the shared agent-version cache from disk."""
    path = _agent_version_cache_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cached_agent_client_version(agent_name: str, max_age_seconds: int | None = AGENT_VERSION_CACHE_TTL_SECONDS) -> str:
    """Return a cached agent version from disk when present and sufficiently fresh."""
    raw = _read_agent_version_disk_cache().get(str(agent_name or "").strip().lower())
    if not isinstance(raw, dict):
        return ""

    version = str(raw.get("version") or "").strip()
    checked_at = raw.get("checked_at")
    if not version:
        return ""
    if max_age_seconds is None:
        return version
    if not isinstance(checked_at, (int, float)):
        return ""
    if (time.time() - float(checked_at)) > max_age_seconds:
        return ""
    return version


def _write_agent_version_disk_cache(agent_name: str, version: str) -> None:
    """Persist a resolved latest agent version for reuse across skua processes."""
    normalized = str(agent_name or "").strip().lower()
    value = str(version or "").strip()
    if not normalized or not value:
        return

    path = _agent_version_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_agent_version_disk_cache()
        data[normalized] = {"version": value, "checked_at": time.time()}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError:
        return


def _normalize_agent_install_commands(agent_name: str, commands: list) -> list:
    """Normalize legacy install commands for compatibility."""
    normalized = []
    for cmd in commands or []:
        c = (cmd or "").strip()
        if agent_name == "codex":
            if c == "npm install -g @openai/codex":
                c = "npm install -g --prefix /home/dev/.local @openai/codex"
        if c:
            normalized.append(c)
    return normalized


def _render_run_instruction(command: str) -> str:
    """Render a shell command as a valid Docker RUN instruction.

    Multi-line commands are emitted as a single shell block so embedded lines
    do not get parsed as Dockerfile instructions.
    """
    lines = [line.strip() for line in str(command or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return f"RUN {lines[0]}"

    rendered = "RUN set -eux; \\\n"
    rendered += " \\\n".join(f"    {line};" for line in lines[:-1])
    rendered += f" \\\n    {lines[-1]}"
    return rendered


def agent_install_uses_floating_version(agent: AgentConfig = None) -> bool:
    """Return True when the agent install commands resolve latest at build time.

    Floating installs are not captured by the generated Dockerfile hash alone, so
    callers may want to force a rebuild without using Docker's layer cache.
    """
    if not agent:
        return False

    commands = []
    if agent.install and agent.install.commands:
        commands = _normalize_agent_install_commands(agent.name, agent.install.commands)
    else:
        commands = DEFAULT_AGENT_INSTALLS.get(agent.name, [])

    for raw_cmd in commands:
        cmd = str(raw_cmd or "").strip()
        if not cmd:
            continue
        if "curl -fsSL" in cmd and "| bash" in cmd:
            return True
        if "@openai/codex@" in cmd:
            continue
        if "@openai/codex" in cmd and "npm install" in cmd:
            return True
    return False


def _agent_version_label_key(agent_name: str) -> str:
    """Return a stable image label key for a given agent."""
    safe = re.sub(r"[^a-z0-9_.-]+", "-", str(agent_name or "").strip().lower())
    if not safe:
        safe = "agent"
    return f"{AGENT_VERSION_LABEL_PREFIX}{safe}"


def _latest_npm_package_version(package_name: str) -> str:
    """Return latest published npm package version, or empty string on failure."""
    pkg = str(package_name or "").strip()
    if not pkg:
        return ""
    try:
        result = subprocess.run(
            ["npm", "view", pkg, "version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def latest_agent_client_version(agent_name: str) -> str:
    """Return latest upstream client version for a known agent, else empty."""
    normalized = str(agent_name or "").strip().lower()
    if not normalized:
        return ""
    if normalized in _AGENT_VERSION_CACHE:
        return _AGENT_VERSION_CACHE[normalized]
    cached_version = _cached_agent_client_version(normalized)
    if cached_version:
        _AGENT_VERSION_CACHE[normalized] = cached_version
        return cached_version
    package_name = AGENT_VERSION_NPM_PACKAGES.get(normalized, "")
    version = _latest_npm_package_version(package_name)
    if version:
        _write_agent_version_disk_cache(normalized, version)
    else:
        # Reuse the last known version when the live lookup is temporarily unavailable.
        version = _cached_agent_client_version(normalized, max_age_seconds=None)
    _AGENT_VERSION_CACHE[normalized] = version
    return version


def _build_agent_version_labels(agent: AgentConfig = None, agents: list = None) -> dict:
    """Return image labels that record resolved upstream client versions."""
    labels = {}
    names = []
    if agent and getattr(agent, "name", ""):
        names.append(str(agent.name).strip().lower())
    for item in agents or []:
        if item and getattr(item, "name", ""):
            names.append(str(item.name).strip().lower())

    seen = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        version = latest_agent_client_version(name)
        if version:
            labels[_agent_version_label_key(name)] = version
    return labels


def floating_agent_update_available(image_name: str, agent: AgentConfig = None) -> tuple:
    """Return (needs_refresh, reason) when a floating-installed agent has an update."""
    if not agent_install_uses_floating_version(agent):
        return False, ""

    agent_name = str(getattr(agent, "name", "") or "").strip().lower()
    if not agent_name:
        return False, ""

    latest = latest_agent_client_version(agent_name)
    if not latest:
        return False, ""

    label_key = _agent_version_label_key(agent_name)
    current = _image_label(image_name, label_key)
    if not current:
        return True, (
            f"latest {agent_name} client is {latest}, but image label '{label_key}' is missing"
        )
    if current != latest:
        return True, f"{agent_name} client update available ({current} -> {latest})"
    return False, ""


def image_rebuild_needed(
    image_name: str,
    container_dir: Path | None,
    security: SecurityProfile = None,
    agent: AgentConfig = None,
    base_image: str = "debian:bookworm-slim",
    extra_packages: list = None,
    extra_commands: list = None,
    layer_on_base: bool = False,
) -> tuple:
    """Return (needs_rebuild, force_refresh, reason) for an image preflight check."""
    if not image_exists(image_name):
        return True, False, "image is missing"

    if agent_install_uses_floating_version(agent):
        refresh_needed, refresh_reason = floating_agent_update_available(image_name, agent)
        if refresh_needed:
            return True, True, refresh_reason or "floating client update available"

    if container_dir is None:
        return False, False, ""

    if not image_matches_build_context(
        image_name=image_name,
        container_dir=container_dir,
        security=security,
        agent=agent,
        base_image=base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        layer_on_base=layer_on_base,
    ):
        return True, False, "build context changed"

    return False, False, ""


def ensure_agent_base_image(
    container_dir: Path,
    image_name_base: str,
    default_base_image: str,
    security: SecurityProfile = None,
    agent: AgentConfig = None,
    global_extra_packages: list = None,
    global_extra_commands: list = None,
    quiet: bool = False,
    verbose: bool = False,
) -> tuple:
    """Ensure the shared per-agent base image exists and is current."""
    image_name = image_name_for_agent(image_name_base, agent.name if agent else "")
    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=default_base_image,
        agent=agent,
        project=None,
        global_extra_packages=global_extra_packages,
        global_extra_commands=global_extra_commands,
        image_name_base=image_name_base,
    )
    needs_rebuild, force_refresh, reason = image_rebuild_needed(
        image_name=image_name,
        container_dir=container_dir,
        security=security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
    )
    if not needs_rebuild:
        return image_name, True, False, ""

    success, output = build_image(
        container_dir=container_dir,
        image_name=image_name,
        security=security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        quiet=quiet,
        verbose=verbose,
        pull=force_refresh,
        no_cache=force_refresh,
    )
    return image_name, success, True, reason or output


def base_image_for_agent(default_base_image: str, agent: AgentConfig = None) -> str:
    """Resolve the base image for a specific agent."""
    if not agent:
        return default_base_image

    if agent and agent.install and agent.install.base_image:
        configured = agent.install.base_image.strip()
        # Backward-compat: old codex preset pointed to codex-universal.
        # If that legacy preset has no custom install data, use lightweight defaults.
        if (
            agent.name == "codex"
            and configured == LEGACY_CODEX_UNIVERSAL_IMAGE
            and not agent.install.commands
            and not agent.install.required_packages
        ):
            return default_base_image
        return configured
    return default_base_image


def generate_dockerfile(
    agent: AgentConfig = None,
    agents: list = None,
    security: SecurityProfile = None,
    base_image: str = "debian:bookworm-slim",
    extra_packages: list = None,
    extra_commands: list = None,
) -> str:
    """Generate a Dockerfile from configuration.

    Args:
        agent: Legacy single AgentConfig (used when agents is not provided).
        agents: AgentConfig list to install into the image.
        security: SecurityProfile. If agent.sudo is False, sudo is removed.
        base_image: Base Docker image.
        extra_packages: Additional apt packages to install.
        extra_commands: Additional RUN commands to execute.
    """
    packages = list(CORE_PACKAGES)

    # If security says no sudo, we still need sudo during build but remove it after
    remove_sudo = security and not security.agent.sudo

    selected_agents = [a for a in (agents or []) if a]
    if not selected_agents and agent:
        selected_agents = [agent]

    packages.extend(DEFAULT_PACKAGES)
    for a in selected_agents:
        packages.extend(DEFAULT_AGENT_REQUIRED_PACKAGES.get(a.name, []))
        if a.install.required_packages:
            packages.extend(a.install.required_packages)
    if extra_packages:
        packages.extend(extra_packages)

    # Deduplicate while preserving order
    seen = set()
    unique_packages = []
    for p in packages:
        if p not in seen:
            seen.add(p)
            unique_packages.append(p)

    pkg_line = " \\\n    ".join(unique_packages)

    # Agent install commands
    install_cmds = []
    if selected_agents:
        for a in selected_agents:
            if a.install.commands:
                install_cmds.extend(_normalize_agent_install_commands(a.name, a.install.commands))
            else:
                install_cmds.extend(DEFAULT_AGENT_INSTALLS.get(a.name, []))
    else:
        default_name = (agent.name if agent and agent.name else "claude")
        install_cmds = DEFAULT_AGENT_INSTALLS.get(default_name, DEFAULT_AGENT_INSTALLS.get("claude", []))

    # Deduplicate commands while preserving order
    seen_cmds = set()
    unique_install_cmds = []
    for cmd in install_cmds:
        if cmd not in seen_cmds:
            seen_cmds.add(cmd)
            unique_install_cmds.append(cmd)
    install_cmds = unique_install_cmds

    install_lines = "\n".join(
        rendered for cmd in install_cmds
        if (rendered := _render_run_instruction(cmd))
    )

    # Extra commands
    extra_lines = ""
    if extra_commands:
        extra_lines = "\n" + "\n".join(
            rendered for cmd in extra_commands
            if (rendered := _render_run_instruction(cmd))
        )

    # Sudo removal
    sudo_removal = ""
    if remove_sudo:
        sudo_removal = """
# ── Remove sudo (security: agent.sudo=false) ─────────────────────────
RUN sudo deluser dev sudo 2>/dev/null || true && \\
    sudo sed -i '/^dev /d' /etc/sudoers && \\
    sudo rm -f /etc/sudoers.d/* && \\
    sudo chmod 0440 /etc/sudoers
"""

    dockerfile = f"""FROM {base_image}

ARG DEBIAN_FRONTEND=noninteractive
ARG USER_UID=1000
ARG USER_GID=1000

# ── Core system packages ─────────────────────────────────────────────
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \\
    {pkg_line} \\
    && rm -rf /var/lib/apt/lists/*

# ── Create non-root user (match host UID/GID when possible) ──────────
RUN set -eux; \\
    if ! getent group "$USER_GID" >/dev/null; then groupadd --gid "$USER_GID" dev; fi; \\
    if getent passwd "$USER_UID" >/dev/null; then \\
        existing_user="$(getent passwd "$USER_UID" | cut -d: -f1)"; \\
        if [ "$existing_user" != "dev" ]; then usermod -l dev "$existing_user"; fi; \\
        usermod -d /home/dev -m dev; \\
        usermod -g "$USER_GID" dev; \\
    else \\
        useradd --uid "$USER_UID" --gid "$USER_GID" -m -s /bin/bash dev; \\
    fi; \\
    echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# ── Allow tcpdump for non-root (monitoring) ──────────────────────────
RUN set -eux; \
    tcpdump_bin="$(command -v tcpdump || true)"; \
    if [ -n "$tcpdump_bin" ]; then \
        setcap cap_net_raw,cap_net_admin=eip "$tcpdump_bin" || true; \
    fi

# ── Install agent ────────────────────────────────────────────────────
ENV NPM_CONFIG_PREFIX="/home/dev/.local"
USER dev
{install_lines}
WORKDIR /home/dev
{extra_lines}

# ── Non-sensitive config defaults (copied into volume on first run) ──
COPY --chown=dev:dev claude-settings/ /home/dev/.claude-defaults/

# ── Placeholder directories ─────────────────────────────────────────
RUN mkdir -p /home/dev/.ssh /home/dev/project /home/dev/.claude /home/dev/.entrypoint.d/hooks

# ── Environment ──────────────────────────────────────────────────────
ENV EDITOR=vim
ENV PATH="/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
{sudo_removal}
# ── Entrypoint ───────────────────────────────────────────────────────
COPY --chown=dev:dev entrypoint.sh /home/dev/.entrypoint.d/entrypoint.sh
RUN chmod +x /home/dev/.entrypoint.d/entrypoint.sh
COPY --chown=dev:dev check_monitoring.sh /home/dev/.entrypoint.d/check_monitoring.sh
RUN chmod +x /home/dev/.entrypoint.d/check_monitoring.sh
COPY --chown=dev:dev tmux-attach-banner.sh /home/dev/.entrypoint.d/tmux-attach-banner.sh
RUN chmod +x /home/dev/.entrypoint.d/tmux-attach-banner.sh

ENTRYPOINT ["/home/dev/.entrypoint.d/entrypoint.sh"]

# ── Agent monitoring hooks ────────────────────────────────────────────
COPY --chown=dev:dev hooks/ /home/dev/.entrypoint.d/hooks/
RUN chmod +x /home/dev/.entrypoint.d/hooks/*.sh
COPY --chown=dev:dev .entrypoint.d/ /home/dev/.entrypoint.d/
RUN chmod +x /home/dev/.entrypoint.d/*.sh 2>/dev/null || true
"""
    return dockerfile


def generate_project_overlay_dockerfile(
    base_image: str,
    extra_packages: list = None,
    extra_commands: list = None,
) -> str:
    """Generate a thin project-layer Dockerfile on top of an existing agent image."""
    package_lines = ""
    packages = [str(pkg).strip() for pkg in (extra_packages or []) if str(pkg).strip()]
    if packages:
        pkg_line = " \\\n    ".join(packages)
        package_lines = f"""
# ── Project packages ────────────────────────────────────────────────
USER root
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \\
    {pkg_line} \\
    && rm -rf /var/lib/apt/lists/*
"""

    extra_lines = ""
    commands = [str(cmd).strip() for cmd in (extra_commands or []) if str(cmd).strip()]
    if commands:
        extra_lines = "\n" + "\n".join(
            rendered for cmd in commands
            if (rendered := _render_run_instruction(cmd))
        )

    return f"""FROM {base_image}
{package_lines}
USER dev
WORKDIR /home/dev
{extra_lines}
"""


# ── Build ────────────────────────────────────────────────────────────────

def build_image(
    container_dir: Path,
    image_name: str,
    security: SecurityProfile = None,
    agent: AgentConfig = None,
    agents: list = None,
    base_image: str = "debian:bookworm-slim",
    extra_packages: list = None,
    extra_commands: list = None,
    quiet: bool = False,
    verbose: bool = False,
    pull: bool = False,
    no_cache: bool = False,
    layer_on_base: bool = False,
):
    """Build a Docker image, generating the Dockerfile from config.

    Uses container_dir for entrypoint.sh and default agent settings.
    """
    context_hash = compute_build_context_hash(
        container_dir=container_dir,
        security=security,
        agent=agent,
        agents=agents,
        base_image=base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        layer_on_base=layer_on_base,
    )

    build_path = Path(tempfile.mkdtemp(prefix=".build-context-", dir=container_dir))
    try:
        # Generate Dockerfile
        if layer_on_base:
            dockerfile_content = generate_project_overlay_dockerfile(
                base_image=base_image,
                extra_packages=extra_packages,
                extra_commands=extra_commands,
            )
        else:
            dockerfile_content = generate_dockerfile(
                agent=agent,
                agents=agents,
                security=security,
                base_image=base_image,
                extra_packages=extra_packages,
                extra_commands=extra_commands,
            )
        (build_path / "Dockerfile").write_text(dockerfile_content)

        if not layer_on_base:
            # Copy entrypoint and baked-in defaults for full agent/base image builds.
            shutil.copy2(container_dir / "entrypoint.sh", build_path / "entrypoint.sh")
            check_monitoring_src = container_dir / "check_monitoring.sh"
            if check_monitoring_src.is_file():
                shutil.copy2(check_monitoring_src, build_path / "check_monitoring.sh")
            tmux_attach_banner_src = container_dir / "tmux-attach-banner.sh"
            if tmux_attach_banner_src.is_file():
                shutil.copy2(tmux_attach_banner_src, build_path / "tmux-attach-banner.sh")

            hooks_dst = build_path / "hooks"
            hooks_dst.mkdir()
            hooks_src = container_dir / "hooks"
            if hooks_src.is_dir():
                for f in sorted(hooks_src.iterdir()):
                    if f.is_file():
                        shutil.copy2(f, hooks_dst / f.name)

            epd_dst = build_path / ".entrypoint.d"
            epd_dst.mkdir()
            epd_src = container_dir / ".entrypoint.d"
            if epd_src.is_dir():
                for f in sorted(epd_src.iterdir()):
                    if f.is_file():
                        shutil.copy2(f, epd_dst / f.name)

            settings_dir = build_path / "claude-settings"
            settings_dir.mkdir()
            claude_home = Path.home() / ".claude"
            for fname in ("settings.json", "settings.local.json"):
                src = claude_home / fname
                if src.exists():
                    shutil.copy2(src, settings_dir / fname)

        uid = os.getuid()
        gid = os.getgid()
        cmd = [
            "docker", "build",
            "--build-arg", f"USER_UID={uid}",
            "--build-arg", f"USER_GID={gid}",
            "--label", f"{MANAGED_IMAGE_LABEL}=true",
            "--label", f"{BUILD_CONTEXT_HASH_LABEL}={context_hash}",
        ]
        version_labels = _build_agent_version_labels(agent=agent, agents=agents)
        for key, value in sorted(version_labels.items()):
            cmd.extend(["--label", f"{key}={value}"])
        cmd.extend(["-t", image_name, str(build_path)])
        if pull:
            cmd.insert(-1, "--pull")
        if no_cache:
            cmd.insert(-1, "--no-cache")
        if quiet:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                combined = "\n".join(
                    part for part in [result.stderr, result.stdout] if part
                )
                lines = [line.rstrip() for line in combined.splitlines() if line.strip()]
                if lines:
                    print("Docker build failed. Last output:")
                    for line in lines[-12:]:
                        print(f"  {line}")
                return False, combined
            return True, ""
        if verbose:
            result = subprocess.run(cmd)
            return result.returncode == 0, ""

        # Non-verbose: show a single-line progress bar based on build steps.
        # --progress=plain is a BuildKit-only flag; check availability first.
        progress_cmd = list(cmd)
        has_buildx = subprocess.run(
            ["docker", "buildx", "version"],
            capture_output=True,
        ).returncode == 0
        if has_buildx:
            # Insert before the build path (last element)
            progress_cmd.insert(-1, "--progress=plain")
        step_re = re.compile(r"^(step|STEP)\s+(\d+)\s*/\s*(\d+)")
        tail = deque(maxlen=20)
        printed_progress = False
        current_step = 0
        total_steps = 0

        def render_progress(cur: int, total: int) -> None:
            if total <= 0:
                return
            width = 28
            filled = int(width * cur / total)
            bar = "#" * filled + "-" * (width - filled)
            msg = f"\r  Build progress: [{bar}] {cur}/{total}"
            print(msg, end="", flush=True)

        proc = subprocess.Popen(
            progress_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.strip()
            if stripped:
                tail.append(stripped)
            match = step_re.match(stripped)
            if match:
                current_step = int(match.group(2))
                total_steps = int(match.group(3))
                render_progress(current_step, total_steps)
                printed_progress = True

        proc.wait()
        if printed_progress:
            print()

        if proc.returncode != 0:
            if tail:
                print("Docker build failed. Last output:")
                for line in tail:
                    print(f"  {line}")
            return False, "\n".join(tail)
        return True, ""

    finally:
        if build_path.exists():
            shutil.rmtree(build_path, ignore_errors=True)


# ── Run command construction ─────────────────────────────────────────────

def build_run_command(
    project: Project,
    environment: Environment,
    security: SecurityProfile,
    agent: AgentConfig,
    image_name: str,
    data_dir: Path,
    repo_volume: str = "",
    source_mounts: list = None,
    cred_sources: list = None,
) -> list:
    """Build the docker run command list from resolved configuration."""
    container_name = f"skua-{project.name}"
    project_mount_path = _project_mount_path(project)

    docker_cmd = [
        "docker", "run", "-it", "--rm",
        "--name", container_name,
    ]

    # Container runtime (gVisor, Kata, etc.)
    container_runtime = environment.docker.container_runtime
    if container_runtime:
        docker_cmd.extend(["--runtime", container_runtime])

    # Git identity
    if project.git.name:
        docker_cmd.extend(["-e", f"GIT_AUTHOR_NAME={project.git.name}"])
        docker_cmd.extend(["-e", f"GIT_AUTHOR_EMAIL={project.git.email}"])
        docker_cmd.extend(["-e", f"GIT_COMMITTER_NAME={project.git.name}"])
        docker_cmd.extend(["-e", f"GIT_COMMITTER_EMAIL={project.git.email}"])

    # SSH key mounts
    ssh_keys = ssh_private_keys(project.ssh)
    ssh_key = ssh_keys[0] if ssh_keys else ""
    is_remote_host = bool(getattr(project, "host", ""))
    if ssh_key and Path(ssh_key).is_file():
        key_name = Path(ssh_key).name
        docker_cmd.extend(["-e", f"SKUA_SSH_KEY_NAME={key_name}"])
        known_hosts_mounted = False
        for idx, ssh_key in enumerate(ssh_keys):
            key_path = Path(ssh_key)
            if not key_path.is_file():
                continue
            key_name = key_path.name

            if is_remote_host:
                if idx == 0:
                    key_b64 = base64.b64encode(key_path.read_bytes()).decode("ascii")
                    docker_cmd.extend(["-e", f"SKUA_SSH_KEY_B64={key_b64}"])
            else:
                docker_cmd.extend(["-v", f"{key_path}:/home/dev/.ssh-mount/{key_name}:ro"])

            pub_key = Path(f"{key_path}.pub")
            if pub_key.is_file():
                if is_remote_host:
                    if idx == 0:
                        pub_b64 = base64.b64encode(pub_key.read_bytes()).decode("ascii")
                        docker_cmd.extend(["-e", f"SKUA_SSH_PUB_KEY_B64={pub_b64}"])
                else:
                    docker_cmd.extend(["-v", f"{pub_key}:/home/dev/.ssh-mount/{key_name}.pub:ro"])

            known_hosts = key_path.parent / "known_hosts"
            if known_hosts.is_file() and not known_hosts_mounted:
                if is_remote_host:
                    known_hosts_b64 = base64.b64encode(known_hosts.read_bytes()).decode("ascii")
                    docker_cmd.extend(["-e", f"SKUA_SSH_KNOWN_HOSTS_B64={known_hosts_b64}"])
                else:
                    docker_cmd.extend(["-v", f"{known_hosts}:/home/dev/.ssh-mount/known_hosts:ro"])
                known_hosts_mounted = True

    # Project directory mounts (local bind-mounts or remote named volumes)
    mount_specs = list(source_mounts or [])
    if mount_specs:
        primary_mount = next((m for m in mount_specs if m.get("primary")), mount_specs[0])
        project_mount_path = primary_mount["target"]
        for mount in mount_specs:
            docker_cmd.extend(["-v", f"{mount['source']}:{mount['target']}"])
    else:
        if repo_volume:
            docker_cmd.extend(["-v", f"{repo_volume}:{project_mount_path}"])
        elif project.directory and Path(project.directory).is_dir():
            docker_cmd.extend(["-v", f"{project.directory}:{project_mount_path}"])
    docker_cmd.extend(["-e", f"SKUA_PROJECT_NAME={project.name}"])
    docker_cmd.extend(["-e", f"SKUA_PROJECT_DIR={project_mount_path}"])
    docker_cmd.extend(["-e", f"SKUA_IMAGE_REQUEST_FILE={project_mount_path}/.skua/image-request.yaml"])
    docker_cmd.extend(["-e", f"SKUA_ADAPT_GUIDE_FILE={project_mount_path}/.skua/ADAPT.md"])
    source_manifest = [
        {
            "name": mount.get("name", ""),
            "path": mount["target"],
            "primary": bool(mount.get("primary")),
        }
        for mount in (mount_specs or [{"name": project.name, "target": project_mount_path, "primary": True}])
    ]
    docker_cmd.extend(["-e", f"SKUA_PROJECT_SOURCES={json.dumps(source_manifest, separators=(',', ':'))}"])

    # Persistence mount
    auth_dir = ".claude"
    if agent and agent.auth and agent.auth.dir:
        auth_dir = agent.auth.dir
    auth_dir = auth_dir.lstrip("/")
    auth_mount = f"/home/dev/{auth_dir}"
    agent_name = (agent.name if agent and agent.name else project.agent) or "agent"
    agent_command = (
        agent.runtime.command
        if agent and agent.runtime and agent.runtime.command
        else agent_name
    )
    _login_subcmd = "\\login" #if agent_name == "codex" else "login"
    login_command = (
        agent.auth.login_command
        if agent and agent.auth and agent.auth.login_command
        else f"{agent_command} {_login_subcmd}"
    )
    auth_files = []
    if agent and agent.auth and agent.auth.files:
        auth_files = [Path(f).name for f in agent.auth.files if str(f).strip()]
    elif agent_name == "codex":
        auth_files = ["auth.json"]
    elif agent_name == "claude":
        auth_files = [".credentials.json", ".claude.json"]

    docker_cmd.extend(["-e", f"SKUA_AGENT_NAME={agent_name}"])
    docker_cmd.extend(["-e", f"SKUA_AGENT_COMMAND={agent_command}"])
    docker_cmd.extend(["-e", f"SKUA_AGENT_LOGIN_COMMAND={login_command}"])
    docker_cmd.extend(["-e", f"SKUA_AUTH_DIR={auth_dir}"])
    docker_cmd.extend(["-e", f"SKUA_AUTH_FILES={','.join(auth_files)}"])
    docker_cmd.extend(["-e", f"SKUA_CREDENTIAL_NAME={project.credential or '(none)'}"])

    if environment.persistence.mode == "bind":
        data_dir.mkdir(parents=True, exist_ok=True)
        docker_cmd.extend(["-v", f"{data_dir}:{auth_mount}"])
        for src_path, dest_name in (cred_sources or []):
            safe_dest = Path(dest_name).name.strip()
            if safe_dest and Path(src_path).is_file():
                docker_cmd.extend(["-v", f"{src_path}:{auth_mount}/{safe_dest}"])
    else:
        vol_name = f"skua-{project.name}-{project.agent}"
        docker_cmd.extend(["-v", f"{vol_name}:{auth_mount}"])

    # Network mode
    net = environment.network.mode
    if net == "host":
        docker_cmd.append("--network=host")
    elif net == "none":
        docker_cmd.append("--network=none")
    elif net == "internal":
        # For plain docker driver, internal means no network
        # (true internal networks require compose)
        if environment.driver == "docker":
            docker_cmd.append("--network=none")

    # Codex monitoring uses tcpdump from a non-root background process.
    # The binary has file capabilities baked into the image, but the container
    # still needs the matching kernel capabilities at runtime.
    if agent_name in ("codex", "claude"):
        docker_cmd.extend(["--cap-add=NET_RAW", "--cap-add=NET_ADMIN"])

    docker_cmd.append(image_name)
    return docker_cmd


# ── Container operations ─────────────────────────────────────────────────

def exec_into_container(container_name: str, replace_process: bool = True) -> bool:
    """Exec into a running container.

    Defaults to attaching to the container tmux session when available.
    When ``replace_process`` is True, replaces the current process via ``execvp``.
    Otherwise, runs as a subprocess and returns True on zero exit status.
    """
    attach_cmd = (
        'if [ "${SKUA_TMUX_ENABLE:-1}" = "0" ] || ! command -v tmux >/dev/null 2>&1; then '
        '  exec /bin/bash -i; '
        'fi; '
        'session="${SKUA_TMUX_SESSION:-skua}"; '
        'if ! tmux has-session -t "$session" 2>/dev/null; then '
        '  tmux new-session -d -s "$session" "exec /bin/bash -i"; '
        'fi; '
        'exec tmux attach-session -t "$session" \\; '
        'run-shell "/home/dev/.entrypoint.d/tmux-attach-banner.sh \\"$session\\""'
    )
    cmd = ["docker", "exec", "-it", container_name, "bash", "-lc", attach_cmd]
    if replace_process:
        os.execvp("docker", cmd)
        return False
    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def run_container(docker_cmd: list):
    """Replace this process with docker run (execvp)."""
    os.execvp("docker", docker_cmd)


def _hash_with_marker(hasher, marker: str, value):
    """Update a hash with a marker and optional bytes payload."""
    hasher.update(marker.encode("utf-8"))
    hasher.update(b"\0")
    if value is None:
        hasher.update(b"<missing>")
    elif isinstance(value, str):
        hasher.update(value.encode("utf-8"))
    else:
        hasher.update(value)
    hasher.update(b"\0")


def compute_build_context_hash(
    container_dir: Path,
    security: SecurityProfile = None,
    agent: AgentConfig = None,
    agents: list = None,
    base_image: str = "debian:bookworm-slim",
    extra_packages: list = None,
    extra_commands: list = None,
    layer_on_base: bool = False,
) -> str:
    """Compute deterministic hash for skua-managed Docker build context."""
    if layer_on_base:
        dockerfile_content = generate_project_overlay_dockerfile(
            base_image=base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
        )
    else:
        dockerfile_content = generate_dockerfile(
            agent=agent,
            agents=agents,
            security=security,
            base_image=base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
        )
    entrypoint_path = container_dir / "entrypoint.sh"

    hasher = hashlib.sha256()
    _hash_with_marker(hasher, "version", "v3")
    _hash_with_marker(hasher, "dockerfile", dockerfile_content)
    _hash_with_marker(hasher, "base_image_ref", base_image)
    _hash_with_marker(hasher, "base_image_id", _local_image_id(base_image))
    if not layer_on_base:
        _hash_with_marker(hasher, "entrypoint", entrypoint_path.read_bytes() if entrypoint_path.exists() else None)
    _hash_with_marker(hasher, "uid", str(os.getuid()))
    _hash_with_marker(hasher, "gid", str(os.getgid()))

    if not layer_on_base:
        claude_home = Path.home() / ".claude"
        settings_json = claude_home / "settings.json"
        _hash_with_marker(
            hasher,
            "claude-default:settings.json",
            settings_json.read_bytes() if settings_json.exists() else None,
        )

        check_monitoring_path = container_dir / "check_monitoring.sh"
        _hash_with_marker(
            hasher,
            "check_monitoring",
            check_monitoring_path.read_bytes() if check_monitoring_path.exists() else None,
        )
        tmux_attach_banner_path = container_dir / "tmux-attach-banner.sh"
        _hash_with_marker(
            hasher,
            "tmux_attach_banner",
            tmux_attach_banner_path.read_bytes() if tmux_attach_banner_path.exists() else None,
        )

        for subdir in ("hooks", ".entrypoint.d"):
            script_dir = container_dir / subdir
            if script_dir.is_dir():
                for script_file in sorted(script_dir.iterdir()):
                    if script_file.is_file():
                        _hash_with_marker(hasher, f"{subdir}:{script_file.name}", script_file.read_bytes())

    return hasher.hexdigest()


def _image_label(image_name: str, label_key: str) -> str:
    """Return a docker image label value, or empty string when unavailable."""
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{label_key}" }}}}',
                image_name,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""

    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if value in ("", "<no value>", "<nil>"):
        return ""
    return value


def _local_image_id(image_name: str) -> str:
    """Return a local Docker image ID for a ref, or empty string when unavailable."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image_name],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if value in ("", "<no value>", "<nil>"):
        return ""
    return value


def image_matches_build_context(
    image_name: str,
    container_dir: Path,
    security: SecurityProfile = None,
    agent: AgentConfig = None,
    agents: list = None,
    base_image: str = "debian:bookworm-slim",
    extra_packages: list = None,
    extra_commands: list = None,
    layer_on_base: bool = False,
) -> bool:
    """Return True when image label hash matches current generated build context."""
    expected_hash = compute_build_context_hash(
        container_dir=container_dir,
        security=security,
        agent=agent,
        agents=agents,
        base_image=base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        layer_on_base=layer_on_base,
    )
    actual_hash = _image_label(image_name, BUILD_CONTEXT_HASH_LABEL)
    return bool(actual_hash) and actual_hash == expected_hash


def start_container(docker_cmd: list) -> bool:
    """Run docker command and return success."""
    result = subprocess.run(docker_cmd)
    return result.returncode == 0


def wait_for_running_container(name: str, timeout_seconds: float = 10.0) -> bool:
    """Wait for a container to appear in docker ps."""
    deadline = time.time() + max(timeout_seconds, 0.1)
    while time.time() < deadline:
        if is_container_running(name):
            return True
        time.sleep(0.2)
    return is_container_running(name)
