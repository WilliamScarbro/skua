# SPDX-License-Identifier: BUSL-1.1
"""skua update — update a project configuration."""

import sys

from skua.config import ConfigStore


def cmd_update(args):
    store = ConfigStore()

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    name = args.name
    project = store.load_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        available = store.list_resources("Project")
        if available:
            print("Available projects:")
            for p in available:
                print(f"  {p}")
        sys.exit(1)

    updated = False

    image = getattr(args, "image", None)
    if image is not None:
        project.image.base_image = image
        updated = True

    if not updated:
        print("No changes specified. Use --image to set the Docker base image.")
        print(f"  skua update {name} --image <image>")
        sys.exit(1)

    store.save_resource(project)
    print(f"Project '{name}' updated.")
    if image is not None:
        label = "Image:"
        print(f"  {label:<14} {image or '(cleared)'}")
