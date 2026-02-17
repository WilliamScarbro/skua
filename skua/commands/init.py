# SPDX-License-Identifier: BUSL-1.1
"""skua init — first-run wizard and preset installation."""

import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.utils import detect_git_identity, find_ssh_keys


def cmd_init(args):
    store = ConfigStore()

    if store.is_initialized() and not getattr(args, "force", False):
        print("Skua is already initialized.")
        print(f"  Config: {store.config_dir}")
        print("  Use --force to re-initialize.")
        return

    print("============================================")
    print("  skua — first-time setup")
    print("============================================")
    print()

    # Git identity
    default_name, default_email = detect_git_identity()
    if default_name:
        prompt_name = f"Git user name [{default_name}]: "
    else:
        prompt_name = "Git user name: "
    if default_email:
        prompt_email = f"Git user email [{default_email}]: "
    else:
        prompt_email = "Git user email: "

    git_name = input(prompt_name).strip() or default_name
    git_email = input(prompt_email).strip() or default_email

    if not git_name or not git_email:
        print("Error: Git name and email are required.")
        sys.exit(1)

    # SSH key
    ssh_key = ""
    keys = find_ssh_keys()
    if keys:
        print("\nAvailable SSH keys:")
        for k in keys:
            print(f"  {k}")
        print()
    ssh_input = input("SSH private key path (leave empty to skip): ").strip()
    if ssh_input:
        ssh_key = str(Path(ssh_input).expanduser().resolve())
        if not Path(ssh_key).is_file():
            print(f"Warning: {ssh_key} not found, skipping.")
            ssh_key = ""

    # Install shipped presets
    preset_dir = Path(__file__).resolve().parent.parent / "presets"
    store.install_presets(preset_dir)
    print("\n[OK] Installed default presets")

    # Save global config
    global_data = {
        "git": {
            "name": git_name,
            "email": git_email,
        },
        "defaults": {
            "environment": "local-docker",
            "security": "open",
            "agent": "claude",
        },
    }
    if ssh_key:
        global_data["defaults"]["sshKey"] = ssh_key

    # Detect container asset directory (entrypoint.sh is the anchor file)
    container_dir = Path(__file__).resolve().parent.parent / "container"
    if (container_dir / "entrypoint.sh").exists():
        global_data["toolDir"] = str(container_dir)

    store.save_global(global_data)
    print(f"[OK] Configuration saved to {store.config_dir}")

    print()
    print("Next steps:")
    print("  skua build                          Build the Docker image")
    print("  skua add <name> --dir /path          Add a project")
    print("  skua run <name>                      Start a container")
    print()
    print("Available security profiles:")
    for name in store.list_resources("SecurityProfile"):
        print(f"  {name}")
