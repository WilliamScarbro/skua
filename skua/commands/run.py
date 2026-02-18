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
import tempfile
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


def _is_snap_binary(path: str) -> bool:
    if not path:
        return False

    raw = str(Path(path).expanduser())
    if raw.startswith("/snap/") or "/snap/bin/" in raw or raw.startswith("/var/lib/snapd/snap/bin/"):
        return True

    try:
        resolved = str(Path(raw).resolve())
    except OSError:
        resolved = raw
    return resolved.startswith("/snap/")


def _find_non_snap_docker_binary() -> str:
    """Return a preferred non-Snap docker CLI path when available."""
    current = shutil.which("docker") or ""
    if current and not _is_snap_binary(current):
        return current

    candidates = [
        "/usr/local/bin/docker",
        str(Path.home() / ".local" / "bin" / "docker"),
        "/usr/bin/docker",
    ]
    for candidate in candidates:
        p = Path(candidate).expanduser()
        if p.is_file() and os.access(p, os.X_OK) and not _is_snap_binary(str(p)):
            return str(p)
    return ""


def _prefer_non_snap_docker_on_path() -> str:
    """Prepend non-Snap docker binary dir to PATH; returns selected binary or empty."""
    docker_bin = _find_non_snap_docker_binary()
    if not docker_bin:
        return ""
    docker_dir = str(Path(docker_bin).parent)
    path_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if docker_dir in path_parts:
        path_parts = [docker_dir] + [p for p in path_parts if p != docker_dir]
    else:
        path_parts = [docker_dir] + path_parts
    os.environ["PATH"] = os.pathsep.join(path_parts)
    return docker_bin


def _ensure_local_ssh_client_for_remote_docker(host: str):
    """Fail fast when Docker remote mode cannot execute the local SSH client."""
    ssh_path = shutil.which("ssh")
    if not ssh_path:
        print("Error: Remote Docker host requires a local SSH client, but 'ssh' is not in PATH.")
        print("  Install OpenSSH client and retry (example: apt install openssh-client).")
        sys.exit(1)

    if not os.access(ssh_path, os.X_OK):
        print(f"Error: Local SSH client is not executable: {ssh_path}")
        print("  Docker remote mode shells out to this binary (DOCKER_HOST=ssh://...).")
        print("  Fix permissions or reinstall OpenSSH client, then retry.")
        sys.exit(1)

    try:
        subprocess.run([ssh_path, "-V"], capture_output=True, text=True, check=False)
    except PermissionError:
        print(f"Error: Cannot execute local SSH client '{ssh_path}' (permission denied).")
        print("  Docker remote mode shells out to this binary (DOCKER_HOST=ssh://...).")
        print("  Fix local execute permissions or reinstall OpenSSH client, then retry.")
        print(f"  Remote host: {host}")
        sys.exit(1)
    except OSError as exc:
        print(f"Error: Failed to execute local SSH client '{ssh_path}': {exc}")
        print("  Docker remote mode requires a working local SSH client.")
        print(f"  Remote host: {host}")
        sys.exit(1)


def _probe_current_docker_connection() -> tuple:
    """Return (ok, error_message) for `docker version` with current env/PATH."""
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker CLI was not found in PATH."

    if result.returncode == 0:
        return True, ""

    msg = (result.stderr or result.stdout or "").strip()
    if not msg:
        msg = f"docker exited with status {result.returncode}"
    return False, msg


def _print_docker_cli_install_hint():
    """Print guidance for installing a non-Snap Docker CLI without replacing Snap."""
    print("Tip: install a non-Snap Docker CLI binary and retry with DOCKER_HOST mode.")
    print("  No local daemon is required for Skua remote mode.")
    print("  Prefer CLI-only packages (for example: docker-ce-cli) or a standalone docker CLI binary.")
    print("  Then verify:")
    print("    which docker")
    print("    docker --version")


def _docker_cli_installer_script() -> Path:
    """Return path to the standalone Docker CLI installer script."""
    return Path(__file__).resolve().parent.parent / "scripts" / "install_docker_cli.sh"


def _run_docker_cli_installer() -> bool:
    """Run the installer script for a non-Snap Docker CLI."""
    installer = _docker_cli_installer_script()
    if not installer.is_file():
        print(f"Error: Docker CLI installer script not found: {installer}")
        return False

    cmd = [str(installer)] if os.access(installer, os.X_OK) else ["bash", str(installer)]
    print(f"Running installer: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def _prompt_remote_docker_recovery_action() -> str:
    """Prompt for next action after DOCKER_HOST failure: install, fallback, or cancel."""
    print("Choose how to continue:")
    print("  1. Install standalone Docker CLI now (keeps Snap Docker unchanged)")
    print("  2. Continue with SSH fallback now (no local Docker CLI needed)")
    print("  3. Cancel")
    choice = input("Select option [1/2/3, default 1]: ").strip().lower()
    if choice in ("", "1", "i", "install"):
        return "install"
    if choice in ("2", "f", "fallback", "ssh"):
        return "fallback"
    return "cancel"


def _enable_ssh_docker_wrapper(host: str):
    """Route `docker ...` calls through `ssh <host> docker ...` for this process."""
    wrapper_dir = Path(tempfile.mkdtemp(prefix="skua-ssh-docker-"))
    wrapper = wrapper_dir / "docker"
    host_quoted = shlex.quote(host)

    wrapper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "tty_flag=''\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$arg\" = \"-it\" ] || [ \"$arg\" = \"-ti\" ]; then\n"
        "    tty_flag='-tt'\n"
        "    break\n"
        "  fi\n"
        "done\n"
        f"exec ssh $tty_flag {host_quoted} docker \"$@\"\n"
    )
    wrapper.chmod(0o700)

    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{wrapper_dir}:{current_path}" if current_path else str(wrapper_dir)
    os.environ.pop("DOCKER_HOST", None)
    os.environ["SKUA_DOCKER_TRANSPORT"] = "ssh-wrapper"
    os.environ["SKUA_DOCKER_REMOTE_HOST"] = host


def _configure_remote_docker_transport(host: str):
    """Try DOCKER_HOST transport first, then offer SSH wrapper fallback."""
    os.environ.pop("SKUA_DOCKER_TRANSPORT", None)
    os.environ.pop("SKUA_DOCKER_REMOTE_HOST", None)
    selected_bin = _prefer_non_snap_docker_on_path()
    if selected_bin:
        print(f"Using docker CLI: {selected_bin}")
    os.environ["DOCKER_HOST"] = f"ssh://{host}"
    print(f"Connecting to remote host '{host}' via DOCKER_HOST...")

    ok, err = _probe_current_docker_connection()
    if ok:
        return

    print("Warning: Remote Docker connection via DOCKER_HOST failed.")
    print(f"  {err}")
    _print_docker_cli_install_hint()

    if sys.stdin.isatty() and sys.stdout.isatty():
        action = _prompt_remote_docker_recovery_action()
        if action == "install":
            if not _run_docker_cli_installer():
                print("Warning: Docker CLI installer failed.")
                fallback = input("Use SSH fallback transport now? [Y/n]: ").strip().lower()
                if fallback == "n":
                    sys.exit(1)
            else:
                selected_bin = _prefer_non_snap_docker_on_path()
                if selected_bin:
                    print(f"Using docker CLI: {selected_bin}")
                os.environ["DOCKER_HOST"] = f"ssh://{host}"
                ok, err = _probe_current_docker_connection()
                if ok:
                    return
                print("Warning: DOCKER_HOST is still failing after install.")
                print(f"  {err}")
                fallback = input("Use SSH fallback transport now? [Y/n]: ").strip().lower()
                if fallback == "n":
                    sys.exit(1)
        elif action == "cancel":
            sys.exit(1)
    else:
        print("Non-interactive session detected; falling back to SSH transport.")

    print("Using SSH fallback transport: ssh <host> docker ...")
    _enable_ssh_docker_wrapper(host)
    ok, err = _probe_current_docker_connection()
    if not ok:
        print("Error: SSH fallback transport failed.")
        print(f"  {err}")
        sys.exit(1)


def _clone_repo_into_remote_volume(project, vol_name: str):
    """Clone the project repo into a Docker named volume using alpine/git.

    Requires the current process Docker transport to target the remote host.
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

    clone_env = os.environ.copy()
    clone_env["SKUA_REMOTE_GIT_REPO"] = project.repo

    ssh_cmd_parts = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    key_var = ""
    known_hosts_var = ""
    key_path_value = str(getattr(project.ssh, "private_key", "") or "").strip()
    if key_path_value:
        key_path = Path(key_path_value).expanduser()
        if key_path.is_file():
            key_b64 = base64.b64encode(key_path.read_bytes()).decode("ascii")
            key_var = "SKUA_REMOTE_GIT_SSH_KEY_B64"
            clone_env[key_var] = key_b64
            ssh_cmd_parts.extend(["-i", "/tmp/skua-ssh/id_key", "-o", "IdentitiesOnly=yes"])

            known_hosts_path = key_path.parent / "known_hosts"
            if known_hosts_path.is_file():
                known_hosts_b64 = base64.b64encode(known_hosts_path.read_bytes()).decode("ascii")
                known_hosts_var = "SKUA_REMOTE_GIT_KNOWN_HOSTS_B64"
                clone_env[known_hosts_var] = known_hosts_b64
                ssh_cmd_parts.extend(["-o", "UserKnownHostsFile=/tmp/skua-ssh/known_hosts"])
        else:
            print(f"Warning: SSH key not found for remote clone: {key_path}")
            print("  Falling back to remote host SSH defaults.")

    setup_script = (
        "set -eu\n"
        "if [ -n \"${SKUA_REMOTE_GIT_SSH_KEY_B64:-}\" ]; then\n"
        "  mkdir -p /tmp/skua-ssh\n"
        "  printf '%s' \"$SKUA_REMOTE_GIT_SSH_KEY_B64\" | base64 -d > /tmp/skua-ssh/id_key\n"
        "  chmod 600 /tmp/skua-ssh/id_key\n"
        "fi\n"
        "if [ -n \"${SKUA_REMOTE_GIT_KNOWN_HOSTS_B64:-}\" ]; then\n"
        "  mkdir -p /tmp/skua-ssh\n"
        "  printf '%s' \"$SKUA_REMOTE_GIT_KNOWN_HOSTS_B64\" | base64 -d > /tmp/skua-ssh/known_hosts\n"
        "  chmod 600 /tmp/skua-ssh/known_hosts\n"
        "fi\n"
        f"export GIT_SSH_COMMAND={shlex.quote(' '.join(ssh_cmd_parts))}\n"
        "git clone \"$SKUA_REMOTE_GIT_REPO\" /workspace\n"
    )

    clone_cmd = [
        "docker", "run", "--rm",
        "-v", f"{vol_name}:/workspace",
        "-e", "SKUA_REMOTE_GIT_REPO",
    ]
    if key_var:
        clone_cmd.extend(["-e", key_var])
    if known_hosts_var:
        clone_cmd.extend(["-e", known_hosts_var])
    clone_cmd.extend(["--entrypoint", "sh", "alpine/git", "-lc", setup_script])

    print(f"Cloning {project.repo} into remote volume '{vol_name}'...")
    result = subprocess.run(clone_cmd, env=clone_env)
    if result.returncode != 0:
        print("Error: Failed to clone repository into remote volume.")
        print("  Tip: Confirm repository access for the configured SSH key and remote host network reachability.")
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


def _seed_auth_into_remote_volume(project_name: str, agent_name: str, cred, agent, overwrite: bool = False) -> int:
    """Seed auth files from local host into a remote Docker named volume."""
    sources = resolve_credential_sources(cred, agent)
    copied = 0
    vol_name = f"skua-{project_name}-{agent_name}"

    for src, dest_name in sources:
        if not src.is_file():
            continue

        safe_dest = Path(dest_name).name.strip()
        if not safe_dest:
            continue

        if not overwrite:
            exists = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{vol_name}:/auth", "alpine", "test", "-f", f"/auth/{safe_dest}"],
                capture_output=True,
                text=True,
            )
            if exists.returncode == 0:
                continue

        payload = base64.b64encode(src.read_bytes()).decode("ascii")
        copy_env = os.environ.copy()
        copy_env["SKUA_AUTH_DEST"] = safe_dest
        copy_env["SKUA_AUTH_FILE_B64"] = payload
        copy_cmd = [
            "docker", "run", "--rm",
            "-v", f"{vol_name}:/auth",
            "-e", "SKUA_AUTH_DEST",
            "-e", "SKUA_AUTH_FILE_B64",
            "alpine", "sh", "-lc",
            'set -eu; printf "%s" "$SKUA_AUTH_FILE_B64" | base64 -d > "/auth/$SKUA_AUTH_DEST"; chmod 600 "/auth/$SKUA_AUTH_DEST"',
        ]
        result = subprocess.run(copy_cmd, env=copy_env)
        if result.returncode == 0:
            copied += 1
        else:
            print(f"Warning: failed to sync remote auth file '{safe_dest}' into volume '{vol_name}'.")

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
        _ensure_local_ssh_client_for_remote_docker(host)
        _configure_remote_docker_transport(host)

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
    elif host:
        refreshed = _maybe_refresh_local_credentials(agent=agent, cred=cred)
        copied = _seed_auth_into_remote_volume(
            project_name=name,
            agent_name=project.agent,
            cred=cred,
            agent=agent,
            overwrite=refreshed,
        )
        if copied:
            action = "Synced" if refreshed else "Seeded"
            print(f"{action} {copied} remote auth file(s).")

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
