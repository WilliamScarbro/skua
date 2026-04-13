# SPDX-License-Identifier: BUSL-1.1
"""skua default-image — manage named, prebuilt default images."""

import subprocess
import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.config.resources import DefaultImage
from skua.docker import (
    build_image,
    ensure_agent_base_image,
    image_exists,
    image_name_for_project,
    resolve_project_image_inputs,
)


def cmd_default_image(args):
    action = args.action
    if action == "list":
        _cmd_list(args)
    elif action == "build":
        _cmd_build(args)
    elif action == "save":
        _cmd_save(args)
    elif action == "remove":
        _cmd_remove(args)
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


# ── list ──────────────────────────────────────────────────────────────────

def _cmd_list(args):
    store = ConfigStore()
    defaults = store.load_all_resources("DefaultImage")
    if not defaults:
        print("No default images configured.")
        print("Build one with: skua default-image build <name>")
        print("Or save an existing project image: skua default-image save <project> <name>")
        return

    col_name = max(len(d.name) for d in defaults)
    col_name = max(col_name, 4)
    col_agent = max((len(d.agent) for d in defaults), default=5)
    col_agent = max(col_agent, 5)
    col_image = max((len(d.image) for d in defaults), default=5)
    col_image = max(col_image, 5)

    header = f"{'NAME':<{col_name}}  {'AGENT':<{col_agent}}  {'IMAGE':<{col_image}}  DESCRIPTION"
    print(header)
    print("-" * len(header))
    for d in defaults:
        exists_marker = "" if image_exists(d.image) else " [missing]"
        print(
            f"{d.name:<{col_name}}  {(d.agent or '-'):<{col_agent}}  "
            f"{d.image:<{col_image}}  {d.description}{exists_marker}"
        )


# ── build ─────────────────────────────────────────────────────────────────

def _cmd_build(args):
    store = ConfigStore()
    name = args.name

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    container_dir = store.get_container_dir()
    if container_dir is None:
        print("Error: Cannot find container build assets (entrypoint.sh).")
        sys.exit(1)

    g = store.load_global()
    global_base_image = g.get("baseImage", "debian:bookworm-slim")
    image_name_base = g.get("imageName", "skua-base")
    image_config = g.get("image", {})
    global_packages = image_config.get("extraPackages", [])
    global_commands = image_config.get("extraCommands", [])
    defaults = g.get("defaults", {})
    security_name = defaults.get("security", "open")
    security = store.load_security(security_name)

    # Load existing resource if present, then apply CLI overrides
    existing = store.load_default_image(name)
    agent_name = getattr(args, "agent", None) or (existing.agent if existing else "") or defaults.get("agent", "claude")
    description = getattr(args, "description", None) or (existing.description if existing else "")
    base_image = getattr(args, "base_image", None) or (existing.base_image if existing else "") or global_base_image

    extra_packages = list(getattr(args, "package", None) or (existing.extra_packages if existing else []))
    extra_commands = list(getattr(args, "extra_command", None) or (existing.extra_commands if existing else []))

    agent = store.load_agent(agent_name)
    if agent is None:
        print(f"Error: Agent '{agent_name}' not found.")
        sys.exit(1)

    # The docker image name for this default image
    target_image = getattr(args, "image", None) or (existing.image if existing else "") or f"skua-default-{name}"

    merged_packages = _merge_unique(global_packages + extra_packages)
    merged_commands = _merge_unique(global_commands + extra_commands)

    print(f"Building default image '{name}'...")
    print(f"  Agent:       {agent_name}")
    print(f"  Base image:  {base_image}")
    print(f"  Target:      {target_image}")
    if merged_packages:
        print(f"  Packages:    {', '.join(merged_packages)}")
    print()

    success, output = build_image(
        container_dir=container_dir,
        image_name=target_image,
        security=security,
        agent=agent,
        base_image=base_image,
        extra_packages=merged_packages,
        extra_commands=merged_commands,
        quiet=False,
        verbose=getattr(args, "verbose", False),
    )

    if not success:
        print(f"Error: build failed.")
        if output:
            print(output)
        sys.exit(1)

    print(f"Built: {target_image}")

    default_img = DefaultImage(
        name=name,
        image=target_image,
        description=description,
        agent=agent_name,
        base_image=base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
    )
    store.save_resource(default_img)
    print(f"Saved default image '{name}'.")
    print(f"\nUse with: skua add <project> --default-image {name}")


# ── save ──────────────────────────────────────────────────────────────────

def _cmd_save(args):
    store = ConfigStore()
    source = args.source
    new_name = args.name
    description = getattr(args, "description", None) or ""
    agent_name = getattr(args, "agent", None) or ""

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    # Resolve the source Docker image
    source_image = _resolve_source_image(store, source)
    if source_image is None:
        sys.exit(1)

    if not image_exists(source_image):
        print(f"Error: Docker image '{source_image}' does not exist locally.")
        print("Build it first, then save.")
        sys.exit(1)

    # Derive agent from project if source is a project name
    if not agent_name:
        project = store.load_project(source)
        if project:
            agent_name = project.agent

    # Determine the stable Docker image name for the default
    target_image = f"skua-default-{new_name}"

    # Tag the source image with the stable default name
    result = subprocess.run(
        ["docker", "tag", source_image, target_image],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error: failed to tag image: {result.stderr.strip()}")
        sys.exit(1)

    # Check if we're replacing an existing default
    existing = store.load_default_image(new_name)
    if existing:
        print(f"Replacing existing default image '{new_name}' (was: {existing.image}).")
        description = description or existing.description
        agent_name = agent_name or existing.agent

    default_img = DefaultImage(
        name=new_name,
        image=target_image,
        description=description,
        agent=agent_name,
    )
    store.save_resource(default_img)
    print(f"Saved '{source_image}' as default image '{new_name}' ({target_image}).")
    print(f"\nUse with: skua add <project> --default-image {new_name}")


def _resolve_source_image(store, source: str):
    """Return the Docker image name for source (project name or image name)."""
    project = store.load_project(source)
    if project is not None:
        g = store.load_global()
        image_name_base = g.get("imageName", "skua-base")
        image = image_name_for_project(image_name_base, project)
        if not image_exists(image):
            print(f"Error: project '{source}' has no built image ('{image}' not found).")
            print(f"Build it first with: skua build {source}")
            return None
        print(f"Using image from project '{source}': {image}")
        return image

    # Treat as a direct Docker image reference
    return source


# ── remove ────────────────────────────────────────────────────────────────

def _cmd_remove(args):
    store = ConfigStore()
    name = args.name

    existing = store.load_default_image(name)
    if existing is None:
        print(f"Error: Default image '{name}' not found.")
        sys.exit(1)

    store.delete_resource("DefaultImage", name)
    print(f"Removed default image '{name}'.")
    print(f"  Docker image '{existing.image}' was NOT deleted (use 'docker rmi' if needed).")


# ── helpers ───────────────────────────────────────────────────────────────

def _merge_unique(items: list) -> list:
    out = []
    seen = set()
    for item in items or []:
        value = str(item).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
