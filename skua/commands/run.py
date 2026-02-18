# SPDX-License-Identifier: BUSL-1.1
"""skua run — start or attach to a container for a project."""

import copy
import os
import shutil
import subprocess
import sys
import json
import shlex
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skua.config import ConfigStore, validate_project
from skua.commands.credential import resolve_credential_sources, agent_default_source_dir
from skua.docker import (
    is_container_running,
    exec_into_container,
    build_run_command,
    build_image,
    image_exists,
    image_name_for_project,
    resolve_project_image_inputs,
    start_container,
    wait_for_running_container,
    _project_mount_path,
)
from skua.project_adapt import ensure_adapt_workspace


def _clone_repo_into_remote_volume(project, vol_name: str):
    """Clone the project repo into a Docker named volume using alpine/git.

    Requires DOCKER_HOST to already be set to the remote host.
    Skips silently if the repo is already cloned in the volume.
    """
    check = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{vol_name}:/workspace",
            "alpine", "sh", "-c",
            "test -d /workspace/.git && echo cloned || echo empty",
        ],
        capture_output=True, text=True,
    )
    if check.returncode == 0 and "cloned" in check.stdout:
        print(f"Using existing repo clone in volume '{vol_name}'.")
        return

    print(f"Cloning {project.repo} into remote volume '{vol_name}'...")
    result = subprocess.run([
        "docker", "run", "--rm",
        "-v", f"{vol_name}:/workspace",
        "alpine/git",
        "clone", project.repo, "/workspace",
    ])
    if result.returncode != 0:
        print("Error: Failed to clone repository into remote volume.")
        print("  Tip: For private SSH repositories, ensure git/SSH access is configured on the remote host.")
        sys.exit(1)


def _seed_auth_from_host(data_dir: Path, cred, agent, overwrite: bool = False) -> int:
    """Seed missing auth files from the host into the container persistence directory.

    Uses :func:`resolve_credential_sources` to determine which host files map
    to which destination names, so all credential-resolution logic lives in one
    place (``skua.commands.credential``).
    """
    sources = resolve_credential_sources(cred, agent)
    copied = 0
    for src, dest_name in sources:
        dest = data_dir / dest_name
        if dest.exists() and not overwrite:
            continue
        if src.is_file():
            shutil.copy2(src, dest)
            copied += 1
    return copied


def _parse_expiry_datetime(value):
    """Parse common expiry value formats into a UTC datetime."""
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts <= 0:
            return None
        # Heuristic: values above 1e12 are very likely milliseconds.
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        elif ts < 1_000_000_000:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_expiry_datetime(int(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _jwt_expiry_datetime(token: str):
    """Best-effort parse of JWT `exp` claim without signature verification."""
    if not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    if not payload_b64:
        return None
    pad = "=" * (-len(payload_b64) % 4)
    try:
        payload_raw = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(payload_raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        if exp <= 0:
            return None
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    return _parse_expiry_datetime(exp)


def _extract_expiry_values(obj) -> list:
    """Recursively collect datetime values from common expiry keys."""
    values = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).strip().lower()
            is_expiry_key = (
                "expir" in key_l
                or key_l in {"exp", "expires", "expires_at", "expiresat", "expires_on", "expireson"}
                or key_l.endswith("_exp")
            )
            if is_expiry_key:
                parsed = _parse_expiry_datetime(value)
                if parsed is not None:
                    values.append(parsed)
            # Codex-style auth JSON may only expose JWT tokens. Extract `exp`.
            if isinstance(value, str) and "token" in key_l:
                jwt_exp = _jwt_expiry_datetime(value)
                if jwt_exp is not None:
                    values.append(jwt_exp)
            values.extend(_extract_expiry_values(value))
    elif isinstance(obj, list):
        for item in obj:
            values.extend(_extract_expiry_values(item))
    return values


def _credential_file_expiry(path: Path):
    """Return earliest detected expiry in a JSON credential file, else None."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    expiries = _extract_expiry_values(data)
    return min(expiries) if expiries else None


def _credential_refresh_reason(cred, agent, now=None) -> str:
    """Return a reason to refresh local credentials, or empty string if healthy/unknown."""
    now = now or datetime.now(timezone.utc)
    stale_cutoff = now + timedelta(minutes=2)
    sources = resolve_credential_sources(cred, agent)
    if not sources:
        return ""
    existing = [(src, dest_name) for src, dest_name in sources if src.is_file()]

    if not existing:
        return "no local credential files were found"

    stale = []
    for src, dest_name in existing:
        expiry = _credential_file_expiry(src)
        if expiry and expiry <= stale_cutoff:
            stale.append((dest_name, expiry))

    if stale:
        parts = []
        for dest_name, expiry in stale:
            ts = expiry.strftime("%Y-%m-%d %H:%M:%S %Z")
            parts.append(f"{dest_name} (expires {ts})")
        return "credential appears expired/near-expiry: " + ", ".join(parts)

    return ""


def _run_local_login(login_cmd: str) -> bool:
    """Run local agent login flow. Returns True when command was run (even non-zero exit)."""
    cmd_parts = shlex.split(login_cmd)
    if not cmd_parts:
        print("Warning: cannot run local login; login command is empty.")
        return False
    if not shutil.which(cmd_parts[0]):
        print(f"Warning: '{cmd_parts[0]}' is not installed; skipping local login refresh.")
        return False

    print(f"Running local login: {login_cmd}")
    print("Complete the sign-in flow, then return here.")
    try:
        subprocess.run(cmd_parts, check=True)
    except subprocess.CalledProcessError:
        print(f"Warning: '{login_cmd}' exited non-zero. Continuing with detected credential files.")
    except KeyboardInterrupt:
        print("\nCancelled local login refresh.")
        return False
    return True


def _maybe_refresh_local_credentials(agent, cred) -> bool:
    """Prompt for local re-login if credentials look missing/stale."""
    reason = _credential_refresh_reason(cred, agent)
    if not reason:
        return False

    login_cmd = (agent.auth.login_command or f"{agent.runtime.command or agent.name} login").strip()
    if not login_cmd:
        print(f"Warning: {reason}. No login command is configured for agent '{agent.name}'.")
        return False

    print(f"Warning: {reason}.")
    answer = input(f"Run '{login_cmd}' locally to refresh now? [Y/n]: ").strip().lower()
    if answer == "n":
        return False

    if not _run_local_login(login_cmd):
        return False

    post_reason = _credential_refresh_reason(cred, agent)
    if post_reason:
        print(f"Warning: credentials still look stale after login: {post_reason}")
    return True


def _detached_run_command(docker_cmd: list) -> list:
    """Convert `docker run -it ...` into detached mode."""
    cmd = [token for token in docker_cmd if token != "-it"]
    if len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "run" and "-d" not in cmd:
        cmd.insert(2, "-d")
    detached_entry = (
        'if [ "${SKUA_TMUX_ENABLE:-1}" = "0" ] || ! command -v tmux >/dev/null 2>&1; then '
        "  while true; do sleep 3600; done; "
        "fi; "
        'session="${SKUA_TMUX_SESSION:-skua}"; '
        'start_dir="${SKUA_PROJECT_DIR:-/home/dev/project}"; '
        '[ -d "$start_dir" ] || start_dir="/home/dev"; '
        'if ! tmux has-session -t "$session" 2>/dev/null; then '
        '  tmux new-session -d -s "$session" -c "$start_dir" /bin/bash; '
        '  if [ -f /tmp/skua-entrypoint-info.txt ]; then '
        '    tmux send-keys -t "$session" "cat /tmp/skua-entrypoint-info.txt; echo" C-m; '
        "  fi; "
        "fi; "
        'while tmux has-session -t "$session" 2>/dev/null; do sleep 1; done'
    )
    cmd.extend(["bash", "-lc", detached_entry])
    return cmd


def cmd_run(args):
    store = ConfigStore()
    name = args.name

    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found. Add it with: skua add {name}")
        sys.exit(1)

    host = getattr(project, "host", "") or ""

    # Route Docker operations to remote host when specified
    if host:
        os.environ["DOCKER_HOST"] = f"ssh://{host}"
        print(f"Connecting to remote host '{host}'...")

    container_name = f"skua-{name}"

    # Check if already running
    if is_container_running(container_name):
        print(f"Container '{container_name}' is already running.")
        answer = input("Attach to it? [Y/n]: ").strip().lower()
        if answer != "n":
            print("Attaching to container tmux session (detach: Ctrl-b then d)...")
            exec_into_container(container_name)
        return

    # Load referenced resources
    env = store.load_environment(project.environment)
    sec = store.load_security(project.security)
    agent = store.load_agent(project.agent)

    if env is None:
        print(f"Error: Environment '{project.environment}' not found.")
        sys.exit(1)
    if sec is None:
        print(f"Error: Security profile '{project.security}' not found.")
        sys.exit(1)
    if agent is None:
        print(f"Error: Agent '{project.agent}' not found.")
        sys.exit(1)

    # Remote projects must use named volumes (bind mounts don't work across hosts)
    if host:
        env = copy.deepcopy(env)
        env.persistence.mode = "volume"

    # Validate configuration
    result = validate_project(project, env, sec, agent)
    if result.warnings:
        for w in result.warnings:
            print(f"  Warning: {w}")
    if not result.valid:
        print("\nConfiguration validation failed:")
        for e in result.errors:
            print(f"  x {e}")
        print("\nRun 'skua validate' for details, or fix the configuration.")
        sys.exit(1)

    # Handle repo — remote projects clone into a Docker volume, local projects clone to disk
    repo_volume = ""
    if host and project.repo:
        repo_volume = f"skua-{name}-repo"
        _clone_repo_into_remote_volume(project, repo_volume)
    elif project.repo:
        clone_dir = store.repo_dir(name)
        if not clone_dir.exists():
            print(f"Cloning {project.repo} into {clone_dir}...")
            clone_cmd = ["git", "clone"]
            if project.ssh.private_key:
                ssh_cmd = f"ssh -i {project.ssh.private_key} -o StrictHostKeyChecking=no"
                clone_cmd = ["git", "-c", f"core.sshCommand={ssh_cmd}", "clone"]
            clone_cmd += [project.repo, str(clone_dir)]
            try:
                subprocess.run(clone_cmd, check=True)
            except subprocess.CalledProcessError:
                print(f"Error: Failed to clone {project.repo}")
                sys.exit(1)
        else:
            print(f"Using existing clone at {clone_dir}")
        project.directory = str(clone_dir)

    if not host and project.directory and Path(project.directory).is_dir():
        ensure_adapt_workspace(Path(project.directory), project.name, project.agent)

    # Determine image name
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    image_name = image_name_for_project(image_name_base, project)
    if not image_exists(image_name):
        print(f"Image '{image_name}' not found for agent '{project.agent}'.")
        print("Building image lazily...")
        container_dir = store.get_container_dir()
        if container_dir is None:
            print("Error: Cannot find container build assets (entrypoint.sh).")
            print("Set toolDir in global.yaml or reinstall skua.")
            sys.exit(1)

        base_image = g.get("baseImage", "debian:bookworm-slim")
        defaults = g.get("defaults", {})
        build_security_name = defaults.get("security", "open")
        build_security = store.load_security(build_security_name) or sec
        image_config = g.get("image", {})
        global_extra_packages = image_config.get("extraPackages", [])
        global_extra_commands = image_config.get("extraCommands", [])
        resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
            default_base_image=base_image,
            agent=agent,
            project=project,
            global_extra_packages=global_extra_packages,
            global_extra_commands=global_extra_commands,
        )

        success = build_image(
            container_dir=container_dir,
            image_name=image_name,
            security=build_security,
            agent=agent,
            base_image=resolved_base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
        )
        if not success:
            print(f"Error: failed to build image '{image_name}'.")
            sys.exit(1)

    # Build persistence path
    data_dir = store.project_data_dir(name, project.agent)

    # Resolve credential (None is fine — resolve_credential_sources falls back to agent default dir)
    cred = None
    if project.credential:
        cred = store.load_credential(project.credential)
        if cred is None:
            print(f"Warning: Credential '{project.credential}' not found.")

    # Seed/sync persisted auth files from host if needed
    if env.persistence.mode == "bind":
        data_dir.mkdir(parents=True, exist_ok=True)
        refreshed = _maybe_refresh_local_credentials(agent=agent, cred=cred)
        copied = _seed_auth_from_host(
            data_dir=data_dir,
            cred=cred,
            agent=agent,
            overwrite=refreshed,
        )
        if copied:
            action = "Synced" if refreshed else "Seeded"
            print(f"{action} {copied} auth file(s).")

    # Build and exec docker command
    docker_cmd = build_run_command(
        project=project,
        environment=env,
        security=sec,
        agent=agent,
        image_name=image_name,
        data_dir=data_dir,
        repo_volume=repo_volume,
    )

    # Print summary
    print(f"Starting skua-{name}...")
    if host:
        print(f"  Host:        {host} (remote)")
    if repo_volume:
        print(f"  Repo vol:    {repo_volume} -> {_project_mount_path(project)}")
    else:
        print(f"  Project:     {project.directory or '(none)'}")
    print(f"  Environment: {project.environment}")
    print(f"  Security:    {project.security}")
    print(f"  Agent:       {project.agent}")
    if project.credential:
        if cred and cred.files:
            cred_label = f"{project.credential} ({len(cred.files)} explicit file(s))"
        elif cred and cred.source_dir:
            cred_label = f"{project.credential} ({cred.source_dir})"
        elif cred:
            cred_label = f"{project.credential} (default: {agent_default_source_dir(agent)})"
        else:
            cred_label = f"{project.credential} (not found)"
        print(f"  Credential:  {cred_label}")
    auth_dir = (agent.auth.dir or f".{project.agent}").lstrip("/")
    print(f"  Image:       {image_name}")
    ssh_display = Path(project.ssh.private_key).name if project.ssh.private_key else "(none)"
    print(f"  SSH key:     {ssh_display}")
    print(f"  Network:     {env.network.mode}")
    if env.persistence.mode == "bind":
        print(f"  Auth dir:    {data_dir} -> /home/dev/{auth_dir}")
    else:
        print(f"  Auth dir:    volume skua-{name}-{project.agent} -> /home/dev/{auth_dir}")
    print()

    detached_cmd = _detached_run_command(docker_cmd)
    if not start_container(detached_cmd):
        print(f"Error: failed to start container '{container_name}'.")
        sys.exit(1)
    if not wait_for_running_container(container_name):
        print(f"Error: container '{container_name}' did not start correctly.")
        sys.exit(1)
    print("Attaching to container tmux session (detach: Ctrl-b then d)...")
    exec_into_container(container_name)
