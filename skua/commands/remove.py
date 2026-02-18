# SPDX-License-Identifier: BUSL-1.1
"""skua remove — remove a project configuration."""

import shutil
import subprocess
import sys

from skua.config import ConfigStore
from skua.docker import is_container_running, image_name_for_project
from skua.utils import confirm


def _run_docker_remove(cmd: list, label: str) -> bool:
    """Run a docker remove command and print a warning on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"Warning: docker not found; skipping {label}.")
        return False
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "unknown error"
        print(f"Warning: failed to remove {label}: {err}")
        return False
    return True


def cmd_remove(args):
    store = ConfigStore()
    name = args.name

    project = store.load_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        sys.exit(1)

    host = getattr(project, "host", "") or ""
    if host:
        from skua.commands.run import (
            _ensure_local_ssh_client_for_remote_docker,
            _configure_remote_docker_transport,
        )
        _ensure_local_ssh_client_for_remote_docker(host)
        _configure_remote_docker_transport(host)

    env = store.load_environment(project.environment)
    container_name = f"skua-{name}"
    if is_container_running(container_name):
        if host:
            if confirm(f"Remote container '{container_name}' is running. Stop and remove it?", default=True):
                _run_docker_remove(["docker", "rm", "-f", container_name], f"remote container '{container_name}'")
            else:
                print("Remove cancelled.")
                return
        else:
            print(f"Error: Container '{container_name}' is running. Stop it first.")
            sys.exit(1)

    if host:
        auth_vol = f"skua-{name}-{project.agent}"
        repo_vol = f"skua-{name}-repo" if project.repo else ""
        image_base = store.load_global().get("imageName", "skua-base")
        image_name = image_name_for_project(image_base, project)

        print("Remote cleanup targets:")
        print(f"  Container: {container_name}")
        print(f"  Auth vol:  {auth_vol}")
        if repo_vol:
            print(f"  Repo vol:  {repo_vol}")
        print(f"  Image:     {image_name}")

        if confirm("Also remove remote Docker resources now?", default=True):
            _run_docker_remove(["docker", "rm", "-f", container_name], f"remote container '{container_name}'")
            _run_docker_remove(["docker", "volume", "rm", auth_vol], f"remote volume '{auth_vol}'")
            if repo_vol:
                _run_docker_remove(["docker", "volume", "rm", repo_vol], f"remote volume '{repo_vol}'")
            _run_docker_remove(["docker", "image", "rm", "-f", image_name], f"remote image '{image_name}'")
    else:
        # Offer to clean local data
        persist_mode = env.persistence.mode if env else "bind"
        if persist_mode == "bind":
            data_dir = store.project_data_dir(name, project.agent)
            if data_dir.exists():
                if confirm(f"Also remove {project.agent} data at {data_dir}?"):
                    shutil.rmtree(data_dir)
                    print("  Agent data removed.")
        else:
            vol_name = f"skua-{name}-{project.agent}"
            if confirm(f"Also remove Docker volume '{vol_name}'?"):
                _run_docker_remove(["docker", "volume", "rm", vol_name], f"volume '{vol_name}'")
                print("  Docker volume removed.")

    # Remove project resource file
    store.delete_resource("Project", name)
    print(f"Project '{name}' removed from config.")
