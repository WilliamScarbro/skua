# SPDX-License-Identifier: BUSL-1.1
"""skua validate — validate project configuration consistency."""

import sys

from skua.config import ConfigStore
from skua.config.validation import validate_project


def cmd_validate(args):
    store = ConfigStore()
    name = args.name

    project = store.load_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        sys.exit(1)

    env = store.load_environment(project.environment)
    sec = store.load_security(project.security)
    agent = store.load_agent(project.agent)

    print(f"Project: {name}")
    print(f"  Environment:  {project.environment:<20}", end="")
    print("ok" if env else "NOT FOUND")
    print(f"  Security:     {project.security:<20}", end="")
    print("ok" if sec else "NOT FOUND")
    print(f"  Agent:        {project.agent:<20}", end="")
    print("ok" if agent else "NOT FOUND")
    print()

    if env is None or sec is None or agent is None:
        missing = []
        if env is None:
            missing.append(f"environment '{project.environment}'")
        if sec is None:
            missing.append(f"security '{project.security}'")
        if agent is None:
            missing.append(f"agent '{project.agent}'")
        print(f"Missing resources: {', '.join(missing)}")
        print("Run 'skua init' to install default presets.")
        sys.exit(1)

    # Show capabilities
    caps = env.capabilities()
    required = sec.required_capabilities()
    print("  Environment capabilities:")
    for cap in sorted(caps):
        marker = "<- required" if cap in required else ""
        print(f"    {cap} {marker}")
    print()

    # Run validation
    result = validate_project(project, env, sec, agent)

    if result.warnings:
        print("  Warnings:")
        for w in result.warnings:
            print(f"    ! {w}")
        print()

    if result.errors:
        print("  Errors:")
        for e in result.errors:
            print(f"    x {e}")
        print()
        print("  Result: INVALID")
        sys.exit(1)
    else:
        print("  Result: VALID")
