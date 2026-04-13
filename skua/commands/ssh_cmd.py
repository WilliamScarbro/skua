# SPDX-License-Identifier: BUSL-1.1
"""skua ssh — manage project SSH key configuration."""

import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.config.resources import normalize_project_ssh, ssh_private_keys
from skua.utils import choose_ssh_key, select_option


def cmd_ssh(args):
    action = args.action
    if action == "list":
        _cmd_list(args)
        return
    if action == "add":
        _cmd_add(args)
        return
    if action == "remove":
        _cmd_remove(args)
        return
    print(f"Unknown action: {action}")
    sys.exit(1)


def _load_project_or_die(store: ConfigStore, name: str):
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    project = store.load_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        print("Run 'skua list' to see configured projects.")
        sys.exit(1)
    project.ssh = normalize_project_ssh(project.ssh)
    return project


def _resolve_key_path(ssh_key: str) -> str:
    resolved = str(Path(ssh_key).expanduser().resolve())
    if not Path(resolved).is_file():
        print(f"Warning: SSH key not found: {resolved}")
    return resolved


def _save_project_ssh(store: ConfigStore, project):
    project.ssh = normalize_project_ssh(project.ssh)
    store.save_resource(project)


def _cmd_list(args):
    store = ConfigStore()
    project = _load_project_or_die(store, args.name)
    keys = ssh_private_keys(project.ssh)

    print(f"Project '{project.name}' SSH keys:")
    if not keys:
        print("  (none)")
        return

    for idx, key in enumerate(keys, start=1):
        marker = " (primary)" if idx == 1 else ""
        print(f"  {idx}. {key}{marker}")


def _cmd_add(args):
    store = ConfigStore()
    project = _load_project_or_die(store, args.name)
    existing = ssh_private_keys(project.ssh)

    ssh_key = getattr(args, "ssh_key", None) or ""
    if not ssh_key:
        if getattr(args, "no_prompt", False):
            print("Error: --ssh-key is required with --no-prompt.")
            sys.exit(1)
        default_key = project.ssh.private_key or store.get_global_defaults().get("sshKey", "")
        ssh_key = choose_ssh_key(default_key)

    if ssh_key:
        ssh_key = _resolve_key_path(ssh_key)
    else:
        print("No SSH key selected.")
        return

    if ssh_key in existing:
        print(f"Project '{project.name}' already has SSH key: {ssh_key}")
        return

    project.ssh.private_keys = existing + [ssh_key]
    if not project.ssh.private_key:
        project.ssh.private_key = ssh_key
    _save_project_ssh(store, project)

    primary = " (primary)" if project.ssh.private_key == ssh_key else ""
    print(f"Project '{project.name}' SSH key added: {ssh_key}{primary}")


def _cmd_remove(args):
    store = ConfigStore()
    project = _load_project_or_die(store, args.name)
    existing = ssh_private_keys(project.ssh)
    if not existing:
        print(f"Project '{project.name}' has no SSH keys configured.")
        return

    if getattr(args, "all", False):
        project.ssh.private_key = ""
        project.ssh.private_keys = []
        _save_project_ssh(store, project)
        print(f"Project '{project.name}' SSH keys cleared.")
        return

    ssh_key = getattr(args, "ssh_key", None) or ""
    if ssh_key:
        ssh_key = _resolve_key_path(ssh_key)
    else:
        if getattr(args, "no_prompt", False):
            print("Error: --ssh-key is required with --no-prompt unless using --all.")
            sys.exit(1)
        selected = select_option(
            "Select SSH private key to remove:",
            existing + ["Cancel"],
            default_index=0,
        )
        if selected == "Cancel":
            print("No SSH key selected.")
            return
        if not selected:
            print("No SSH key selected.")
            return
        ssh_key = _resolve_key_path(selected)

    if ssh_key not in existing:
        print(f"Error: Project '{project.name}' does not have SSH key: {ssh_key}")
        sys.exit(1)

    project.ssh.private_keys = [key for key in existing if key != ssh_key]
    project.ssh.private_key = project.ssh.private_keys[0] if project.ssh.private_keys else ""
    _save_project_ssh(store, project)
    print(f"Project '{project.name}' SSH key removed: {ssh_key}")
