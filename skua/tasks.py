# SPDX-License-Identifier: BUSL-1.1
"""Programmatic task planning and dispatch APIs for Skua."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

from skua.config import ConfigStore
from skua.docker import _project_mount_path, is_container_running


_ORDER_LINE_RE = re.compile(r"^\d+\.\s+`([^`]+)`\s*$")
_LEADING_INDEX_RE = re.compile(r"^\d+[-_]+")


@dataclass
class TaskBrief:
    id: str
    slug: str
    title: str
    brief_file: str
    brief_path: str
    objective: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class TaskPlan:
    plan_dir: str
    readme_path: str = ""
    suggested_order: list[str] = field(default_factory=list)
    tasks: list[TaskBrief] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan_dir": self.plan_dir,
            "readme_path": self.readme_path,
            "suggested_order": list(self.suggested_order),
            "tasks": [asdict(task) for task in self.tasks],
        }


@dataclass
class TaskMapping:
    task: TaskBrief
    project: str

    def to_dict(self) -> dict:
        return {
            "task": asdict(self.task),
            "project": self.project,
        }


@dataclass
class PromptExecution:
    project: str
    container: str
    command: list[str]
    background: bool = False
    log_path: str = ""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to read '{path}': {exc}") from exc


def _task_slug_from_path(path: Path) -> str:
    stem = _LEADING_INDEX_RE.sub("", path.stem)
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "task"


def _extract_heading(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _extract_section(markdown: str, name: str) -> str:
    header = f"## {name}".strip()
    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() != header:
            continue
        collected = []
        for follow in lines[idx + 1:]:
            if follow.startswith("## "):
                break
            collected.append(follow)
        return "\n".join(collected).strip()
    return ""


def _parse_suggested_order(readme_text: str) -> list[str]:
    ordered = []
    for line in readme_text.splitlines():
        match = _ORDER_LINE_RE.match(line.strip())
        if match:
            ordered.append(match.group(1))
    return ordered


def load_task_brief(path: str | Path, depends_on: list[str] | None = None) -> TaskBrief:
    brief_path = Path(path).expanduser().resolve()
    if not brief_path.is_file():
        raise ValueError(f"Task brief '{brief_path}' does not exist.")

    text = _read_text(brief_path)
    return TaskBrief(
        id=brief_path.stem,
        slug=_task_slug_from_path(brief_path),
        title=_extract_heading(text, brief_path.stem),
        brief_file=brief_path.name,
        brief_path=str(brief_path),
        objective=_extract_section(text, "Objective"),
        depends_on=list(depends_on or []),
    )


def make_task_plan(tasks: list[TaskBrief], plan_dir: str | Path = "", readme_path: str | Path = "", suggested_order: list[str] | None = None) -> TaskPlan:
    if not tasks:
        raise ValueError("Task plan must contain at least one task.")
    base = str(Path(plan_dir).expanduser().resolve()) if plan_dir else ""
    readme = str(Path(readme_path).expanduser().resolve()) if readme_path else ""
    return TaskPlan(
        plan_dir=base,
        readme_path=readme,
        suggested_order=list(suggested_order or []),
        tasks=list(tasks),
    )


def load_task_plan(plan_dir: str | Path) -> TaskPlan:
    base = Path(plan_dir).expanduser().resolve()
    if not base.is_dir():
        raise ValueError(f"Plan directory '{base}' does not exist.")

    task_files = sorted(
        p for p in base.glob("*.md")
        if p.name.lower() != "readme.md"
    )
    if not task_files:
        raise ValueError(f"No task brief markdown files found in '{base}'.")

    readme_path = base / "README.md"
    readme_text = _read_text(readme_path) if readme_path.is_file() else ""
    suggested_order = _parse_suggested_order(readme_text)
    order_index = {name: idx for idx, name in enumerate(suggested_order)}

    tasks = []
    for path in task_files:
        text = _read_text(path)
        depends_on = []
        if path.name in order_index and order_index[path.name] > 0:
            depends_on.append(suggested_order[order_index[path.name] - 1])
        tasks.append(
            TaskBrief(
                id=path.stem,
                slug=_task_slug_from_path(path),
                title=_extract_heading(text, path.stem),
                brief_file=path.name,
                brief_path=str(path),
                objective=_extract_section(text, "Objective"),
                depends_on=depends_on,
            )
        )

    tasks.sort(key=lambda item: (order_index.get(item.brief_file, 9999), item.brief_file))
    return make_task_plan(
        tasks=tasks,
        plan_dir=base,
        readme_path=readme_path if readme_path.is_file() else "",
        suggested_order=suggested_order,
    )


def render_task_plan_text(plan: TaskPlan) -> str:
    lines = [f"Plan: {plan.plan_dir}"]
    if plan.suggested_order:
        lines.append("Suggested order:")
        for idx, name in enumerate(plan.suggested_order, start=1):
            lines.append(f"  {idx}. {name}")
    lines.append("Tasks:")
    for idx, task in enumerate(plan.tasks, start=1):
        dep = ", ".join(task.depends_on) if task.depends_on else "-"
        lines.append(f"  {idx}. {task.title}")
        lines.append(f"     file: {task.brief_file}")
        lines.append(f"     project slug: {task.slug}")
        lines.append(f"     depends on: {dep}")
        if task.objective:
            lines.append(f"     objective: {task.objective.splitlines()[0]}")
    return "\n".join(lines)


def _clear_remote_docker_transport():
    os.environ.pop("DOCKER_HOST", None)
    os.environ.pop("SKUA_DOCKER_TRANSPORT", None)
    os.environ.pop("SKUA_DOCKER_REMOTE_HOST", None)


def _configure_project_transport(project):
    host = getattr(project, "host", "") or ""
    if not host:
        _clear_remote_docker_transport()
        return

    from skua.commands.run import (
        _configure_remote_docker_transport,
        _ensure_local_ssh_client_for_remote_docker,
    )

    _ensure_local_ssh_client_for_remote_docker(host)
    _configure_remote_docker_transport(host)


def _template_uses_shell(template: str) -> bool:
    t = template or ""
    shell_markers = ("\n", ";", "|", "&&", "||", ">", "<", "$(", "${", "`")
    return any(marker in t for marker in shell_markers)


def _normalize_prompt_argv(agent_name: str, argv: list[str]) -> list[str]:
    if agent_name == "claude" and argv:
        has_prompt = "-p" in argv or "--print" in argv
        if argv[0] == "claude" and has_prompt and "--dangerously-skip-permissions" not in argv:
            return [argv[0], "--dangerously-skip-permissions", *argv[1:]]
    return argv


def build_agent_prompt_command(agent, project_name: str, prompt: str) -> list[str]:
    agent_name = (agent.name or "").strip().lower()
    template = str(getattr(agent.runtime, "prompt_command", "") or "").strip()
    if not template:
        template = str(getattr(agent.runtime, "adapt_command", "") or "").strip()

    if template:
        rendered = template.format(
            prompt=prompt,
            prompt_shell=shlex.quote(prompt),
            project=project_name,
        )
        if _template_uses_shell(template):
            return ["bash", "-lc", rendered]
        sentinel_prompt = "__SKUA_PROMPT__"
        sentinel_prompt_shell = "__SKUA_PROMPT_SHELL__"
        rendered_with_sentinels = template.format(
            prompt=sentinel_prompt,
            prompt_shell=sentinel_prompt_shell,
            project=project_name,
        )
        argv = shlex.split(rendered_with_sentinels)
        replaced = []
        for token in argv:
            if sentinel_prompt in token or sentinel_prompt_shell in token:
                token = token.replace(sentinel_prompt_shell, prompt)
                token = token.replace(sentinel_prompt, prompt)
            replaced.append(token)
        argv = _normalize_prompt_argv(agent_name, replaced)
        if agent_name == "codex" and "--skip-git-repo-check" not in argv:
            try:
                idx = argv.index("exec")
                argv.insert(idx + 1, "--skip-git-repo-check")
            except ValueError:
                pass
        return argv

    runtime = (agent.runtime.command or "").strip()
    base = shlex.split(runtime) if runtime else [agent_name or "agent"]
    if agent_name == "codex":
        return base + ["exec", "--skip-git-repo-check", prompt]
    if agent_name == "claude":
        return _normalize_prompt_argv(agent_name, base + ["-p", prompt])

    raise ValueError(
        f"Non-interactive prompting is unsupported for agent '{agent.name}'. "
        "Configure runtime.prompt_command."
    )


def compose_dispatch_prompt(task: TaskBrief, plan: TaskPlan) -> str:
    order = ", ".join(plan.suggested_order) or "Use the local brief ordering."
    dependencies = ", ".join(task.depends_on) if task.depends_on else "none"
    brief_text = _read_text(Path(task.brief_path))
    return (
        "You are assigned one workstream in a coordinated Skua refactor.\n"
        "Stay within the scope of this brief. Preserve backward compatibility. "
        "If you need an interface from another workstream, document the contract and stop there.\n\n"
        f"Plan directory: {plan.plan_dir}\n"
        f"Assigned task: {task.title}\n"
        f"Brief file: {task.brief_file}\n"
        f"Dependencies: {dependencies}\n"
        f"Suggested global order: {order}\n\n"
        "Assigned brief:\n\n"
        f"{brief_text}\n"
    )


def resolve_task_projects(plan: TaskPlan, projects: list[str] | None = None, project_prefix: str = "") -> list[TaskMapping]:
    explicit = list(projects or [])
    if explicit:
        if len(explicit) != len(plan.tasks):
            raise ValueError(f"Expected {len(plan.tasks)} project names, got {len(explicit)}.")
        return [TaskMapping(task=task, project=project) for task, project in zip(plan.tasks, explicit)]
    if project_prefix:
        return [TaskMapping(task=task, project=f"{project_prefix}{task.slug}") for task in plan.tasks]
    raise ValueError("Provide either explicit project names or project_prefix.")


def ensure_project_running(project_name: str):
    from skua.commands.run import cmd_run

    cmd_run(
        SimpleNamespace(name=project_name, no_attach=True, replace_process=False),
        lock_project=True,
    )


def run_task_prompt(
    project_name: str,
    prompt: str,
    title: str = "",
    ensure_running: bool = False,
    background: bool = False,
    dry_run: bool = False,
) -> PromptExecution:
    store = ConfigStore()
    project = store.resolve_project(project_name)
    if project is None:
        raise ValueError(f"Project '{project_name}' not found.")

    _configure_project_transport(project)

    container_name = f"skua-{project_name}"
    if ensure_running and not is_container_running(container_name):
        ensure_project_running(project_name)
    elif not is_container_running(container_name):
        raise RuntimeError(
            f"Container '{container_name}' is not running. "
            f"Run 'skua run {project_name}' or retry with ensure_running=True."
        )

    agent = store.load_agent(project.agent)
    if agent is None:
        raise ValueError(f"Agent '{project.agent}' not found.")

    inner_cmd = f"cd {shlex.quote(_project_mount_path(project))} && {shlex.join(build_agent_prompt_command(agent, project_name, prompt))}"
    title_slug = re.sub(r"[^a-z0-9]+", "-", (title or "task").lower()).strip("-") or "task"
    log_path = f"/tmp/skua-task-{title_slug}.log"

    if background:
        shell_cmd = f"{inner_cmd} > {shlex.quote(log_path)} 2>&1"
        cmd = ["docker", "exec", "-d", container_name, "bash", "-lc", shell_cmd]
    else:
        cmd = ["docker", "exec", "-i", container_name, "bash", "-lc", inner_cmd]

    if dry_run:
        return PromptExecution(
            project=project_name,
            container=container_name,
            command=cmd,
            background=background,
            log_path=log_path if background else "",
        )

    result = subprocess.run(cmd, text=True, capture_output=not background, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Task prompt failed for project '{project_name}'. "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    return PromptExecution(
        project=project_name,
        container=container_name,
        command=cmd,
        background=background,
        log_path=log_path if background else "",
        returncode=result.returncode,
        stdout=result.stdout if not background else "",
        stderr=result.stderr if not background else "",
    )


def dispatch_task_plan(
    plan: TaskPlan | str | Path,
    projects: list[str] | None = None,
    project_prefix: str = "",
    execute: bool = False,
    ensure_running: bool = False,
    background: bool = False,
    dry_run: bool = False,
) -> tuple[list[TaskMapping], list[PromptExecution]]:
    loaded_plan = load_task_plan(plan) if isinstance(plan, (str, Path)) else plan
    mappings = resolve_task_projects(loaded_plan, projects=projects, project_prefix=project_prefix)
    executions = []
    if not execute:
        return mappings, executions

    for mapping in mappings:
        executions.append(
            run_task_prompt(
                project_name=mapping.project,
                prompt=compose_dispatch_prompt(mapping.task, loaded_plan),
                title=mapping.task.slug,
                ensure_running=ensure_running,
                background=background,
                dry_run=dry_run,
            )
        )
    return mappings, executions
