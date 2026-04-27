# SPDX-License-Identifier: BUSL-1.1
"""skua agent — manage the agent assigned to a project."""

import sys

from skua.commands.add import _cred_matches_agent
from skua.config import ConfigStore


def cmd_agent(args):
    action = getattr(args, "action", None)
    dispatch = {
        "list": _cmd_list,
        "set": _cmd_set,
    }
    if not action or action not in dispatch:
        print("usage: skua agent <action> [options]")
        print()
        print("actions:")
        print("  list                          List configured agents")
        print("  set <project> <agent>         Change a project's agent")
        sys.exit(1)
    dispatch[action](args)


def _require_initialized(store: ConfigStore) -> None:
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)


def _cmd_list(args):
    store = ConfigStore()
    _require_initialized(store)
    agents = store.list_resources("AgentConfig")
    if not agents:
        print("No agents configured.")
        return
    default_agent = str(store.get_global_defaults().get("agent", "") or "")
    print("Configured agents:")
    for name in agents:
        marker = " (default)" if name == default_agent else ""
        print(f"  {name}{marker}")


def _cmd_set(args):
    store = ConfigStore()
    _require_initialized(store)

    project_name = str(getattr(args, "name", "") or "").strip()
    agent_name = str(getattr(args, "agent", "") or "").strip()
    if not project_name or not agent_name:
        print("Error: project and agent are required.")
        sys.exit(1)

    project = store.load_project(project_name)
    if project is None:
        print(f"Error: Project '{project_name}' not found.")
        print("Run 'skua list' to see configured projects.")
        sys.exit(1)

    if store.load_agent(agent_name) is None:
        print(f"Error: Agent '{agent_name}' not found.")
        available = store.list_resources("AgentConfig")
        if available:
            print("Available agents:")
            for a in available:
                print(f"  {a}")
        sys.exit(1)

    if project.agent == agent_name:
        print(f"Project '{project_name}' already uses agent '{agent_name}'. No change.")
        return

    previous = project.agent
    project.agent = agent_name

    keep_credential = bool(getattr(args, "keep_credential", False))
    cred_cleared = ""
    if project.credential and not _cred_matches_agent(store, project.credential, agent_name):
        if keep_credential:
            print(
                f"Warning: Credential '{project.credential}' is configured for a different "
                f"agent. Keeping it (--keep-credential) may cause auth failures at run time."
            )
        else:
            cred_cleared = project.credential
            project.credential = ""

    store.save_resource(project)

    print(f"Project '{project_name}' agent: {previous or '(unset)'} -> {agent_name}")
    if cred_cleared:
        print(
            f"  Credential '{cred_cleared}' was cleared because it does not match "
            f"agent '{agent_name}'."
        )
        print(
            "  Add a compatible credential with 'skua credential add' and re-run "
            "'skua agent set' or edit the project."
        )
