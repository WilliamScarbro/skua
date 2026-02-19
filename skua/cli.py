# SPDX-License-Identifier: BUSL-1.1
"""CLI argument parsing and command dispatch."""

import argparse
import sys

from skua import __version__


def _add_adapt_args(parser):
    parser.add_argument("name", nargs="?", help="Project name to adapt")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Apply pending image-request changes for all projects",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Show the resolved agent prompt/command for this project and exit",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Run automated agent discovery to generate/update image-request.yaml before applying",
    )
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
        help="Alias for default behavior: apply existing image-request.yaml without discovery",
    )
    parser.add_argument("--clear", action="store_true", help="Clear project image customization")
    parser.add_argument("--write-only", action="store_true", help="Only create adapt files; do not apply")
    parser.add_argument("--build", action="store_true", help="Build adapted image immediately")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip approval prompts (auto-approve wishlist and build-error retry)",
    )


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
    p_build = sub.add_parser("build", help="Build images required by configured projects")
    p_build.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show full Docker build output",
    )

    # add
    p_add = sub.add_parser("add", help="Add a project configuration")
    p_add.add_argument("name", help="Project name (alphanumeric, hyphens, underscores)")
    p_add.add_argument("--dir", help="Project directory path")
    p_add.add_argument("--repo", help="Git repository URL to clone (mutually exclusive with --dir)")
    p_add.add_argument("--host", help="SSH config host for remote execution (requires --repo)")
    p_add.add_argument("--ssh-key", help="SSH private key path")
    p_add.add_argument("--env", help="Environment resource name (default: from global)")
    p_add.add_argument("--security", help="Security profile name (default: from global)")
    p_add.add_argument("--agent", help="Agent config name (default: from global)")
    p_add.add_argument("--credential", help="Named credential set to use for this project")
    p_add.add_argument("--no-credential", action="store_true",
                        help="Skip credential setup (use when the agent will authenticate inside the container)")
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
    p_list = sub.add_parser("list", help="List projects and running containers")
    p_list.add_argument(
        "-a", "--agent",
        action="store_true",
        help="Include agent configuration columns (agent, credential)",
    )
    p_list.add_argument(
        "-s", "--security",
        action="store_true",
        help="Include security columns (security profile, network mode)",
    )
    p_list.add_argument(
        "-g", "--git",
        action="store_true",
        help="Include git status column for repo projects",
    )
    p_list.add_argument(
        "-i", "--image", "--images",
        action="store_true",
        help="Include image column",
    )
    p_list.add_argument(
        "--local",
        action="store_true",
        help="Only show projects running on the local host",
    )

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

    # credential
    p_cred = sub.add_parser("credential", help="Manage named credential sets")
    cred_sub = p_cred.add_subparsers(dest="action")

    cred_sub.add_parser("list", help="List configured credentials")

    p_cred_add = cred_sub.add_parser("add", help="Add a credential set")
    p_cred_add.add_argument("name", nargs="?", help="Credential name")
    p_cred_add.add_argument("--agent", help="Agent name (e.g. claude, codex)")
    src_group = p_cred_add.add_mutually_exclusive_group()
    src_group.add_argument(
        "--source-dir",
        metavar="DIR",
        help="Directory on the host containing credential files",
    )
    src_group.add_argument(
        "--file",
        action="append",
        dest="files",
        default=[],
        metavar="PATH",
        help="Explicit credential file path (repeatable; alternative to --source-dir)",
    )
    src_group.add_argument(
        "--login",
        action="store_true",
        help="Sign in locally using the agent's login command, then auto-detect credentials",
    )

    p_cred_rm = cred_sub.add_parser("remove", help="Remove a credential set")
    p_cred_rm.add_argument("name", help="Credential name to remove")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Lazy import commands to keep startup fast
    from skua.commands import (
        cmd_build, cmd_init, cmd_add, cmd_remove, cmd_run,
        cmd_adapt, cmd_list, cmd_clean, cmd_purge, cmd_config, cmd_validate,
        cmd_describe, cmd_credential,
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
        "credential": _handle_credential,
    }
    commands[args.command](args)


def _handle_credential(args):
    """Dispatch credential subcommands, showing help if no action given."""
    from skua.commands import cmd_credential
    if not args.action:
        print("usage: skua credential <action> [options]")
        print()
        print("actions:")
        print("  list            List configured credentials")
        print("  add [name]      Add a credential set")
        print("  remove <name>   Remove a credential set")
        sys.exit(1)
    cmd_credential(args)


if __name__ == "__main__":
    main()
