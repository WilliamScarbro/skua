# SPDX-License-Identifier: BUSL-1.1
"""skua build — ensure required project Docker images exist."""
import sys

from skua.config import ConfigStore
from skua.docker import (
    build_image,
    image_exists,
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
    image_name_base = g.get("imageName", "skua-base")
    base_image = g.get("baseImage", "debian:bookworm-slim")

    # Load security config
    defaults = g.get("defaults", {})
    security_name = defaults.get("security", "open")
    security = store.load_security(security_name)
    required_projects = _required_projects(store)
    if not required_projects:
        print("No projects configured, so no agent images are required.")
        print("Add a project with 'skua add <name> --dir <path>' or --repo first.")
        return

    missing_agents = set()
    project_specs = []
    for project in required_projects:
        agent = store.load_agent(project.agent)
        if agent is None:
            missing_agents.add(project.agent)
            continue
        project_specs.append((project, agent))

    if missing_agents:
        print("Error: missing agent configs referenced by projects:")
        for name in sorted(missing_agents):
            print(f"  - {name}")
        print("Run 'skua init' to install default presets, or fix project configs.")
        sys.exit(1)

    # Collect extra packages/commands from global config
    image_config = g.get("image", {})
    global_packages = image_config.get("extraPackages", [])
    global_commands = image_config.get("extraCommands", [])

    print("Building Docker images...")
    print(f"  Base image:  {base_image}")
    print(f"  Image base:  {image_name_base}")
    print(f"  Security:    {security_name}")
    print(f"  Projects:    {len(project_specs)}")
    if global_packages:
        print(f"  Global pkgs: {', '.join(global_packages)}")
    print(f"  Source:      {container_dir}")
    print()

    built = []
    existing = []
    failed = []
    seen_images = set()
    for project, agent in project_specs:
        image_name = image_name_for_project(image_name_base, project)
        if image_name in seen_images:
            continue
        seen_images.add(image_name)
        resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
            default_base_image=base_image,
            agent=agent,
            project=project,
            global_extra_packages=global_packages,
            global_extra_commands=global_commands,
        )
        if image_exists(image_name):
            print(f"-> Using existing image '{image_name}' (project: {project.name})")
            existing.append(image_name)
            continue

        print(
            f"-> Image '{image_name}' missing; building for project "
            f"'{project.name}' (agent '{agent.name}') from '{resolved_base_image}'..."
        )
        success = build_image(
            container_dir=container_dir,
            image_name=image_name,
            security=security,
            agent=agent,
            base_image=resolved_base_image,
            extra_packages=extra_packages,
            extra_commands=extra_commands,
        )
        if success:
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
    print("Use 'skua adapt <name>' to apply project image-request updates before running.")
