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
        "--dockerfile",
        action="store_true",
        help="Print the generated Dockerfile for this project and exit",
    )
    parser.add_argument(
        "--show-smoke-test",
        action="store_true",
        help="Print the smoke test script (.skua/smoke-test.sh) for this project and exit",
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
    p_build = sub.add_parser("build", help="Build image required by a project")
    p_build.add_argument("name", help="Project name to build image for")
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
    p_add.add_argument("--image", help="Docker base image for the project container")
    p_add.add_argument("--default-image", dest="default_image",
                        help="Start from a named default image (skua default-image list)")
    p_add.add_argument("--quick", action="store_true",
                        help="Use all defaults, skip interactive prompts")
    p_add.add_argument("--no-prompt", action="store_true",
                        help="Skip interactive prompts for missing values")

    # source
    p_src = sub.add_parser("source", help="Manage project sources (directories and repositories)")
    src_sub = p_src.add_subparsers(dest="action")

    p_src_list = src_sub.add_parser("list", help="List sources for a project")
    p_src_list.add_argument("name", help="Project name")

    p_src_add = src_sub.add_parser("add", help="Add a source to a project")
    p_src_add.add_argument("name", help="Project name")
    src_loc = p_src_add.add_mutually_exclusive_group(required=True)
    src_loc.add_argument("--dir", help="Local directory to add as a source")
    src_loc.add_argument("--repo", help="Git repository URL to add as a source")
    p_src_add.add_argument("--source-name", dest="source_name", help="Label for the source (default: derived from path/URL)")
    p_src_add.add_argument("--mount-path", dest="mount_path", help="Container mount path (default: /home/dev/<name>)")
    p_src_add.add_argument("--primary", action="store_true", help="Make this the primary source")
    p_src_add.add_argument("--ssh-key", dest="ssh_key", help="SSH private key for this source")
    p_src_add.add_argument("--host", help="SSH config host for remote source")

    p_src_rm = src_sub.add_parser("remove", help="Remove a source from a project")
    p_src_rm.add_argument("name", help="Project name")
    p_src_rm.add_argument("source", help="Source name or 1-based index")

    p_src_pri = src_sub.add_parser("set-primary", help="Set a source as primary")
    p_src_pri.add_argument("name", help="Project name")
    p_src_pri.add_argument("source", help="Source name or 1-based index")

    # remove
    p_rm = sub.add_parser("remove", help="Remove a project configuration")
    p_rm.add_argument("name", help="Project name to remove")

    # run
    p_run = sub.add_parser("run", help="Run a container for a project")
    p_run.add_argument("name", help="Project name to run")

    # stop
    p_stop = sub.add_parser("stop", help="Stop a running project container")
    p_stop.add_argument("name", help="Project name to stop")
    p_stop.add_argument(
        "-f", "--force",
        action="store_true",
        help="Skip git status confirmation prompts",
    )

    # restart
    p_restart = sub.add_parser("restart", help="Restart a project container")
    p_restart.add_argument("name", help="Project name to restart")
    p_restart.add_argument(
        "-f", "--force",
        action="store_true",
        help="Skip git status confirmation prompts",
    )

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
        help="Include agent configuration columns (agent, credential); activity is always shown",
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

    # dashboard
    p_dashboard = sub.add_parser("dashboard", help="Live interactive project dashboard")
    p_dashboard.add_argument(
        "-a", "--agent",
        action="store_true",
        help="Include agent configuration columns (agent, credential)",
    )
    p_dashboard.add_argument(
        "-s", "--security",
        action="store_true",
        help="Include security columns (security profile, network mode)",
    )
    p_dashboard.add_argument(
        "-g", "--git",
        action="store_true",
        help="Include git status column for repo projects",
    )
    p_dashboard.add_argument(
        "-i", "--image", "--images",
        action="store_true",
        help="Include image column",
    )
    p_dashboard.add_argument(
        "--local",
        action="store_true",
        help="Only show projects running on the local host",
    )
    p_dashboard.add_argument(
        "--refresh-seconds",
        type=float,
        default=2.0,
        help="Dashboard auto-refresh interval in seconds (0 disables periodic polling)",
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

    # default-image
    p_di = sub.add_parser("default-image", help="Manage named, prebuilt default images")
    di_sub = p_di.add_subparsers(dest="action")

    di_sub.add_parser("list", help="List configured default images")

    p_di_build = di_sub.add_parser("build", help="Build a default image")
    p_di_build.add_argument("name", help="Default image name")
    p_di_build.add_argument("--agent", help="Agent to install (default: from global)")
    p_di_build.add_argument("--base-image", dest="base_image", help="Base OS Docker image")
    p_di_build.add_argument("--image", help="Target Docker image name (default: skua-default-<name>)")
    p_di_build.add_argument(
        "--package", action="append", default=[],
        metavar="PKG", help="Apt package to include (repeatable)",
    )
    p_di_build.add_argument(
        "--command", dest="extra_command", action="append", default=[],
        metavar="CMD", help="Extra setup command (repeatable)",
    )
    p_di_build.add_argument("--description", help="Human-readable description")
    p_di_build.add_argument("-v", "--verbose", action="store_true", help="Show full Docker build output")

    p_di_save = di_sub.add_parser(
        "save",
        help="Save an existing image as a default (source: project name or Docker image)",
    )
    p_di_save.add_argument("source", help="Project name or Docker image to save")
    p_di_save.add_argument("name", help="Name for the new default image")
    p_di_save.add_argument("--description", help="Human-readable description")
    p_di_save.add_argument("--agent", help="Agent this image is built for")

    p_di_rm = di_sub.add_parser("remove", help="Remove a default image entry")
    p_di_rm.add_argument("name", help="Default image name to remove")

    # ssh
    p_ssh = sub.add_parser("ssh", help="Manage project SSH key settings")
    ssh_sub = p_ssh.add_subparsers(dest="action")

    p_ssh_list = ssh_sub.add_parser("list", help="List SSH private keys for a project")
    p_ssh_list.add_argument("name", help="Project name")

    p_ssh_add = ssh_sub.add_parser("add", help="Add an SSH private key for a project")
    p_ssh_add.add_argument("name", help="Project name")
    p_ssh_add.add_argument("--ssh-key", help="SSH private key path")
    p_ssh_add.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not prompt for key selection; requires --ssh-key",
    )

    p_ssh_rm = ssh_sub.add_parser("remove", help="Remove one or all SSH private keys for a project")
    p_ssh_rm.add_argument("name", help="Project name")
    p_ssh_rm.add_argument("--ssh-key", help="SSH private key path to remove")
    p_ssh_rm.add_argument("--all", action="store_true", help="Remove all SSH private keys from the project")
    p_ssh_rm.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not prompt for key selection; requires --ssh-key or --all",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Lazy import commands to keep startup fast
    from skua.commands import (
        cmd_build, cmd_init, cmd_add, cmd_remove, cmd_run, cmd_stop, cmd_restart,
        cmd_adapt, cmd_list, cmd_clean, cmd_purge, cmd_config, cmd_validate,
        cmd_describe, cmd_credential, cmd_dashboard, cmd_source, cmd_ssh,
        cmd_default_image,
    )

    commands = {
        "init": cmd_init,
        "build": cmd_build,
        "add": cmd_add,
        "source": _handle_source,
        "remove": cmd_remove,
        "run": cmd_run,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "adapt": cmd_adapt,
        "list": cmd_list,
        "dashboard": cmd_dashboard,
        "clean": cmd_clean,
        "purge": cmd_purge,
        "config": cmd_config,
        "validate": cmd_validate,
        "describe": cmd_describe,
        "credential": _handle_credential,
        "ssh": _handle_ssh,
        "default-image": _handle_default_image,
    }
    commands[args.command](args)


def _handle_source(args):
    """Dispatch source subcommands, showing help if no action given."""
    from skua.commands import cmd_source
    cmd_source(args)


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


def _handle_ssh(args):
    """Dispatch ssh subcommands, showing help if no action given."""
    from skua.commands import cmd_ssh
    if not args.action:
        print("usage: skua ssh <action> [options]")
        print()
        print("actions:")
        print("  list <project>     List a project's SSH private keys")
        print("  add <project>      Add an SSH private key to a project")
        print("  remove <project>   Remove one or all SSH private keys from a project")
        sys.exit(1)
    cmd_ssh(args)


def _handle_default_image(args):
    """Dispatch default-image subcommands, showing help if no action given."""
    from skua.commands import cmd_default_image
    if not args.action:
        print("usage: skua default-image <action> [options]")
        print()
        print("actions:")
        print("  list                      List configured default images")
        print("  build <name>              Build a default image from spec")
        print("  save <source> <name>      Save a project or Docker image as a default")
        print("  remove <name>             Remove a default image entry")
        sys.exit(1)
    cmd_default_image(args)


if __name__ == "__main__":
    main()
