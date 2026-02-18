# SPDX-License-Identifier: BUSL-1.1
"""skua add — add a project configuration."""

import sys
from pathlib import Path

from skua.config import ConfigStore, Project
from skua.config.resources import ProjectGitSpec, ProjectSshSpec, ProjectImageSpec
from skua.project_adapt import ADAPT_GUIDE_NAME, ensure_adapt_workspace
from skua.utils import find_ssh_keys


def cmd_add(args):
    store = ConfigStore()

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    name = args.name
    repo_url = getattr(args, "repo", None) or ""

    # Validate name
    if not all(c.isalnum() or c in "-_" for c in name):
        print("Error: Project name must be alphanumeric (hyphens and underscores allowed).")
        sys.exit(1)

    if store.load_project(name) is not None:
        print(f"Error: Project '{name}' already exists. Remove it first with 'skua remove {name}'.")
        sys.exit(1)

    # --dir and --repo are mutually exclusive
    if args.dir and repo_url:
        print("Error: --dir and --repo are mutually exclusive. Specify one or the other.")
        sys.exit(1)

    # Validate repo URL looks like a git URL
    if repo_url and not _is_git_url(repo_url):
        print(f"Error: '{repo_url}' does not look like a git URL.")
        print("  Expected: https://... or git@...:...")
        sys.exit(1)

    g = store.load_global()
    defaults = g.get("defaults", {})
    quick = getattr(args, "quick", False)

    # Keep shipped presets up to date without overwriting user customizations.
    preset_dir = Path(__file__).resolve().parent.parent / "presets"
    if preset_dir.exists():
        store.install_presets(preset_dir, overwrite=False)

    # Project directory
    project_dir = args.dir
    if not project_dir and not repo_url and not quick:
        project_dir = input("Project directory path: ").strip()
    if project_dir:
        project_dir = str(Path(project_dir).expanduser().resolve())
        if not Path(project_dir).is_dir():
            print(f"Error: Directory does not exist: {project_dir}")
            sys.exit(1)

    # SSH key
    ssh_key = args.ssh_key or ""
    if not ssh_key and not quick and not args.no_prompt:
        global_ssh = defaults.get("sshKey", "")
        hint = f" (global default: {Path(global_ssh).name})" if global_ssh else ""
        keys = find_ssh_keys()
        if keys:
            print("\nAvailable SSH keys:")
            for k in keys:
                print(f"  {k}")
            print()
        ssh_key = input(f"SSH private key path{hint} (leave empty for global default): ").strip()

    if ssh_key:
        ssh_key = str(Path(ssh_key).expanduser().resolve())
        if not Path(ssh_key).is_file():
            print(f"Warning: SSH key not found: {ssh_key}")

    # Environment, security, agent references
    env_name = args.env or defaults.get("environment", "local-docker")
    sec_name = args.security or defaults.get("security", "open")
    available_agents = store.list_resources("AgentConfig")
    default_agent = defaults.get("agent", "claude")
    agent_name = args.agent

    if not agent_name and not quick and not args.no_prompt:
        if available_agents:
            print("\nAvailable agents:")
            for a in available_agents:
                print(f"  {a}")
            print()
        agent_input = input(f"Agent [{default_agent}]: ").strip()
        agent_name = agent_input or default_agent

    if not agent_name:
        agent_name = default_agent

    if store.load_agent(agent_name) is None:
        print(f"Error: Agent '{agent_name}' not found.")
        if available_agents:
            print("Available agents:")
            for a in available_agents:
                print(f"  {a}")
        else:
            print("No agent presets installed. Run 'skua init' first.")
        sys.exit(1)

    project = Project(
        name=name,
        directory=project_dir or "",
        repo=repo_url,
        environment=env_name,
        security=sec_name,
        agent=agent_name,
        git=ProjectGitSpec(),
        ssh=ProjectSshSpec(private_key=ssh_key),
        image=ProjectImageSpec(),
    )

    store.save_resource(project)

    # Create persistence dir
    env = store.load_environment(env_name)
    if env and env.persistence.mode == "bind":
        store.project_data_dir(name, agent_name).mkdir(parents=True, exist_ok=True)
    if project_dir and Path(project_dir).is_dir():
        ensure_adapt_workspace(Path(project_dir), name, agent_name)

    # Print summary
    print(f"\nProject '{name}' added.")
    if repo_url:
        print(f"  {'Repo:':<14} {repo_url}")
    print(f"  {'Directory:':<14} {project_dir or '(none)'}")
    print(f"  {'Environment:':<14} {env_name}")
    print(f"  {'Security:':<14} {sec_name}")
    print(f"  {'Agent:':<14} {agent_name}")
    if project_dir and Path(project_dir).is_dir():
        print(f"  {'Adapt guide:':<14} {Path(project_dir) / '.skua' / ADAPT_GUIDE_NAME}")
    if ssh_key:
        print(f"  {'SSH key:':<14} {ssh_key}")
    print(f"\nRun with: skua run {name}")

    # Auto-validate
    _try_validate(store, project)


def _is_git_url(url: str) -> bool:
    """Check if a string looks like a git URL."""
    return (
        url.startswith("https://")
        or url.startswith("http://")
        or url.startswith("git://")
        or url.startswith("git@")
        or url.startswith("ssh://")
    )


def _try_validate(store, project):
    """Run validation and print warnings (non-fatal)."""
    from skua.config.validation import validate_project as validate

    env = store.load_environment(project.environment)
    sec = store.load_security(project.security)
    agent = store.load_agent(project.agent)

    missing = []
    if env is None:
        missing.append(f"environment '{project.environment}'")
    if sec is None:
        missing.append(f"security profile '{project.security}'")
    if agent is None:
        missing.append(f"agent '{project.agent}'")

    if missing:
        print(f"\nWarning: missing resources: {', '.join(missing)}")
        print("Run 'skua init' to install default presets.")
        return

    result = validate(project, env, sec, agent)
    if result.warnings:
        print("\nWarnings:")
        for w in result.warnings:
            print(f"  ! {w}")
    if result.errors:
        print("\nValidation errors:")
        for e in result.errors:
            print(f"  x {e}")
        print("Run 'skua validate' for details.")
