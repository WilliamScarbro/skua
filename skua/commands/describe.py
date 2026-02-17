# SPDX-License-Identifier: BUSL-1.1
"""skua describe — show resolved configuration for a project."""

import sys

from skua.config import ConfigStore
from skua.config.resources import resource_to_dict

import yaml


def cmd_describe(args):
    store = ConfigStore()
    name = args.name

    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        sys.exit(1)

    env = store.load_environment(project.environment)
    sec = store.load_security(project.security)
    agent = store.load_agent(project.agent)

    print(f"=== Project: {name} ===\n")
    print(yaml.dump(resource_to_dict(project), default_flow_style=False, sort_keys=False))

    if env:
        print(f"=== Environment: {project.environment} ===\n")
        print(yaml.dump(resource_to_dict(env), default_flow_style=False, sort_keys=False))

    if sec:
        print(f"=== Security: {project.security} ===\n")
        print(yaml.dump(resource_to_dict(sec), default_flow_style=False, sort_keys=False))

    if agent:
        print(f"=== Agent: {project.agent} ===\n")
        print(yaml.dump(resource_to_dict(agent), default_flow_style=False, sort_keys=False))
