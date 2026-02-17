# SPDX-License-Identifier: BUSL-1.1
"""skua prep — apply project image requests from template/flags."""

import subprocess
import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.docker import (
    build_image,
    image_exists,
    image_name_for_project,
    resolve_project_image_inputs,
)
from skua.project_prep import (
    ensure_prep_workspace,
    load_image_request,
    request_has_updates,
    apply_image_request_to_project,
    write_applied_image_request,
)


def cmd_prep(args):
    store = ConfigStore()
    name = args.name

    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        sys.exit(1)

    env = store.load_environment(project.environment)
    if env is None:
        print(f"Error: Environment '{project.environment}' not found.")
        sys.exit(1)
    if env.mode != "unmanaged":
        print(
            f"Error: skua prep currently supports unmanaged mode only "
            f"(project uses mode '{env.mode}')."
        )
        sys.exit(1)

    agent = store.load_agent(project.agent)
    if agent is None:
        print(f"Error: Agent '{project.agent}' not found.")
        sys.exit(1)

    project_dir = _ensure_project_directory(store, project)
    if project_dir is None:
        print("Error: project directory is not set.")
        sys.exit(1)

    guide_path, request_path = ensure_prep_workspace(project_dir, project.name, project.agent)
    print(f"Prep guide:    {guide_path}")
    print(f"Request file:  {request_path}")

    request_from_flags = _request_from_flags(args)
    if request_from_flags is not None:
        request = request_from_flags
        request_source = "flags"
    else:
        request = load_image_request(request_path)
        request_source = str(request_path)

    if args.write_only:
        print("Prep files ensured. No image config was applied.")
        return

    if args.clear:
        request = {
            "schemaVersion": 1,
            "status": "ready",
            "summary": "Reset project image customization.",
            "baseImage": "",
            "fromImage": "",
            "packages": [],
            "commands": [],
        }

    if not args.clear and not request_has_updates(request):
        print("No requested image changes found.")
        print("Ask your agent to update the request template, then run this command again.")
        return

    changed = apply_image_request_to_project(project, request)
    if not changed:
        print("Project image configuration already matches request; no changes applied.")
    else:
        store.save_resource(project)
        print(f"Applied image request from: {request_source}")
        print(f"Project image version: v{project.image.version}")
        _print_project_image_summary(project)
        if request_source != "flags":
            write_applied_image_request(request_path, request, project.image.version)

    if args.build:
        _build_project_image(store, project, agent)
    else:
        print(f"Next: run 'skua run {project.name}' to build/use the updated image.")


def _ensure_project_directory(store: ConfigStore, project) -> Path:
    """Ensure project.directory exists; clone repo when needed."""
    if project.repo:
        clone_dir = store.repo_dir(project.name)
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
        project.directory = str(clone_dir)

    if not project.directory:
        return None

    p = Path(project.directory).expanduser().resolve()
    if not p.is_dir():
        print(f"Error: Project directory does not exist: {p}")
        sys.exit(1)
    return p


def _request_from_flags(args):
    """Build an image request from CLI flags, or None when no request flags given."""
    has_flag_request = bool(
        args.base_image
        or args.from_image
        or args.package
        or args.extra_command
    )
    if not has_flag_request:
        return None
    return {
        "schemaVersion": 1,
        "status": "ready",
        "summary": "Applied from skua prep CLI flags.",
        "baseImage": args.base_image or "",
        "fromImage": args.from_image or "",
        "packages": list(args.package or []),
        "commands": list(args.extra_command or []),
    }


def _print_project_image_summary(project):
    img = project.image
    print("Resolved image config:")
    print(f"  fromImage:    {img.from_image or '(none)'}")
    print(f"  baseImage:    {img.base_image or '(none)'}")
    print(f"  packages:     {', '.join(img.extra_packages) if img.extra_packages else '(none)'}")
    print(f"  commands:     {len(img.extra_commands)} command(s)")


def _build_project_image(store: ConfigStore, project, agent):
    """Build the prepared project image immediately."""
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    image_name = image_name_for_project(image_name_base, project)
    if image_exists(image_name):
        print(f"Image already exists: {image_name}")
        return

    container_dir = store.get_container_dir()
    if container_dir is None:
        print("Error: Cannot find container build assets (entrypoint.sh).")
        print("Set toolDir in global.yaml or reinstall skua.")
        sys.exit(1)

    base_image = g.get("baseImage", "debian:bookworm-slim")
    image_config = g.get("image", {})
    global_packages = image_config.get("extraPackages", [])
    global_commands = image_config.get("extraCommands", [])
    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=base_image,
        agent=agent,
        project=project,
        global_extra_packages=global_packages,
        global_extra_commands=global_commands,
    )
    defaults = g.get("defaults", {})
    build_security_name = defaults.get("security", "open")
    build_security = store.load_security(build_security_name)

    print(f"Building image: {image_name}")
    print(f"  Base image: {resolved_base_image}")
    if extra_packages:
        print(f"  Packages:   {', '.join(extra_packages)}")
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
