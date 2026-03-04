# SPDX-License-Identifier: BUSL-1.1
"""skua build — build/refresh a single project's Docker image."""
import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.docker import (
    agent_install_uses_floating_version,
    build_image,
    image_exists,
    image_matches_build_context,
    image_name_for_project,
    resolve_project_image_inputs,
)


def _required_projects(store: ConfigStore) -> list:
    """Return all resolvable projects in stable name order."""
    required = []
    for name in store.list_resources("Project"):
        project = store.resolve_project(name)
        if project:
            required.append(project)
    return required


def cmd_build(args):
    store = ConfigStore()

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    container_dir = store.get_container_dir()
    if container_dir is None:
        print("Error: Cannot find container build assets (entrypoint.sh).")
        print("Set toolDir in global.yaml or reinstall skua.")
        sys.exit(1)

    # Determine image naming and base image
    g = store.load_global()
    preset_dir = Path(__file__).resolve().parent.parent / "presets"
    if preset_dir and preset_dir.exists():
        project_name = getattr(args, "name", "")
        project = store.resolve_project(project_name)
        if project is not None:
            store.refresh_agent_preset(preset_dir, project.agent, overwrite=True)
    image_name_base = g.get("imageName", "skua-base")
    base_image = g.get("baseImage", "debian:bookworm-slim")

    # Load security config
    defaults = g.get("defaults", {})
    security_name = defaults.get("security", "open")
    security = store.load_security(security_name)
    project_name = getattr(args, "name", "")
    project = store.resolve_project(project_name)
    if project is None:
        print(f"Error: Project '{project_name}' not found.")
        print(f"Add it with: skua add {project_name} --dir /path/to/project")
        sys.exit(1)

    agent = store.load_agent(project.agent)
    if agent is None:
        print(f"Error: missing agent config '{project.agent}' for project '{project.name}'.")
        print("Run 'skua init' to install default presets, or fix the project config.")
        sys.exit(1)

    # Collect extra packages/commands from global config
    image_config = g.get("image", {})
    global_packages = image_config.get("extraPackages", [])
    global_commands = image_config.get("extraCommands", [])

    print("Building Docker image...")
    print(f"  Base image:  {base_image}")
    print(f"  Image base:  {image_name_base}")
    print(f"  Security:    {security_name}")
    print(f"  Project:     {project.name}")
    if global_packages:
        print(f"  Global pkgs: {', '.join(global_packages)}")
    print(f"  Source:      {container_dir}")
    print()

    built = []
    rebuilt = []
    existing = []
    failed = []
    image_name = image_name_for_project(image_name_base, project)
    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=base_image,
        agent=agent,
        project=project,
        global_extra_packages=global_packages,
        global_extra_commands=global_commands,
    )
    force_refresh = agent_install_uses_floating_version(agent)
    needs_rebuild = False
    if image_exists(image_name):
        if force_refresh:
            print(
                f"-> Agent '{agent.name}' uses a floating install; rebuilding '{image_name}' "
                "without Docker cache to refresh the client"
            )
            needs_rebuild = True
        elif image_matches_build_context(
            image_name=image_name,
            container_dir=container_dir,
            security=security,
            agent=agent,
            base_image=resolved_base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
        ):
            print(f"-> Using existing image '{image_name}' (project: {project.name})")
            existing.append(image_name)
        else:
            print(f"-> Image '{image_name}' is out-of-date; rebuilding (project: {project.name})")
            needs_rebuild = True

    if not existing:
        if not needs_rebuild:
            print(
                f"-> Image '{image_name}' missing; building for project "
                f"'{project.name}' (agent '{agent.name}') from '{resolved_base_image}'..."
            )
        success, _ = build_image(
            container_dir=container_dir,
            image_name=image_name,
            security=security,
            agent=agent,
            base_image=resolved_base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
            verbose=getattr(args, "verbose", False),
            pull=force_refresh,
            no_cache=force_refresh,
        )
        if success:
            if needs_rebuild:
                rebuilt.append(image_name)
            else:
                built.append(image_name)
        else:
            failed.append(image_name)

    if failed:
        print("\nBuild failed for:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)

    print("\nBuild complete:")
    for name in existing:
        print(f"  - {name} (existing)")
    for name in built:
        print(f"  - {name} (built)")
    for name in rebuilt:
        print(f"  - {name} (rebuilt)")
    print("Use 'skua adapt <name>' to apply project image-request updates before running.")
