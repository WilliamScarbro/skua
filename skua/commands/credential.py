# SPDX-License-Identifier: BUSL-1.1
"""skua credential — manage named credential sets."""

import shutil
import subprocess
import sys
from pathlib import Path

from skua.config import ConfigStore, Credential


# ── Shared helpers (also imported by run.py) ──────────────────────────────

def agent_default_source_dir(agent) -> Path:
    """Return the default host directory for an agent's credentials.

    Derives the path from ``agent.auth.dir`` (e.g. ``.claude``, ``.codex``).
    Falls back to ``~/.{agent.name}`` when ``auth.dir`` is unset.
    """
    home = Path.home()
    if agent and agent.auth and agent.auth.dir:
        auth_dir = agent.auth.dir.lstrip("/")
    elif agent and agent.name:
        auth_dir = f".{agent.name}"
    else:
        auth_dir = ".agent"
    return home / auth_dir


def resolve_credential_sources(cred, agent) -> list:
    """Return ``[(src_path, dest_filename), ...]`` pairs for credential seeding.

    Priority:
      1. ``cred.files``      – explicit absolute paths the user supplied
      2. ``cred.source_dir`` – a directory; ``agent.auth.files`` names what to grab
      3. agent default dir   – derived from ``agent.auth.dir`` (e.g. ``~/.claude``)

    When ``cred`` is ``None`` the agent's default directory is used.
    """
    auth_files = []
    if agent and agent.auth and agent.auth.files:
        auth_files = list(agent.auth.files)

    if cred and cred.files:
        return [(Path(f).expanduser(), Path(f).name) for f in cred.files]

    if cred and cred.source_dir:
        src_dir = Path(cred.source_dir)
    else:
        src_dir = agent_default_source_dir(agent)

    return [(src_dir / Path(f).name, Path(f).name) for f in auth_files]


# ── Dispatcher ────────────────────────────────────────────────────────────

def cmd_credential(args):
    action = args.action
    if action == "list":
        _cmd_list(args)
    elif action == "add":
        _cmd_add(args)
    elif action == "remove":
        _cmd_remove(args)
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


# ── list ──────────────────────────────────────────────────────────────────

def _cmd_list(args):
    store = ConfigStore()
    names = store.list_resources("Credential")
    if not names:
        print("No credentials configured.")
        print("Add one with: skua credential add <name>")
        return

    print(f"{'NAME':<20} {'AGENT':<12} {'SOURCE':<36} STATUS")
    print("-" * 80)
    for name in names:
        cred = store.load_credential(name)
        if cred is None:
            continue
        agent = store.load_agent(cred.agent)
        source_label, status = _credential_status(cred, agent)
        # Truncate long source labels
        if len(source_label) > 35:
            source_label = "\u2026" + source_label[-34:]
        print(f"{cred.name:<20} {cred.agent:<12} {source_label:<36} {status}")


def _credential_status(cred, agent) -> tuple:
    """Return (source_label, status_str) for display."""
    if cred.files:
        ok = sum(1 for f in cred.files if Path(f).expanduser().is_file())
        total = len(cred.files)
        label = f"{total} explicit file(s)"
        status = "ok" if ok == total else f"{ok}/{total} found"
        return label, status

    auth_files = list(agent.auth.files) if agent and agent.auth and agent.auth.files else []

    if cred.source_dir:
        src_dir = Path(cred.source_dir)
        label = cred.source_dir
    else:
        src_dir = agent_default_source_dir(agent) if agent else Path.home()
        label = f"{src_dir} (default)"

    if not src_dir.is_dir():
        return label, "dir missing"

    ok = sum(1 for f in auth_files if (src_dir / Path(f).name).is_file())
    total = len(auth_files)
    if total == 0:
        return label, "ok"
    status = "ok" if ok == total else f"{ok}/{total} found"
    return label, status


# ── add ───────────────────────────────────────────────────────────────────

def _cmd_add(args):
    store = ConfigStore()

    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    # Credential name
    name = getattr(args, "name", None) or ""
    if not name:
        name = input("Credential name: ").strip()
    if not name:
        print("Error: Credential name is required.")
        sys.exit(1)
    if not all(c.isalnum() or c in "-_" for c in name):
        print("Error: Credential name must be alphanumeric (hyphens and underscores allowed).")
        sys.exit(1)
    if store.load_credential(name) is not None:
        print(f"Error: Credential '{name}' already exists. Remove it first.")
        sys.exit(1)

    # Agent selection
    agent_name = getattr(args, "agent", None) or ""
    available_agents = store.list_resources("AgentConfig")
    if not agent_name:
        if available_agents:
            print("\nAvailable agents:")
            for a in available_agents:
                print(f"  {a}")
            print()
        default_agent = store.get_global_defaults().get("agent", "claude")
        agent_input = input(f"Agent [{default_agent}]: ").strip()
        agent_name = agent_input or default_agent

    agent = store.load_agent(agent_name)
    if agent is None:
        print(f"Error: Agent '{agent_name}' not found.")
        sys.exit(1)

    # Source resolution — in priority order:
    #   1. --file paths  →  explicit list stored on the credential
    #   2. --source-dir  →  a directory (agent.auth.files says what to look for)
    #   3. --login       →  run agent login, then auto-detect default dir
    #   4. (nothing)     →  check agent default dir; prompt if not found
    explicit_files = [
        str(Path(f).expanduser().resolve())
        for f in (getattr(args, "files", None) or [])
    ]
    source_dir = getattr(args, "source_dir", None) or ""
    do_login = getattr(args, "login", False)

    if explicit_files:
        source_dir = ""
        missing = [f for f in explicit_files if not Path(f).is_file()]
        if missing:
            for m in missing:
                print(f"Warning: file not found: {m}")

    elif source_dir:
        source_dir = str(Path(source_dir).expanduser().resolve())
        if not Path(source_dir).is_dir():
            print(f"Error: Directory does not exist: {source_dir}")
            sys.exit(1)

    elif do_login:
        source_dir = _signin_locally(agent_name, agent)
        explicit_files = []

    else:
        # Auto-detect from agent default dir, then prompt if empty-handed
        default_dir = agent_default_source_dir(agent)
        if default_dir.is_dir() and _any_auth_files_present(default_dir, agent.auth.files):
            print(f"Found credentials in: {default_dir}")
            source_dir = str(default_dir)
        else:
            hint = f" [{default_dir}]" if default_dir.is_dir() else ""
            path_input = input(
                f"Credential directory{hint} (or press Enter for default): "
            ).strip()
            if not path_input:
                if default_dir.is_dir():
                    source_dir = str(default_dir)
                else:
                    print(
                        "Error: No credentials found at the default location.\n"
                        "Use --login to sign in, or --source-dir / --file to specify a path."
                    )
                    sys.exit(1)
            else:
                source_dir = str(Path(path_input).expanduser().resolve())

    cred = Credential(
        name=name,
        agent=agent_name,
        source_dir=source_dir,
        files=explicit_files,
    )
    store.save_resource(cred)

    print(f"\nCredential '{name}' added.")
    print(f"  Agent: {agent_name}")
    if explicit_files:
        print(f"  Files ({len(explicit_files)}):")
        for fpath in explicit_files:
            status = "[OK]" if Path(fpath).is_file() else "[--]"
            print(f"    {status} {fpath}")
    else:
        print(f"  Source dir: {source_dir or '(not set)'}")
        if source_dir:
            _show_file_status(Path(source_dir), agent)
    print(f"\nUse with: skua add <project> --credential {name}")


def _signin_locally(agent_name: str, agent) -> str:
    """Run the agent's login command locally and return the detected source dir."""
    login_cmd = agent.auth.login_command
    if not login_cmd:
        print(f"Error: No login_command configured for agent '{agent_name}'.")
        sys.exit(1)

    cmd_parts = login_cmd.split()
    if not shutil.which(cmd_parts[0]):
        print(f"Error: '{cmd_parts[0]}' is not installed on this system.")
        print("Install it first, or use --source-dir to provide the credential path manually.")
        sys.exit(1)

    print(f"\nRunning: {login_cmd}")
    print("Complete the sign-in flow, then return here.")
    print()
    try:
        subprocess.run(cmd_parts, check=True)
    except subprocess.CalledProcessError:
        print(f"Warning: '{login_cmd}' exited with an error. Attempting to detect credentials anyway.")
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)

    # Auto-detect credential files in the agent's default directory after login
    default_dir = agent_default_source_dir(agent)
    if default_dir.is_dir() and _any_auth_files_present(default_dir, agent.auth.files):
        print(f"\nDetected credentials in: {default_dir}")
        return str(default_dir)

    print(f"\nCould not auto-detect credentials in {default_dir}.")
    path_input = input("Enter credential directory path (or leave empty to skip): ").strip()
    return path_input


def _any_auth_files_present(directory: Path, auth_files: list) -> bool:
    """Return True if at least one expected auth file exists in directory."""
    for fname in auth_files or []:
        if (directory / Path(fname).name).is_file():
            return True
    return False


def _show_file_status(source_dir: Path, agent):
    """Print which expected auth files are present in source_dir."""
    if not source_dir.is_dir():
        print(f"  Warning: {source_dir} does not exist.")
        return
    files = agent.auth.files if agent and agent.auth else []
    if not files:
        return
    print("  Credential files:")
    for fname in files:
        fpath = source_dir / Path(fname).name
        status = "[OK]" if fpath.is_file() else "[--]"
        print(f"    {status} {fpath}")


# ── remove ────────────────────────────────────────────────────────────────

def _cmd_remove(args):
    store = ConfigStore()
    name = args.name

    cred = store.load_credential(name)
    if cred is None:
        print(f"Error: Credential '{name}' not found.")
        sys.exit(1)

    # Warn if any project references this credential
    projects = store.list_resources("Project")
    referencing = []
    for pname in projects:
        p = store.load_project(pname)
        if p and p.credential == name:
            referencing.append(pname)
    if referencing:
        print(f"Warning: The following projects reference this credential: {', '.join(referencing)}")
        print("Update them with 'skua credential add' and reassign via 'skua add --credential'.")

    store.delete_resource("Credential", name)
    print(f"Credential '{name}' removed.")
