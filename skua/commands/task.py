# SPDX-License-Identifier: BUSL-1.1
"""skua task — inspect and dispatch multi-agent task plans."""

import json
import shlex
import sys
from pathlib import Path

import yaml

from skua.tasks import (
    dispatch_task_plan,
    load_task_plan,
    render_task_plan_text,
    run_task_prompt,
)


def _write_or_print(text: str, dest: str = ""):
    if dest:
        path = Path(dest).expanduser()
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path}")
        return
    print(text)


def _load_prompt_text(args) -> str:
    if getattr(args, "prompt", None):
        return str(args.prompt)
    path = Path(args.prompt_file).expanduser()
    return path.read_text(encoding="utf-8")


def cmd_task_plan(args):
    try:
        plan = load_task_plan(args.plan_dir)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    fmt = getattr(args, "format", "text")
    if fmt == "json":
        rendered = json.dumps(plan.to_dict(), indent=2)
    elif fmt == "yaml":
        rendered = yaml.safe_dump(plan.to_dict(), sort_keys=False)
    else:
        rendered = render_task_plan_text(plan)
    _write_or_print(rendered, getattr(args, "write", ""))


def cmd_task_prompt(args):
    prompt = _load_prompt_text(args)
    try:
        result = run_task_prompt(
            project_name=args.project,
            prompt=prompt,
            title=getattr(args, "title", "") or Path(getattr(args, "prompt_file", "") or "task").stem,
            ensure_running=bool(getattr(args, "ensure_running", False)),
            background=bool(getattr(args, "background", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"Project:    {result.project}")
    print(f"Container:  {result.container}")
    print(f"Command:    {shlex.join(result.command)}")
    if result.background:
        print(f"Log path:   {result.log_path}")
        return
    if result.stdout:
        print(result.stdout.rstrip())


def cmd_task_dispatch(args):
    try:
        mappings, executions = dispatch_task_plan(
            plan=args.plan_dir,
            projects=list(getattr(args, "project", []) or []),
            project_prefix=getattr(args, "project_prefix", "") or "",
            execute=bool(getattr(args, "execute", False)),
            ensure_running=bool(getattr(args, "ensure_running", False)),
            background=bool(getattr(args, "background", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print("Task mapping:")
    for idx, mapping in enumerate(mappings, start=1):
        deps = ", ".join(mapping.task.depends_on) if mapping.task.depends_on else "-"
        print(f"  {idx}. {mapping.task.brief_file} -> {mapping.project} (depends on: {deps})")

    for execution in executions:
        print(f"  Command: {shlex.join(execution.command)}")
        if execution.background:
            print(f"  Log: {execution.log_path}")


def cmd_task(args):
    action = getattr(args, "action", "")
    if action == "plan":
        return cmd_task_plan(args)
    if action == "prompt":
        return cmd_task_prompt(args)
    if action == "dispatch":
        return cmd_task_dispatch(args)

    print(f"Error: Unknown task action '{action}'.")
    sys.exit(1)
