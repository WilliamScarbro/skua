# SPDX-License-Identifier: BUSL-1.1
"""skua dashboard — live interactive project dashboard."""

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from skua.commands.adapt import cmd_adapt
from skua.commands.add import cmd_add, _cred_matches_agent
from skua.commands.list_cmd import (
    _container_image_id,
    _container_image_name,
    _agent_activity,
    _format_host,
    _format_source,
    _git_status,
    _has_pending_adapt_request,
    _image_id,
    _image_suffix,
    _short_image_id,
)
from skua.commands.remove import cmd_remove
from skua.commands.restart import cmd_restart
from skua.commands.run import cmd_run
from skua.commands.stop import cmd_stop
from skua.config import ConfigStore
from skua.docker import (
    build_image,
    get_running_skua_containers,
    image_exists,
    image_matches_build_context,
    image_name_for_project,
    resolve_project_image_inputs,
)
from skua.utils import find_ssh_keys, parse_ssh_config_hosts, select_option


@dataclass
class DashboardSnapshot:
    """Rendered table and metadata for one dashboard refresh."""

    columns: list
    rows: list
    summary: list


@dataclass
class DashboardJob:
    """Persistent metadata for a dashboard background job."""

    job_id: int
    action: str
    project: str
    command: list[str]
    status: str
    created_at: str
    started_at: str
    ended_at: str
    return_code: int | None
    pid: int | None
    log_path: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "project": self.project,
            "command": list(self.command),
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "return_code": self.return_code,
            "pid": self.pid,
            "log_path": self.log_path,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DashboardJob":
        return cls(
            job_id=int(data.get("job_id", 0)),
            action=str(data.get("action", "")),
            project=str(data.get("project", "")),
            command=[str(x) for x in (data.get("command") or [])],
            status=str(data.get("status", "failed")),
            created_at=str(data.get("created_at", "")),
            started_at=str(data.get("started_at", "")),
            ended_at=str(data.get("ended_at", "")),
            return_code=data.get("return_code"),
            pid=data.get("pid"),
            log_path=str(data.get("log_path", "")),
            detail=str(data.get("detail", "")),
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _format_age(started_at: str) -> str:
    if not started_at:
        return "-"
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return "-"
    delta = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    return f"{delta // 3600}h"


def _shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def _background_command(action_key: str, project_name: str) -> list[str] | None:
    if action_key == "build":
        return [sys.executable, "-m", "skua", "build", project_name]
    if action_key == "adapt":
        return [sys.executable, "-m", "skua", "adapt", project_name, "--apply-only", "--force"]
    if action_key == "stop":
        return [sys.executable, "-m", "skua", "stop", project_name, "--force"]
    if action_key == "remove":
        return [sys.executable, "-m", "skua", "remove", project_name]
    return None


class DashboardJobManager:
    """Create, run, and persist background dashboard jobs."""

    def __init__(self, config_dir: Path | None = None, max_jobs: int = 200):
        base_dir = config_dir or ConfigStore().config_dir
        self.jobs_dir = Path(base_dir) / "jobs"
        self.logs_dir = self.jobs_dir / "logs"
        self.state_file = self.jobs_dir / "jobs.json"
        self.max_jobs = max(20, int(max_jobs))
        self.jobs: list[DashboardJob] = []
        self._processes: dict[int, subprocess.Popen] = {}
        self._next_id = 1
        self._load()

    def _ensure_dirs(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        self._ensure_dirs()
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        items = data.get("jobs", []) if isinstance(data, dict) else []
        loaded = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            try:
                job = DashboardJob.from_dict(raw)
            except Exception:
                continue
            if job.job_id <= 0:
                continue
            if job.status in ("queued", "running"):
                job.status = "orphaned"
                if not job.ended_at:
                    job.ended_at = _utc_now_iso()
                if not job.detail:
                    job.detail = "Dashboard restarted before this job completed."
            loaded.append(job)
        self.jobs = loaded[-self.max_jobs:]
        self._next_id = max((job.job_id for job in self.jobs), default=0) + 1

    def _persist(self) -> None:
        self._ensure_dirs()
        payload = {
            "version": 1,
            "jobs": [job.to_dict() for job in self.jobs[-self.max_jobs:]],
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=False))
        tmp.replace(self.state_file)

    def _append_log_header(self, path: Path, job: DashboardJob) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[job {job.job_id}] action={job.action} project={job.project}\n")
            f.write(f"[job {job.job_id}] started={job.started_at}\n")
            f.write(f"[job {job.job_id}] command={_shell_join(job.command)}\n\n")

    def _append_log_footer(self, path: Path, job: DashboardJob) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n[job {job.job_id}] ended={job.ended_at} status={job.status}")
            if job.return_code is not None:
                f.write(f" return_code={job.return_code}")
            f.write("\n")

    def enqueue(self, action_key: str, project_name: str, command: list[str] | None = None) -> DashboardJob:
        cmd = command if command is not None else _background_command(action_key, project_name)
        if not cmd:
            raise ValueError(f"Action does not support background execution: {action_key}")

        now = _utc_now_iso()
        log_path = self.logs_dir / f"{self._next_id:06d}-{action_key}-{project_name}.log"
        job = DashboardJob(
            job_id=self._next_id,
            action=action_key,
            project=project_name,
            command=list(cmd),
            status="queued",
            created_at=now,
            started_at=now,
            ended_at="",
            return_code=None,
            pid=None,
            log_path=str(log_path),
            detail="",
        )
        self._next_id += 1
        self.jobs.append(job)
        self.jobs = self.jobs[-self.max_jobs:]

        try:
            self._append_log_header(log_path, job)
            with log_path.open("a", encoding="utf-8") as log_file:
                proc = subprocess.Popen(
                    job.command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            job.pid = proc.pid
            job.status = "running"
            self._processes[job.job_id] = proc
        except Exception as exc:
            job.status = "failed"
            job.ended_at = _utc_now_iso()
            job.detail = f"{type(exc).__name__}: {exc}"
            self._append_log_footer(log_path, job)
        self._persist()
        return job

    def poll(self) -> bool:
        changed = False
        for job in self.jobs:
            if job.status != "running":
                continue
            proc = self._processes.get(job.job_id)
            if proc is None:
                continue
            rc = proc.poll()
            if rc is None:
                continue
            job.return_code = rc
            job.ended_at = _utc_now_iso()
            job.status = "success" if rc == 0 else "failed"
            self._append_log_footer(Path(job.log_path), job)
            self._processes.pop(job.job_id, None)
            changed = True
        if changed:
            self._persist()
        return changed

    def cancel(self, job_id: int) -> bool:
        for job in self.jobs:
            if job.job_id != job_id or job.status != "running":
                continue
            proc = self._processes.get(job_id)
            if proc is not None:
                proc.terminate()
            elif job.pid:
                try:
                    os.kill(int(job.pid), signal.SIGTERM)
                except OSError:
                    pass
            job.status = "canceled"
            job.ended_at = _utc_now_iso()
            job.detail = "Canceled from dashboard."
            self._append_log_footer(Path(job.log_path), job)
            self._processes.pop(job_id, None)
            self._persist()
            return True
        return False

    def clear_completed(self) -> int:
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.status in ("queued", "running")]
        removed = before - len(self.jobs)
        if removed:
            self._persist()
        return removed

    def list_for_view(self) -> list[DashboardJob]:
        return list(reversed(self.jobs))

    def summary(self) -> str:
        running = sum(1 for j in self.jobs if j.status == "running")
        failed = sum(1 for j in self.jobs if j.status in ("failed", "orphaned"))
        done = sum(1 for j in self.jobs if j.status in ("success", "failed", "canceled", "orphaned"))
        return f"Jobs: {running} running, {failed} failed/orphaned, {done} completed"

    def tail(self, job: DashboardJob, max_lines: int = 200) -> str:
        path = Path(job.log_path)
        if not path.exists():
            return "(log file not found)"
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-max(20, max_lines):]) if lines else "(no output yet)"


def _collect_snapshot(args) -> DashboardSnapshot:
    store = ConfigStore()
    project_names = store.list_resources("Project")
    running_by_host = {"": set(get_running_skua_containers())}
    unreachable_hosts: set = set()
    show_agent = bool(getattr(args, "agent", False))
    show_security = bool(getattr(args, "security", False))
    show_git = bool(getattr(args, "git", False))
    local_only = bool(getattr(args, "local", False))
    show_image = bool(getattr(args, "image", False))
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")

    if not project_names:
        return DashboardSnapshot(
            columns=[],
            rows=[],
            summary=["No projects configured. Add one with: skua add <name> --dir <path> or --repo <url>"],
        )

    projects = [(name, store.resolve_project(name)) for name in project_names]
    projects = [(name, p) for name, p in projects if p is not None]
    if local_only:
        projects = [(name, p) for name, p in projects if not getattr(p, "host", "")]

    def _running_for_host(host: str) -> set:
        normalized = host or ""
        if normalized not in running_by_host:
            result = get_running_skua_containers(host=normalized)
            if result is None:
                unreachable_hosts.add(normalized)
                running_by_host[normalized] = set()
            else:
                running_by_host[normalized] = set(result)
        return running_by_host[normalized]

    show_host = any(getattr(p, "host", "") for _, p in projects)
    needs_running_image = False
    running_image_values = {}
    if show_image:
        for name, project in projects:
            container_name = f"skua-{name}"
            host = getattr(project, "host", "") or ""
            if container_name not in _running_for_host(host):
                running_image_values[name] = "-"
                continue
            img_name = image_name_for_project(image_name_base, project)
            project_id = _image_id(img_name, host=host)
            container_id = _container_image_id(container_name, host=host)
            if project_id and container_id and project_id == container_id:
                running_image_values[name] = "-"
                continue
            display_id = _short_image_id(container_id)
            running_name = display_id or _container_image_name(container_name, host=host) or "-"
            running_image_values[name] = running_name
            if running_name != "-":
                needs_running_image = True

    activity_values = {}
    for name, project in projects:
        container_name = f"skua-{name}"
        host = getattr(project, "host", "") or ""
        if container_name in _running_for_host(host):
            activity_values[name] = _agent_activity(container_name, host=host)
        else:
            activity_values[name] = "-"

    columns = [("NAME", 16)]
    columns.append(("ACTIVITY", 14))
    columns.append(("STATUS", 12))
    if show_host:
        columns.append(("HOST", 14))
    columns.append(("SOURCE", 38))
    if show_git:
        columns.append(("GIT", 9))
    if show_image:
        columns.append(("IMAGE", 36))
        if needs_running_image:
            columns.append(("RUNNING-IMAGE", 36))
    if show_agent:
        columns.extend([("AGENT", 10), ("CREDENTIAL", 20)])
    if show_security:
        columns.extend([("SECURITY", 12), ("NETWORK", 10)])

    pending_count = 0
    running_count = 0
    needs_adapt = False
    needs_build = False
    rows = []
    for name, project in projects:
        container_name = f"skua-{name}"
        host = getattr(project, "host", "") or ""
        running = _running_for_host(host)
        pending_adapt = _has_pending_adapt_request(project)
        img_name = image_name_for_project(image_name_base, project)
        if container_name in running:
            status = "running"
            running_count += 1
        elif host and host in unreachable_hosts:
            status = "unreachable"
        else:
            status = "built" if image_exists(img_name) else "missing"
        if pending_adapt:
            status += "*"
            pending_count += 1

        row = [name]
        row.append(activity_values.get(name, "-"))
        row.append(status)
        if show_host:
            row.append(_format_host(project))
        row.append(_format_source(project))
        if show_git:
            row.append(_git_status(project, store) or "-")
        if show_image:
            suffix, flags = _image_suffix(project, store)
            if "(A)" in flags:
                needs_adapt = True
            if "(B)" in flags:
                needs_build = True
            sep = " " if suffix else ""
            row.append(img_name + sep + suffix)
            if needs_running_image:
                row.append(running_image_values.get(name, "-"))
        if show_agent:
            credential = project.credential or "(none)"
            row.extend([project.agent, credential])
        if show_security:
            env = store.load_environment(project.environment)
            network = env.network.mode if env else "?"
            row.extend([project.security, network])
        rows.append({"name": name, "cells": row})

    summary = [f"{len(project_names)} project(s), {running_count} running, {pending_count} pending adapt"]
    if pending_count:
        summary.append("  * pending image-request changes")
    if show_image and (needs_adapt or needs_build):
        if needs_adapt:
            summary.append("  (A) image-request changes pending; run 'skua adapt'")
        if needs_build:
            summary.append("  (B) image out of date; run 'skua build' or 'skua adapt --build'")
    if show_image and needs_running_image:
        summary.append("  RUNNING-IMAGE indicates a restart is needed to use the latest image")

    return DashboardSnapshot(columns=columns, rows=rows, summary=summary)


def _build_selected_project(name: str, verbose: bool = False) -> bool:
    store = ConfigStore()
    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        return False

    container_dir = store.get_container_dir()
    if container_dir is None:
        print("Error: Cannot find container build assets (entrypoint.sh).")
        print("Set toolDir in global.yaml or reinstall skua.")
        return False

    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    base_image = g.get("baseImage", "debian:bookworm-slim")
    security = store.load_security(project.security)
    agent = store.load_agent(project.agent)
    if security is None:
        print(f"Error: Security profile '{project.security}' not found.")
        return False
    if agent is None:
        print(f"Error: Agent '{project.agent}' not found.")
        return False

    image_config = g.get("image", {})
    global_packages = image_config.get("extraPackages", [])
    global_commands = image_config.get("extraCommands", [])
    image_name = image_name_for_project(image_name_base, project)
    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=base_image,
        agent=agent,
        project=project,
        global_extra_packages=global_packages,
        global_extra_commands=global_commands,
    )

    if image_exists(image_name) and image_matches_build_context(
        image_name=image_name,
        container_dir=container_dir,
        security=security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
    ):
        print(f"Image '{image_name}' is already up-to-date.")
        return True

    if image_exists(image_name):
        print(f"Rebuilding image '{image_name}' for project '{name}'...")
    else:
        print(f"Building image '{image_name}' for project '{name}'...")
    success, _ = build_image(
        container_dir=container_dir,
        image_name=image_name,
        security=security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        verbose=verbose,
    )
    if success:
        print(f"Build complete for '{image_name}'.")
    else:
        print(f"Build failed for '{image_name}'.")
    return success


def _run_action(action_key: str, project_name: str) -> bool:
    if action_key == "run":
        cmd_run(SimpleNamespace(name=project_name, replace_process=False))
        return True
    if action_key == "build":
        return _build_selected_project(project_name, verbose=False)
    if action_key == "stop":
        result = cmd_stop(SimpleNamespace(name=project_name, force=True))
        return bool(result) if isinstance(result, bool) else True
    if action_key == "adapt":
        cmd_adapt(
            SimpleNamespace(
                name=project_name,
                all=False,
                show_prompt=False,
                dockerfile=False,
                show_smoke_test=False,
                discover=False,
                base_image="",
                from_image="",
                package=[],
                extra_command=[],
                apply_only=True,
                clear=False,
                write_only=False,
                build=False,
                force=True,
            )
        )
        return True
    if action_key == "remove":
        cmd_remove(SimpleNamespace(name=project_name))
        return True
    if action_key == "restart":
        cmd_restart(SimpleNamespace(name=project_name, force=True, replace_process=False))
        return True
    return False


def _prompt_text(prompt: str, default: str = "", required: bool = False) -> tuple:
    label = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        raw = input(label).strip()
        if raw == ":q":
            return "cancel", ""
        if raw == ":b":
            return "back", ""
        value = raw or default
        if required and not value:
            print("Value is required.")
            continue
        return "ok", value


def _prompt_select(prompt: str, options: list, default_index: int = 0) -> tuple:
    if not options:
        return "cancel", ""
    base = [str(o) for o in options]
    if "(Back)" not in base:
        base.append("(Back)")
    if "(Cancel)" not in base:
        base.append("(Cancel)")
    selected = select_option(prompt, base, default_index=max(0, min(default_index, len(base) - 1)))
    if selected == "(Back)":
        return "back", ""
    if selected == "(Cancel)":
        return "cancel", ""
    return "ok", selected


def _step_enabled(step: int, values: dict) -> bool:
    if step == 3:
        return values.get("source_mode") == "Git repository"
    if step == 4:
        return values.get("source_mode") == "Git repository" and values.get("run_mode") == "Remote SSH host"
    return True


def _advance_step(step: int, values: dict) -> int:
    nxt = step + 1
    while nxt <= 10 and not _step_enabled(nxt, values):
        nxt += 1
    return nxt


def _retreat_step(step: int, values: dict) -> int:
    prev = step - 1
    while prev >= 0 and not _step_enabled(prev, values):
        prev -= 1
    return prev


def _prompt_new_project_args() -> SimpleNamespace | None:
    store = ConfigStore()
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        return None

    g = store.load_global()
    defaults = g.get("defaults", {})

    print("[dashboard] add project (:b back, :q cancel on text prompts; use selector entries for Back/Cancel)")
    values = {
        "name": "",
        "source_mode": "Local directory",
        "dir": "",
        "repo": "",
        "run_mode": "Local docker host",
        "host": "",
        "ssh_key": "",
        "env": defaults.get("environment", "local-docker"),
        "security": defaults.get("security", "open"),
        "agent": defaults.get("agent", "claude"),
        "credential": "",
        "no_credential": False,
        "image": "",
    }
    step = 0
    while step <= 10:
        status = "ok"
        result = ""
        if step == 0:
            status, result = _prompt_text("Project name", values["name"], required=True)
            if status == "ok":
                values["name"] = result
        elif step == 1:
            status, result = _prompt_select("Project source:", ["Local directory", "Git repository"], 0 if values["source_mode"] == "Local directory" else 1)
            if status == "ok":
                values["source_mode"] = result
                if result == "Local directory":
                    values["repo"] = ""
                    values["host"] = ""
                    values["run_mode"] = "Local docker host"
                else:
                    values["dir"] = ""
        elif step == 2:
            if values["source_mode"] == "Local directory":
                default_dir = values["dir"] or str(Path.cwd())
                status, result = _prompt_text("Project directory", default_dir, required=True)
                if status == "ok":
                    values["dir"] = result
            else:
                status, result = _prompt_text("Git repository URL (SSH preferred)", values["repo"], required=True)
                if status == "ok":
                    values["repo"] = result
        elif step == 3:
            status, result = _prompt_select(
                "Run location:",
                ["Local docker host", "Remote SSH host"],
                0 if values["run_mode"] == "Local docker host" else 1,
            )
            if status == "ok":
                values["run_mode"] = result
                if result != "Remote SSH host":
                    values["host"] = ""
        elif step == 4:
            hosts = parse_ssh_config_hosts()
            options = hosts + ["Manual entry..."] if hosts else ["Manual entry..."]
            default_idx = 0
            if values["host"] and values["host"] in hosts:
                default_idx = hosts.index(values["host"])
            status, result = _prompt_select("Select SSH host:", options, default_idx)
            if status == "ok":
                if result == "Manual entry...":
                    status, host_value = _prompt_text("SSH host (must exist in ~/.ssh/config)", values["host"], required=True)
                    if status == "ok":
                        values["host"] = host_value
                else:
                    values["host"] = result
        elif step == 5:
            keys = [str(p) for p in find_ssh_keys()]
            global_ssh = defaults.get("sshKey", "")
            if global_ssh:
                global_ssh_path = str(Path(global_ssh).expanduser().resolve())
                if Path(global_ssh_path).is_file() and global_ssh_path not in keys:
                    keys.append(global_ssh_path)
            if keys:
                keys = sorted(keys)
                options = keys + ["None", "Manual entry..."]
                default_idx = len(keys)
                if values["ssh_key"] and values["ssh_key"] in keys:
                    default_idx = keys.index(values["ssh_key"])
                elif global_ssh:
                    resolved_global_ssh = str(Path(global_ssh).expanduser().resolve())
                    if resolved_global_ssh in keys:
                        default_idx = keys.index(resolved_global_ssh)
                status, result = _prompt_select("Select SSH private key:", options, default_idx)
                if status == "ok":
                    if result == "None":
                        values["ssh_key"] = ""
                    elif result == "Manual entry...":
                        status, key_value = _prompt_text("SSH private key path (leave empty for none)", values["ssh_key"], required=False)
                        if status == "ok":
                            values["ssh_key"] = key_value
                    else:
                        values["ssh_key"] = result
            else:
                status, result = _prompt_text("SSH private key path (leave empty for none)", values["ssh_key"], required=False)
                if status == "ok":
                    values["ssh_key"] = result
        elif step == 6:
            envs = store.list_resources("Environment")
            opts = envs if envs else [values["env"]]
            status, result = _prompt_select(
                "Select environment:",
                opts + ["Manual entry..."],
                opts.index(values["env"]) if values["env"] in opts else 0,
            )
            if status == "ok":
                if result == "Manual entry...":
                    status, env_value = _prompt_text("Environment", values["env"], required=True)
                    if status == "ok":
                        values["env"] = env_value
                else:
                    values["env"] = result
        elif step == 7:
            secs = store.list_resources("SecurityProfile")
            opts = secs if secs else [values["security"]]
            status, result = _prompt_select(
                "Select security profile:",
                opts + ["Manual entry..."],
                opts.index(values["security"]) if values["security"] in opts else 0,
            )
            if status == "ok":
                if result == "Manual entry...":
                    status, sec_value = _prompt_text("Security profile", values["security"], required=True)
                    if status == "ok":
                        values["security"] = sec_value
                else:
                    values["security"] = result
        elif step == 8:
            agents = store.list_resources("AgentConfig")
            opts = agents if agents else [values["agent"]]
            status, result = _prompt_select(
                "Select agent:",
                opts + ["Manual entry..."],
                opts.index(values["agent"]) if values["agent"] in opts else 0,
            )
            if status == "ok":
                if result == "Manual entry...":
                    status, agent_value = _prompt_text("Agent", values["agent"], required=True)
                    if status == "ok":
                        values["agent"] = agent_value
                else:
                    values["agent"] = result
                values["credential"] = ""
                values["no_credential"] = False
        elif step == 9:
            available_creds = sorted(
                c for c in store.list_resources("Credential") if _cred_matches_agent(store, c, values["agent"])
            )
            if available_creds:
                options = ["Auto-detect/add local credential", "None (log in in container)"] + available_creds
                status, result = _prompt_select("Credential:", options, 0)
                if status == "ok":
                    values["credential"] = ""
                    values["no_credential"] = False
                    if result == "None (log in in container)":
                        values["no_credential"] = True
                    elif result in available_creds:
                        values["credential"] = result
            else:
                values["credential"] = ""
                values["no_credential"] = False
                step = _advance_step(step, values)
                continue
        elif step == 10:
            status, result = _prompt_text("Project base image override (optional)", values["image"], required=False)
            if status == "ok":
                values["image"] = result

        if status == "cancel":
            print("Cancelled.")
            return None
        if status == "back":
            prev_step = _retreat_step(step, values)
            if prev_step < 0:
                print("Already at the first prompt.")
                continue
            step = prev_step
            continue
        step = _advance_step(step, values)

    return SimpleNamespace(
        name=values["name"],
        repo=values["repo"],
        host=values["host"],
        dir=values["dir"],
        ssh_key=values["ssh_key"],
        env=values["env"],
        security=values["security"],
        agent=values["agent"],
        image=values["image"],
        quick=False,
        no_prompt=False,
        no_credential=values["no_credential"],
        credential=values["credential"],
    )


def _run_add_project_interactive(suspend=None) -> str:
    suspend_cm = suspend() if suspend is not None else nullcontext()
    with suspend_cm:
        args = _prompt_new_project_args()
        if args is None:
            return "new project: cancelled"
        try:
            cmd_add(args)
            return f"new project {args.name}: ok"
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            return f"new project {args.name}: failed (status {code})"
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            return f"new project {args.name}: failed ({type(exc).__name__}: {exc})"


def _execute_action(action_key: str, project_name: str) -> tuple:
    try:
        success = _run_action(action_key, project_name)
        return bool(success), ""
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return (code == 0), f"Command exited with status {code}."
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        return False, f"{type(exc).__name__}: {exc}"


def _run_action_interactive(action_key: str, project_name: str, suspend=None) -> str:
    action_label = {"run": "run", "build": "build", "stop": "stop", "adapt": "adapt", "remove": "remove", "restart": "restart"}[action_key]
    suspend_cm = suspend() if suspend is not None else nullcontext()
    with suspend_cm:
        print(f"[dashboard] {action_label} {project_name}")
        success, detail = _execute_action(action_key, project_name)
        if detail:
            print(detail)
    return f"{action_label} {project_name}: {'ok' if success else 'failed'}"


def cmd_dashboard(args):
    try:
        from rich.console import Group
        from rich.table import Table
        from rich.text import Text
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.widgets import Footer, Static
    except ImportError:
        print("Error: 'skua dashboard' requires the 'textual' package.")
        print("Install it with: pip3 install textual")
        raise SystemExit(1)

    class DashboardApp(App):
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("h", "toggle_help", "Help"),
            Binding("tab", "toggle_focus", "Focus"),
            Binding("up,k", "cursor_up", "Up"),
            Binding("down,j", "cursor_down", "Down"),
            Binding("enter", "run_selected", "Run"),
            Binding("b", "build_selected", "Build"),
            Binding("s", "stop_selected", "Stop"),
            Binding("a", "adapt_selected", "Adapt"),
            Binding("d", "remove_selected", "Remove"),
            Binding("r", "restart_selected", "Restart"),
            Binding("n", "new_project", "New"),
            Binding("o", "open_job_output", "Output"),
            Binding("x", "cancel_job", "Cancel Job"),
            Binding("c", "clear_jobs", "Clear Jobs"),
        ]

        def __init__(self, dashboard_args):
            super().__init__()
            self.dashboard_args = dashboard_args
            self.snapshot = DashboardSnapshot(columns=[], rows=[], summary=[])
            self.selected = 0
            self.selected_job = 0
            self.focus = "projects"
            self.show_job_output = False
            self.show_help = False
            self.message = ""
            self.jobs = DashboardJobManager()
            self._refresh_lock = threading.Lock()
            self._refresh_inflight = False

        def compose(self) -> ComposeResult:
            yield Static(id="dashboard-view")
            yield Footer()

        def on_mount(self) -> None:
            self._request_refresh()
            self.set_interval(2.0, self._request_refresh)

        def _request_refresh(self) -> None:
            jobs_changed = self.jobs.poll()
            with self._refresh_lock:
                if self._refresh_inflight:
                    if jobs_changed:
                        self._refresh_view()
                    return
                self._refresh_inflight = True
            thread = threading.Thread(target=self._refresh_worker, daemon=True)
            thread.start()

        def _refresh_worker(self) -> None:
            try:
                snapshot = _collect_snapshot(self.dashboard_args)
                self.call_from_thread(self._apply_snapshot, snapshot)
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                self.call_from_thread(self._apply_refresh_error, f"{type(exc).__name__}: {exc}")

        def _apply_refresh_error(self, detail: str) -> None:
            with self._refresh_lock:
                self._refresh_inflight = False
            self.message = f"refresh failed: {detail}"
            self._refresh_view()

        def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
            with self._refresh_lock:
                self._refresh_inflight = False
            prev_name = self._selected_project_name()
            self.snapshot = snapshot
            if self.snapshot.rows:
                if prev_name is not None:
                    for idx, row in enumerate(self.snapshot.rows):
                        if row["name"] == prev_name:
                            self.selected = idx
                            break
                    else:
                        self.selected = min(self.selected, len(self.snapshot.rows) - 1)
                else:
                    self.selected = min(self.selected, len(self.snapshot.rows) - 1)
            else:
                self.selected = 0
            self._refresh_view()

        def _selected_project_name(self) -> str | None:
            if not self.snapshot.rows:
                return None
            if self.selected < 0 or self.selected >= len(self.snapshot.rows):
                return None
            return self.snapshot.rows[self.selected]["name"]

        def _refresh_view(self) -> None:
            view = self.query_one("#dashboard-view", Static)
            title = Text("skua dashboard (auto-refresh: 2s)", style="bold")
            message = Text(self.message) if self.message else Text("")
            jobs_view = self.jobs.list_for_view()
            if jobs_view:
                self.selected_job = min(max(0, self.selected_job), len(jobs_view) - 1)
            else:
                self.selected_job = 0
            focus_line = Text(
                f"Focus: {self.focus} | {self.jobs.summary()}",
                style="cyan bold" if self.focus == "jobs" else "dim",
            )

            if self.show_help:
                help_text = Text(
                    "Keys: Up/Down select | Enter run | b build | s stop | a adapt | d remove | r restart | n new\n"
                    "      tab/j focus projects/jobs | o output | x cancel job | c clear completed jobs\n"
                    "      h toggle help | q quit"
                )
                view.update(Group(title, message, focus_line, help_text))
                return

            if self.show_job_output and jobs_view:
                selected = jobs_view[self.selected_job]
                log_title = Text(
                    f"Job #{selected.job_id} {selected.action} {selected.project} [{selected.status}] (press o to close)",
                    style="bold",
                )
                log_text = Text(self.jobs.tail(selected), style="white")
                view.update(Group(title, message, focus_line, log_title, Text(""), log_text))
                return

            if not self.snapshot.rows:
                divider = Text(self._divider_line(), style="dim")
                summary = Text("\n".join(self.snapshot.summary))
                hint = Text("Press q to quit. Press h for help.", style="dim")
                jobs_table = self._render_jobs_table(jobs_view)
                view.update(Group(title, message, focus_line, Text(""), divider, summary, Text(""), jobs_table, Text(""), divider, hint))
                return

            table = Table(box=None, show_edge=False, pad_edge=False)
            for col_name, col_width in self.snapshot.columns:
                table.add_column(col_name, width=col_width, overflow="ellipsis", no_wrap=True)
            for idx, row in enumerate(self.snapshot.rows):
                style = "reverse" if idx == self.selected else ""
                rendered_cells = []
                for col_index, cell in enumerate(row["cells"]):
                    col_name = self.snapshot.columns[col_index][0] if col_index < len(self.snapshot.columns) else ""
                    rendered_cells.append(Text(str(cell), style=self._cell_style(col_name, str(cell))))
                table.add_row(*rendered_cells, style=style)

            summary = Text()
            for idx, line in enumerate(self.snapshot.summary):
                summary.append(line, style=self._summary_style(line))
                if idx < len(self.snapshot.summary) - 1:
                    summary.append("\n")
            jobs_table = self._render_jobs_table(jobs_view)
            divider = Text(self._divider_line(), style="dim")
            hint = Text("Press h for help. Press q to quit.", style="dim")
            view.update(Group(title, message, focus_line, table, Text(""), divider, summary, Text(""), jobs_table, Text(""), divider, hint))

        def _render_jobs_table(self, jobs_view: list[DashboardJob]):
            table = Table(box=None, show_edge=False, pad_edge=False)
            table.add_column("JOBS", width=6)
            table.add_column("ACTION", width=8)
            table.add_column("PROJECT", width=18, overflow="ellipsis", no_wrap=True)
            table.add_column("STATUS", width=10)
            table.add_column("AGE", width=6)
            table.add_column("EXIT", width=6)
            if not jobs_view:
                table.add_row("-", "-", "-", "none", "-", "-", style="dim")
                return table
            for idx, job in enumerate(jobs_view[:10]):
                style = "reverse" if self.focus == "jobs" and idx == self.selected_job else ""
                rc = "-" if job.return_code is None else str(job.return_code)
                status_style = self._job_status_style(job.status)
                table.add_row(
                    str(job.job_id),
                    job.action,
                    job.project,
                    Text(job.status, style=status_style),
                    _format_age(job.started_at),
                    rc,
                    style=style,
                )
            return table

        @staticmethod
        def _job_status_style(status: str) -> str:
            if status in ("running", "queued"):
                return "yellow bold"
            if status in ("success",):
                return "green bold"
            if status in ("failed", "orphaned"):
                return "red bold"
            if status in ("canceled",):
                return "magenta bold"
            return ""

        @staticmethod
        def _summary_style(text: str) -> str:
            lowered = text.lower()
            if "pending" in lowered or "(a)" in lowered or "(b)" in lowered or "restart is needed" in lowered:
                return "yellow bold"
            if "missing" in lowered or "failed" in lowered:
                return "red bold"
            return ""

        @staticmethod
        def _cell_style(column: str, value: str) -> str:
            value = str(value or "")
            if column == "NAME":
                return "white bold"
            if column == "HOST":
                if value.startswith("SSH:"):
                    return "blue bold"
                return "blue"
            if column == "SOURCE":
                if value.startswith("GITHUB:"):
                    return "green"
                if value.startswith("DIR:"):
                    return "magenta"
                return "yellow"
            if column == "GIT":
                if value in ("CURRENT",):
                    return "green"
                if value in ("AHEAD",):
                    return "blue bold"
                if value in ("BEHIND", "DIVERGED"):
                    return "yellow bold"
                if value in ("UNCLEAN",):
                    return "red bold"
                return "dim"
            if column == "ACTIVITY":
                if value in ("-", ""):
                    return "dim"
                if value in ("done",):
                    return "green bold"
                if value in ("idle",):
                    return "blue"
                if value in ("processing", "thinking") or value.startswith("think:"):
                    return "yellow bold"
                if set(value) <= {"X"}:
                    if len(value) >= 4:
                        return "yellow bold"
                    if len(value) >= 2:
                        return "green bold"
                    return "blue bold"
                if value in ("?",):
                    return "red bold"
                return "blue"
            if column == "STATUS":
                if "!" in value or value.startswith("stale") or value.endswith("*"):
                    return "yellow bold"
                if value.startswith("running"):
                    return "green bold"
                if value.startswith("built"):
                    return "blue"
                if value.startswith("missing") or value.startswith("unreachable"):
                    return "red bold"
                return ""
            if column in ("IMAGE", "RUNNING-IMAGE"):
                if "(A)" in value or "(B)" in value:
                    return "magenta bold"
                if value == "-":
                    return "dim"
                return ""
            if column == "AGENT":
                return "cyan bold"
            if column == "CREDENTIAL":
                if value == "(none)":
                    return "dim"
                return "cyan"
            if column == "SECURITY":
                if value in ("strict", "proxy"):
                    return "green bold"
                return "yellow"
            if column == "NETWORK":
                if value in ("none",):
                    return "yellow bold"
                return "blue"
            return ""

        def _divider_line(self) -> str:
            width = getattr(self, "size", None).width if getattr(self, "size", None) else 80
            return "─" * max(20, min(120, width - 2))

        def action_toggle_help(self) -> None:
            self.show_help = not self.show_help
            self._refresh_view()

        def action_toggle_focus(self) -> None:
            self.focus = "jobs" if self.focus == "projects" else "projects"
            self.show_job_output = False
            self._refresh_view()

        def action_cursor_up(self) -> None:
            if self.focus == "jobs":
                jobs_view = self.jobs.list_for_view()
                if jobs_view:
                    self.selected_job = max(0, self.selected_job - 1)
                    self._refresh_view()
                return
            if self.snapshot.rows:
                self.selected = max(0, self.selected - 1)
                self._refresh_view()

        def action_cursor_down(self) -> None:
            if self.focus == "jobs":
                jobs_view = self.jobs.list_for_view()
                if jobs_view:
                    self.selected_job = min(len(jobs_view) - 1, self.selected_job + 1)
                    self._refresh_view()
                return
            if self.snapshot.rows:
                self.selected = min(len(self.snapshot.rows) - 1, self.selected + 1)
                self._refresh_view()

        def _run_selected(self, action_key: str) -> None:
            project_name = self._selected_project_name()
            if not project_name:
                return
            background = _background_command(action_key, project_name)
            if background is not None:
                try:
                    job = self.jobs.enqueue(action_key, project_name, command=background)
                    self.message = f"queued job #{job.job_id}: {action_key} {project_name}"
                    self.focus = "jobs"
                    self.show_job_output = False
                    self.selected_job = 0
                except Exception as exc:
                    self.message = f"failed to queue {action_key} {project_name}: {type(exc).__name__}: {exc}"
            else:
                self.message = _run_action_interactive(action_key, project_name, suspend=self.suspend)
            self._refresh_view()
            self._request_refresh()

        def action_run_selected(self) -> None:
            if self.focus == "jobs":
                self.action_open_job_output()
                return
            self._run_selected("run")

        def action_build_selected(self) -> None:
            self._run_selected("build")

        def action_stop_selected(self) -> None:
            self._run_selected("stop")

        def action_adapt_selected(self) -> None:
            self._run_selected("adapt")

        def action_remove_selected(self) -> None:
            self._run_selected("remove")

        def action_restart_selected(self) -> None:
            self._run_selected("restart")

        def action_new_project(self) -> None:
            self.message = _run_add_project_interactive(suspend=self.suspend)
            self._refresh_view()
            self._request_refresh()

        def action_open_job_output(self) -> None:
            jobs_view = self.jobs.list_for_view()
            if not jobs_view:
                self.message = "no jobs to display"
                self._refresh_view()
                return
            self.selected_job = min(self.selected_job, len(jobs_view) - 1)
            self.focus = "jobs"
            self.show_job_output = not self.show_job_output
            self._refresh_view()

        def action_cancel_job(self) -> None:
            jobs_view = self.jobs.list_for_view()
            if not jobs_view:
                self.message = "no running job selected"
                self._refresh_view()
                return
            self.selected_job = min(self.selected_job, len(jobs_view) - 1)
            job = jobs_view[self.selected_job]
            if self.jobs.cancel(job.job_id):
                self.message = f"canceled job #{job.job_id}"
            else:
                self.message = f"job #{job.job_id} is not running"
            self._refresh_view()

        def action_clear_jobs(self) -> None:
            removed = self.jobs.clear_completed()
            self.message = f"cleared {removed} completed job(s)"
            self.show_job_output = False
            self._refresh_view()

    DashboardApp(args).run()
