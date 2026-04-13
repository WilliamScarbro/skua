# SPDX-License-Identifier: BUSL-1.1
"""skua remove — remove a project configuration."""

import shutil
import subprocess
import sys

from skua.config import ConfigStore
from skua.docker import image_name_for_agent, is_container_running
from skua.project_lock import ProjectBusyError, format_project_busy_error, project_operation_lock
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


def cmd_remove(args, lock_project: bool = True):
    store = ConfigStore()
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        print("Error: Provide a project name.")
        sys.exit(1)

    if lock_project:
        try:
            with project_operation_lock(store, name, "removing"):
                return cmd_remove(args, lock_project=False)
        except ProjectBusyError as exc:
            print(format_project_busy_error(exc, "remove this project"))
            sys.exit(1)

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
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    base_agent_image = image_name_for_agent(image_name_base, project.agent)
    other_projects = [p for p in store.load_all_resources("Project") if p.name != name]
    images_in_use_elsewhere = {img for p in other_projects for img in (p.resources.images or [])}
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
        repo_vols = []
        if getattr(project, "sources", None):
            from skua.commands.run import _project_sources, _source_volume_name
            for index, source in enumerate(_project_sources(project)):
                if getattr(source, "repo", ""):
                    repo_vols.append(_source_volume_name(name, source, index))
        elif project.repo:
            repo_vols.append(f"skua-{name}-repo")
        project_images = [
            img for img in (project.resources.images or [])
            if img != base_agent_image and img not in images_in_use_elsewhere
        ]

        print("Remote cleanup targets:")
        print(f"  Container: {container_name}")
        print(f"  Auth vol:  {auth_vol}")
        for repo_vol in repo_vols:
            print(f"  Repo vol:  {repo_vol}")
        for img in project_images:
            print(f"  Image:     {img}")

        if confirm("Also remove remote Docker resources now?", default=True):
            _run_docker_remove(["docker", "rm", "-f", container_name], f"remote container '{container_name}'")
            _run_docker_remove(["docker", "volume", "rm", auth_vol], f"remote volume '{auth_vol}'")
            for repo_vol in repo_vols:
                _run_docker_remove(["docker", "volume", "rm", repo_vol], f"remote volume '{repo_vol}'")
            for img in project_images:
                _run_docker_remove(["docker", "rmi", img], f"remote image '{img}'")
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

        project_images = [
            img for img in (project.resources.images or [])
            if img != base_agent_image and img not in images_in_use_elsewhere
        ]
        if project_images:
            img_list = ", ".join(project_images)
            if confirm(f"Also remove project image(s) ({img_list})?"):
                for img in project_images:
                    if _run_docker_remove(["docker", "rmi", img], f"image '{img}'"):
                        print(f"  Image removed: {img}")

        if getattr(project, "sources", None):
            from skua.commands.run import _project_sources, _sanitize_mount_name
            for source in _project_sources(project):
                if getattr(source, "repo", "") and not getattr(source, "host", ""):
                    clone_key = (getattr(source, "project", "") or getattr(source, "name", "") or "repo")
                    clone_dir = store.repo_dir(f"{name}-{_sanitize_mount_name(clone_key)}")
                    if clone_dir.exists():
                        if confirm(f"Also remove local repo clone at {clone_dir}?"):
                            shutil.rmtree(clone_dir)
                            print(f"  Repo clone removed: {clone_dir}")

    # Remove project resource file
    store.delete_resource("Project", name)
    print(f"Project '{name}' removed from config.")
