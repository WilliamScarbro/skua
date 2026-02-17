# SPDX-License-Identifier: BUSL-1.1
"""skua config — show or edit global configuration."""

from pathlib import Path

from skua.config import ConfigStore


def cmd_config(args):
    store = ConfigStore()
    g = store.load_global()
    git = g.setdefault("git", {})
    defaults = g.setdefault("defaults", {})
    changed = False

    if args.git_name:
        git["name"] = args.git_name
        changed = True
    if args.git_email:
        git["email"] = args.git_email
        changed = True
    if args.tool_dir:
        p = Path(args.tool_dir).expanduser().resolve()
        if not (p / "Dockerfile").exists():
            print(f"Warning: No Dockerfile found in {p}")
        g["toolDir"] = str(p)
        changed = True
    if args.ssh_key:
        p = Path(args.ssh_key).expanduser().resolve()
        if not p.is_file():
            print(f"Warning: SSH key not found: {p}")
        defaults["sshKey"] = str(p)
        changed = True
    if args.default_env:
        defaults["environment"] = args.default_env
        changed = True
    if args.default_security:
        defaults["security"] = args.default_security
        changed = True
    if args.default_agent:
        defaults["agent"] = args.default_agent
        changed = True

    if changed:
        store.save_global(g)
        print("Config updated.")

    # Display current config
    print(f"\nGlobal config ({store.global_file}):")
    print(f"  git.name:            {git.get('name', '(not set)')}")
    print(f"  git.email:           {git.get('email', '(not set)')}")
    print(f"  toolDir:             {g.get('toolDir', '(auto-detect)')}")
    print(f"  imageName:           {g.get('imageName', 'skua-base')}")
    print(f"  defaults.sshKey:     {defaults.get('sshKey', '(not set)')}")
    print(f"  defaults.environment:{defaults.get('environment', 'local-docker')}")
    print(f"  defaults.security:   {defaults.get('security', 'open')}")
    print(f"  defaults.agent:      {defaults.get('agent', 'claude')}")
