# SPDX-License-Identifier: BUSL-1.1
"""Docker operations — build images, run containers, query state."""

import base64
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from skua.config.resources import Environment, SecurityProfile, AgentConfig, Project


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


def get_running_skua_containers(host: str = "") -> list:
    """Return list of running skua container names for local or remote host."""
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
        return []

    if result.returncode != 0:
        return []
    if result.stdout.strip():
        return result.stdout.strip().split("\n")
    return []


def image_name_for_agent(base_image_name: str, agent_name: str) -> str:
    """Return an agent-specific image name, preserving an optional tag."""
    base = (base_image_name or "skua-base").strip()
    agent = (agent_name or "claude").strip()

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


def image_name_for_project(base_image_name: str, project: Project) -> str:
    """Return the image tag to use for a specific project."""
    agent_name = (project.agent or "claude") if project else "claude"
    agent_image = image_name_for_agent(base_image_name, agent_name)
    if not project_has_image_customizations(project):
        return agent_image

    repo, tag = _split_image_ref_tag(agent_image)
    project_part = _sanitize_mount_name(project.name or "project")
    version = int(getattr(project.image, "version", 0) or 0)
    if version < 1:
        version = 1
    return f"{repo}-{project_part}-v{version}{tag}"


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
    if not mount_name and project.directory:
        mount_name = _sanitize_mount_name(Path(project.directory).name)
    if not mount_name and project.name:
        mount_name = _sanitize_mount_name(project.name)
    if not mount_name:
        mount_name = "project"

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
    "net-tools", "iputils-ping", "dnsutils",
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

    install_lines = "\n".join(f"RUN {cmd}" for cmd in install_cmds)

    # Extra commands
    extra_lines = ""
    if extra_commands:
        extra_lines = "\n" + "\n".join(f"RUN {cmd}" for cmd in extra_commands)

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

# ── Install agent ────────────────────────────────────────────────────
ENV NPM_CONFIG_PREFIX="/home/dev/.local"
USER dev
{install_lines}
WORKDIR /home/dev
{extra_lines}

# ── Non-sensitive config defaults (copied into volume on first run) ──
COPY --chown=dev:dev claude-settings/ /home/dev/.claude-defaults/

# ── Placeholder directories ─────────────────────────────────────────
RUN mkdir -p /home/dev/.ssh /home/dev/project /home/dev/.claude

# ── Environment ──────────────────────────────────────────────────────
ENV EDITOR=vim
ENV PATH="/home/dev/.local/bin:${{PATH}}"
{sudo_removal}
# ── Entrypoint ───────────────────────────────────────────────────────
COPY --chown=dev:dev entrypoint.sh /home/dev/entrypoint.sh
RUN chmod +x /home/dev/entrypoint.sh

ENTRYPOINT ["/home/dev/entrypoint.sh"]
"""
    return dockerfile


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
    )

    build_path = container_dir / ".build-context"
    try:
        if build_path.exists():
            shutil.rmtree(build_path)
        build_path.mkdir()

        # Generate Dockerfile
        dockerfile_content = generate_dockerfile(
            agent=agent,
            agents=agents,
            security=security,
            base_image=base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
        )
        (build_path / "Dockerfile").write_text(dockerfile_content)

        # Copy entrypoint
        shutil.copy2(container_dir / "entrypoint.sh", build_path / "entrypoint.sh")

        # Copy Claude settings (no credentials)
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
            "-t", image_name,
            str(build_path),
        ]
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
        else:
            result = subprocess.run(cmd)
        return result.returncode == 0

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
    ssh_key = project.ssh.private_key
    is_remote_host = bool(getattr(project, "host", ""))
    if ssh_key and Path(ssh_key).is_file():
        key_name = Path(ssh_key).name
        docker_cmd.extend(["-e", f"SKUA_SSH_KEY_NAME={key_name}"])

        if is_remote_host:
            key_b64 = base64.b64encode(Path(ssh_key).read_bytes()).decode("ascii")
            docker_cmd.extend(["-e", f"SKUA_SSH_KEY_B64={key_b64}"])
        else:
            docker_cmd.extend(["-v", f"{ssh_key}:/home/dev/.ssh-mount/{key_name}:ro"])

        pub_key = Path(f"{ssh_key}.pub")
        if pub_key.is_file():
            if is_remote_host:
                pub_b64 = base64.b64encode(pub_key.read_bytes()).decode("ascii")
                docker_cmd.extend(["-e", f"SKUA_SSH_PUB_KEY_B64={pub_b64}"])
            else:
                docker_cmd.extend(["-v", f"{pub_key}:/home/dev/.ssh-mount/{key_name}.pub:ro"])

        known_hosts = Path(ssh_key).parent / "known_hosts"
        if known_hosts.is_file():
            if is_remote_host:
                known_hosts_b64 = base64.b64encode(known_hosts.read_bytes()).decode("ascii")
                docker_cmd.extend(["-e", f"SKUA_SSH_KNOWN_HOSTS_B64={known_hosts_b64}"])
            else:
                docker_cmd.extend(["-v", f"{known_hosts}:/home/dev/.ssh-mount/known_hosts:ro"])

    # Project directory mount (local bind-mount or remote named volume)
    if repo_volume:
        docker_cmd.extend(["-v", f"{repo_volume}:{project_mount_path}"])
    elif project.directory and Path(project.directory).is_dir():
        docker_cmd.extend(["-v", f"{project.directory}:{project_mount_path}"])
    docker_cmd.extend(["-e", f"SKUA_PROJECT_DIR={project_mount_path}"])
    docker_cmd.extend(["-e", f"SKUA_IMAGE_REQUEST_FILE={project_mount_path}/.skua/image-request.yaml"])
    docker_cmd.extend(["-e", f"SKUA_ADAPT_GUIDE_FILE={project_mount_path}/.skua/ADAPT.md"])

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
    login_command = (
        agent.auth.login_command
        if agent and agent.auth and agent.auth.login_command
        else f"{agent_command} login"
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

    docker_cmd.append(image_name)
    return docker_cmd


# ── Container operations ─────────────────────────────────────────────────

def exec_into_container(container_name: str):
    """Exec into a running container, replacing this process.

    Defaults to attaching to the container tmux session when available.
    """
    attach_cmd = (
        'if [ "${SKUA_TMUX_ENABLE:-1}" = "0" ] || ! command -v tmux >/dev/null 2>&1; then '
        "  exec /bin/bash; "
        "fi; "
        'session="${SKUA_TMUX_SESSION:-skua}"; '
        'tmux new-session -A -s "$session"'
    )
    os.execvp(
        "docker",
        ["docker", "exec", "-it", container_name, "bash", "-lc", attach_cmd],
    )


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
) -> str:
    """Compute deterministic hash for skua-managed Docker build context."""
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
    _hash_with_marker(hasher, "version", "v1")
    _hash_with_marker(hasher, "dockerfile", dockerfile_content)
    _hash_with_marker(hasher, "entrypoint", entrypoint_path.read_bytes() if entrypoint_path.exists() else None)
    _hash_with_marker(hasher, "uid", str(os.getuid()))
    _hash_with_marker(hasher, "gid", str(os.getgid()))

    claude_home = Path.home() / ".claude"
    for fname in ("settings.json", "settings.local.json"):
        src = claude_home / fname
        _hash_with_marker(hasher, f"claude-default:{fname}", src.read_bytes() if src.exists() else None)

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


def image_matches_build_context(
    image_name: str,
    container_dir: Path,
    security: SecurityProfile = None,
    agent: AgentConfig = None,
    agents: list = None,
    base_image: str = "debian:bookworm-slim",
    extra_packages: list = None,
    extra_commands: list = None,
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
