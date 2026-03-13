# SPDX-License-Identifier: BUSL-1.1
"""skua ssh — manage project SSH key configuration."""

import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.utils import choose_ssh_key


def cmd_ssh(args):
    action = args.action
    if action == "add":
        _cmd_add(args)
        return
    print(f"Unknown action: {action}")
    sys.exit(1)


def _cmd_add(args):
    store = ConfigStore()

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    project = store.load_project(args.name)
    if project is None:
        print(f"Error: Project '{args.name}' not found.")
        print("Run 'skua list' to see configured projects.")
        sys.exit(1)

    if getattr(args, "clear", False):
        ssh_key = ""
    else:
        ssh_key = getattr(args, "ssh_key", None) or ""
        if not ssh_key:
            if getattr(args, "no_prompt", False):
                print("Error: --ssh-key is required with --no-prompt unless using --clear.")
                sys.exit(1)
            default_key = project.ssh.private_key or store.get_global_defaults().get("sshKey", "")
            ssh_key = choose_ssh_key(default_key)

    if ssh_key:
        ssh_key = str(Path(ssh_key).expanduser().resolve())
        if not Path(ssh_key).is_file():
            print(f"Warning: SSH key not found: {ssh_key}")

    project.ssh.private_key = ssh_key
    store.save_resource(project)

    if ssh_key:
        print(f"Project '{project.name}' SSH key set to: {ssh_key}")
    else:
        print(f"Project '{project.name}' SSH key cleared.")
