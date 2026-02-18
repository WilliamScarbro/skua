# SPDX-License-Identifier: BUSL-1.1
"""skua run — start or attach to a container for a project."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from skua.config import ConfigStore, validate_project
from skua.docker import (
    is_container_running,
    exec_into_container,
    build_run_command,
    build_image,
    image_exists,
    image_name_for_project,
    resolve_project_image_inputs,
    run_container,
)
from skua.project_adapt import ensure_adapt_workspace


def _seed_auth_from_host(data_dir: Path, auth_dir: str, auth_files: list) -> int:
    """Seed missing persisted auth files from host HOME.

    For each auth file, checks:
      1) ~/auth_dir/<file>
      2) ~/<file>
    and copies the first match into data_dir.
    """
    copied = 0
    home = Path.home()
    rel_auth_dir = (auth_dir or ".claude").lstrip("/")
    codex_home = os.environ.get("CODEX_HOME", "").strip()

    for fname in auth_files or []:
        name = Path(fname).name
        if not name:
            continue

        dest = data_dir / name
        if dest.exists():
            continue

        candidates = [home / rel_auth_dir / name]
        if rel_auth_dir == ".codex" and codex_home:
            candidates.append(Path(codex_home).expanduser() / name)
        candidates.append(home / name)
        for src in candidates:
            if src.is_file():
                shutil.copy2(src, dest)
                copied += 1
                break

    return copied


def cmd_run(args):
    store = ConfigStore()
    name = args.name

    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found. Add it with: skua add {name}")
        sys.exit(1)

    container_name = f"skua-{name}"

    # Check if already running
    if is_container_running(container_name):
        print(f"Container '{container_name}' is already running.")
        answer = input("Attach to it? [Y/n]: ").strip().lower()
        if answer != "n":
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

    # Clone repo if needed
    if project.repo:
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

    if project.directory and Path(project.directory).is_dir():
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
    auth_dir = (agent.auth.dir or ".claude").lstrip("/")
    auth_files = list(agent.auth.files)
    # Backward-compat for older installed presets that had no auth.files.
    if not auth_files:
        if project.agent == "codex":
            auth_files = ["auth.json"]
        elif project.agent == "claude":
            auth_files = [".credentials.json", ".claude.json"]

    # Seed persisted auth files from host if needed
    if env.persistence.mode == "bind":
        data_dir.mkdir(parents=True, exist_ok=True)
        copied = _seed_auth_from_host(
            data_dir=data_dir,
            auth_dir=auth_dir,
            auth_files=auth_files,
        )
        if copied:
            print(f"Seeded {copied} auth file(s) from host.")

    # Build and exec docker command
    docker_cmd = build_run_command(
        project=project,
        environment=env,
        security=sec,
        agent=agent,
        image_name=image_name,
        data_dir=data_dir,
    )

    # Print summary
    print(f"Starting skua-{name}...")
    print(f"  Project:     {project.directory or '(none)'}")
    print(f"  Environment: {project.environment}")
    print(f"  Security:    {project.security}")
    print(f"  Agent:       {project.agent}")
    print(f"  Image:       {image_name}")
    ssh_display = Path(project.ssh.private_key).name if project.ssh.private_key else "(none)"
    print(f"  SSH key:     {ssh_display}")
    print(f"  Network:     {env.network.mode}")
    if env.persistence.mode == "bind":
        print(f"  Auth dir:    {data_dir} -> /home/dev/{auth_dir}")
    else:
        print(f"  Auth dir:    volume skua-{name}-{project.agent} -> /home/dev/{auth_dir}")
    print()

    run_container(docker_cmd)
