# SPDX-License-Identifier: BUSL-1.1
"""CLI argument parsing and command dispatch."""

import argparse
import sys

from skua import __version__


def _add_adapt_args(parser):
    parser.add_argument("name", help="Project name to adapt")
    parser.add_argument("--base-image", help="Override generated Dockerfile base image")
    parser.add_argument("--from-image", help="Adapt an existing image as Dockerfile parent")
    parser.add_argument("--package", action="append", default=[], help="Apt package to add (repeatable)")
    parser.add_argument(
        "--command",
        dest="extra_command",
        action="append",
        default=[],
        help="Extra setup command (repeatable)",
    )
    parser.add_argument(
        "--apply-only",
        action="store_true",
        help="Skip automated agent run and apply existing image-request.yaml",
    )
    parser.add_argument("--clear", action="store_true", help="Clear project image customization")
    parser.add_argument("--write-only", action="store_true", help="Only create adapt files; do not apply")
    parser.add_argument("--build", action="store_true", help="Build adapted image immediately")


def main():
    parser = argparse.ArgumentParser(
        prog="skua",
        description="Skua - Dockerized Coding Agent Manager",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="First-time setup wizard")
    p_init.add_argument("--force", action="store_true",
                        help="Re-initialize even if already configured")

    # build
    sub.add_parser("build", help="Build images required by configured projects")

    # add
    p_add = sub.add_parser("add", help="Add a project configuration")
    p_add.add_argument("name", help="Project name (alphanumeric, hyphens, underscores)")
    p_add.add_argument("--dir", help="Project directory path")
    p_add.add_argument("--repo", help="Git repository URL to clone (mutually exclusive with --dir)")
    p_add.add_argument("--ssh-key", help="SSH private key path")
    p_add.add_argument("--env", help="Environment resource name (default: from global)")
    p_add.add_argument("--security", help="Security profile name (default: from global)")
    p_add.add_argument("--agent", help="Agent config name (default: from global)")
    p_add.add_argument("--quick", action="store_true",
                        help="Use all defaults, skip interactive prompts")
    p_add.add_argument("--no-prompt", action="store_true",
                        help="Skip interactive prompts for missing values")

    # remove
    p_rm = sub.add_parser("remove", help="Remove a project configuration")
    p_rm.add_argument("name", help="Project name to remove")

    # run
    p_run = sub.add_parser("run", help="Run a container for a project")
    p_run.add_argument("name", help="Project name to run")

    # adapt
    p_adapt = sub.add_parser(
        "adapt",
        help="Apply project image-request template and optionally build project image",
    )
    _add_adapt_args(p_adapt)

    # list
    sub.add_parser("list", help="List projects and running containers")

    # clean
    p_clean = sub.add_parser("clean", help="Clean persisted agent credentials")
    p_clean.add_argument("name", nargs="?", help="Project name (omit for all)")

    # purge
    p_purge = sub.add_parser("purge", help="Remove all skua local state")
    p_purge.add_argument("--yes", action="store_true", help="Skip confirmation prompts")

    # config
    p_cfg = sub.add_parser("config", help="Show or edit global configuration")
    p_cfg.add_argument("--git-name", help="Set git user name")
    p_cfg.add_argument("--git-email", help="Set git user email")
    p_cfg.add_argument("--tool-dir", help="Set path to directory containing Dockerfile")
    p_cfg.add_argument("--ssh-key", help="Set default SSH private key path")
    p_cfg.add_argument("--default-env", help="Set default environment")
    p_cfg.add_argument("--default-security", help="Set default security profile")
    p_cfg.add_argument("--default-agent", help="Set default agent")

    # validate
    p_val = sub.add_parser("validate", help="Validate project configuration")
    p_val.add_argument("name", help="Project name to validate")

    # describe
    p_desc = sub.add_parser("describe", help="Show resolved configuration for a project")
    p_desc.add_argument("name", help="Project name to describe")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Lazy import commands to keep startup fast
    from skua.commands import (
        cmd_build, cmd_init, cmd_add, cmd_remove, cmd_run,
        cmd_adapt, cmd_list, cmd_clean, cmd_purge, cmd_config, cmd_validate,
        cmd_describe,
    )

    commands = {
        "init": cmd_init,
        "build": cmd_build,
        "add": cmd_add,
        "remove": cmd_remove,
        "run": cmd_run,
        "adapt": cmd_adapt,
        "list": cmd_list,
        "clean": cmd_clean,
        "purge": cmd_purge,
        "config": cmd_config,
        "validate": cmd_validate,
        "describe": cmd_describe,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
