# SPDX-License-Identifier: BUSL-1.1
"""skua build — ensure required project Docker images exist."""
import sys

from skua.config import ConfigStore
from skua.docker import build_image, image_exists, image_name_for_agent, base_image_for_agent


def _required_project_agents(store: ConfigStore) -> list:
    """Return sorted unique agent names required by configured projects."""
    required = set()
    for name in store.list_resources("Project"):
        project = store.resolve_project(name)
        if project and project.agent:
            required.add(project.agent)
    return sorted(required)


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
    required_agent_names = _required_project_agents(store)
    if not required_agent_names:
        print("No projects configured, so no agent images are required.")
        print("Add a project with 'skua add <name> --dir <path>' or --repo first.")
        return

    agents = []
    missing_agents = []
    for name in required_agent_names:
        agent = store.load_agent(name)
        if agent is None:
            missing_agents.append(name)
        else:
            agents.append(agent)

    if missing_agents:
        print("Error: missing agent configs referenced by projects:")
        for name in missing_agents:
            print(f"  - {name}")
        print("Run 'skua init' to install default presets, or fix project configs.")
        sys.exit(1)

    # Collect extra packages/commands from global config
    image_config = g.get("image", {})
    extra_packages = image_config.get("extraPackages", [])
    extra_commands = image_config.get("extraCommands", [])

    print("Building Docker images...")
    print(f"  Base image:  {base_image}")
    print(f"  Image base:  {image_name_base}")
    print(f"  Security:    {security_name}")
    print(f"  Agents:      {', '.join(a.name for a in agents)}")
    if extra_packages:
        print(f"  Extra pkgs:  {', '.join(extra_packages)}")
    print(f"  Source:      {container_dir}")
    print()

    built = []
    existing = []
    failed = []
    for agent in agents:
        image_name = image_name_for_agent(image_name_base, agent.name)
        agent_base_image = base_image_for_agent(base_image, agent)
        if image_exists(image_name):
            print(f"-> Using existing image '{image_name}'")
            existing.append(image_name)
            continue

        print(f"-> Image '{image_name}' missing; building for agent '{agent.name}' from '{agent_base_image}'...")
        success = build_image(
            container_dir=container_dir,
            image_name=image_name,
            security=security,
            agent=agent,
            base_image=agent_base_image,
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
    print("Run 'skua add <name> --dir <path>' to add a project.")
