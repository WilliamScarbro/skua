# SPDX-License-Identifier: BUSL-1.1
"""skua add — add a project configuration."""

import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from skua.commands.credential import agent_default_source_dir, resolve_credential_sources
from skua.config import ConfigStore, Credential, Project
from skua.config.resources import ProjectGitSpec, ProjectSshSpec, ProjectImageSpec
from skua.project_adapt import ADAPT_GUIDE_NAME, ensure_adapt_workspace
from skua.utils import find_ssh_keys, parse_ssh_config_hosts, select_option


def cmd_add(args):
    store = ConfigStore()

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    name = args.name
    repo_url = getattr(args, "repo", None) or ""
    host = getattr(args, "host", None) or ""

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

    # --host requires --repo and is incompatible with --dir
    if host and args.dir:
        print("Error: --host and --dir are mutually exclusive. Use --repo for remote projects.")
        sys.exit(1)
    if host and not repo_url:
        print("Error: --host requires --repo. Remote projects must specify a git repository URL.")
        sys.exit(1)

    # Validate SSH config host
    if host:
        available_hosts = parse_ssh_config_hosts()
        if host not in available_hosts:
            print(f"Error: '{host}' is not defined in ~/.ssh/config.")
            if available_hosts:
                print("  Defined hosts:")
                for h in available_hosts:
                    print(f"    {h}")
            else:
                print("  No hosts found in ~/.ssh/config.")
            sys.exit(1)

    # Validate repo URL looks like a git URL
    if repo_url and not _is_git_url(repo_url):
        print(f"Error: '{repo_url}' does not look like a git URL.")
        print("  Expected: https://... or git@...:...")
        sys.exit(1)
    if repo_url:
        try:
            normalized_repo = _normalize_repo_url_for_ssh(repo_url)
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        if normalized_repo != repo_url:
            print("Warning: HTTPS git integration is not supported; converting repo URL to SSH.")
            print(f"  {repo_url} -> {normalized_repo}")
        repo_url = normalized_repo

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
        keys = [str(p) for p in find_ssh_keys()]
        global_ssh = defaults.get("sshKey", "")
        if global_ssh:
            global_ssh_path = str(Path(global_ssh).expanduser().resolve())
            if Path(global_ssh_path).is_file() and global_ssh_path not in keys:
                keys.append(global_ssh_path)
        if keys:
            keys = sorted(keys)
            options = keys + ["None"]
            selected = select_option("Select SSH private key:", options, default_index=len(options) - 1)
            ssh_key = "" if selected == "None" else selected
        else:
            ssh_key = input("SSH private key path (leave empty for none): ").strip()

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
            default_idx = available_agents.index(default_agent) if default_agent in available_agents else 0
            agent_name = select_option("Select agent:", available_agents, default_idx)
        else:
            agent_input = input(f"Agent [{default_agent}]: ").strip()
            agent_name = agent_input or default_agent

    if not agent_name:
        agent_name = default_agent

    agent = store.load_agent(agent_name)
    if agent is None:
        print(f"Error: Agent '{agent_name}' not found.")
        if available_agents:
            print("Available agents:")
            for a in available_agents:
                print(f"  {a}")
        else:
            print("No agent presets installed. Run 'skua init' first.")
        sys.exit(1)

    # Credential selection (required)
    cred_name = getattr(args, "credential", None) or ""
    available_creds = sorted(
        c for c in store.list_resources("Credential")
        if _cred_matches_agent(store, c, agent_name)
    )

    if cred_name:
        cred = store.load_credential(cred_name)
        if cred is None:
            print(f"Error: Credential '{cred_name}' not found.")
            print("Run 'skua credential list' to see available credentials.")
            sys.exit(1)
        if cred.agent and cred.agent != agent_name:
            print(
                f"Error: Credential '{cred_name}' is for agent '{cred.agent}', "
                f"not '{agent_name}'."
            )
            sys.exit(1)
    elif available_creds:
        cred_name = _select_existing_credential(available_creds, quick, args.no_prompt)
    else:
        cred_name = _auto_add_local_credential(store, agent_name, agent)

    project = Project(
        name=name,
        directory=project_dir or "",
        repo=repo_url,
        host=host,
        environment=env_name,
        security=sec_name,
        agent=agent_name,
        credential=cred_name,
        git=ProjectGitSpec(),
        ssh=ProjectSshSpec(private_key=ssh_key),
        image=ProjectImageSpec(),
    )

    store.save_resource(project)

    # Create persistence dir (local projects only)
    if not host:
        env = store.load_environment(env_name)
        if env and env.persistence.mode == "bind":
            store.project_data_dir(name, agent_name).mkdir(parents=True, exist_ok=True)
    if project_dir and Path(project_dir).is_dir():
        ensure_adapt_workspace(Path(project_dir), name, agent_name)

    # Create Docker volume for repo on remote host
    if host:
        vol_name = f"skua-{name}-repo"
        print(f"Creating Docker volume '{vol_name}' on {host}...")
        result = subprocess.run(
            ["ssh", host, "docker", "volume", "create", vol_name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Warning: Failed to create volume on {host}: {result.stderr.strip()}")
        else:
            print(f"  Volume '{vol_name}' ready.")

    # Print summary
    print(f"\nProject '{name}' added.")
    if host:
        _print_summary_attr("Host", f"{host} (remote)")
    _print_summary_attr("Repo", repo_url)
    if not host:
        _print_summary_attr("Directory", project_dir)
    _print_summary_attr("Environment", env_name)
    _print_summary_attr("Security", sec_name)
    _print_summary_attr("Agent", agent_name)
    _print_summary_attr("Credential", cred_name)
    if project_dir and Path(project_dir).is_dir():
        print(f"  {'Adapt guide:':<14} {Path(project_dir) / '.skua' / ADAPT_GUIDE_NAME}")
    if ssh_key:
        print(f"  {'SSH key:':<14} {ssh_key}")
    print(f"\nRun with: skua run {name}")

    # Auto-validate
    _try_validate(store, project)


def _cred_matches_agent(store, cred_name: str, agent_name: str) -> bool:
    """Return True if the credential is compatible with the given agent."""
    cred = store.load_credential(cred_name)
    if cred is None:
        return False
    return not cred.agent or cred.agent == agent_name


def _print_summary_attr(label: str, value: str):
    """Print a summary row only when value is set."""
    if value:
        print(f"  {f'{label}:':<14} {value}")


def _select_existing_credential(available_creds: list, quick: bool, no_prompt: bool) -> str:
    """Choose a credential from existing agent-compatible entries."""
    if quick or no_prompt:
        selected = available_creds[0]
        print(f"Using credential: {selected}")
        return selected

    return select_option("Select credential:", available_creds, default_index=0)


def _auto_add_local_credential(store, agent_name: str, agent) -> str:
    """Detect local credentials for agent and create a named credential resource."""
    sources = resolve_credential_sources(None, agent)
    found = [str(src) for src, _ in sources if src.is_file()]
    if not found:
        print(f"Error: No local credentials detected for agent '{agent_name}'.")
        print(f"Run '{agent.auth.login_command or f'{agent_name} login'}' first, then retry.")
        print(
            "Or add one explicitly with: "
            f"skua credential add <name> --agent {agent_name} --source-dir <dir>"
        )
        sys.exit(1)

    default_name = _default_credential_name(store, f"{agent_name}-local")
    print(f"\nNo credentials configured for agent '{agent_name}'.")
    print("Found local credential files:")
    for path in found:
        print(f"  {path}")

    while True:
        name_input = input(f"Name for imported credential [{default_name}]: ").strip()
        cred_name = name_input or default_name
        if not all(c.isalnum() or c in "-_" for c in cred_name):
            print("Credential name must be alphanumeric (hyphens and underscores allowed).")
            continue
        if store.load_credential(cred_name) is not None:
            print(f"Credential '{cred_name}' already exists. Choose another name.")
            continue
        break

    source_dir = str(agent_default_source_dir(agent))
    cred = Credential(name=cred_name, agent=agent_name, source_dir=source_dir, files=[])
    store.save_resource(cred)
    print(f"Added credential '{cred_name}' from local files.")
    return cred_name


def _default_credential_name(store, base_name: str) -> str:
    """Generate a unique default credential name."""
    if store.load_credential(base_name) is None:
        return base_name
    i = 2
    while True:
        candidate = f"{base_name}-{i}"
        if store.load_credential(candidate) is None:
            return candidate
        i += 1


def _is_git_url(url: str) -> bool:
    """Check if a string looks like a git URL."""
    return (
        url.startswith("https://")
        or url.startswith("http://")
        or url.startswith("git://")
        or url.startswith("git@")
        or url.startswith("ssh://")
    )


def _normalize_repo_url_for_ssh(url: str) -> str:
    """Normalize repo URLs to SSH form when HTTP(S) is provided."""
    if url.startswith("https://") or url.startswith("http://"):
        ssh_url = _https_repo_to_ssh(url)
        if not ssh_url:
            raise ValueError(f"Cannot convert HTTPS repo URL to SSH: {url}")
        return ssh_url
    return url


def _https_repo_to_ssh(url: str) -> str:
    """Convert an HTTP(S) git URL to SSH format."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").strip()
    path = parsed.path.strip().strip("/")
    if not host or not path:
        return ""
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return ""
    if parsed.port:
        return f"ssh://git@{host}:{parsed.port}/{path}"
    return f"git@{host}:{path}"


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
