# SPDX-License-Identifier: BUSL-1.1
"""skua clean — clean persisted agent credentials."""

import subprocess
import sys

from skua.config import ConfigStore
from skua.utils import confirm


def cmd_clean(args):
    store = ConfigStore()
    name = args.name

    if name:
        project = store.load_project(name)
        if project is None:
            print(f"Error: Project '{name}' not found.")
            sys.exit(1)
        env = store.load_environment(project.environment)
        _clean_project(store, project, env)
    else:
        project_names = store.list_resources("Project")
        if not project_names:
            print("No projects configured.")
            return
        if not confirm("Clean agent credentials for ALL projects?"):
            return
        for pname in project_names:
            project = store.load_project(pname)
            env = store.load_environment(project.environment) if project else None
            if project:
                _clean_project(store, project, env)


def _clean_project(store, project, env):
    name = project.name
    persist_mode = env.persistence.mode if env else "bind"
    agent = store.load_agent(project.agent)
    auth_files = list(agent.auth.files) if agent and agent.auth.files else []
    if not auth_files and project.agent == "claude":
        auth_files = [".credentials.json", ".claude.json"]

    if persist_mode == "bind":
        data_dir = store.project_data_dir(name, project.agent)
        if data_dir.exists():
            for fname in auth_files:
                f = data_dir / fname
                if f.exists():
                    f.unlink()
            print(f"Cleaned agent data for '{name}' ({project.agent}).")
        else:
            print(f"No data to clean for '{name}'.")
    else:
        vol_name = f"skua-{name}-{project.agent}"
        result = subprocess.run(
            ["docker", "volume", "rm", vol_name],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"Removed volume '{vol_name}' for '{name}'.")
        else:
            print(f"Volume '{vol_name}' not found or in use.")
